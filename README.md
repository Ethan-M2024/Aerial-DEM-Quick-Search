# SatSearch

Browse the **clearest, highest-resolution satellite imagery and elevation (DEM)
data for any area of interest** — right in your browser. No login, no API keys,
no signup. All data comes from the public **Microsoft Planetary Computer**.

Upload a shapefile of your area, pick a source and date range, and get the best
(least cloudy, sharpest) scenes rendered inline. Grab elevation data too.

## What you get

**Imagery** (highest resolution first)
| Source | Resolution | Coverage | Years |
|--------|-----------|----------|-------|
| NAIP aerial | ~0.6 m | US only | ~2010+ |
| Sentinel-2 | 10 m | global | 2015+ |
| Landsat | 30 m | global | 1972+ |

**Elevation (DEM)** — highest resolution first
| Source | Resolution | Coverage |
|--------|-----------|----------|
| USGS 3DEP (via The National Map) | **1 m** | US |
| USGS 3DEP | 3 m | US |
| USGS 3DEP | 10 m | US |
| Copernicus DEM | 30 m | global |
| NASADEM | 30 m | global |

US high-res DEM (1/3/10 m) comes straight from the public **USGS National Map**
API as downloadable GeoTIFF tiles. Global fallback from Planetary Computer.

> Note: no satellite imagery exists before 1972. "Best per year" starts at each
> source's first year.

## Run it (Anaconda Prompt)

Clone, then three commands:

```
conda env create -f environment.yml
conda activate satsearch
python app.py
```

Then open **http://127.0.0.1:5000**

Prefer plain pip? `pip install -r requirements.txt && python app.py`
(geopandas installs more cleanly via conda).

## How to use

1. **Load area** — upload a **zipped shapefile** (`.shp` + `.shx` + `.dbf` +
   `.prj`, all zipped into one `.zip`) or a `.geojson` file.
2. Pick an **imagery source**, **date range**, and **max cloud %**.
3. **Find best** — all matching scenes, sorted clearest-first.
   **Best per year** — one top scene for each year in the range.
4. **Get elevation** — DEM for your area.
5. **Download** — every result has a ⬇ link:
   - Imagery + global DEM (Sentinel/Landsat/NAIP/Copernicus/NASADEM):
     **GeoTIFF clipped to your shapefile**.
   - USGS 1/3/10 m DEM: the original full lidar tiles (GeoTIFF).

Previews are **clipped to your uploaded shape** — everything outside the polygon
is masked out. Click a preview to open it full size.
