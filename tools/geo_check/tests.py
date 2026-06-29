"""
Regression tests for main.py
=================================

These lock in the behaviour exercised against the Telford (TWLP) data and the
Severn Trent Telford site, so future edits can't silently change the answers.

The test data lives in ``fixtures/`` next to this file — extracted copies of the
real layers, so the tests do NOT depend on the external ``twlp_geojson/`` or
``sites/`` downloads:

* ``site.geojson``              — the Severn Trent Telford site (MultiPolygon, WGS84)
* ``world_heritage.geojson``    — TWLP layer 35, Ironbridge Gorge WHS (Polygon)
* ``employment_sites.geojson``  — TWLP layer 25, 28 employment areas (Polygons)
* ``ancient_monuments.geojson`` — TWLP layer 00, 29 scheduled monuments (Points)

Requires shapely + pyproj (same as geo_check). No pytest needed:

    python3 tools/tests/tests.py     # standalone runner, exits non-zero on failure

It is also discoverable by pytest if you have it (``pytest tools/tests``).

Numeric assertions use tolerances, not exact equality: the EPSG:27700 reprojection
goes through pyproj/PROJ, whose grid-shift result can vary by a metre or two
between installs. The tolerances are wide enough to absorb that and tight enough
to catch a real regression (e.g. a CRS or axis bug that moves numbers by 10s of m).
"""

import json
import logging
import os
import sys
from contextlib import contextmanager

# Make `import geo_check` work regardless of CWD / how this is launched.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

import main as gc  # noqa: E402

_FIX = os.path.join(_HERE, "../pdf_to_markdown/tests/fixtures")


def _fix(name):
    return os.path.join(_FIX, name)


def _load(name):
    with open(_fix(name)) as f:
        return json.load(f)


SITE = _fix("site.geojson")
WORLD_HERITAGE = _fix("world_heritage.geojson")
EMPLOYMENT = _fix("employment_sites.geojson")
MONUMENTS = _fix("ancient_monuments.geojson")


@contextmanager
def _capture_logs(level=logging.WARNING):
    """Collect geo_check log records emitted inside the block."""
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = _H(level)
    logger = logging.getLogger("geo_check")
    prev_level = logger.level
    logger.addHandler(h)
    logger.setLevel(min(level, prev_level or level))
    try:
        yield records
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev_level)


