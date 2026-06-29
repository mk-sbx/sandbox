"""
main.py
============

Check a query geometry (e.g. a MultiPolygon) against a GeoJSON dataset and
return the features it matches under a chosen spatial predicate
(``intersects``, ``within``, ``contains``, ``overlaps`` ...).

Why this module exists
----------------------
"Does this shape hit anything in that dataset?" looks like a one-liner, but the
quiet failures are what bite you:

* **Wrong CRS.** GeoJSON per RFC 7946 is *always* WGS84 lon/lat (CRS84). Real
  files in the wild still carry a legacy ``crs`` member, or are silently in a
  projected system (British National Grid, Web Mercator). Comparing geometries
  in different CRSs gives confidently wrong answers, not errors.
* **Swapped axes.** lat/lon vs lon/lat is the single most common GIS bug. The
  numbers still "work", they're just in the wrong place.
* **Invalid geometry.** Self-intersections, unclosed rings, and bowtie polygons
  make predicates raise or return nonsense.
* **3D coords, empty geometries, null properties, mixed geometry types.**

This module fails *loudly and early* on those, normalizes what it safely can,
and only then runs the spatial test.

Observability
-------------
Every assumption and conversion is logged through the ``geo_check`` logger:
the CRS decided for each input, axis/range checks (with the observed lon/lat
extent), validity repairs, reprojections (with a sample coordinate before and
after), and a final bounding-box **consistency cross-check** between the query
and the dataset. Pass ``verbose=True`` (library) or ``-v`` (CLI) to stream this
at INFO; ``-vv`` for DEBUG. Nothing is printed unless you ask for it, so the
function stays quiet inside larger pipelines.

The bbox cross-check is the one guard that catches a CRS/axis mismatch whose
coordinates *both* stay inside valid lon/lat ranges (the typical UK case, where
lat ~52 is a valid longitude and lon ~-2 a valid latitude). If the query and
dataset end up in boxes that don't overlap in the shared target CRS, you almost
certainly have a swap or wrong region, and you get a loud WARNING.

Tooling
-------
* **Shapely 2.x** — geometry model, validity repair, spatial predicates, and the
  STRtree spatial index. This is the right tool; do the geometry in Shapely, not
  by hand.
* **pyproj** — only imported if a reprojection is actually required.
* **geopandas** is a fine higher-level alternative (``gpd.read_file`` +
  ``gpd.sjoin``); it wraps exactly these pieces. Prefer this module when you want
  explicit control over the defensive checks, or want to avoid the heavier
  dependency stack. See the note at the bottom of this file.

Install:  ``pip install shapely pyproj``

Quick start
-----------
    from geo_check import check_geometry_against_dataset

    matches = check_geometry_against_dataset(
        dataset="world_heritage.geojson",   # path | dict | JSON string
        query="query_multipolygon.geojson", # path | dict | geometry | shapely geom
        predicate="intersects",
        verbose=True,                        # stream the CRS/conversion log
    )
    for m in matches:
        print(m.feature_id, m.properties.get("SITE_NAME"))
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# Module logger. Quiet by default (NullHandler) so importing this in a larger
# program doesn't spam logs; verbose=True / the CLI wires up a StreamHandler.
_LOG = logging.getLogger("geo_check")
_LOG.addHandler(logging.NullHandler())


def _enable_verbose_logging(level: int = logging.INFO) -> None:
    """Attach a console handler to the geo_check logger (idempotent)."""
    _LOG.setLevel(level)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in _LOG.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[geo_check] %(message)s"))
        _LOG.addHandler(handler)
    _LOG.propagate = False


# ---------------------------------------------------------------------------
# Pure-Python guards (no third-party deps) — run these BEFORE touching Shapely
# so bad input fails fast with a clear message.
# ---------------------------------------------------------------------------

# CRS identifiers that mean "WGS84 lon/lat" and need no reprojection.
_WGS84_ALIASES = {
    None,
    "urn:ogc:def:crs:ogc:1.3:crs84",
    "urn:ogc:def:crs:ogc::crs84",
    "urn:ogc:def:crs:epsg::4326",
    "epsg:4326",
    "wgs84",
    "crs84",
}


def detect_crs(obj: dict, override: str | None = None, *, label: str = "input") -> str | None:
    """Return a normalized CRS string, or ``None`` for WGS84 lon/lat.

    Honours an explicit ``override``. Otherwise looks for a legacy GeoJSON
    ``crs`` member (removed from the spec in RFC 7946, but still emitted by
    older tools). Absence of a ``crs`` member means WGS84 by spec.

    ``label`` is only used to make the log line legible ("dataset" / "query").
    """
    if override is not None:
        norm = _normalize_crs_token(override)
        _LOG.info(
            "%s CRS: explicit override %r -> %s",
            label, override, norm or "WGS84 lon/lat (no reprojection)",
        )
        return norm

    crs = obj.get("crs") if isinstance(obj, dict) else None
    if not crs:
        _LOG.info(
            "%s CRS: no 'crs' member; assuming WGS84 lon/lat (CRS84) per RFC 7946.",
            label,
        )
        return None
    # GeoJSON named CRS:  {"type": "name", "properties": {"name": "EPSG:4326"}}
    name = (crs.get("properties") or {}).get("name") if isinstance(crs, dict) else None
    norm = _normalize_crs_token(name)
    if norm is None:
        _LOG.info(
            "%s CRS: legacy 'crs' member %r normalizes to WGS84 lon/lat (no reprojection).",
            label, name,
        )
    else:
        _LOG.info(
            "%s CRS: legacy 'crs' member declares %r (normalized %r) -> will reproject.",
            label, name, norm,
        )
    return norm


def _normalize_crs_token(token: str | None) -> str | None:
    if token is None:
        return None
    t = token.strip().lower()
    if t in _WGS84_ALIASES:
        return None
    return t  # something non-WGS84; caller decides whether to reproject or reject


def iter_coords(geometry: dict) -> Iterator[tuple[float, float]]:
    """Yield (x, y) pairs from any GeoJSON geometry dict, ignoring Z."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "GeometryCollection":
        for g in geometry.get("geometries", []):
            yield from iter_coords(g)
        return

    def walk(node: Any) -> Iterator[tuple[float, float]]:
        # A position is a list whose first two items are numbers.
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            yield (float(node[0]), float(node[1]))
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from walk(item)

    if coords is not None:
        yield from walk(coords)


