# Development History — Aerial+DEM Quick Search

This document is a detailed record of how SatSearch was built: every request,
every design decision, every bug and its root cause, and every fix. It exists
so a future contributor (human or AI) can understand *why* the code looks the
way it does without re-deriving it from scratch.

---

## 1. Origin: satellite photo search for Springfield, OR (1950–2026)

**Request:** an app to find satellite photos of Springfield, OR from
1950–2026 with under 10% cloud cover.

**Key fact surfaced immediately:** no satellite has ever imaged anything
before 1972 (Landsat 1). A 1950 request is unsatisfiable by definition —
pre-1972 imagery would have to come from USGS aerial photography, a
different, non-satellite archive.

**Decisions made via clarifying questions:**
- Data source: **Google Earth Engine** (chosen over USGS EarthExplorer,
  Sentinel Hub, and commercial Planet imagery)
- Output: searchable list with previews (not raw downloads, not a full map
  viewer)
- Interface: Python CLI

**What was built:** `satsearch.py`, a CLI that queried 10 Landsat
collections (MSS through OLI, Landsat 1 through 9) via `earthengine-api`,
filtered by cloud cover and date, and printed a table with thumbnail URLs.
Verified working: 444 scenes found from 1972-07-28 to 2026-06-13.

This CLI was later deleted once the project pivoted to Earth Engine-free
architecture (see §3).

---

## 2. CLI → web app, then a "year timeline" view

**Request 1:** turn the CLI into a web app so photos can be viewed inline
without downloading.

**Built:** `app.py` (Flask) + `templates/index.html`. Same Earth Engine
backend, now serving thumbnails in a browser grid instead of printing URLs
to a terminal.

**Request 2:** show exactly one image per target year — 1970, 1980, 1990,
2000, 2010, 2020, 2024, 2025, 2026.

**Built:** `/api/timeline` endpoint. Per year, pull all scenes under the
cloud threshold and keep the one with the lowest cloud %. 1970 has no
satellite coverage, so it falls back to the earliest possible year, 1972,
with a UI note explaining why. Verified: all 9 years returned clean scenes
(0.0–0.1% cloud each).

---

## 3. The Earth Engine login wall — full pivot to a no-login backend

**Problem raised by the user:** "why can't it be free without signing up?"
Google Earth Engine requires a Google account, a registered Cloud project,
and browser-based OAuth (`earthengine authenticate`) before any query can
run. That's a hard requirement of the platform — there was no way to keep
Earth Engine and remove the login.

**Fix:** dropped Earth Engine entirely. Replaced it with the **Microsoft
Planetary Computer STAC API** (`https://planetarycomputer.microsoft.com/api/stac/v1/search`),
which is fully public — no key, no account, no OAuth. Verified via a raw
`curl` STAC search before writing any code, confirming real scenes with
cloud-cover metadata and `rendered_preview` thumbnail assets came back
with zero authentication.

`app.py` was rewritten around this API. This is the architecture the
project has used ever since.

---

## 4. From "one town" to "any shape": shapefile upload, multi-resolution
   imagery, and DEM data

**Request:** let the user upload a shapefile of any area, search across the
full 1950–2026 range for the clearest and highest-resolution imagery, and
add DEM (elevation) data too — packaged so it can be cloned from GitHub and
run with one command in an Anaconda prompt.

**Data sources evaluated and confirmed live (via direct `curl` tests)
before building:**

| Purpose | Source | Resolution | Coverage |
|---|---|---|---|
| Imagery | NAIP (Planetary Computer) | ~0.6 m | US, ~2010+ |
| Imagery | Sentinel-2 L2A (Planetary Computer) | 10 m | Global, 2015+ |
| Imagery | Landsat C2 L1/L2 (Planetary Computer) | 30 m | Global, 1972+ |
| DEM | Copernicus DEM GLO-30 | 30 m | Global |
| DEM | NASADEM | 30 m | Global |

**Built:**
- `geometry_from_upload()` — reads a zipped shapefile or GeoJSON with
  `geopandas`, reprojects to WGS84, and merges all features into one
  geometry.
