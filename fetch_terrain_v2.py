#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_terrain_v2.py — Descarga imagen satelital, carreteras principales y
núcleos de población/aeródromos REALES para las mismas zonas que ya generó
fetch_terrain.py (mismo centro y radio por aeropuerto, para que todo coincida
exactamente con la elevación).

Fuentes:
  - Imagen: ESRI World Imagery (satélite/aéreo), tiles públicos sin API key.
    https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer
  - Carreteras y núcleos de población: OpenStreetMap vía Overpass API
    (carreteras: motorway/trunk/primary/secondary; núcleos: city/town/village/
    hamlet con nombre, más cualquier aeródromo con nombre) — servidor público
    compartido, se hace una pausa entre consultas por cortesía.

Requisitos:  pip install requests pillow --break-system-packages
Uso:         python3 fetch_terrain_v2.py
Salida:      ./terrain_out/<icao>_sat.jpg    (imagen satelital)
             ./terrain_out/<icao>_roads.json  (carreteras principales, lat/lon)
             ./terrain_out/<icao>_places.json (ciudades/pueblos/aldeas/aeródromos)

Copia los tres junto a los <icao>_rg.png / <icao>.json que ya tenías en terrain/.
"""

import io
import json
import math
import os
import time
import requests
from PIL import Image

OUT_DIR = "terrain_out"
TILE = 256
SAT_ZOOM = 13          # ~20 m/píxel de origen; suficiente para distinguir núcleos urbanos
SAT_OUT_SIZE = 1536
ESRI_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
# Varios espejos públicos de Overpass: si uno falla o rechaza la petición
# (rate-limit, mantenimiento, etc.) se prueba el siguiente automáticamente.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
OVERPASS_HEADERS = {
    # Overpass rechaza (406) peticiones sin un User-Agent identificable.
    "User-Agent": "avion-cabina-terrain-fetch/1.0 (uso personal, hobby, sin fines comerciales)"
}
ROAD_CLASSES = ["motorway", "trunk", "primary", "secondary"]
PLACE_TYPES = ["city", "town", "village", "hamlet"]
MAX_PLACES = 200  # evita saturar la escena en radios grandes (León=60km)

# Mismos centros/radios que fetch_terrain.py — imprescindible que coincidan
# exactamente, o la imagen/carreteras no alinearán con el relieve ya descargado.
LOCATIONS = [
    {"icao": "LEAS", "lat": 43.5636, "lon": -6.0347, "radius_km": 45},
    {"icao": "LELN", "lat": 42.5890, "lon": -5.6556, "radius_km": 60},
    {"icao": "LEXJ", "lat": 43.4269, "lon": -3.8200, "radius_km": 45},
    {"icao": "LEBB", "lat": 43.3011, "lon": -2.9106, "radius_km": 45},
    {"icao": "LEST", "lat": 42.8963, "lon": -8.4151, "radius_km": 45},
]


def lonlat_to_tile(lon, lat, z):
    lat_rad = math.radians(lat)
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def km_to_deg(lat_deg, dx_km, dy_km):
    dlat = dy_km / 111.32
    dlon = dx_km / (111.32 * math.cos(math.radians(lat_deg)))
    return dlon, dlat


def fetch_tile_img(url, session, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=15, headers={"User-Agent": "avion-cabina-terrain-fetch/1.0"})
            if r.status_code == 200:
                return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return Image.new("RGB", (TILE, TILE), (170, 170, 170))  # gris: tile no disponible


def overpass_query(query, session, min_expected=1):
    """Prueba varios espejos de Overpass, con reintentos, antes de rendirse.
    Un resultado con 'elements' vacío se trata como fallo y se reintenta: para
    estas zonas (ciudades reales con carreteras/núcleos de sobra) una lista
    vacía case siempre significa timeout/corte silencioso de Overpass, no que
    de verdad no haya nada que encontrar."""
    last_err = None
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                r = session.post(url, data={"data": query}, headers=OVERPASS_HEADERS, timeout=120)
                r.raise_for_status()
                data = r.json()
                if len(data.get("elements", [])) >= min_expected:
                    return data
                last_err = RuntimeError('respuesta vacía de ' + url + ' (probable timeout interno)')
            except Exception as e:
                last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err


def build_satellite(lat, lon, radius_km, z, session):
    dlon, dlat = km_to_deg(lat, radius_km, radius_km)
    lon_min, lon_max = lon - dlon, lon + dlon
    lat_min, lat_max = lat - dlat, lat + dlat

    xA, yA = lonlat_to_tile(lon_min, lat_max, z)
    xB, yB = lonlat_to_tile(lon_max, lat_min, z)
    txA, txB = sorted([int(math.floor(xA)), int(math.floor(xB))])
    tyA, tyB = sorted([int(math.floor(yA)), int(math.floor(yB))])
    cols, rows = txB - txA + 1, tyB - tyA + 1

    canvas = Image.new("RGB", (cols * TILE, rows * TILE))
    print(f"  Satélite: descargando {cols}x{rows} tiles (zoom {z})...")
    for j in range(rows):
        for i in range(cols):
            tx, ty = txA + i, tyA + j
            url = ESRI_URL.format(z=z, y=ty, x=tx)
            img = fetch_tile_img(url, session)
            canvas.paste(img, (i * TILE, j * TILE))

    px0, py0 = lonlat_to_tile(lon_min, lat_max, z)
    px1, py1 = lonlat_to_tile(lon_max, lat_min, z)
    col0, col1 = (px0 - txA) * TILE, (px1 - txA) * TILE
    row0, row1 = (py0 - tyA) * TILE, (py1 - tyA) * TILE
    crop = canvas.crop((int(col0), int(row0), int(col1), int(row1)))
    return crop.resize((SAT_OUT_SIZE, SAT_OUT_SIZE), Image.LANCZOS)


def fetch_roads(lat, lon, radius_km, session):
    dlon, dlat = km_to_deg(lat, radius_km, radius_km)
    south, north = lat - dlat, lat + dlat
    west, east = lon - dlon, lon + dlon
    cls_re = "|".join(ROAD_CLASSES)
    query = f"""
    [out:json][timeout:90];
    (
      way["highway"~"^({cls_re})$"]({south},{west},{north},{east});
    );
    out geom;
    """
    print("  Carreteras: consultando Overpass (puede tardar un poco)...")
    try:
        data = overpass_query(query, session)
    except Exception as e:
        print(f"  ! Overpass falló ({e}); este sitio quedará sin carreteras.")
        return []

    roads = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        pts = [[nd["lat"], nd["lon"]] for nd in el["geometry"]]
        if len(pts) < 2:
            continue
        # Decima trazados muy densos: no hace falta cada nodo original para una
        # línea de referencia visual (evita ficheros enormes en autopistas largas).
        if len(pts) > 60:
            step = max(1, len(pts) // 60)
            pts = [pts[0]] + pts[1:-1:step] + [pts[-1]]
        roads.append({"cls": el.get("tags", {}).get("highway", "road"), "points": pts})
    return roads


def fetch_places(lat, lon, radius_km, session):
    dlon, dlat = km_to_deg(lat, radius_km, radius_km)
    south, north = lat - dlat, lat + dlat
    west, east = lon - dlon, lon + dlon
    place_re = "|".join(PLACE_TYPES)
    query = f"""
    [out:json][timeout:90];
    (
      node["place"~"^({place_re})$"]["name"]({south},{west},{north},{east});
      node["aeroway"="aerodrome"]["name"]({south},{west},{north},{east});
      way["aeroway"="aerodrome"]["name"]({south},{west},{north},{east});
    );
    out center;
    """
    print("  Núcleos de población y aeródromos: consultando Overpass...")
    try:
        data = overpass_query(query, session)
    except Exception as e:
        print(f"  ! Overpass falló ({e}); este sitio quedará sin núcleos/aeródromos.")
        return []

    rank = {"city": 0, "town": 1, "aerodrome": 1, "village": 2, "hamlet": 3}
    places = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        if tags.get("aeroway") == "aerodrome":
            ptype = "airport"
        else:
            ptype = tags.get("place")
            if ptype not in PLACE_TYPES:
                continue
        if el["type"] == "node":
            lat_, lon_ = el["lat"], el["lon"]
        elif "center" in el:
            lat_, lon_ = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        places.append({"name": name, "type": ptype, "lat": lat_, "lon": lon_})

    places.sort(key=lambda p: rank.get(p["type"], 4))
    return places[:MAX_PLACES]


def _already_has_data(path, key):
    """True si el fichero existe Y tiene contenido real (no un intento fallido
    que dejó una lista vacía) — así un reintento solo repite lo que de verdad
    falló, no lo que ya funcionó."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get(key))
    except Exception:
        return False


