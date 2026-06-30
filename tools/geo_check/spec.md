# geo_check — specification

A small, defensive toolkit for answering **"does this shape hit anything in that
dataset, and how?"** against GeoJSON data — without falling into the quiet GIS
failure modes (wrong CRS, swapped axes, invalid geometry) that produce
*confidently wrong* answers rather than errors.


- **Code:** `tools/geo_check/main.py`
- **Tests:** `tools/geo_check/tests.py` (standalone runner; pytest-compatible)
- **Deps:** Shapely 2.x (required), pyproj (only imported when a reprojection or
  metric comparison is actually needed)

---

## 1. Scope

### In scope

1. **Predicate check** (`check_geometry_against_dataset`) — return the dataset
   features that satisfy a binary spatial predicate against a query geometry.
   Supported predicates (read as `query.<predicate>(feature)`):
   `intersects, within, contains, overlaps, touches, crosses, covers,
   covered_by, disjoint`.
2. **Site check** (`check_site`) — a higher-level "what does this site hit?"
   that **branches on the dataset feature's geometry kind**:
   - **Areal** (Polygon / MultiPolygon): true 2-D **overlap** — yes/no plus, per
     overlapping feature, the overlap **area in m²**, the fraction of the feature
     and of the site covered, and the feature's properties.
   - **Non-areal** (Point / MultiPoint / Line…): **proximity** — straight-line
     distance from the site; features within a configurable **radius (metres)**
     are returned with distance + properties, plus the nearest feature *outside*
     the radius for context.
3. **Defensive input handling**, always on:
   - CRS detection (explicit override → legacy GeoJSON `crs` member → RFC 7946
     default of WGS84 lon/lat).
   - lon/lat range guard (catches projected metres and out-of-range axis swaps).
   - validity repair (`make_valid`), 2-D flattening (`force_2d`), empty-geometry
     rejection.
   - a **bounding-box consistency cross-check** between query and dataset in the
     shared comparison CRS — the only guard that catches an axis swap whose
     values *both* stay inside valid lon/lat range (the typical UK case).
4. **Observability** — every assumption and conversion is logged through the
   `geo_check` logger (`verbose=True` / `-v` at INFO, `-vv` at DEBUG). Silent by
   default so it is safe inside larger pipelines.
5. **CLI** — `python3 tools/geo_check/main.py <dataset> <query> [--mode predicate|site] …`
   (see §5).

### Out of scope (deliberately)

- **Antimeridian-crossing geometries** (lon jumping ±180). Not handled; split
  such inputs first. Not a concern for UK data.
- **Attribute / non-spatial filtering**, joins producing merged property tables,
  or writing results back to GeoJSON. Results are returned as lightweight
  dataclasses; serialisation is the caller's job.
- **Geodesic (ellipsoidal) area/distance.** Metric maths is done in a *projected*
  CRS (planar), defaulting to British National Grid. Good to sub-percent at
  site/parcel scale; not intended for continental distances.
- **Datums beyond what pyproj/PROJ provides.** We pick the CRS and call pyproj;
  grid-shift accuracy is whatever the local PROJ install offers.
- **Raster data**, non-GeoJSON vector formats (Shapefile, GeoPackage…). GeoJSON
  dict / file / JSON-string in; an already-built Shapely geometry is also
  accepted for the query/site.

---

## 2. Public API

```python
# --- Predicate mode --------------------------------------------------------
check_geometry_against_dataset(
    dataset, query, predicate="intersects", *,
    dataset_crs=None, query_crs=None, target_crs="EPSG:4326",
    fix_invalid=True, check_coordinate_order=True,
    expected_bounds=None, verbose=False,
) -> list[Match]

any_match(dataset, query, predicate="intersects", **kwargs) -> bool

# --- Site mode -------------------------------------------------------------
check_site(
    dataset, site, *,
    radius_m=500.0, dataset_crs=None, site_crs=None,
    metric_crs="EPSG:27700", fix_invalid=True,
    check_coordinate_order=True, keep_geometry=False, verbose=False,
) -> SiteCheckResult
```