- `/api/search` — searches the selected source, sorted by "best" (lowest
  cloud %, then finest resolution). Supports a `best_per_year` mode.
- `/api/dem` — same idea for elevation sources.
- `SOURCES` and `DEM_SOURCES` dicts drive both the backend logic and the
  dropdown menus in the UI — one source of truth.
- Packaging: `environment.yml` (conda) + `requirements.txt` (pip) so the
  entire app runs with `conda env create -f environment.yml && conda
  activate satsearch && python app.py`.

---

## 5. "They have 1 m DEM on USGS" — adding real high-resolution elevation

**Problem:** the initial DEM sources (Copernicus, NASADEM) topped out at
30 m. The user correctly pointed out USGS 3DEP lidar exists at 1 m
resolution.

**Investigation:** tried Planetary Computer's `3dep-lidar-dtm` /
`-dsm` / `-hag` collections first — these turned out to have very sparse,
patchy US coverage (confirmed empty over Springfield, OR; only a few
scattered project areas existed). Pivoted to querying **USGS The National
Map (TNM) API** directly
(`https://tnmaccess.nationalmap.gov/api/v1/products`), a separate public,
no-login USGS service. This returned real 1 m GeoTIFF tiles over
Springfield immediately.

**Built:** `DEM_SOURCES` gained a `provider` field distinguishing:
- `"tnm"` — USGS 1 m / 3 m / 10 m, fetched via `tnm_dem()`, returning direct
  S3 download links and file sizes from USGS.
- `"planetary"` — Copernicus/NASADEM global fallback via
  `planetary_dem()`.

Verified: 1 m tiles found over Springfield (`OR_McKenzieRiver_2021_B21`
project), ~100–240 MB each, downloadable.

---

## 6. Pushing to GitHub

**Request:** "can we add this to my github page?" and later, name the repo
**Aerial+DEM Quick Search**.