def _messages(records):
    return " || ".join(r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# Predicate mode
# ---------------------------------------------------------------------------

def test_predicate_intersects_world_heritage():
    """The site intersects exactly the Ironbridge Gorge WHS feature."""
    matches = gc.check_geometry_against_dataset(WORLD_HERITAGE, SITE, "intersects")
    assert len(matches) == 1, f"expected 1 match, got {len(matches)}"
    m = matches[0]
    assert m.feature_id == 1
    assert "Ironbridge" in (m.properties.get("SITE_NAME") or "")


def test_predicate_no_match_employment():
    """The site intersects none of the 28 employment-site polygons."""
    matches = gc.check_geometry_against_dataset(EMPLOYMENT, SITE, "intersects")
    assert matches == [], f"expected no matches, got {len(matches)}"


# ---------------------------------------------------------------------------
# Site mode — areal overlap
# ---------------------------------------------------------------------------

def test_site_overlap_world_heritage():
    """Areal overlap: ~29,788 m², ~98.9% of the site sits inside the WHS."""
    r = gc.check_site(WORLD_HERITAGE, SITE)
    assert r.overlaps is True
    assert r.n_areal == 1 and r.n_nonareal == 0
    assert len(r.area_overlaps) == 1
    a = r.area_overlaps[0]
    assert a.feature_id == 1
    assert "Ironbridge" in (a.properties.get("SITE_NAME") or "")
    # overlap area in m² (EPSG:27700) — tolerance absorbs PROJ grid differences.
    assert 29500 <= a.overlap_area_m2 <= 30100, a.overlap_area_m2
    # the site is almost entirely inside the WHS, but the WHS dwarfs the site.
    assert a.fraction_of_site >= 0.98, a.fraction_of_site
    assert 0.004 <= a.fraction_of_feature <= 0.010, a.fraction_of_feature


def test_site_no_overlap_employment():
    """No employment-site polygon overlaps the site; all 28 are areal."""
    r = gc.check_site(EMPLOYMENT, SITE)
    assert r.overlaps is False
    assert r.area_overlaps == []
    assert r.n_areal == 28 and r.n_nonareal == 0


# ---------------------------------------------------------------------------
# Site mode — point proximity
# ---------------------------------------------------------------------------

def test_site_proximity_monuments_1km():
    """3 scheduled monuments within 1 km, nearest-first, at ~251/315/951 m."""
    r = gc.check_site(MONUMENTS, SITE, radius_m=1000)
    assert r.overlaps is False
    assert r.n_areal == 0 and r.n_nonareal == 29
    assert len(r.proximity_matches) == 3
    dists = [p.distance_m for p in r.proximity_matches]
    # sorted ascending
    assert dists == sorted(dists), dists
    expected = [251, 315, 951]
    for got, exp in zip(dists, expected):
        assert abs(got - exp) <= 10, f"distance {got} not within 10 m of {exp}"
    for p in r.proximity_matches:
        assert p.geom_type == "Point"
        assert p.distance_m <= 1000


def test_site_proximity_monuments_tight_radius_reports_nearest():
    """Radius 100 m finds nothing; nearest-outside is still reported (~251 m)."""
    r = gc.check_site(MONUMENTS, SITE, radius_m=100)
    assert r.proximity_matches == []
    assert r.nearest_outside_m is not None
    assert abs(r.nearest_outside_m - 251) <= 10, r.nearest_outside_m


# ---------------------------------------------------------------------------
# CRS handling — the behaviours these tools exist to protect
# ---------------------------------------------------------------------------

def _reproject_to_bng(geojson_dict):
    """Return a copy of a WGS84 FeatureCollection reprojected to EPSG:27700,
    tagged with a legacy ``crs`` member — i.e. a dataset NOT in WGS84."""
    from pyproj import Transformer
    from shapely.geometry import shape, mapping
    from shapely.ops import transform
    t = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    out = json.loads(json.dumps(geojson_dict))  # deep copy
    for feat in out["features"]:
        feat["geometry"] = mapping(transform(t.transform, shape(feat["geometry"])))
    out["crs"] = {"type": "name", "properties": {"name": "EPSG:27700"}}
    return out


def test_crs_reconciliation_site_wgs84_vs_dataset_bng():
    """A WGS84 site vs an EPSG:27700 dataset reconciles to the same overlap,
    and the CRS mismatch is logged."""
    bng_dataset = _reproject_to_bng(_load("world_heritage.geojson"))
    with _capture_logs(logging.WARNING) as recs:
        r = gc.check_site(bng_dataset, SITE)  # site is WGS84, dataset is BNG
    assert r.overlaps is True
    assert len(r.area_overlaps) == 1
    a = r.area_overlaps[0]
    # Same answer as the all-WGS84 run — the differing CRS must not change it.
    assert 29500 <= a.overlap_area_m2 <= 30100, a.overlap_area_m2
    assert a.fraction_of_site >= 0.98
    assert "reconciliation" in _messages(recs).lower(), _messages(recs)


def test_axis_swap_is_caught_by_consistency_check():
    """Swapping the site's lat/lon (both stay in valid degree range) yields no
    match AND a 'do not overlap' consistency warning rather than a silent miss."""
    site = _load("site.geojson")

    def swap(node):
        if (isinstance(node, list) and len(node) >= 2
                and isinstance(node[0], (int, float))
                and isinstance(node[1], (int, float))):
            return [node[1], node[0]] + list(node[2:])
        return [swap(n) for n in node]

    g = site["features"][0]["geometry"]
    g["coordinates"] = swap(g["coordinates"])

    with _capture_logs(logging.WARNING) as recs:
        matches = gc.check_geometry_against_dataset(WORLD_HERITAGE, site, "intersects")
    assert matches == []
    assert "do not overlap" in _messages(recs).lower(), _messages(recs)


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