def assert_lonlat_ranges(
    geometry: dict,
    *,
    label: str = "geometry",
    expected_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Raise if coordinates fall outside valid lon/lat ranges (or an AOI).

    Catches data that is actually in a projected CRS (metres, so values in the
    thousands) and the lat/lon-vs-lon/lat swap *when one swapped value escapes
    the valid range*.

    Important limitation: when BOTH swapped values happen to be in range — which
    is exactly the case for UK data, where lat ~52 is a valid longitude and lon
    ~-2 is a valid latitude — a pure range check cannot see the swap. The only
    reliable guard then is ``expected_bounds``: pass the rough lon/lat box your
    data should live in ``(min_lon, min_lat, max_lon, max_lat)`` and a swap (or
    simply the wrong region / CRS) will land outside it and raise.

    Returns the observed ``(min_lon, min_lat, max_lon, max_lat)`` so callers can
    log it and cross-check the two inputs against each other.
    """
    saw_any = False
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for x, y in iter_coords(geometry):
        saw_any = True
        minx, miny = min(minx, x), min(miny, y)
        maxx, maxy = max(maxx, x), max(maxy, y)
        if not (-180.0 <= x <= 180.0) or not (-90.0 <= y <= 90.0):
            hint = ""
            if -90.0 <= x <= 90.0 and -180.0 <= y <= 180.0 and abs(y) > 90.0:
                hint = " (looks like lat/lon — GeoJSON wants lon, lat order)"
            elif abs(x) > 180.0 or abs(y) > 90.0:
                hint = " (values out of degree range — is this a projected CRS, e.g. metres?)"
            raise ValueError(
                f"{label}: coordinate ({x}, {y}) outside WGS84 lon/lat range{hint}."
            )
        if expected_bounds is not None:
            min_lon, min_lat, max_lon, max_lat = expected_bounds
            if not (min_lon <= x <= max_lon and min_lat <= y <= max_lat):
                raise ValueError(
                    f"{label}: coordinate ({x}, {y}) outside expected area "
                    f"{expected_bounds}. Possible swapped axes, wrong region, or "
                    f"wrong CRS."
                )
    if not saw_any:
        raise ValueError(f"{label}: no coordinates found (empty geometry?).")

    bounds = (minx, miny, maxx, maxy)
    _LOG.info(
        "%s: lon/lat range check OK; bbox lon[%.6f, %.6f] lat[%.6f, %.6f].",
        label, minx, maxx, miny, maxy,
    )
    return bounds


def _bbox_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """True if two (minx, miny, maxx, maxy) boxes overlap (touching counts)."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# ---------------------------------------------------------------------------
# Shapely-backed core. Shapely is imported lazily so the guards above and the
# module docstring are usable even where Shapely isn't installed.
# ---------------------------------------------------------------------------

def _shapely():
    try:
        import shapely  # noqa: F401
        from shapely.geometry import shape, base  # noqa: F401
        return __import__("shapely")
    except ImportError as e:  # pragma: no cover - environment dependent
        raise ImportError(
            "geo_check needs Shapely 2.x for spatial predicates. "
            "Install it with:  pip install shapely"
        ) from e


def _to_shape(geometry_dict: dict):
    from shapely.geometry import shape
    return shape(geometry_dict)


def _normalize_shape(geom, *, fix_invalid: bool = True, label: str = "geometry"):
    """Force 2D, drop empties, and repair invalid geometry."""
    import shapely
    from shapely import force_2d, make_valid

    if geom.is_empty:
        raise ValueError("geometry is empty after parsing.")

    if geom.has_z:
        _LOG.info("%s: has Z coordinates; flattening to 2D (force_2d).", label)
        geom = force_2d(geom)

    if not geom.is_valid:
        reason = shapely.is_valid_reason(geom)
        if not fix_invalid:
            raise ValueError(
                f"invalid geometry: {reason}. "
                "Pass fix_invalid=True to auto-repair."
            )
        _LOG.warning("%s: invalid geometry (%s); repairing with make_valid.", label, reason)
        repaired = make_valid(geom)
        if repaired.is_empty:
            raise ValueError("geometry became empty after validity repair.")
        if repaired.geom_type != geom.geom_type:
            _LOG.warning(
                "%s: make_valid changed geom type %s -> %s.",
                label, geom.geom_type, repaired.geom_type,
            )
        geom = repaired
    return geom


def _maybe_reproject(geom, src_crs: str | None, target_crs: str, *, label: str = "geometry"):
    """Reproject a Shapely geometry from src_crs to target_crs using pyproj.

    src_crs is None for WGS84 (no-op). Raises a helpful error if pyproj is
    needed but missing.
    """
    if src_crs is None:
        _LOG.debug("%s: source CRS is WGS84 lon/lat; no reprojection needed.", label)
        return geom
    if _normalize_crs_token(target_crs) is None and src_crs is None:
        return geom
    try:
        from pyproj import Transformer
        from shapely.ops import transform
    except ImportError as e:
        raise ImportError(
            f"Input is in CRS '{src_crs}' and needs reprojection to {target_crs}, "
            "which requires pyproj.  Install it with:  pip install pyproj"
        ) from e
    before = geom.bounds
    transformer = Transformer.from_crs(src_crs, target_crs, always_xy=True)
    out = transform(transformer.transform, geom)
    after = out.bounds
    _LOG.info(
        "%s: reprojected %s -> %s. bbox %s -> %s.",
        label, src_crs, target_crs,
        tuple(round(v, 3) for v in before), tuple(round(v, 3) for v in after),
    )
    return out


def _to_crs(geom, src_crs: str | None, target_crs: str, *, label: str = "geometry"):
    """Reproject to an explicit ``target_crs``, treating ``src_crs=None`` as WGS84.

    Unlike ``_maybe_reproject`` (which treats ``src_crs=None`` as "already in the
    comparison CRS, no-op"), this always lands the geometry in ``target_crs`` —
    needed when the working CRS is a *projected* one (e.g. EPSG:27700) so that
    area and distance come out in metres. A WGS84 geometry must still be
    projected to get there.
    """
    src = src_crs or "EPSG:4326"
    if _normalize_crs_token(src) is None and _normalize_crs_token(target_crs) is None:
        return geom  # both WGS84 lon/lat
    if str(src).strip().lower() == str(target_crs).strip().lower():
        return geom
    try:
        from pyproj import Transformer
        from shapely.ops import transform
    except ImportError as e:
        raise ImportError(
            f"Reprojecting {label} from '{src}' to '{target_crs}' requires pyproj. "
            "Install it with:  pip install pyproj"
        ) from e
    before = geom.bounds
    transformer = Transformer.from_crs(src, target_crs, always_xy=True)
    out = transform(transformer.transform, geom)
    _LOG.info(
        "%s: reprojected %s -> %s. bbox %s -> %s.",
        label, src, target_crs,
        tuple(round(v, 3) for v in before), tuple(round(v, 3) for v in out.bounds),
    )
    return out


# Supported binary predicates -> Shapely method name.
_PREDICATES = {
    "intersects": "intersects",
    "within": "within",
    "contains": "contains",
    "overlaps": "overlaps",
    "touches": "touches",
    "crosses": "crosses",
    "covers": "covers",
    "covered_by": "covered_by",
    "disjoint": "disjoint",
}


@dataclass
class Match:
    """One dataset feature that satisfied the predicate against the query."""
    feature_index: int
    feature_id: Any
    predicate: str
    properties: dict = field(default_factory=dict)


def _features(obj: dict | list) -> list[dict]:
    """Normalize input into a list of GeoJSON Feature dicts."""
    if isinstance(obj, list):
        return obj
    t = obj.get("type")
    if t == "FeatureCollection":
        return obj.get("features", [])
    if t == "Feature":
        return [obj]
    # Bare geometry dict -> wrap as a feature.
    if t in {"Point", "LineString", "Polygon", "MultiPoint",
             "MultiLineString", "MultiPolygon", "GeometryCollection"}:
        return [{"type": "Feature", "geometry": obj, "properties": {}}]
    raise ValueError(f"Unrecognized GeoJSON object type: {t!r}")


def _load(src: dict | str | Path) -> dict:
    """Accept a dict, a JSON string, or a path to a .geojson/.json file."""
    if isinstance(src, dict):
        return src
    if isinstance(src, (str, Path)):
        s = str(src)
        p = Path(s)
        if p.exists():
            _LOG.debug("loading GeoJSON from file: %s", p)
            return json.loads(p.read_text(encoding="utf-8"))
        # treat as raw JSON text
        _LOG.debug("source is not an existing path; parsing as raw JSON text.")
        return json.loads(s)
    raise TypeError(f"Unsupported source type: {type(src).__name__}")


def _prepare_query(query, *, query_crs, target_crs, fix_invalid, check_order,
                   expected_bounds=None):
    """Turn the query input into a single normalized Shapely geometry.

    Returns ``(geom, raw_bounds)`` where ``raw_bounds`` is the lon/lat bbox
    observed during the range check (``None`` if the check was skipped, e.g.
    because a Shapely geometry was passed in directly or a non-WGS84 CRS was
    declared).
    """
    # Already a Shapely geometry?
    try:
        from shapely.geometry.base import BaseGeometry
        if isinstance(query, BaseGeometry):
            _LOG.info("query: received a ready-made Shapely geometry (%s).", query.geom_type)
            geom = _normalize_shape(query, fix_invalid=fix_invalid, label="query")
            return geom, None
    except ImportError:
        pass

    obj = _load(query)
    crs = detect_crs(obj if isinstance(obj, dict) else {}, query_crs, label="query")

    feats = _features(obj)
    geom_dicts = [f.get("geometry") for f in feats if f.get("geometry")]
    if not geom_dicts:
        raise ValueError("query contains no geometry.")
    _LOG.info("query: %d feature(s), %d with geometry.", len(feats), len(geom_dicts))

    raw_bounds = None
    if check_order and crs is None:
        per_feature = [
            assert_lonlat_ranges(gd, label="query", expected_bounds=expected_bounds)
            for gd in geom_dicts
        ]
        raw_bounds = (
            min(b[0] for b in per_feature), min(b[1] for b in per_feature),
            max(b[2] for b in per_feature), max(b[3] for b in per_feature),
        )
    elif crs is not None:
        _LOG.info("query: non-WGS84 CRS declared; skipping lon/lat range check.")

    shapes = [_to_shape(gd) for gd in geom_dicts]
    # If the query is a FeatureCollection with several features, union them so
    # "the query" is one geometry to test against the dataset.
    if len(shapes) == 1:
        geom = shapes[0]
    else:
        _LOG.info("query: unioning %d geometries into a single query shape.", len(shapes))
        from shapely import union_all
        geom = union_all(shapes)

    geom = _normalize_shape(geom, fix_invalid=fix_invalid, label="query")
    geom = _maybe_reproject(geom, crs, target_crs, label="query")
    return geom, raw_bounds


def check_geometry_against_dataset(
    dataset: dict | str | Path,
    query: "dict | str | Path | Any",
    predicate: str = "intersects",
    *,
    dataset_crs: str | None = None,
    query_crs: str | None = None,
    target_crs: str = "EPSG:4326",
    fix_invalid: bool = True,
    check_coordinate_order: bool = True,
    expected_bounds: tuple[float, float, float, float] | None = None,
    verbose: bool = False,
) -> list[Match]:
    """Return the dataset features that satisfy ``predicate`` against ``query``.

    Parameters
    ----------
    dataset
        GeoJSON FeatureCollection / Feature / geometry, as a dict, a JSON
        string, or a path to a file.
    query
        The geometry to test (e.g. your MultiPolygon). Same accepted forms as
        ``dataset``, plus a ready-made Shapely geometry. A multi-feature query
        is unioned into a single geometry.
    predicate
        One of: intersects, within, contains, overlaps, touches, crosses,
        covers, covered_by, disjoint. ``predicate`` is evaluated as
        ``query.<predicate>(feature_geometry)`` — read it as
        "query <predicate> feature". So ``within`` finds features that *contain*
        the query; ``contains`` finds features the query *encloses*.
    dataset_crs, query_crs
        Override the CRS of each input (e.g. ``"EPSG:27700"`` for British
        National Grid). Leave as ``None`` to trust the GeoJSON spec / any legacy
        ``crs`` member. Non-WGS84 inputs are reprojected to ``target_crs``.
    target_crs
        CRS the comparison happens in. Defaults to WGS84. For area/length-aware
        predicates on UK data you may prefer ``"EPSG:27700"`` so the maths is in
        metres.
    fix_invalid
        Auto-repair invalid geometry with ``make_valid``. If False, invalid
        input raises.
    check_coordinate_order
        Run the lon/lat range guard (catches projected coords and out-of-range
        swaps). Skipped automatically when a non-WGS84 CRS is declared.
    expected_bounds
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` box your data should
        fall within. This is the only reliable guard against an axis swap whose
        values both stay in valid lon/lat range (the typical UK case). Anything
        outside the box raises.
    verbose
        Stream the CRS decisions, range checks, conversions, and the final
        query-vs-dataset bbox consistency cross-check to stderr (INFO level).

    Returns
    -------
    list[Match]
        One entry per matching dataset feature.

    Notes
    -----
    * Uses an STRtree spatial index, so it scales to large datasets: only the
      features whose bounding boxes overlap the query are tested precisely.
    * Antimeridian-crossing geometries (lon jumping +180/-180) are NOT handled
      specially; split such inputs first. Not a concern for UK data.
    * Floating-point slivers can make ``touches``/``overlaps`` finicky; consider
      ``shapely.set_precision`` on both sides if you see boundary flapping.
    """
    if verbose:
        _enable_verbose_logging()

    _shapely()  # fail early with a clear message if Shapely is missing
    from shapely import STRtree

    if predicate not in _PREDICATES:
        raise ValueError(
            f"Unknown predicate {predicate!r}. Choose from: {', '.join(_PREDICATES)}"
        )
    method = _PREDICATES[predicate]
    _LOG.info("predicate: query.%s(feature); comparison CRS: %s.", method, target_crs)

    q, q_raw_bounds = _prepare_query(
        query,
        query_crs=query_crs,
        target_crs=target_crs,
        fix_invalid=fix_invalid,
        check_order=check_coordinate_order,
        expected_bounds=expected_bounds,
    )

    ds = _load(dataset)
    ds_crs = detect_crs(ds if isinstance(ds, dict) else {}, dataset_crs, label="dataset")
    feats = _features(ds)
    _LOG.info("dataset: %d feature(s).", len(feats))

    geoms = []
    index_map = []  # parallel: position in geoms -> feature index
    skipped_null = 0
    skipped_unfixable = 0
    ds_raw_bounds: tuple[float, float, float, float] | None = None
    for i, feat in enumerate(feats):
        gd = feat.get("geometry")
        if not gd:
            skipped_null += 1
            continue  # null-geometry feature; skip
        if check_coordinate_order and ds_crs is None:
            b = assert_lonlat_ranges(gd, label=f"dataset feature {i}",
                                     expected_bounds=expected_bounds)
            if ds_raw_bounds is None:
                ds_raw_bounds = b
            else:
                ds_raw_bounds = (
                    min(ds_raw_bounds[0], b[0]), min(ds_raw_bounds[1], b[1]),
                    max(ds_raw_bounds[2], b[2]), max(ds_raw_bounds[3], b[3]),
                )
        try:
            g = _normalize_shape(_to_shape(gd), fix_invalid=fix_invalid,
                                 label=f"dataset feature {i}")
        except ValueError as e:
            _LOG.warning("dataset feature %d: skipped (%s).", i, e)
            skipped_unfixable += 1
            continue  # unfixable feature; skip rather than poison the whole run
        g = _maybe_reproject(g, ds_crs, target_crs, label=f"dataset feature {i}")
        geoms.append(g)
        index_map.append(i)

    if skipped_null or skipped_unfixable:
        _LOG.info(
            "dataset: %d usable geometr(ies); skipped %d null-geometry, %d unfixable.",
            len(geoms), skipped_null, skipped_unfixable,
        )

    if not geoms:
        _LOG.warning("dataset: no usable geometries; returning no matches.")
        return []

    # ---- Consistency cross-check (the key CRS safety net) -----------------
    # Both inputs are now in target_crs. Compare their bounding boxes: if they
    # don't overlap, for any overlap-based predicate there can be no match, and
    # that usually means a CRS / axis-order mismatch rather than a true miss.
    q_bounds = q.bounds
    ds_bounds = geoms[0].bounds
    for g in geoms[1:]:
        b = g.bounds
        ds_bounds = (min(ds_bounds[0], b[0]), min(ds_bounds[1], b[1]),
                     max(ds_bounds[2], b[2]), max(ds_bounds[3], b[3]))
    _LOG.info("consistency: query bbox   = %s", tuple(round(v, 6) for v in q_bounds))
    _LOG.info("consistency: dataset bbox = %s", tuple(round(v, 6) for v in ds_bounds))
    if _bbox_overlap(q_bounds, ds_bounds):
        _LOG.info("consistency: query and dataset bounding boxes OVERLAP (units consistent).")
    else:
        _LOG.warning(
            "consistency: query and dataset bounding boxes DO NOT OVERLAP. "
            "For overlap-based predicates this guarantees zero matches and "
            "strongly suggests a CRS or axis-order mismatch (e.g. lon/lat "
            "swapped, or one input in a projected CRS). Check your inputs."
        )

    # Spatial index prefilters by bbox for overlapping predicates. "disjoint"
    # can't be prefiltered (a non-overlapping bbox is still disjoint), so test
    # every feature in that one case.
    if predicate == "disjoint":
        candidates = range(len(geoms))
        _LOG.info("predicate is 'disjoint'; testing all %d features (no bbox prefilter).",
                  len(geoms))
    else:
        tree = STRtree(geoms)
        candidates = tree.query(q)  # bbox-level candidates
        _LOG.info("STRtree prefilter: %d of %d features are bbox candidates.",
                  len(candidates), len(geoms))

    matches: list[Match] = []
    for pos in candidates:
        g = geoms[int(pos)]
        if getattr(q, method)(g):
            i = index_map[int(pos)]
            feat = feats[i]
            matches.append(
                Match(
                    feature_index=i,
                    feature_id=feat.get("id"),
                    predicate=predicate,
                    properties=feat.get("properties") or {},
                )
            )
    _LOG.info("result: %d feature(s) satisfy '%s'.", len(matches), predicate)
    return matches


def any_match(dataset, query, predicate: str = "intersects", **kwargs) -> bool:
    """True if at least one dataset feature satisfies the predicate."""
    return len(check_geometry_against_dataset(dataset, query, predicate, **kwargs)) > 0


# ---------------------------------------------------------------------------
# Site check: area-overlap (for polygonal datasets) + point/feature proximity
# (for point / line datasets), with metric-CRS reconciliation.
# ---------------------------------------------------------------------------

@dataclass
class AreaOverlap:
    """An areal dataset feature whose polygon overlaps the site."""
    feature_index: int
    feature_id: Any
    overlap_area_m2: float
    feature_area_m2: float
    fraction_of_feature: float   # overlap_area / feature_area  (0..1)
    fraction_of_site: float      # overlap_area / site_area     (0..1)
    properties: dict = field(default_factory=dict)
    geometry: Any = None         # shapely intersection geom in the metric CRS (if kept)


@dataclass
class ProximityMatch:
    """A non-areal dataset feature (point/line) within the radius of the site."""
    feature_index: int
    feature_id: Any
    distance_m: float
    geom_type: str
    properties: dict = field(default_factory=dict)
    geometry: Any = None         # shapely feature geom in the metric CRS (if kept)


@dataclass
class SiteCheckResult:
    """Outcome of checking one site geometry against a dataset."""
    overlaps: bool                       # did any areal feature overlap the site?
    metric_crs: str
    radius_m: float
    n_features: int
    n_areal: int
    n_nonareal: int
    area_overlaps: list[AreaOverlap] = field(default_factory=list)
    proximity_matches: list[ProximityMatch] = field(default_factory=list)
    nearest_outside_m: float | None = None   # nearest non-areal feature beyond radius


def check_site(
    dataset: dict | str | Path,
    site: "dict | str | Path | Any",
    *,
    radius_m: float = 500.0,
    dataset_crs: str | None = None,
    site_crs: str | None = None,
    metric_crs: str = "EPSG:27700",
    fix_invalid: bool = True,
    check_coordinate_order: bool = True,
    keep_geometry: bool = False,
    verbose: bool = False,
) -> SiteCheckResult:
    """Check a site against a dataset, branching on the dataset's geometry kind.

    * **Areal features** (Polygon / MultiPolygon): test true 2D *overlap* with
      the site. Returns yes/no plus, for each overlapping feature, the overlap
      area (m²), the fraction of the feature and of the site covered, and the
      feature's properties.
    * **Non-areal features** (Point / MultiPoint / LineString ...): test
      *proximity* — the straight-line distance from the site to the feature.
      Features within ``radius_m`` are returned with their distance and
      properties.

    CRS handling
    ------------
    The site and the dataset CRS are detected **independently** (the site is not
    assumed to share the dataset's CRS) and both are reprojected into
    ``metric_crs`` so that area is in m² and distance/``radius_m`` are in metres.
    For UK data the default ``EPSG:27700`` (British National Grid) is correct;
    override it for other regions (e.g. a local UTM zone). A loud log line is
    emitted when the two declared CRSs differ.

    Parameters
    ----------
    radius_m
        Proximity radius in **metres** (interpreted in ``metric_crs``).
    dataset_crs, site_crs
        Override the detected CRS of each input (e.g. the site arrives as
        EPSG:4326 lon/lat while the dataset is EPSG:27700).
    metric_crs
        Projected CRS the comparison happens in. Must be metre-based for the
        area/distance numbers and ``radius_m`` to mean metres.
    keep_geometry
        If True, attach the shapely overlap / feature geometry (in ``metric_crs``)
        to each result for downstream use. Off by default to keep results light.
    verbose
        Stream the CRS/conversion/consistency log at INFO.

    geopandas, as an alternative to the JSON+shapely core (evaluation)
    ------------------------------------------------------------------
    Verdict: worth adopting if this grows into a *batch* tool across many layers;
    not worth it for single site-vs-layer checks.

    * What it replaces / improves: ``gpd.read_file`` reads the CRS from the file
      (incl. formats GeoJSON can't carry — GeoPackage, Shapefile ``.prj``), and
      ``to_crs()`` reprojects a whole layer in one call (vs. our per-feature
      ``_to_crs``). Vectorized ``.area`` / ``.distance()`` / ``.intersection()``
      / ``make_valid()`` over a ``GeoSeries`` are terser and faster than the
      Python feature loop, and ``sjoin_nearest(max_distance=...)`` does proximity
      natively. The win is clear at 10k+ features (here the biggest layer is
      ~2k, where the loop is already fine).
    * What it costs: a heavy dependency stack (pandas, pyogrio/fiona, GEOS) vs.
      the current shapely+pyproj-only footprint, and — most relevant here — it
      *hides* exactly the silent CRS/axis/validity steps this module logs. There
      is no GeoDataFrame equivalent of the axis-swap-within-valid-degrees
      cross-check.
    * Recommendation: keep this JSON+shapely core for explicit, logged single
      checks; introduce geopandas only behind a batch entry point (one site x
      all layers -> a summary table), where ``to_crs`` + ``sjoin_nearest`` would
      genuinely cut code and time.
    """
    if verbose:
        _enable_verbose_logging()

    _shapely()
    _LOG.info(
        "site check: metric CRS %s; proximity radius %.1f m (areal features use "
        "true overlap, not the radius).", metric_crs, radius_m,
    )

    # --- Site: normalize to WGS84 (runs the lon/lat guards), then to metric. ---
    site_wgs, _site_raw = _prepare_query(
        site, query_crs=site_crs, target_crs="EPSG:4326",
        fix_invalid=fix_invalid, check_order=check_coordinate_order,
    )
    site_decl = detect_crs(_load(site) if not _is_shapely(site) else {}, site_crs,
                           label="site (declared)") if not _is_shapely(site) else _normalize_crs_token(site_crs)
    site_m = _to_crs(site_wgs, None, metric_crs, label="site")
    site_area = site_m.area
    _LOG.info("site: area in %s = %.1f m².", metric_crs, site_area)

    # --- Dataset CRS, with an explicit note when it differs from the site. ---
    ds = _load(dataset)
    ds_crs = detect_crs(ds if isinstance(ds, dict) else {}, dataset_crs, label="dataset")
    if (site_decl or None) != (ds_crs or None):
        _LOG.warning(
            "CRS reconciliation: site declares %s but dataset declares %s; "
            "reprojecting both into %s before comparing.",
            site_decl or "WGS84", ds_crs or "WGS84", metric_crs,
        )

    feats = _features(ds)
    _LOG.info("dataset: %d feature(s).", len(feats))

    area_overlaps: list[AreaOverlap] = []
    proximity: list[ProximityMatch] = []
    n_areal = n_nonareal = 0
    nearest_outside: float | None = None
    ds_bounds: tuple[float, float, float, float] | None = None

    for i, feat in enumerate(feats):
        gd = feat.get("geometry")
        if not gd:
            continue
        if check_coordinate_order and ds_crs is None:
            assert_lonlat_ranges(gd, label=f"dataset feature {i}")
        try:
            g = _normalize_shape(_to_shape(gd), fix_invalid=fix_invalid,
                                 label=f"dataset feature {i}")
        except ValueError as e:
            _LOG.warning("dataset feature %d: skipped (%s).", i, e)
            continue
        g = _to_crs(g, ds_crs, metric_crs, label=f"dataset feature {i}")

        b = g.bounds
        ds_bounds = b if ds_bounds is None else (
            min(ds_bounds[0], b[0]), min(ds_bounds[1], b[1]),
            max(ds_bounds[2], b[2]), max(ds_bounds[3], b[3]),
        )

        props = feat.get("properties") or {}
        is_areal = g.geom_type in ("Polygon", "MultiPolygon") or (
            g.geom_type == "GeometryCollection" and g.area > 0
        )

        if is_areal:
            n_areal += 1
            inter = site_m.intersection(g)
            if (not inter.is_empty) and inter.area > 0:
                fa = g.area
                area_overlaps.append(AreaOverlap(
                    feature_index=i,
                    feature_id=feat.get("id"),
                    overlap_area_m2=inter.area,
                    feature_area_m2=fa,
                    fraction_of_feature=(inter.area / fa) if fa else 0.0,
                    fraction_of_site=(inter.area / site_area) if site_area else 0.0,
                    properties=props,
                    geometry=inter if keep_geometry else None,
                ))
        else:
            n_nonareal += 1
            dist = site_m.distance(g)  # metres in metric_crs
            if dist <= radius_m:
                proximity.append(ProximityMatch(
                    feature_index=i,
                    feature_id=feat.get("id"),
                    distance_m=dist,
                    geom_type=g.geom_type,
                    properties=props,
                    geometry=g if keep_geometry else None,
                ))
            elif nearest_outside is None or dist < nearest_outside:
                nearest_outside = dist

    # Consistency cross-check in the shared metric CRS.
    if ds_bounds is not None:
        sb = site_m.bounds
        if _bbox_overlap(sb, ds_bounds):
            _LOG.info("consistency: site and dataset bounding boxes OVERLAP (units consistent).")
        else:
            gap = max(0.0, (ds_bounds[0] - sb[2]), (sb[0] - ds_bounds[2]),
                      (ds_bounds[1] - sb[3]), (sb[1] - ds_bounds[3]))
            _LOG.warning(
                "consistency: site and dataset bounding boxes DO NOT OVERLAP "
                "(gap ~%.0f m in %s). For overlap this means no matches and "
                "likely a CRS/axis-order mismatch — verify the site CRS.",
                gap, metric_crs,
            )

    area_overlaps.sort(key=lambda a: a.overlap_area_m2, reverse=True)
    proximity.sort(key=lambda p: p.distance_m)

    result = SiteCheckResult(
        overlaps=bool(area_overlaps),
        metric_crs=metric_crs,
        radius_m=radius_m,
        n_features=len(feats),
        n_areal=n_areal,
        n_nonareal=n_nonareal,
        area_overlaps=area_overlaps,
        proximity_matches=proximity,
        nearest_outside_m=nearest_outside,
    )
    _LOG.info(
        "result: overlaps=%s; %d areal overlap(s), %d feature(s) within %.0f m "
        "(of %d non-areal).",
        result.overlaps, len(area_overlaps), len(proximity), radius_m, n_nonareal,
    )
    return result


def _is_shapely(obj) -> bool:
    try:
        from shapely.geometry.base import BaseGeometry
        return isinstance(obj, BaseGeometry)
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# geopandas alternative (sketch)
# ---------------------------------------------------------------------------
# If you'd rather use geopandas, the same job is:
#
#     import geopandas as gpd
#     ds = gpd.read_file("world_heritage.geojson")          # reads CRS from file
#     q  = gpd.read_file("query_multipolygon.geojson")
#     q  = q.to_crs(ds.crs)                                  # align CRS explicitly
#     ds["geometry"] = ds.geometry.make_valid()             # defensive repair
#     hits = gpd.sjoin(ds, q, predicate="intersects", how="inner")
#
# geopandas handles CRS metadata and the spatial join for you, at the cost of a
# heavier dependency (pandas, fiona/pyogrio, pyproj). The defensive axis/range
# checks above are still worth running on raw GeoJSON that has no CRS metadata.


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Check a query/site geometry against a GeoJSON dataset.")
    ap.add_argument("dataset", help="dataset .geojson")
    ap.add_argument("query", help="query / site .geojson")
    ap.add_argument(
        "--mode", default="predicate", choices=("predicate", "site"),
        help="'predicate': run a spatial predicate (default). "
             "'site': overlap for areal datasets + proximity for point/line datasets.",
    )
    ap.add_argument("--predicate", default="intersects", choices=list(_PREDICATES))
    ap.add_argument("--dataset-crs", default=None)
    ap.add_argument("--query-crs", default=None, help="CRS of the query/site, if not WGS84.")
    ap.add_argument("--target-crs", default="EPSG:4326", help="predicate mode: comparison CRS.")
    ap.add_argument("--metric-crs", default="EPSG:27700",
                    help="site mode: projected CRS for area/distance (default British National Grid).")
    ap.add_argument("--radius", type=float, default=500.0,
                    help="site mode: proximity radius in metres (default 500).")
    ap.add_argument(
        "--expected-bounds", default=None,
        help="min_lon,min_lat,max_lon,max_lat AOI box; coords outside it raise.",
    )
    ap.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v for INFO (CRS/conversion/consistency log), -vv for DEBUG.",
    )
    args = ap.parse_args()

    if args.verbose:
        _enable_verbose_logging(logging.DEBUG if args.verbose >= 2 else logging.INFO)

    def _name(props: dict) -> str:
        for k in ("SITE_NAME", "POLICY_NAM", "NAME", "Name", "name"):
            v = props.get(k)
            if v:
                return str(v)
        return ""

    if args.mode == "site":
        r = check_site(
            args.dataset, args.query,
            radius_m=args.radius,
            dataset_crs=args.dataset_crs, site_crs=args.query_crs,
            metric_crs=args.metric_crs,
        )
        print(f"overlaps: {'YES' if r.overlaps else 'NO'}  "
              f"(dataset: {r.n_areal} areal, {r.n_nonareal} non-areal feature(s); "
              f"metric CRS {r.metric_crs})")
        if r.area_overlaps:
            print(f"\narea overlaps ({len(r.area_overlaps)}):")
            for a in r.area_overlaps:
                print(f"  [{a.feature_index}] id={a.feature_id} {_name(a.properties)}")
                print(f"        overlap {a.overlap_area_m2:,.0f} m²  "
                      f"({a.fraction_of_feature*100:.1f}% of feature, "
                      f"{a.fraction_of_site*100:.1f}% of site)")
        if r.n_nonareal:
            print(f"\nwithin {r.radius_m:.0f} m ({len(r.proximity_matches)} of {r.n_nonareal} non-areal):")
            for p in r.proximity_matches:
                print(f"  [{p.feature_index}] id={p.feature_id} {_name(p.properties)}"
                      f"  — {p.distance_m:,.0f} m ({p.geom_type})")
            if not r.proximity_matches and r.nearest_outside_m is not None:
                print(f"  (none within radius; nearest is {r.nearest_outside_m:,.0f} m away)")
    else:
        expected_bounds = None
        if args.expected_bounds:
            parts = [float(x) for x in args.expected_bounds.split(",")]
            if len(parts) != 4:
                ap.error("--expected-bounds needs 4 comma-separated numbers")
            expected_bounds = tuple(parts)

        res = check_geometry_against_dataset(
            args.dataset, args.query, args.predicate,
            dataset_crs=args.dataset_crs, query_crs=args.query_crs,
            target_crs=args.target_crs,
            expected_bounds=expected_bounds,
        )
        print(f"{len(res)} feature(s) matched '{args.predicate}':")
        for m in res:
            print(f"  [{m.feature_index}] id={m.feature_id} {_name(m.properties)}")