Since GitHub repo names can't contain spaces or `+`, the repo was created
as `Aerial-DEM-Quick-Search` (confirmed public, per the user's choice) at:

**https://github.com/Ethan-M2024/Aerial-DEM-Quick-Search**

Initialized git, committed all files, created the repo with `gh repo
create`, and pushed. All subsequent work in this document was committed
and pushed incrementally to this same repo.

---

## 7. Cropping previews to the exact shapefile + adding real downloads

**Request:** crop images to the uploaded shape (not just a bounding
box/buffer), and add the ability to download images and DEMs.

**Investigation:** tested Planetary Computer's dynamic tiler `crop`
endpoint directly — `POST /api/data/v1/item/{crop.png|crop.tif}` accepting
a GeoJSON Feature body. Confirmed it returns a polygon-masked PNG for
preview and a polygon-masked GeoTIFF for download, per scene.

**Built:**
- `AOI_STORE` — uploaded shapes are now kept server-side in memory keyed by
  a generated `aoi_id`, instead of being round-tripped through the client
  on every request (smaller payloads, and the shape can be reused by the
  proxy).
- `render_params()` — extracts the visualization parameters (band
  selection, rescale, etc.) Planetary Computer already computed for each
  item's `rendered_preview`, so the crop endpoint renders identically.
- `/proxy` route — a server-side pass-through to the crop endpoint. Given
  an `aoi_id` and the render params, it returns a **PNG clipped to the
  polygon** for on-screen preview, or a **GeoTIFF clipped to the polygon**
  as a downloadable attachment (`Content-Disposition: attachment`).
- Every imagery and global-DEM row now carries both a `preview` and a
  `download` URL built this way.
- USGS TNM DEM (1/3/10 m) downloads remain the original full lidar tiles
  straight from USGS S3, since those aren't served through Planetary
  Computer's tiler.

Verified end-to-end: uploaded a real shapefile, confirmed the returned PNG
was clipped to the polygon shape (not a rectangle), and confirmed the
GeoTIFF download had the correct `Content-Disposition` header and
`image/tiff` content type.

---

## 8. Image quality pass: black bars and blank scenes

**Problem:** the user reported black bars across Landsat imagery, and asked
to guarantee true 10 m resolution from 2000–2026 with better overall data
quality.

**Root causes identified:**
1. **Landsat 7 SLC-off.** Landsat 7's Scan Line Corrector failed on
   2003-05-31. Every Landsat 7 scene acquired after that date has
   permanent diagonal black gaps. This is a real, unfixable hardware defect
   in the source data, not a bug in this app — but the app was blindly
   returning those scenes.
2. **Swath-edge / partial-coverage scenes.** Some "best" (lowest-cloud)
   scenes only barely overlapped the requested area, so most of the
   returned image was blank nodata.
3. **The 10 m ceiling is real, not a bug.** True 10 m optical imagery only
   exists from **Sentinel-2's launch in mid-2015 onward**. There is no free
   10 m source for 2000–2015 — the best available for that window is
   Landsat at 30 m. This was clarified in the UI and README rather than
   "fixed," since it isn't fixable with free data.

**Built:**
- `SLC_OFF_DATE = "2003-05-31"` — any `landsat-7` scene dated after this is
  dropped in `to_rows()`. This naturally shifts 2003–2013 coverage to
  Landsat 5, and 2013+ to Landsat 8/9.
- `aoi_coverage()` — computes what fraction of the AOI's area a scene's
  footprint actually intersects, using `shapely`. Scenes below a coverage
  threshold are dropped as swath-edge junk.
- UI copy and README updated to state plainly that true 10 m starts in
  2015, and that SLC-off/swath-edge scenes are filtered automatically.

Verified: searched 2004–2012 Landsat and confirmed **zero** `landsat-7`
scenes remained (all `landsat-5`); fetched a "best" Sentinel-2 scene and
visually confirmed a full, clean, unstriped image of Springfield.

---

## 9. Filtering caused 0 results for NAIP and large areas

**Problem:** immediately after the quality filters shipped, NAIP (and any
large-area search) started returning **0 scenes**.

**Root cause:** NAIP is delivered as thousands of small individual tiles.
The new `aoi_coverage()` filter required 98% of the AOI to be covered by a
*single* scene's footprint — a bar no individual NAIP tile could ever
clear over an area larger than one tile. The filter was silently deleting
every result.

**Fix:**
- Added a `"mosaic": True` flag to the NAIP entry in `SOURCES`, and skip
  the coverage filter entirely for mosaic-style sources (`build_rows()`).
- Lowered the coverage threshold for non-mosaic sources (Sentinel/Landsat)
  from 98% to 60%, giving more headroom for irregularly-shaped AOIs.
- Added a hard safety net: **filtering can never return zero results on
  its own.** `build_rows()` falls back to the unfiltered scene list if
  filtering would otherwise leave nothing.

Verified: NAIP over a large test area went from **0 → 18** results;
Sentinel-2 and Landsat continued returning results (113 and 52
respectively) on the same large area.

---

## 10. "I only get a small fragment of the image, not the entire thing" — NAIP mosaicking

**Problem:** even with results returning again, NAIP previews showed only
one small tile — a fragment of the requested area, not the whole thing.
This is because NAIP genuinely *is* stored as many small non-overlapping
tiles; there was never a single "whole area" scene to return.

**Fix — built a real tile-stitching mosaic engine:**
- `register_mosaic()` — registers a Planetary Computer **mosaic search**
  (a CQL2 filter matching `collection = naip` within a given year) and gets
  back a `searchid` that can be tiled like a normal XYZ/web-map layer.
- `_lonlat_to_px()` / `_choose_zoom()` — standard Web Mercator tile math to
  pick a zoom level that keeps the stitched canvas under ~1536 px per side.
- `stitch_mosaic()` — fetches every `256×256` XYZ tile covering the AOI's
  bounding box from the mosaic's `tiles/WebMercatorQuad/{z}/{x}/{y}`
  endpoint, pastes them into one canvas, crops to the exact bounds, then
  **masks everything outside the uploaded polygon** using `PIL.ImageDraw`
  (draws the polygon as a filled mask, applies it as the alpha channel).
- `geotiff_bytes()` — for downloads, converts the stitched/masked image
  into a real **georeferenced GeoTIFF** (EPSG:3857) using `rasterio`, with
  a correctly computed `Affine` transform so the file opens correctly in
  GIS software.
- `naip_rows()` — replaces the old per-tile NAIP search with one row per
  *acquisition year*, pointing at the new `/mosaic/preview` and
  `/mosaic/download` routes instead of a single tile's URL.
- New dependencies: `rasterio`, `pillow`, `numpy` (added to both
  `environment.yml` and `requirements.txt`).

Verified: previously a 256×256 fragment; after the fix, a full 874×972
seamless merged image covering the entire requested area, visually
confirmed to show all of Springfield/Eugene with no tile seams or gaps.
GeoTIFF download validated with `rasterio` — correct CRS (EPSG:3857), 4
bands, correct bounds.

---

## 11. Crash on 3D (Z-enabled) shapefiles: "too many values to unpack"

**Problem:** NAIP search failed outright with `ValueError: too many values
to unpack (expected 2)`.

**Root cause:** the polygon-masking code in `stitch_mosaic()` (added in
§10) iterated over polygon ring coordinates like this:

```python
for lon, lat in coords
```

This assumes every coordinate is a 2-tuple `(lon, lat)`. But shapefiles
exported with a Z dimension (elevation) — a common default in ArcGIS —
produce 3-tuples `(lon, lat, z)`. Unpacking a 3-tuple into two names is
exactly what raises "too many values to unpack." This was invisible until
a real-world 3D shapefile was uploaded, since the earlier test shapefiles
built during development were all 2D.

**Reproduction:** built a synthetic 3D polygon (`has_z: True`) with
`shapely`/`geopandas`, zipped it, uploaded it through the running app, and
reproduced the exact traceback and error message the user reported.

**Fix — two layers of defense:**
1. **At the source:** `geometry_from_upload()` now calls
   `shapely.force_2d(merged)` immediately after merging the uploaded
   geometry, so nothing downstream ever sees a Z coordinate again.
2. **Defense in depth:** `ring_px()` inside `stitch_mosaic()` was changed
   from unpacking (`for lon, lat in coords`) to indexing
   (`pt[0], pt[1]` for `pt in coords`), so even if a 3D geometry reached
   this code by some other path, it would silently ignore the Z value
   instead of crashing.

Verified: re-ran the exact synthetic 3D shapefile that reproduced the bug
— now returns `200 OK` with a valid full-size image. Re-ran the standard
2D regression test immediately after to confirm no behavior changed for
normal shapefiles.

---

## Current architecture summary

```
Browser (templates/index.html)
   |
   |  upload shapefile/GeoJSON --> POST /api/upload --> geometry_from_upload()
   |                                                     (force_2d, WGS84, stored in AOI_STORE)
   |
   |  search imagery --> POST /api/search --> stac_search() [Planetary Computer STAC]
   |                                          --> build_rows() [SLC-off + coverage filters]
   |                                          --> rank_best() [cloud %, then resolution]
   |                                          --> proxy_urls() [preview/download links]
   |                        (NAIP: naip_rows() --> mosaic engine instead)
   |
   |  search DEM --> POST /api/dem --> tnm_dem() [USGS 1/3/10 m, direct tiles]
   |                                or planetary_dem() [Copernicus/NASADEM, cropped]
   |
   |  view/download --> GET /proxy [single-scene crop via Planetary Computer tiler]
   |                 --> GET /mosaic/preview, /mosaic/download [stitched NAIP mosaics]
```

**No API keys or logins anywhere in this stack.** Every data source
(Microsoft Planetary Computer, USGS TNM) is public and unauthenticated.

## Known limitations (by data availability, not app bugs)

- No imagery exists before 1972 (first Landsat launch).
- No free 10 m optical imagery exists before mid-2015 (Sentinel-2 launch);
  2000–2015 best available is Landsat at 30 m.
- USGS 1 m lidar DEM coverage is patchy — limited to areas USGS has
  actually flown, not the whole US.
- NAIP is US-only.
