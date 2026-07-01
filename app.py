#!/usr/bin/env python3
"""SatSearch — browse the clearest, highest-resolution satellite imagery and
DEM data for ANY area of interest. No login, no API keys, no signup.

Upload a shapefile (zipped) or GeoJSON, pick a source and dates, and get the
best (least cloudy, highest resolution) scenes rendered inline. DEM elevation
data too. All data comes from the public Microsoft Planetary Computer STAC API.

Run:
    conda env create -f environment.yml
    conda activate satsearch
    python app.py
Then open http://127.0.0.1:5000
"""
import argparse
import io
import json
import tempfile
import zipfile

import geopandas as gpd
import requests
from flask import Flask, jsonify, render_template, request
from shapely.geometry import mapping, shape

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"

# Imagery sources, ordered highest-resolution first.
# gsd = ground sample distance (meters/pixel). Lower = sharper.
SOURCES = {
    "naip": {
        "label": "NAIP aerial (US only, ~0.6 m)",
        "collections": ["naip"],
        "gsd": 0.6, "has_cloud": False, "since": "2010",
    },
    "sentinel-2": {
        "label": "Sentinel-2 (10 m, 2015+)",
        "collections": ["sentinel-2-l2a"],
        "gsd": 10, "has_cloud": True, "since": "2015",
    },
    "landsat": {
        "label": "Landsat (30 m, 1972+)",
        "collections": ["landsat-c2-l2", "landsat-c2-l1"],
        "gsd": 30, "has_cloud": True, "since": "1972",
    },
}

# DEM (elevation) sources, highest-resolution first.
# provider "tnm"       -> USGS The National Map API (US, up to 1 m)
# provider "planetary" -> Planetary Computer STAC (global fallback)
DEM_SOURCES = {
    "usgs-1m": {"label": "USGS 3DEP (US, 1 m)", "provider": "tnm",
                "dataset": "Digital Elevation Model (DEM) 1 meter"},
    "usgs-3m": {"label": "USGS 3DEP (US, 3 m / 1/9 arc-sec)", "provider": "tnm",
                "dataset": "National Elevation Dataset (NED) 1/9 arc-second"},
    "usgs-10m": {"label": "USGS 3DEP (US, 10 m / 1/3 arc-sec)", "provider": "tnm",
                 "dataset": "National Elevation Dataset (NED) 1/3 arc-second"},
    "cop30": {"label": "Copernicus DEM (global, 30 m)", "provider": "planetary",
              "collections": ["cop-dem-glo-30"]},
    "nasadem": {"label": "NASADEM (global, 30 m)", "provider": "planetary",
                "collections": ["nasadem"]},
}

TNM = "https://tnmaccess.nationalmap.gov/api/v1/products"

app = Flask(__name__)


def geometry_from_upload(file_storage):
    """Read an uploaded shapefile .zip or GeoJSON into one WGS84 geometry."""
    name = (file_storage.filename or "").lower()
    data = file_storage.read()
    if name.endswith(".zip"):
        with tempfile.TemporaryDirectory() as td:
            zpath = f"{td}/upload.zip"
            with open(zpath, "wb") as fh:
                fh.write(data)
            with zipfile.ZipFile(zpath) as zf:
                if not any(n.lower().endswith(".shp") for n in zf.namelist()):
                    raise ValueError("Zip has no .shp file inside.")
            gdf = gpd.read_file(f"zip://{zpath}")
    elif name.endswith((".geojson", ".json")):
        gdf = gpd.read_file(io.BytesIO(data))
    else:
        raise ValueError("Upload a zipped shapefile (.zip) or a .geojson file.")
    if gdf.empty:
        raise ValueError("No features found in upload.")
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    merged = gdf.geometry.union_all()
    return mapping(merged)


def stac_search(collections, geometry, start=None, end=None, max_cloud=None,
                has_cloud=True, limit=250):
    features = []
    for coll in collections:
        body = {"collections": [coll], "intersects": geometry, "limit": limit}
        if start and end:
            body["datetime"] = f"{start}T00:00:00Z/{end}T23:59:59Z"
        if has_cloud and max_cloud is not None:
            body["query"] = {"eo:cloud_cover": {"lt": max_cloud}}
        r = requests.post(STAC, json=body, timeout=90)
        r.raise_for_status()
        features.extend(r.json().get("features", []))
    return features


