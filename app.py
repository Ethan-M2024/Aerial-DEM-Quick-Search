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
import math
import tempfile
import uuid
import zipfile
from io import BytesIO
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import geopandas as gpd
import numpy as np
import rasterio
import requests
from flask import Flask, Response, jsonify, render_template, request
from PIL import Image, ImageChops, ImageDraw
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry import mapping, shape

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
# Planetary Computer dynamic tiler — crops/masks a single item to a GeoJSON.
CROP = "https://planetarycomputer.microsoft.com/api/data/v1/item/crop"
# Mosaic tiler — merges many tiles (e.g. NAIP) into one seamless layer.
MOSAIC_REGISTER = "https://planetarycomputer.microsoft.com/api/data/v1/mosaic/register"
MOSAIC_TILES = "https://planetarycomputer.microsoft.com/api/data/v1/mosaic"
TILE = 256
EARTH_R = 6378137.0
# NAIP render: natural-colour RGB from the 4-band "image" asset.
NAIP_ASSETS = "assets=image&asset_bidx=image%7C1%2C2%2C3"

# Uploaded areas of interest, kept in memory for this run: id -> GeoJSON geometry.
AOI_STORE = {}

# Imagery sources, ordered highest-resolution first.
# gsd = ground sample distance (meters/pixel). Lower = sharper.
SOURCES = {
    "naip": {
        "label": "NAIP aerial (US only, ~0.6 m)",
        "collections": ["naip"],
        "gsd": 0.6, "has_cloud": False, "since": "2010",
        "mosaic": True,  # many small tiles per area — don't apply coverage filter
    },
    "sentinel-2": {
        "label": "Sentinel-2 (10 m, 2015-2026) — best 10 m",
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


def render_params(feature):
    """Pull the visualization query (assets, rescale, colormap...) that
    Planetary Computer uses for this item's preview, minus the format flag."""
    href = feature["assets"].get("rendered_preview", {}).get("href")
    if not href:
        return None
    pairs = [(k, v) for k, v in parse_qsl(urlsplit(href).query) if k != "format"]
    return urlencode(pairs)


def proxy_urls(aoi_id, render, name):
    """Server-side crop endpoints: PNG for preview, GeoTIFF for download,
    both masked to the uploaded shape."""
    r = quote(render, safe="")
    preview = f"/proxy?aoi={aoi_id}&r={r}&fmt=png"
    download = f"/proxy?aoi={aoi_id}&r={r}&fmt=tif&name={quote(name)}.tif"
    return preview, download


# ---- Mosaic stitching (for tiled sources like NAIP) ---------------------
# NAIP arrives as thousands of small tiles. To show a whole area we register a
# mosaic, fetch the web-map tiles that cover the area, stitch them into one
# image, crop and mask to the uploaded shape, and (for download) georeference.

def _lonlat_to_px(lon, lat, z):
    """Global Web-Mercator pixel coords at zoom z."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n * TILE
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n * TILE
    return x, y


def _choose_zoom(bounds, max_px=1536, zmax=18):
    minx, miny, maxx, maxy = bounds
    for z in range(zmax, 7, -1):
        x0, y0 = _lonlat_to_px(minx, maxy, z)
        x1, y1 = _lonlat_to_px(maxx, miny, z)
        if max(abs(x1 - x0), abs(y1 - y0)) <= max_px:
            return z
    return 8


def register_mosaic(collection, start, end):
    body = {"filter-lang": "cql2-json", "filter": {"op": "and", "args": [
        {"op": "=", "args": [{"property": "collection"}, collection]},
        {"op": "anyinteracts", "args": [{"property": "datetime"},
         {"interval": [f"{start}T00:00:00Z", f"{end}T23:59:59Z"]}]},
    ]}}
    r = requests.post(MOSAIC_REGISTER, json=body, timeout=60)
    r.raise_for_status()
    return r.json()["searchid"]


def stitch_mosaic(searchid, collection, geometry, assets_qs, max_px=1536):
    """Return (masked RGBA PIL image, rasterio Affine in EPSG:3857)."""
    geom = shape(geometry)
    minx, miny, maxx, maxy = geom.bounds
    z = _choose_zoom((minx, miny, maxx, maxy), max_px)
    px0, py0 = _lonlat_to_px(minx, maxy, z)   # top-left corner (global px)
    px1, py1 = _lonlat_to_px(maxx, miny, z)   # bottom-right corner
    tx0, ty0 = int(px0 // TILE), int(py0 // TILE)
    tx1, ty1 = int(px1 // TILE), int(py1 // TILE)
    canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE))
    base = f"{MOSAIC_TILES}/{searchid}/tiles/WebMercatorQuad"
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            u = f"{base}/{z}/{tx}/{ty}?collection={collection}&{assets_qs}"
            rr = requests.get(u, timeout=60)
            if rr.status_code != 200:
                continue
            try:
                tile = Image.open(BytesIO(rr.content)).convert("RGBA")
            except Exception:
                continue
            canvas.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))
    crop = canvas.crop((int(px0 - tx0 * TILE), int(py0 - ty0 * TILE),
                        int(px1 - tx0 * TILE), int(py1 - ty0 * TILE)))
    # Mask everything outside the polygon.
    mask = Image.new("L", crop.size, 0)
    draw = ImageDraw.Draw(mask)

    def ring_px(coords):
        return [(_lonlat_to_px(lon, lat, z)[0] - px0,
                 _lonlat_to_px(lon, lat, z)[1] - py0) for lon, lat in coords]

    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        draw.polygon(ring_px(poly.exterior.coords), fill=255)
        for interior in poly.interiors:
            draw.polygon(ring_px(interior.coords), fill=0)
    crop.putalpha(ImageChops.darker(crop.getchannel("A"), mask))
    # Georeference in Web Mercator metres.
    res = (2 * math.pi * EARTH_R) / (TILE * 2 ** z)
    transform = Affine(res, 0, px0 * res - math.pi * EARTH_R,
                       0, -res, math.pi * EARTH_R - py0 * res)
    return crop, transform


def geotiff_bytes(img, transform):
    arr = np.array(img)
    h, w = arr.shape[:2]
    count = arr.shape[2]
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=h, width=w, count=count,
                      dtype="uint8", crs="EPSG:3857", transform=transform,
                      compress="deflate") as dst:
            for i in range(count):
                dst.write(arr[:, :, i], i + 1)
        return mem.read()


def naip_rows(geometry, aoi_id, start, end):
    """One merged, shape-clipped NAIP mosaic per acquisition year."""
    feats = stac_search(["naip"], geometry, start, end, has_cloud=False, limit=250)
    years = sorted({(f["properties"].get("datetime") or "")[:4]
                    for f in feats if f["properties"].get("datetime")}, reverse=True)
    rows = []
    for y in years:
        base = f"aoi={aoi_id}&collection=naip&year={y}&{NAIP_ASSETS}"
        rows.append({
            "date": y, "label": y, "satellite": "NAIP mosaic",
            "cloud": None, "gsd": 0.6, "scene_id": f"naip-{y}",
            "preview": f"/mosaic/preview?{base}",
            "download": f"/mosaic/download?{base}&name=naip_{y}.tif",
        })
    return rows


# Landsat 7's scan-line corrector failed 2003-05-31; scenes after that date
# have the diagonal black stripes. We drop them so imagery stays clean.
SLC_OFF_DATE = "2003-05-31"


def aoi_coverage(feature, aoi_shape, aoi_area):
    """Fraction of the AOI that this scene's footprint actually covers.
    Cheap ratio in lon/lat degrees — good enough to reject swath-edge scenes."""
    geom = feature.get("geometry")
    if not geom or aoi_area <= 0:
        return 1.0
    try:
        return shape(geom).intersection(aoi_shape).area / aoi_area
    except Exception:
        return 1.0


def to_rows(features, source_gsd, has_cloud, aoi_id, min_coverage=0.98,
            drop_slc_off=True):
    aoi_shape = shape(AOI_STORE[aoi_id])
    aoi_area = aoi_shape.area
    seen, rows = set(), []
    for f in features:
        fid = f["id"]
        if fid in seen:
            continue
        seen.add(fid)
        p = f["properties"]
        platform = p.get("platform") or p.get("constellation") or ""
        date = (p.get("datetime") or "?")[:10]
        # Quality gate 1: skip Landsat 7 SLC-off (black stripes).
        if drop_slc_off and platform == "landsat-7" and date > SLC_OFF_DATE:
            continue
        # Quality gate 2: skip scenes that barely overlap the area (nodata edges).
        if aoi_coverage(f, aoi_shape, aoi_area) < min_coverage:
            continue
        render = render_params(f)
        if not render:
            continue
        preview, download = proxy_urls(aoi_id, render, fid)
        cloud = p.get("eo:cloud_cover") if has_cloud else None
        rows.append({
            "date": date,
            "satellite": platform,
            "cloud": round(float(cloud), 1) if cloud is not None else None,
            "gsd": p.get("gsd", source_gsd),
            "scene_id": fid,
            "preview": preview,
            "download": download,
        })
    return rows


def build_rows(feats, src, aoi_id):
    """Rows with quality filtering, but never let the filter alone return
    nothing — if it removes everything, fall back to the unfiltered scenes."""
    if src.get("mosaic"):
        return to_rows(feats, src["gsd"], src["has_cloud"], aoi_id, min_coverage=0.0)
    rows = to_rows(feats, src["gsd"], src["has_cloud"], aoi_id, min_coverage=0.6)
    if not rows:
        rows = to_rows(feats, src["gsd"], src["has_cloud"], aoi_id, min_coverage=0.0)
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
        aoi_id = uuid.uuid4().hex
        AOI_STORE[aoi_id] = geom
        b = shape(geom).bounds
        return jsonify({"aoi_id": aoi_id, "bbox": list(b)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def get_aoi(body):
    geom = AOI_STORE.get(body.get("aoi_id"))
    if geom is None:
        raise KeyError("Area of interest not found — upload a shapefile first.")
    return geom


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json(force=True)
    try:
        geometry = get_aoi(body)
    except KeyError as e:
        return jsonify({"error": str(e)}), 400
    aoi_id = body["aoi_id"]
    src_key = body.get("source", "sentinel-2")
    src = SOURCES.get(src_key)
    if not src:
        return jsonify({"error": f"Unknown source {src_key}"}), 400
    start = body.get("start", "1950-01-01")
    end = body.get("end", "2026-12-31")
    max_cloud = float(body.get("max_cloud", 10))
    best_per_year = bool(body.get("best_per_year", False))
    try:
        if src.get("mosaic"):  # NAIP: merge tiles into one image per year
            rows = naip_rows(geometry, aoi_id, start, end)
            return jsonify({"scenes": rows, "count": len(rows)})
        if best_per_year:
            out = []
            y0 = max(int(start[:4]), int(src["since"]))
            y1 = int(end[:4])
            for year in range(y0, y1 + 1):
                feats = stac_search(src["collections"], geometry,
                                    f"{year}-01-01", f"{year}-12-31",
                                    max_cloud, src["has_cloud"])
                rows = rank_best(build_rows(feats, src, aoi_id))
                if rows:
                    best = dict(rows[0]); best["label"] = str(year)
                    out.append(best)
            return jsonify({"scenes": out})
        feats = stac_search(src["collections"], geometry, start, end,
                            max_cloud, src["has_cloud"])
        rows = rank_best(build_rows(feats, src, aoi_id))
        return jsonify({"scenes": rows, "count": len(rows)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dem", methods=["POST"])
def api_dem():
    body = request.get_json(force=True)
    try:
        geometry = get_aoi(body)
    except KeyError as e:
        return jsonify({"error": str(e)}), 400
    dem = DEM_SOURCES.get(body.get("dem", "usgs-1m"))
    if not dem:
        return jsonify({"error": "Unknown DEM source"}), 400
    try:
        if dem["provider"] == "tnm":
            out = tnm_dem(geometry, dem["dataset"])
        else:
            out = planetary_dem(geometry, dem["collections"], body["aoi_id"])
        return jsonify({"tiles": out, "count": len(out)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


def _mosaic_image(args):
    aoi_id = args.get("aoi", "")
    geom = AOI_STORE.get(aoi_id)
    if geom is None:
        return None, None
    collection = args.get("collection", "naip")
    year = args.get("year", "2022")
    assets = f"assets={args.get('assets', 'image')}"
    if args.get("asset_bidx"):
        assets += f"&asset_bidx={quote(args['asset_bidx'])}"
    sid = register_mosaic(collection, f"{year}-01-01", f"{year}-12-31")
    return stitch_mosaic(sid, collection, geom, assets)


@app.route("/mosaic/preview")
def mosaic_preview():
    img, _ = _mosaic_image(request.args)
    if img is None:
        return "Area of interest expired — re-upload your shapefile.", 404
    buf = BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), content_type="image/png")


@app.route("/mosaic/download")
def mosaic_download():
    img, transform = _mosaic_image(request.args)
    if img is None:
        return "Area of interest expired — re-upload your shapefile.", 404
    data = geotiff_bytes(img, transform)
    fn = request.args.get("name", "mosaic.tif")
    return Response(data, content_type="image/tiff",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.route("/proxy")
def proxy():
    """Fetch a shape-clipped PNG (preview) or GeoTIFF (download) from the
    Planetary Computer tiler and stream it back to the browser."""
    aoi_id = request.args.get("aoi", "")
    geom = AOI_STORE.get(aoi_id)
    if geom is None:
        return "Area of interest expired — re-upload your shapefile.", 404
    render = unquote(request.args.get("r", ""))
    fmt = request.args.get("fmt", "png")
    ext = "tif" if fmt == "tif" else "png"
    max_size = 4096 if fmt == "tif" else 1024
    url = f"{CROP}.{ext}?{render}&max_size={max_size}"
    feature = {"type": "Feature", "properties": {}, "geometry": geom}
    up = requests.post(url, json=feature, timeout=180, stream=True)
    headers = {}
    if fmt == "tif":
        fn = request.args.get("name", "download.tif")
        headers["Content-Disposition"] = f'attachment; filename="{fn}"'
    return Response(up.iter_content(8192), status=up.status_code,
                    content_type=up.headers.get("content-type", "application/octet-stream"),
                    headers=headers)


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


def planetary_dem(geometry, collections, aoi_id):
    feats = stac_search(collections, geometry, has_cloud=False, limit=20)
    out = []
    for f in feats:
        render = render_params(f)
        if not render:
            continue
        preview, download = proxy_urls(aoi_id, render, f["id"])
        out.append({
            "date": (f["properties"].get("datetime") or "?")[:10],
            "scene_id": f["id"],
            "preview": preview,
            "download": download,
            "gsd": f["properties"].get("gsd"),
            "clipped": True,
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
