#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_terrain.py — Descarga y procesa relieve REAL (DEM) para el simulador.

Fuente: tiles "Terrarium" (AWS Open Data, derivados de SRTM/otros DEM),
públicos, sin API key, sin límite de uso conocido para uso razonable:
  https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png

Cada píxel de un tile Terrarium codifica una altura real (metros) en sus
canales RGB:  altura = (R*256 + G + B/256) - 32768

Este script, para cada aeropuerto de LOCATIONS:
  1. Calcula qué tiles hacen falta para cubrir un cuadrado de
     ~2*radius_km de lado centrado en el campo.
  2. Los descarga.
  3. Decodifica la altura real de cada píxel y los cose en una sola
     rejilla.
  4. La recorta exactamente al cuadrado deseado y la reescala a
     OUT_SIZE x OUT_SIZE.
  5. La guarda como PNG de 16 bits (un canal, escala de grises) —
     directamente cargable en un <canvas>/<img> del navegador.
  6. Escribe un .json hermano con metadatos: bbox real, metros/píxel,
     elevación del campo, etc. — todo lo que la app necesita para
     mapear coordenadas del simulador a esta textura.

Requisitos:  pip install requests pillow numpy --break-system-packages
Uso:         python3 fetch_terrain.py
Salida:      ./terrain_out/<icao>.png  y  ./terrain_out/<icao>.json
"""

import io
import json
import math
import os
import time
import numpy as np
import requests
from PIL import Image

OUT_DIR = "terrain_out"
TILE = 256
ZOOM = 11          # ~30-40 m/píxel de origen a estas latitudes; suficiente detalle de relieve
OUT_SIZE = 1024     # tamaño final del heightmap exportado (cuadrado)
TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

# Aeródromos reales (lat, lon en grados; elevación de campo en metros;
# radio en km = mitad del lado del cuadrado de terreno a generar).
LOCATIONS = [
    {"icao": "LEAS", "name": "Asturias (Ranón)",      "lat": 43.5636, "lon": -6.0347, "elev_m": 127, "radius_km": 45},
    {"icao": "LELN", "name": "León",                   "lat": 42.5890, "lon": -5.6556, "elev_m": 916, "radius_km": 60},
    {"icao": "LEXJ", "name": "Santander (Seve Ballesteros)", "lat": 43.4269, "lon": -3.8200, "elev_m": 5,   "radius_km": 45},
    {"icao": "LEBB", "name": "Bilbao (Sondica)",        "lat": 43.3011, "lon": -2.9106, "elev_m": 42,  "radius_km": 45},
    {"icao": "LEST", "name": "Santiago de Compostela",  "lat": 42.8963, "lon": -8.4151, "elev_m": 370, "radius_km": 45},
]


def lonlat_to_tile(lon, lat, z):
    lat_rad = math.radians(lat)
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def km_to_deg(lat_deg, dx_km, dy_km):
    """Aproximación local (válida para radios de decenas de km)."""
    dlat = dy_km / 111.32
    dlon = dx_km / (111.32 * math.cos(math.radians(lat_deg)))
    return dlon, dlat


def fetch_tile(z, x, y, session, retries=3):
    url = TERRARIUM_URL.format(z=z, x=x, y=y)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                return np.array(img, dtype=np.float64)
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    # Tile no disponible (mar abierto sin datos, borde, etc.): devuelve ceros.
    return np.zeros((TILE, TILE, 3), dtype=np.float64)


def decode_elevation(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (r * 256 + g + b / 256.0) - 32768.0


def build_region(lat, lon, radius_km, z, session):
    dlon, dlat = km_to_deg(lat, radius_km, radius_km)
    lon_min, lon_max = lon - dlon, lon + dlon
    lat_min, lat_max = lat - dlat, lat + dlat

    xA, yA = lonlat_to_tile(lon_min, lat_max, z)  # esquina NO
    xB, yB = lonlat_to_tile(lon_max, lat_min, z)  # esquina SE

    txA, txB = int(math.floor(xA)), int(math.floor(xB))
    tyA, tyB = int(math.floor(yA)), int(math.floor(yB))
    txA, txB = min(txA, txB), max(txA, txB)
    tyA, tyB = min(tyA, tyB), max(tyA, tyB)

    cols = txB - txA + 1
    rows = tyB - tyA + 1
    grid = np.zeros((rows * TILE, cols * TILE), dtype=np.float64)

    print(f"  Descargando {cols}x{rows} tiles (zoom {z})...")
    for j in range(rows):
        for i in range(cols):
            tx, ty = txA + i, tyA + j
            rgb = fetch_tile(z, tx, ty, session)
            grid[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE] = decode_elevation(rgb)

    # Recorte exacto al bbox deseado dentro de la rejilla de tiles descargada.
    px0, py0 = lonlat_to_tile(lon_min, lat_max, z)
    px1, py1 = lonlat_to_tile(lon_max, lat_min, z)
    col0 = (px0 - txA) * TILE
    col1 = (px1 - txA) * TILE
    row0 = (py0 - tyA) * TILE
    row1 = (py1 - tyA) * TILE
    crop = grid[int(row0):int(row1), int(col0):int(col1)]
    return crop, (lon_min, lat_min, lon_max, lat_max)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    session = requests.Session()

    for loc in LOCATIONS:
        print(f"[{loc['icao']}] {loc['name']}")
        crop, bbox = build_region(loc["lat"], loc["lon"], loc["radius_km"], ZOOM, session)

        # Reescala a OUT_SIZE x OUT_SIZE (float, conserva precisión de altura).
        img_f = Image.fromarray(crop.astype(np.float32), mode="F")
        img_f = img_f.resize((OUT_SIZE, OUT_SIZE), Image.BILINEAR)
        heights = np.array(img_f, dtype=np.float64)

        hmin, hmax = float(heights.min()), float(heights.max())
        # Normaliza a 16 bits sobre el rango real de ESTA región (mejor precisión
        # que un rango global fijo); el rango real se guarda en el JSON.
        span = max(1.0, hmax - hmin)
        norm16 = np.clip((heights - hmin) / span * 65535.0, 0, 65535).astype(np.uint16)

        png_path = os.path.join(OUT_DIR, f"{loc['icao']}.png")
        Image.fromarray(norm16, mode="I;16").save(png_path)

        side_km = loc["radius_km"] * 2
        meta = {
            "icao": loc["icao"],
            "name": loc["name"],
            "field_lat": loc["lat"],
            "field_lon": loc["lon"],
            "field_elev_m": loc["elev_m"],
            "bbox_lon_lat": bbox,     # [lon_min, lat_min, lon_max, lat_max]
            "side_km": side_km,
            "meters_per_pixel": (side_km * 1000.0) / OUT_SIZE,
            "height_min_m": hmin,
            "height_max_m": hmax,
            "png_encoding": "uint16 gray, height = height_min_m + (pixel/65535)*(height_max_m-height_min_m)",
            "png_size": OUT_SIZE
        }
        json_path = os.path.join(OUT_DIR, f"{loc['icao']}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        print(f"  -> {png_path}  ({os.path.getsize(png_path)//1024} KB)   rango real: {hmin:.0f}m a {hmax:.0f}m")

    print("\nListo. Copia la carpeta 'terrain_out' junto al HTML y avísame para conectarla al simulador.")


if __name__ == "__main__":
    main()