def _process_osm_for(loc, session):
    """Descarga roads+places para un sitio si aún faltan. Devuelve True si al
    terminar ambos ficheros tienen datos (éxito), False si alguno sigue vacío."""
    ok = True
    roads_path = os.path.join(OUT_DIR, f"{loc['icao']}_roads.json")
    if _already_has_data(roads_path, "roads"):
        print(f"  -> {roads_path} ya tiene datos, no se repite.")
    else:
        roads = fetch_roads(loc["lat"], loc["lon"], loc["radius_km"], session)
        with open(roads_path, "w", encoding="utf-8") as f:
            json.dump({"roads": roads}, f)
        print(f"  -> {roads_path} ({len(roads)} vias)")
        ok = ok and len(roads) > 0
        time.sleep(2)  # cortesía con el servidor público de Overpass

    places_path = os.path.join(OUT_DIR, f"{loc['icao']}_places.json")
    if _already_has_data(places_path, "places"):
        print(f"  -> {places_path} ya tiene datos, no se repite.")
    else:
        places = fetch_places(loc["lat"], loc["lon"], loc["radius_km"], session)
        with open(places_path, "w", encoding="utf-8") as f:
            json.dump({"places": places}, f, ensure_ascii=False)
        print(f"  -> {places_path} ({len(places)} nucleos/aerodromos)")
        ok = ok and len(places) > 0
        time.sleep(2)  # cortesía con el servidor público de Overpass
    return ok


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    session = requests.Session()
    for loc in LOCATIONS:
        print(f"[{loc['icao']}]")
        sat_path = os.path.join(OUT_DIR, f"{loc['icao']}_sat.jpg")
        if os.path.exists(sat_path):
            print(f"  -> {sat_path} ya existe, no se vuelve a descargar.")
        else:
            sat = build_satellite(loc["lat"], loc["lon"], loc["radius_km"], SAT_ZOOM, session)
            sat.save(sat_path, quality=82, optimize=True)
            print(f"  -> {sat_path} ({os.path.getsize(sat_path)//1024} KB)")

        _process_osm_for(loc, session)

    # Pase(s) de reparación: si algún sitio quedó con carreteras o núcleos
    # vacíos (típico de un timeout puntual del servidor público de Overpass,
    # no de un fallo real de la consulta), se espera más y se reintenta SOLO
    # lo que falta — sin tocar lo que ya funcionó ni repetir el satélite.
    for wait_s in (20, 60):
        pending = [loc for loc in LOCATIONS if not (
            _already_has_data(os.path.join(OUT_DIR, f"{loc['icao']}_roads.json"), "roads") and
            _already_has_data(os.path.join(OUT_DIR, f"{loc['icao']}_places.json"), "places")
        )]
        if not pending:
            break
        print(f"\nReparando {len(pending)} sitio(s) con datos incompletos: "
              f"{', '.join(l['icao'] for l in pending)}. Esperando {wait_s}s antes de reintentar...")
        time.sleep(wait_s)
        for loc in pending:
            print(f"[{loc['icao']}] (reintento)")
            _process_osm_for(loc, session)

    still_pending = [loc["icao"] for loc in LOCATIONS if not (
        _already_has_data(os.path.join(OUT_DIR, f"{loc['icao']}_roads.json"), "roads") and
        _already_has_data(os.path.join(OUT_DIR, f"{loc['icao']}_places.json"), "places")
    )]
    if still_pending:
        print(f"\nAviso: {', '.join(still_pending)} siguen incompletos tras los reintentos "
              f"(Overpass público muy saturado ahora mismo). Vuelve a correr el script más "
              f"tarde: lo ya conseguido no se repite, solo reintenta lo que falta.")
    print("\nListo. Copia terrain_out/*_sat.jpg, *_roads.json y *_places.json junto a lo que ya tenías en terrain/.")


if __name__ == "__main__":
    main()
