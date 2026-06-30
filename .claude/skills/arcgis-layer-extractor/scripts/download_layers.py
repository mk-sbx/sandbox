#!/usr/bin/env python3
"""
Download all (or selected) layers of an ArcGIS Feature Service / MapServer as
GeoJSON, with correct pagination.

This is the generalised version of the workflow's final step. Point it at a
service base URL; it discovers the layers from the service root and downloads
each one, paging through with resultOffset so nothing is truncated.

Quick start
-----------
    pip install requests

    # download every layer of a public service:
    python3 download_layers.py \
        "https://services.arcgis.com/AU9VxLgMXQBv7kGW/arcgis/rest/services/TWLP_Data/FeatureServer"

    # only specific layer ids:
    python3 download_layers.py "<service-url>" --layers 29 31 35

Options
-------
    --layers N [N ...]   restrict to these layer ids (default: all discovered)
    --out DIR            output directory (default: ./arcgis_geojson)
    --token TOKEN        token for secured services (appended to every request)
    --format FMT         geojson | json | csv  (default: geojson)
    --keep-sr            keep the service's native projection (default: reproject to 4326)

For secured services you need a valid token. Get it by signing in to the portal;
do not embed long-lived credentials in shared scripts.

You can also hard-code a friendly layer-id -> name mapping in LAYER_NAMES below
(handy when you already extracted titles from the web map's operationalLayers).
"""

import os
import sys
import json
import time
import argparse
import requests

# Optional: prefill {layer_id: "Friendly_Name"} to get nicer filenames.
# Leave empty to use the service's own layer names.
LAYER_NAMES = {}


def safe_name(s):
    keep = "-_.() "
    return "".join(c if c.isalnum() or c in keep else "_" for c in s).strip().replace(" ", "_")


def discover_layers(base, token):
    """Read the service root to list layer ids and names."""
    params = {"f": "json"}
    if token:
        params["token"] = token
    r = requests.get(base, params=params, timeout=60)
    r.raise_for_status()
    meta = r.json()
    layers = []
    for lyr in meta.get("layers", []) + meta.get("tables", []):
        layers.append((lyr["id"], lyr.get("name", f"layer_{lyr['id']}")))
    if not layers:
        raise RuntimeError(
            "No layers found at the service root. Is the URL a FeatureServer/"
            "MapServer base (not a single /N layer)? Is a token required?"
        )
    return layers


def fetch_layer(base, layer_id, token, fmt, keep_sr):
    """Page through one layer and return its full payload."""
    features = []
    raw_text_parts = []  # for csv
    offset = 0
    page = 2000

    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": fmt,
            "resultOffset": offset,
            "resultRecordCount": page,
            "returnGeometry": "true",
        }
        if not keep_sr and fmt != "csv":
            params["outSR"] = "4326"
        if token:
            params["token"] = token

        url = f"{base}/{layer_id}/query"
        r = requests.get(url, params=params, timeout=180)
        r.raise_for_status()

        if fmt == "csv":
            text = r.text
            if offset == 0:
                raw_text_parts.append(text)
            else:
                # drop header row on subsequent pages
                raw_text_parts.append(text.split("\n", 1)[1] if "\n" in text else "")
            # csv gives no transfer-limit flag; stop when a short page returns
            rows = text.count("\n")
            if rows <= 1 or rows < page:
                break
            offset += page
            time.sleep(0.2)
            continue

        data = r.json()
        batch = data.get("features", [])
        features.extend(batch)
        exceeded = data.get("exceededTransferLimit") or data.get("properties", {}).get("exceededTransferLimit")
        if not batch or not exceeded:
            break
        offset += len(batch)
        time.sleep(0.2)

    if fmt == "csv":
        return "".join(raw_text_parts), len(raw_text_parts)
    return {"type": "FeatureCollection", "features": features}, len(features)


def main():
    ap = argparse.ArgumentParser(description="Download ArcGIS feature service layers.")
    ap.add_argument("service_url", help="FeatureServer/MapServer base URL")
    ap.add_argument("--layers", nargs="+", type=int, default=None)
    ap.add_argument("--out", default="arcgis_geojson")
    ap.add_argument("--token", default=None)
    ap.add_argument("--format", default="geojson", choices=["geojson", "json", "csv"])
    ap.add_argument("--keep-sr", action="store_true")
    args = ap.parse_args()

    base = args.service_url.rstrip("/")
    os.makedirs(args.out, exist_ok=True)

    discovered = dict(discover_layers(base, args.token))
    ids = args.layers if args.layers is not None else sorted(discovered)

    ext = {"geojson": "geojson", "json": "json", "csv": "csv"}[args.format]

    for lid in ids:
        name = LAYER_NAMES.get(lid) or discovered.get(lid, f"layer_{lid}")
        name = safe_name(name)
        try:
            print(f"[{lid:>2}] {name} ... ", end="", flush=True)
            payload, count = fetch_layer(base, lid, args.token, args.format, args.keep_sr)
            path = os.path.join(args.out, f"{lid:02d}_{name}.{ext}")
            with open(path, "w", encoding="utf-8") as f:
                if args.format == "csv":
                    f.write(payload)
                else:
                    json.dump(payload, f, ensure_ascii=False)
            unit = "rows" if args.format == "csv" else "features"
            print(f"{count} {unit} -> {path}")
        except Exception as e:
            print(f"FAILED: {e}")

    print(f"\nDone. Files are in ./{args.out}/")


if __name__ == "__main__":
    main()