**Result types** (dataclasses):

- `Match(feature_index, feature_id, predicate, properties)`
- `AreaOverlap(feature_index, feature_id, overlap_area_m2, feature_area_m2,
  fraction_of_feature, fraction_of_site, properties, geometry=None)`
- `ProximityMatch(feature_index, feature_id, distance_m, geom_type, properties,
  geometry=None)`
- `SiteCheckResult(overlaps, metric_crs, radius_m, n_features, n_areal,
  n_nonareal, area_overlaps, proximity_matches, nearest_outside_m)`

**Inputs accepted** for `dataset` / `query` / `site`: a path to a `.geojson`/
`.json` file, a raw JSON string, a parsed `dict` (FeatureCollection / Feature /
bare geometry), or — for the query/site — a ready-made Shapely geometry. A
multi-feature query is **unioned** into a single geometry before testing.

---

## 3. Key design decisions (and the rationale)

| # | Decision | Why |
|---|----------|-----|
| D1 | **Shapely is the geometry engine**; pyproj only for reprojection. | Don't hand-roll geometry maths. Keep the dependency footprint minimal — pyproj is a lazy import so range/CRS guards still work where it's absent. |
| D2 | **Fail loud and early on bad input**, repair only what's safe. | The dangerous GIS bugs return wrong answers, not exceptions. Range checks, axis-swap detection, and validity repair convert silent wrongness into a clear error or a logged repair. |
| D3 | **CRS is detected per input, independently** (override → legacy `crs` member → assume WGS84 per RFC 7946). The site is **never** assumed to share the dataset's CRS. | A site supplied in lon/lat must still reconcile against a dataset in British National Grid. Both are reprojected into a common CRS; a divergence is logged at WARNING. |
| D4 | **Bounding-box consistency cross-check** after both inputs are in the comparison CRS. | A pure range check cannot see an axis swap when both swapped values stay in valid lon/lat range (UK: lat≈52 is a valid lon, lon≈−2 a valid lat). Non-overlapping bboxes flag the swap/wrong-region/wrong-CRS that the range check misses. |
| D5 | **`check_site` branches on geometry kind**: areal → overlap, non-areal → proximity. | "Overlap" is meaningless for points and "distance" is the wrong question for an enclosing polygon. The dataset's geometry dictates the only sensible question. |
| D6 | **Metric maths happens in a projected CRS** (`metric_crs`, default `EPSG:27700`), so **area is m² and `radius_m` is metres**. | Area/distance in raw WGS84 degrees is meaningless. BNG is correct for UK data; override `metric_crs` (e.g. a local UTM zone) elsewhere. Planar, not geodesic (see out-of-scope). |
| D7 | **Predicate comparison defaults to WGS84** (`target_crs="EPSG:4326"`); metric reprojection is reserved for `check_site`. | Topological predicates (intersects/within…) are CRS-invariant, so reprojecting just to test them adds cost and error. Area/length-aware work uses the metric path instead. |
| D8 | **STRtree spatial index** prefilters candidates by bbox (predicate mode); `disjoint` is the documented exception (a non-overlapping bbox is still disjoint, so it tests all features). | Scales to large datasets — only features whose bboxes overlap the query are tested precisely. |
| D9 | **Unfixable dataset features are skipped, not fatal**; null-geometry features are skipped. | One bad feature shouldn't poison a whole-dataset run. Skips are logged. |
| D10 | **Multi-feature query is unioned** into one geometry. | "The query" is a single area of interest; unioning avoids double-counting and ambiguous per-feature semantics. |
| D11 | **Logging, off by default** via a module logger with a `NullHandler`. | Observability without noise: pipelines stay quiet; humans pass `-v`/`verbose=True` to see every CRS decision, conversion, and the consistency verdict. |
| D12 | **Results are lightweight dataclasses**; `keep_geometry=False` by default. | Callers usually want feature ids + properties + numbers, not megabytes of Shapely geometry. Opt in with `keep_geometry=True`. |
| D13 | **`expected_bounds` available as a hard AOI guard** (predicate mode). | When the caller knows the rough lon/lat box the data must fall in, anything outside raises — the only fully reliable guard against an in-range axis swap. |

