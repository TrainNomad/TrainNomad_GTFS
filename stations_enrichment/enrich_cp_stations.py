"""
Enrichissement des gares portugaises (CP) dans stations.csv.

Ce script :
1. Télécharge le GTFS CP et extrait les gares avec leurs codes UIC
2. Compare avec stations.csv (matching par nom, coordonnées)
3. Génère un fichier de mapping CP → UIC
4. Crée un GeoJSON des gares orphelines pour review manuel
5. Propose les modifications à apporter à stations.csv

Usage :
    python stations_enrichment/enrich_cp_stations.py [--apply]

    --apply : Applique les modifications à stations.csv (sinon preview seulement)
"""
import argparse
import csv
import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from math import radians, sin, cos, sqrt, atan2

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")
MAPPINGS_DIR = os.path.join(BASE_DIR, "mappings")
OUTPUT_MAPPING = os.path.join(MAPPINGS_DIR, "cp_uic.csv")
OUTPUT_GEOJSON = os.path.join(BASE_DIR, "cp_orphan_stations.geojson")

CP_GTFS_URL = "https://publico.cp.pt/gtfs/gtfs.zip"


@dataclass
class Station:
    id: str
    name: str
    uic: str
    lat: float
    lon: float
    country: str = ""


def normalize(s: str) -> str:
    """Normalise un nom pour comparaison (sans accents, lowercase, espaces unifiés)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s.lower())
    return " ".join(s.split())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance en km entre deux points GPS."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def extract_uic_from_stop_id(stop_id: str) -> str:
    """Extrait le code UIC depuis un stop_id CP (format: 94_31039 → 9431039)."""
    # Format CP : 94_XXXXX ou 94XXXXX
    clean = stop_id.replace("_", "").replace("-", "")
    if clean.startswith("94") and len(clean) == 7 and clean.isdigit():
        return clean
    # Essayer d'extraire un code UIC 7 chiffres commençant par 94
    m = re.search(r"(94\d{5})", clean)
    if m:
        return m.group(1)
    return ""


def download_cp_gtfs() -> str:
    """Télécharge le GTFS CP et retourne le chemin du fichier."""
    print(f"📥 Téléchargement du GTFS CP : {CP_GTFS_URL}")
    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, "cp_gtfs.zip")

    with requests.get(CP_GTFS_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

    print(f"   {os.path.getsize(zip_path) / 1e6:.1f} Mo téléchargés")
    return zip_path


def extract_cp_stations(zip_path: str) -> list[Station]:
    """Extrait les gares du GTFS CP avec leurs codes UIC."""
    stations = []

    with zipfile.ZipFile(zip_path) as z:
        with z.open("stops.txt") as f:
            reader = csv.DictReader(line.decode("utf-8-sig") for line in f)
            for row in reader:
                stop_id = row.get("stop_id", "").strip()
                uic = extract_uic_from_stop_id(stop_id)

                if not uic:
                    continue

                try:
                    lat = float(row.get("stop_lat", 0))
                    lon = float(row.get("stop_lon", 0))
                except ValueError:
                    lat, lon = 0.0, 0.0

                stations.append(Station(
                    id=stop_id,
                    name=row.get("stop_name", "").strip(),
                    uic=uic,
                    lat=lat,
                    lon=lon,
                    country="PT"
                ))

    print(f"   {len(stations)} gares CP extraites avec codes UIC")
    return stations


def load_stations_csv() -> dict[str, dict]:
    """Charge stations.csv et indexe par UIC et par nom normalisé."""
    stations = {}
    by_uic = {}
    by_name = defaultdict(list)

    with open(STATIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            sid = row.get("id", "")
            stations[sid] = row

            uic = row.get("uic", "").strip()
            if uic:
                by_uic[uic] = sid

            name = normalize(row.get("name", ""))
            if name:
                by_name[name].append(sid)

    print(f"📖 stations.csv : {len(stations)} entrées, {len(by_uic)} avec UIC")
    return {"rows": stations, "by_uic": by_uic, "by_name": by_name}


def match_stations(cp_stations: list[Station], ref: dict) -> tuple[list, list, list]:
    """
    Matche les gares CP avec stations.csv.

    Retourne :
    - matched : [(cp_station, csv_id, match_type)]
    - to_update : [(csv_id, uic)] - gares existantes sans UIC
    - orphans : [cp_station] - gares CP non trouvées
    """
    matched = []
    to_update = []
    orphans = []

    for cp in cp_stations:
        csv_id = None
        match_type = None

        # 1. Match par UIC exact
        if cp.uic in ref["by_uic"]:
            csv_id = ref["by_uic"][cp.uic]
            match_type = "uic_exact"

        # 2. Match par nom normalisé
        if not csv_id:
            cp_name = normalize(cp.name)
            candidates = ref["by_name"].get(cp_name, [])
            # Filtrer par pays Portugal
            pt_candidates = [c for c in candidates if ref["rows"][c].get("country", "")[:2].upper() == "PT"]
            if len(pt_candidates) == 1:
                csv_id = pt_candidates[0]
                match_type = "name_exact"
            elif len(pt_candidates) > 1:
                # Plusieurs candidats : prendre le plus proche géographiquement
                best_dist = float("inf")
                for cand in pt_candidates:
                    row = ref["rows"][cand]
                    try:
                        clat, clon = float(row.get("latitude", 0)), float(row.get("longitude", 0))
                        if clat and clon and cp.lat and cp.lon:
                            dist = haversine_km(cp.lat, cp.lon, clat, clon)
                            if dist < best_dist:
                                best_dist = dist
                                csv_id = cand
                                match_type = f"name_geo_{dist:.1f}km"
                    except ValueError:
                        pass

        # 3. Match par proximité géographique (< 500m)
        if not csv_id and cp.lat and cp.lon:
            best_dist = 0.5  # 500m max
            for sid, row in ref["rows"].items():
                if row.get("country", "")[:2].upper() != "PT":
                    continue
                try:
                    clat = float(row.get("latitude", 0))
                    clon = float(row.get("longitude", 0))
                    if clat and clon:
                        dist = haversine_km(cp.lat, cp.lon, clat, clon)
                        if dist < best_dist:
                            best_dist = dist
                            csv_id = sid
                            match_type = f"geo_{dist*1000:.0f}m"
                except ValueError:
                    pass

        if csv_id:
            matched.append((cp, csv_id, match_type))
            # Vérifier si le CSV a déjà l'UIC
            existing_uic = ref["rows"][csv_id].get("uic", "").strip()
            if not existing_uic:
                to_update.append((csv_id, cp.uic))
        else:
            orphans.append(cp)

    print(f"\n📊 Résultats du matching :")
    print(f"   ✅ Matchées : {len(matched)}")
    print(f"   📝 À enrichir (UIC manquant) : {len(to_update)}")
    print(f"   ❌ Orphelines : {len(orphans)}")

    return matched, to_update, orphans


def save_mapping(matched: list, output_path: str):
    """Sauvegarde le mapping CP → stations.csv."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["cp_stop_id", "cp_name", "uic", "csv_id", "match_type"])
        for cp, csv_id, match_type in matched:
            writer.writerow([cp.id, cp.name, cp.uic, csv_id, match_type])

    print(f"\n💾 Mapping sauvegardé : {output_path}")