def to_rows(features, source_gsd, has_cloud):
    seen, rows = set(), []
    for f in features:
        fid = f["id"]
        if fid in seen:
            continue
        seen.add(fid)
        p = f["properties"]
        preview = f["assets"].get("rendered_preview", {}).get("href")
        if not preview:
            continue
        cloud = p.get("eo:cloud_cover") if has_cloud else None
        rows.append({
            "date": (p.get("datetime") or "?")[:10],
            "satellite": p.get("platform") or p.get("constellation") or "",
            "cloud": round(float(cloud), 1) if cloud is not None else None,
            "gsd": p.get("gsd", source_gsd),
            "scene_id": fid,
            "preview": preview,
        })
    return rows


def rank_best(rows):
    """Best = least cloud first, then sharpest (smallest gsd)."""
    return sorted(rows, key=lambda r: (
        r["cloud"] if r["cloud"] is not None else 0,
        r["gsd"] if r["gsd"] is not None else 999,
    ))


@app.route("/")
def index():
    return render_template("index.html", sources=SOURCES, dems=DEM_SOURCES)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        geom = geometry_from_upload(request.files["file"])
        b = shape(geom).bounds
        return jsonify({"geometry": geom, "bbox": list(b)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json(force=True)
    geometry = body.get("geometry")
    if not geometry:
        return jsonify({"error": "No area of interest. Upload a shapefile first."}), 400
    src_key = body.get("source", "sentinel-2")
    src = SOURCES.get(src_key)
    if not src:
        return jsonify({"error": f"Unknown source {src_key}"}), 400
    start = body.get("start", "1950-01-01")
    end = body.get("end", "2026-12-31")
    max_cloud = float(body.get("max_cloud", 10))
    best_per_year = bool(body.get("best_per_year", False))
    try:
        if best_per_year:
            out = []
            y0 = max(int(start[:4]), int(src["since"]))
            y1 = int(end[:4])
            for year in range(y0, y1 + 1):
                feats = stac_search(src["collections"], geometry,
                                    f"{year}-01-01", f"{year}-12-31",
                                    max_cloud, src["has_cloud"])
                rows = rank_best(to_rows(feats, src["gsd"], src["has_cloud"]))
                if rows:
                    best = dict(rows[0]); best["label"] = str(year)
                    out.append(best)
            return jsonify({"scenes": out})
        feats = stac_search(src["collections"], geometry, start, end,
                            max_cloud, src["has_cloud"])
        rows = rank_best(to_rows(feats, src["gsd"], src["has_cloud"]))
        return jsonify({"scenes": rows, "count": len(rows)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dem", methods=["POST"])
def api_dem():
    body = request.get_json(force=True)
    geometry = body.get("geometry")
    if not geometry:
        return jsonify({"error": "No area of interest. Upload a shapefile first."}), 400
    dem = DEM_SOURCES.get(body.get("dem", "usgs-1m"))
    if not dem:
        return jsonify({"error": "Unknown DEM source"}), 400
    try:
        if dem["provider"] == "tnm":
            out = tnm_dem(geometry, dem["dataset"])
        else:
            out = planetary_dem(geometry, dem["collections"])
        return jsonify({"tiles": out, "count": len(out)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


def tnm_dem(geometry, dataset):
    minx, miny, maxx, maxy = shape(geometry).bounds
    params = {
        "datasets": dataset,
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "outputFormat": "JSON",
        "max": 50,
    }
    r = requests.get(TNM, params=params, timeout=90)
    r.raise_for_status()
    out = []
    for i in r.json().get("items", []):
        out.append({
            "date": (i.get("publicationDate") or "?")[:10],
            "scene_id": i.get("title", ""),
            "preview": i.get("previewGraphicURL"),
            "download": i.get("downloadURL"),
            "gsd": None,
            "size_mb": round(i.get("sizeInBytes", 0) / 1e6, 1) if i.get("sizeInBytes") else None,
        })
    return out


def planetary_dem(geometry, collections):
    feats = stac_search(collections, geometry, has_cloud=False, limit=20)
    out = []
    for f in feats:
        a = f["assets"]
        out.append({
            "date": (f["properties"].get("datetime") or "?")[:10],
            "scene_id": f["id"],
            "preview": a.get("rendered_preview", {}).get("href"),
            "download": (a.get("data") or a.get("elevation") or {}).get("href"),
            "gsd": f["properties"].get("gsd"),
        })
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()
    app.run(host=args.host, port=args.port, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