### geopandas — considered, not adopted (yet)

A `geopandas` (`read_file` + `to_crs` + `sjoin`/`sjoin_nearest`) implementation
would replace the manual JSON walking and per-feature reprojection with
vectorised calls, and would win clearly at 10k+ features. It was **not** adopted
because (a) it pulls in a heavy stack (pandas, pyogrio/fiona, GEOS) versus the
current shapely+pyproj-only footprint, and (b) it *hides* exactly the silent
CRS/axis/validity steps this tool exists to surface — there is no GeoDataFrame
equivalent of the axis-swap-within-valid-degrees cross-check (D4). **Recommended
trigger to revisit:** a batch entry point (one site × many layers → a summary
table), where `to_crs` + `sjoin_nearest` genuinely cut code and time.

---

## 4. Behavioural contract (locked by tests)

These invariants are pinned in `tools/geo_check/tests.py` against committed
fixtures (extracts of the Telford TWLP layers + the Severn Trent Telford site),
so they are independent of the external `twlp_geojson/` / `sites/` downloads:

- Predicate `intersects` of the site vs the World Heritage layer → exactly the
  one Ironbridge Gorge feature; vs the 28 employment polygons → no match.
- Site overlap vs World Heritage → `overlaps=True`, one `AreaOverlap`, overlap
  ≈ 29,500–30,100 m², ≥ 98% of the site inside the WHS.
- Site vs employment layer → `overlaps=False`, `n_areal=28`, `n_nonareal=0`.
- Site proximity vs scheduled monuments at `radius_m=1000` → 3 points, nearest
  first, ≈ 251 / 315 / 951 m; at `radius_m=100` → none, but `nearest_outside_m`
  ≈ 251 m.
- A WGS84 site vs an EPSG:27700 dataset → the **same** overlap result, and the
  CRS divergence is logged (CRS reconciliation, D3).
- A site with lat/lon **swapped** (both values still in valid degree range) →
  no match **and** a "bounding boxes do not overlap" WARNING, not a silent miss
  (D4).

Numeric assertions use tolerances (e.g. area ±~300 m², distance ±10 m) to absorb
pyproj/PROJ grid-shift differences between installs while still catching a real
CRS/axis regression that moves numbers by tens of metres.

---

## 5. CLI

```
# from the repo root, either form works:
python3 tools/geo_check/main.py <dataset.geojson> <query.geojson> [options]
python3 -m tools.geo_check.main <dataset.geojson> <query.geojson> [options]

  --mode {predicate,site}   default: predicate
  --predicate NAME          predicate mode; one of the nine supported predicates
  --dataset-crs / --query-crs CRS overrides (e.g. EPSG:27700)
  --target-crs CRS          predicate mode comparison CRS (default EPSG:4326)
  --metric-crs CRS          site mode metric CRS   (default EPSG:27700)
  --radius METRES           site mode proximity radius (default 500)
  --expected-bounds min_lon,min_lat,max_lon,max_lat   hard AOI guard
  -v / -vv                  INFO / DEBUG logging
```

---

## 6. Assumptions & limitations (read before trusting a result)

- **GeoJSON without a `crs` member is treated as WGS84 lon/lat** (RFC 7946). If a
  file is silently in another CRS and carries no `crs` member, pass
  `dataset_crs` / `query_crs` explicitly — the range and bbox guards will *often*
  but not *always* catch the mismatch.
- **`radius_m` and overlap areas are only metres/m² if `metric_crs` is a
  metre-based projected CRS.** The default (BNG) is UK-specific.
- **Planar metric maths**: accurate at local scale, not geodesic.
- **Antimeridian** geometries are unsupported (see out-of-scope).
- `touches`/`overlaps` can flap on floating-point slivers; consider
  `shapely.set_precision` on both sides if boundary cases matter.