def save_orphans_geojson(orphans: list[Station], output_path: str):
    """Sauvegarde les gares orphelines en GeoJSON pour review manuel."""
    features = []

    for st in orphans:
        if st.lat and st.lon:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [st.lon, st.lat]
                },
                "properties": {
                    "cp_stop_id": st.id,
                    "name": st.name,
                    "uic": st.uic,
                    "country": "PT",
                    "status": "orphan"
                }
            })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    print(f"🗺️  GeoJSON orphelines : {output_path} ({len(features)} gares)")


def make_slug(name: str) -> str:
    """Crée un slug URL-friendly."""
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def apply_updates(matched: list, to_update: list[tuple[str, str]], orphans: list[Station], stations_csv: str):
    """Applique les mises à jour UIC, cp_id et ajoute les gares orphelines à stations.csv."""
    # Charger le CSV
    rows = []
    headers = []

    with open(stations_csv, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        headers = list(next(reader))
        for row in reader:
            rows.append(list(row))

    # Ajouter la colonne cp_id si elle n'existe pas
    if "cp_id" not in headers:
        # Insérer après renfe_is_enabled ou à la fin
        try:
            insert_idx = headers.index("renfe_is_enabled") + 1
        except ValueError:
            insert_idx = len(headers)
        headers.insert(insert_idx, "cp_id")
        for row in rows:
            row.insert(insert_idx, "")
        print(f"   Colonne 'cp_id' ajoutée à la position {insert_idx}")

    # Index des colonnes importantes
    col_idx = {h: i for i, h in enumerate(headers)}

    # Trouver le prochain ID disponible
    max_id = 0
    for row in rows:
        try:
            row_id = int(row[col_idx["id"]])
            if row_id > max_id:
                max_id = row_id
        except (ValueError, IndexError):
            pass
    next_id = max_id + 1

    # Créer un index id → row index
    id_to_idx = {row[col_idx["id"]]: i for i, row in enumerate(rows)}

    # Appliquer les mises à jour UIC
    uic_updated = 0
    for csv_id, uic in to_update:
        if csv_id in id_to_idx:
            idx = id_to_idx[csv_id]
            if not rows[idx][col_idx["uic"]]:
                rows[idx][col_idx["uic"]] = uic
                uic_updated += 1

    # Appliquer les cp_id pour toutes les gares matchées
    cp_id_updated = 0
    for cp, csv_id, match_type in matched:
        if csv_id in id_to_idx:
            idx = id_to_idx[csv_id]
            if not rows[idx][col_idx["cp_id"]]:
                rows[idx][col_idx["cp_id"]] = cp.id
                cp_id_updated += 1

    # Ajouter les gares orphelines comme nouvelles entrées
    added_count = 0
    for orphan in orphans:
        if not orphan.lat or not orphan.lon:
            continue  # Skip stations without coordinates

        # Créer une nouvelle ligne avec les colonnes par défaut
        new_row = [""] * len(headers)
        new_row[col_idx["id"]] = str(next_id)
        new_row[col_idx["name"]] = orphan.name
        new_row[col_idx["slug"]] = make_slug(orphan.name)
        new_row[col_idx["uic"]] = orphan.uic
        new_row[col_idx["latitude"]] = f"{orphan.lat:.6f}"
        new_row[col_idx["longitude"]] = f"{orphan.lon:.6f}"
        new_row[col_idx["country"]] = "PT"
        new_row[col_idx["time_zone"]] = "Europe/Lisbon"
        new_row[col_idx["is_city"]] = "f"
        new_row[col_idx["is_main_station"]] = "f"
        new_row[col_idx["is_airport"]] = "f"
        new_row[col_idx["is_suggestable"]] = "t"
        new_row[col_idx["cp_id"]] = orphan.id

        rows.append(new_row)
        next_id += 1
        added_count += 1

    # Sauvegarder
    backup_path = stations_csv + ".bak"
    shutil.copy(stations_csv, backup_path)

    with open(stations_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"\n✅ Modifications appliquées à stations.csv :")
    print(f"   {uic_updated} codes UIC ajoutés")
    print(f"   {cp_id_updated} codes cp_id ajoutés")
    print(f"   {added_count} nouvelles gares CP ajoutées")
    print(f"   Backup : {backup_path}")


def main():
    parser = argparse.ArgumentParser(description="Enrichissement des gares CP dans stations.csv")
    parser.add_argument("--apply", action="store_true", help="Applique les modifications à stations.csv")
    args = parser.parse_args()

    print("=" * 60)
    print("🚂 Enrichissement des gares CP (Portugal)")
    print("=" * 60)

    # 1. Télécharger et extraire le GTFS CP
    zip_path = download_cp_gtfs()
    cp_stations = extract_cp_stations(zip_path)

    # 2. Charger stations.csv
    ref = load_stations_csv()

    # 3. Matcher les gares
    matched, to_update, orphans = match_stations(cp_stations, ref)

    # 4. Sauvegarder le mapping
    save_mapping(matched, OUTPUT_MAPPING)

    # 5. Sauvegarder les orphelines en GeoJSON
    if orphans:
        save_orphans_geojson(orphans, OUTPUT_GEOJSON)

    # 6. Preview des mises à jour
    if to_update:
        print(f"\n📝 Codes UIC à ajouter ({len(to_update)}) :")
        for csv_id, uic in to_update[:10]:
            name = ref["rows"][csv_id].get("name", "")
            print(f"   {csv_id} ({name}) → UIC {uic}")
        if len(to_update) > 10:
            print(f"   ... et {len(to_update) - 10} autres")

    # 7. Preview des gares orphelines à ajouter
    orphans_with_coords = [o for o in orphans if o.lat and o.lon]
    if orphans_with_coords:
        print(f"\n🆕 Nouvelles gares à ajouter ({len(orphans_with_coords)}) :")
        for o in orphans_with_coords[:10]:
            print(f"   {o.name} (UIC {o.uic}, {o.lat:.4f}, {o.lon:.4f})")
        if len(orphans_with_coords) > 10:
            print(f"   ... et {len(orphans_with_coords) - 10} autres")

    # 8. Appliquer si demandé
    if args.apply and (to_update or matched or orphans_with_coords):
        apply_updates(matched, to_update, orphans_with_coords, STATIONS_CSV)
    elif to_update or matched or orphans_with_coords:
        print(f"\n💡 Pour appliquer les modifications : python {__file__} --apply")

    # Nettoyage
    shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()
