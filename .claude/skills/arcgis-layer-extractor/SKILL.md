---
name: arcgis-layer-extractor
description: >-
  Extract and download the underlying datasets behind an ArcGIS web map or web
  app (Web AppBuilder, Experience Builder, Instant Apps, or a plain webappviewer
  link). Use this whenever a user shares an arcgis.com / *.maps.arcgis.com app or
  map URL and wants the data, layers, shapefiles, GeoJSON, or "the datasets"
  from it — even if they just say "download this map" or "can you get these
  layers". Also use when a user has a Feature Service / MapServer REST URL and
  wants every layer pulled down, or asks how to export ArcGIS layers in bulk.
  The key insight this skill encodes: an ArcGIS app is a JavaScript shell, so the
  data is never in the page — it must be traced through the app config to the web
  map to the feature service REST endpoints, then exported from there.
---

# ArcGIS Layer Extractor

## What this solves

An ArcGIS web app (the kind at `…/apps/webappviewer/index.html?id=…`,
`…/apps/experiencebuilder/…`, `…/apps/instant/…`, etc.) is a JavaScript
application. Fetching the page gives you an empty HTML shell — the actual
datasets are streamed at runtime from **feature service REST endpoints**. So
"download the datasets" really means: trace the app → web map → layer service
URLs, then export each layer from its REST endpoint.

There is no single download button on most apps. This skill is the reliable path.

## The chain to follow

```
App ID  ──/data?f=json──▶  Web Map ID  ──/data?f=json──▶  operationalLayers[]  ──/query──▶  GeoJSON / CSV / JSON
(in URL)                   (map.itemId)                   (each .url = a layer)            (the actual data)
```

Work through it in order. Each step hands you the input for the next.

### Step 1 — Identify the portal and the app ID

From a URL like
`https://ORG.maps.arcgis.com/apps/webappviewer/index.html?id=APPID`:
- **portal** = `https://ORG.maps.arcgis.com`
- **app id** = the `id` (or `appid`) query parameter.

Don't bother fetching the viewer URL itself — it returns only the JS shell.

### Step 2 — Read the app config to find the web map

Open this in a browser (or fetch it):

```
{portal}/sharing/rest/content/items/{APPID}/data?f=json
```

In the returned JSON, the web map ID is at **`map.itemId`** (Web AppBuilder) or
inside the app's data config (`mapItemId`, or a `values.webmap` for Instant
Apps / StoryMaps). Grab that ID.

### Step 3 — Read the web map to list every layer

```
{portal}/sharing/rest/content/items/{WEBMAPID}/data?f=json
```

The **`operationalLayers`** array is what you want. Each entry has:
- `url` — the REST endpoint, e.g. `…/FeatureServer/29` or `…/MapServer/3`
- `title` — a human-readable name (use it for filenames)

Note that many layers often share one Feature Service, differing only by the
trailing index (`/0`, `/1`, …). Also check `baseMap.baseMapLayers` if the user
wants the basemap, and the top-level `tables` array for non-spatial tables.

### Step 4 — Triage: is each layer directly downloadable?

Look at the host of each `url`:
- **`services*.arcgis.com/...`** → public ArcGIS Online hosted service. Almost
  always directly queryable, no token. **Easiest case.**
- **An org domain or `…/usrsvcs/…`, `…/locatorhub/…`, a `/sharing/proxy`** →
  routed through the organisation's proxy or secured. May need a token or only
  work from inside the app. Flag this to the user; the open-data-portal fallback
  (Step 6) is usually better here.

You can confirm a layer is reachable and see its fields/record count by opening
the layer URL with `?f=json` (e.g. `…/FeatureServer/29?f=json`).

### Step 5 — Export each layer

The download URL for any feature layer:

```
{layerUrl}/query?where=1=1&outFields=*&outSR=4326&f=geojson
```

- `f=geojson` → GeoJSON (auto-reprojected to WGS84). Swap for `f=json` (Esri
  JSON) or `f=csv` (attributes only, no geometry).
- `outSR=4326` → lon/lat. Drop it to keep the service's native projection.
- **Pagination matters.** Services cap each response (commonly 1000–2000
  features) and set `exceededTransferLimit: true` when more remain. Page through
  with `resultOffset` until the flag is false. The bundled script does this for
  you — prefer it over hand-built URLs when there is more than one layer or any
  layer is large.

For bulk download of a whole service, use `scripts/download_layers.py` (below).

### Step 6 — Mention the easy alternatives

Always offer these too, since they may save the user effort:
- **Feature-service item page → Export Data.** If the publisher enabled it,
  `{portal}/home/item.html?id={SERVICE_ITEM_ID}` offers one-click Shapefile /
  File Geodatabase / GeoJSON / CSV. (`SERVICE_ITEM_ID` is the `itemId` on the
  operational layers, often shared across them.) If there's no Export button,
  extract is disabled and the query route is the way.
- **The organisation's open data hub** (e.g. `data.<council>.gov.uk` or a Hub
  site) frequently publishes the same layers with proper download buttons and
  clearer licensing. Worth a quick search by layer name.

## Running this inside Claude

Claude usually **cannot fetch the `…/data?f=json` and `/query` endpoints
directly** — they aren't in search indexes (so `web_fetch` refuses them) and the
sandbox often has no outbound network. Handle it like this:

1. If a browser tool is available, drive Steps 2–3 with it.
2. Otherwise, give the user the exact URL for the current step and ask them to
   paste the JSON back. Two short round-trips (app config, then web map) is all
   it takes. This is the normal, expected flow — not a failure.
3. Once you have the `operationalLayers`, do the work for the user: build the
   per-layer export URLs **and** generate a ready-to-run download script using
   the bundled template, rather than just handing over raw links.

## Deliverables to produce

For a typical "download these datasets" request, return:
1. A short note on what the app is and that the layers are public/secured.
2. A customised copy of `scripts/download_layers.py` pre-filled with the
   service URL and the layer id → name mapping you extracted.
3. The single-layer query URL pattern, so they can grab one by hand.
4. The Export-Data and open-data-hub fallbacks.
5. A reminder to check the data's licence/attribution before redistributing.

## Bundled script

`scripts/download_layers.py` — points at a Feature Service (or MapServer) base
URL, auto-discovers the layers from the service root, and downloads each as
GeoJSON with correct pagination. It accepts an optional explicit layer→name map
and an optional subset of layer ids. See the docstring at the top of the file.
Generalise/trim it for the specific job rather than shipping it verbatim when a
tailored version is clearer.

## Notes and gotchas

- **Tokens**: secured services need `&token=…`. Don't ask users for credentials
  or paste tokens into URLs on their behalf — point them to ArcGIS sign-in.
- **Record count sanity check**: `…/{layer}/query?where=1=1&returnCountOnly=true&f=json`
  tells you how many features to expect before downloading.
- **Attachments / related tables**: `operationalLayers[].featureCollection`,
  `showAttachments`, and the top-level `tables` array hint at extra data the map
  shows that isn't a plain layer.
- **Licensing**: plan-evidence and council data is often openly licensed but
  with attribution terms. Flag this; don't assert it's free to reuse.
