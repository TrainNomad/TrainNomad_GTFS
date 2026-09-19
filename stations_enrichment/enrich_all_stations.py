"""
Enrichissement des gares de tous les opérateurs dans stations.csv.

Ce script :
1. Télécharge les GTFS de chaque opérateur
2. Extrait les gares avec leurs codes (UIC, renfe_id, trenitalia_id, etc.)
3. Compare avec stations.csv
4. Ajoute les gares manquantes et complète les codes

Opérateurs supportés :
- SNCF (France) : uic8_sncf
- RENFE (Espagne) : renfe_id
- TRENITALIA (Italie) : trenitalia_id
- CP (Portugal) : cp_id

Usage :
    python stations_enrichment/enrich_all_stations.py                    # Preview tous
    python stations_enrichment/enrich_all_stations.py --operator SNCF    # Preview SNCF seulement
    python stations_enrichment/enrich_all_stations.py --apply            # Appliquer tous
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
from dataclasses import dataclass, field
from math import radians, sin, cos, sqrt, atan2
from typing import Optional

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")
MAPPINGS_DIR = os.path.join(BASE_DIR, "mappings")
OUTPUT_GEOJSON = os.path.join(BASE_DIR, "all_orphan_stations.geojson")

# Configuration des opérateurs
OPERATORS = {
    "SNCF": {
        "name": "SNCF",
        "country": "FR",
        "timezone": "Europe/Paris",
        "gtfs_url": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip",
        "id_column": "uic8_sncf",  # Colonne dans stations.csv
        "extract_id": lambda stop_id, stop_code: extract_sncf_uic(stop_id),
        "extract_uic": lambda stop_id, stop_code: extract_sncf_uic7(stop_id),
    },
    "RENFE": {
        "name": "Renfe",
        "country": "ES",
        "timezone": "Europe/Madrid",
        "gtfs_url": "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip",
        "id_column": "renfe_id",
        "extract_id": lambda stop_id, stop_code: stop_id.strip(),
        "extract_uic": lambda stop_id, stop_code: f"71{stop_id.strip().zfill(5)}" if stop_id.strip().isdigit() else "",
    },
    "TRENITALIA": {
        "name": "Trenitalia",
        "country": "IT",
        "timezone": "Europe/Rome",
        "gtfs_path": os.path.join(GTFS_DIR, "Trenitalia", "trenitalia_gtfs.zip"),
        "id_column": "trenitalia_id",
        "extract_id": lambda stop_id, stop_code: stop_code.strip() if stop_code else stop_id.strip(),
        "extract_uic": lambda stop_id, stop_code: extract_italian_uic(stop_code or stop_id),
    },
    "ITALO": {
        "name": "Italo",
        "country": "IT",
        "timezone": "Europe/Rome",
        "gtfs_path": os.path.join(GTFS_DIR, "Trenitalia", "italo_gtfs.zip"),
        "id_column": "trenitalia_id",  # Italo utilise les mêmes gares que Trenitalia
        "extract_id": lambda stop_id, stop_code: stop_code.strip() if stop_code else stop_id.strip(),
        "extract_uic": lambda stop_id, stop_code: extract_italian_uic(stop_code or stop_id),
    },
    "CP": {
        "name": "CP",
        "country": "PT",
        "timezone": "Europe/Lisbon",
        "gtfs_url": "https://publico.cp.pt/gtfs/gtfs.zip",
        "id_column": "cp_id",
        "extract_id": lambda stop_id, stop_code: stop_id.strip(),
        "extract_uic": lambda stop_id, stop_code: stop_id.replace("_", "").replace("-", "") if stop_id.startswith("94") else "",
    },
}


@dataclass
class Station:
    id: str
    name: str
    operator_id: str  # ID spécifique à l'opérateur
    uic: str
    lat: float
    lon: float
    country: str
    operator: str


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s.lower())
    return " ".join(s.split())


def make_slug(name: str) -> str:
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


# Fonctions d'extraction spécifiques
def extract_sncf_uic(stop_id: str) -> str:
    """Extrait le code UIC8 SNCF depuis stop_id."""
    digits = re.sub(r"\D", "", stop_id.rsplit("-", 1)[-1]) or re.sub(r"\D", "", stop_id)
    if len(digits) >= 8:
        return digits[-8:]
    return ""


def extract_sncf_uic7(stop_id: str) -> str:
    """Extrait le code UIC7 depuis stop_id SNCF."""
    uic8 = extract_sncf_uic(stop_id)
    if uic8 and len(uic8) == 8:
        return uic8[:-1]  # Enlever le dernier chiffre (clé de contrôle)
    return ""


def extract_italian_uic(code: str) -> str:
    """Extrait le code UIC7 depuis un code italien (830000070 -> 8300070)."""
    clean = re.sub(r"\D", "", code or "")
    if len(clean) == 9 and clean.startswith("83"):
        return clean[:2] + clean[-5:]
    if len(clean) == 7 and clean.startswith("83"):
        return clean
    return ""


def download_gtfs(url: str, name: str) -> str:
    """Télécharge un GTFS et retourne le chemin."""
    print(f"📥 Téléchargement du GTFS {name}...")
    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, "gtfs.zip")

    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        print(f"   {os.path.getsize(zip_path) / 1e6:.1f} Mo téléchargés")
        return zip_path
    except Exception as e:
        print(f"   ❌ Erreur : {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ""


def extract_stations_from_gtfs(zip_path: str, operator_key: str) -> list[Station]:
    """Extrait les gares d'un GTFS."""
    config = OPERATORS[operator_key]
    stations = []

    try:
        with zipfile.ZipFile(zip_path) as z:
            with z.open("stops.txt") as f:
                reader = csv.DictReader(line.decode("utf-8-sig") for line in f)
                for row in reader:
                    stop_id = row.get("stop_id", "").strip()
                    stop_code = row.get("stop_code", "").strip()
                    stop_name = row.get("stop_name", "").strip()

                    operator_id = config["extract_id"](stop_id, stop_code)
                    uic = config["extract_uic"](stop_id, stop_code)

                    if not operator_id:
                        continue

                    try:
                        lat = float(row.get("stop_lat", 0))
                        lon = float(row.get("stop_lon", 0))
                    except ValueError:
                        lat, lon = 0.0, 0.0

                    stations.append(Station(
                        id=stop_id,
                        name=stop_name,
                        operator_id=operator_id,
                        uic=uic,
                        lat=lat,
                        lon=lon,
                        country=config["country"],
                        operator=operator_key
                    ))
    except Exception as e:
        print(f"   ❌ Erreur lecture GTFS : {e}")

    # Dédupliquer par operator_id
    seen = {}
    unique = []
    for st in stations:
        if st.operator_id not in seen:
            seen[st.operator_id] = st
            unique.append(st)

    print(f"   {len(unique)} gares extraites")
    return unique


def load_stations_csv() -> dict:
    """Charge stations.csv."""
    stations = {}
    by_uic = {}
    by_uic8 = {}
    by_operator = defaultdict(dict)
    by_name_country = defaultdict(list)

    with open(STATIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        headers = reader.fieldnames
        for row in reader:
            sid = row.get("id", "")
            stations[sid] = row

            uic = row.get("uic", "").strip()
            if uic:
                by_uic[uic] = sid

            uic8 = row.get("uic8_sncf", "").strip()
            if uic8:
                by_uic8[uic8] = sid

            # Index par ID opérateur
            for op_key, op_config in OPERATORS.items():
                col = op_config["id_column"]
                if col in row and row[col].strip():
                    by_operator[op_key][row[col].strip()] = sid

            # Index par nom + pays
            name = normalize(row.get("name", ""))
            country = row.get("country", "")[:2].upper()
            if name and country:
                by_name_country[(name, country)].append(sid)

    print(f"📖 stations.csv : {len(stations)} entrées, {len(by_uic)} UIC")
    return {
        "rows": stations,
        "headers": headers,
        "by_uic": by_uic,
        "by_uic8": by_uic8,
        "by_operator": by_operator,
        "by_name_country": by_name_country,
    }


def match_stations(gtfs_stations: list[Station], ref: dict, operator_key: str) -> tuple[list, list, list]:
    """Matche les gares GTFS avec stations.csv."""
    config = OPERATORS[operator_key]
    id_column = config["id_column"]

    matched = []
    to_update = []
    orphans = []

    for st in gtfs_stations:
        csv_id = None
        match_type = None

        # 1. Match par ID opérateur
        if st.operator_id in ref["by_operator"].get(operator_key, {}):
            csv_id = ref["by_operator"][operator_key][st.operator_id]
            match_type = f"{id_column}_exact"

        # 2. Match par UIC
        if not csv_id and st.uic:
            if st.uic in ref["by_uic"]:
                csv_id = ref["by_uic"][st.uic]
                match_type = "uic_exact"
            elif operator_key == "SNCF" and len(st.uic) == 7:
                # Essayer UIC8
                for suffix in "0123456789":
                    uic8 = st.uic + suffix
                    if uic8 in ref["by_uic8"]:
                        csv_id = ref["by_uic8"][uic8]
                        match_type = "uic8_match"
                        break

        # 3. Match par nom + pays
        if not csv_id:
            name = normalize(st.name)
            candidates = ref["by_name_country"].get((name, st.country), [])
            if len(candidates) == 1:
                csv_id = candidates[0]
                match_type = "name_country"
            elif len(candidates) > 1 and st.lat and st.lon:
                # Plusieurs candidats : prendre le plus proche
                best_dist = float("inf")
                for cand in candidates:
                    row = ref["rows"][cand]
                    try:
                        clat = float(row.get("latitude", 0) or 0)
                        clon = float(row.get("longitude", 0) or 0)
                        if clat and clon:
                            dist = haversine_km(st.lat, st.lon, clat, clon)
                            if dist < best_dist and dist < 5.0:  # Max 5km
                                best_dist = dist
                                csv_id = cand
                                match_type = f"name_geo_{dist:.1f}km"
                    except ValueError:
                        pass

        # 4. Match par proximité GPS (< 500m)
        if not csv_id and st.lat and st.lon:
            best_dist = 0.5
            for sid, row in ref["rows"].items():
                if row.get("country", "")[:2].upper() != st.country:
                    continue
                try:
                    clat = float(row.get("latitude", 0) or 0)
                    clon = float(row.get("longitude", 0) or 0)
                    if clat and clon:
                        dist = haversine_km(st.lat, st.lon, clat, clon)
                        if dist < best_dist:
                            best_dist = dist
                            csv_id = sid
                            match_type = f"geo_{dist*1000:.0f}m"
                except ValueError:
                    pass

        if csv_id:
            matched.append((st, csv_id, match_type))
            # Vérifier si l'ID opérateur manque
            existing_op_id = ref["rows"][csv_id].get(id_column, "").strip()
            if not existing_op_id:
                to_update.append((csv_id, st.operator_id, st.uic))
        else:
            orphans.append(st)

    return matched, to_update, orphans


def apply_updates(all_matched: dict, all_to_update: dict, all_orphans: dict, stations_csv: str):
    """Applique toutes les modifications à stations.csv."""
    # Charger le CSV
    rows = []
    headers = []

    with open(stations_csv, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        headers = list(next(reader))
        for row in reader:
            rows.append(list(row))

    # Ajouter les colonnes manquantes
    for op_key, op_config in OPERATORS.items():
        col = op_config["id_column"]
        if col not in headers:
            headers.append(col)
            for row in rows:
                row.append("")
            print(f"   Colonne '{col}' ajoutée")

    col_idx = {h: i for i, h in enumerate(headers)}

    # Trouver le prochain ID
    max_id = 0
    for row in rows:
        try:
            row_id = int(row[col_idx["id"]])
            if row_id > max_id:
                max_id = row_id
        except (ValueError, IndexError):
            pass
    next_id = max_id + 1

    id_to_idx = {row[col_idx["id"]]: i for i, row in enumerate(rows)}

    stats = defaultdict(int)

    # Mettre à jour les IDs opérateurs
    for op_key, updates in all_to_update.items():
        id_column = OPERATORS[op_key]["id_column"]
        for csv_id, op_id, uic in updates:
            if csv_id in id_to_idx:
                idx = id_to_idx[csv_id]
                if not rows[idx][col_idx[id_column]]:
                    rows[idx][col_idx[id_column]] = op_id
                    stats[f"{op_key}_id_updated"] += 1
                if uic and not rows[idx][col_idx["uic"]]:
                    rows[idx][col_idx["uic"]] = uic
                    stats[f"{op_key}_uic_updated"] += 1

    # Ajouter les gares orphelines
    for op_key, orphans in all_orphans.items():
        config = OPERATORS[op_key]
        for st in orphans:
            if not st.lat or not st.lon:
                continue

            new_row = [""] * len(headers)
            new_row[col_idx["id"]] = str(next_id)
            new_row[col_idx["name"]] = st.name
            new_row[col_idx["slug"]] = make_slug(st.name)
            new_row[col_idx["uic"]] = st.uic
            new_row[col_idx["latitude"]] = f"{st.lat:.6f}"
            new_row[col_idx["longitude"]] = f"{st.lon:.6f}"
            new_row[col_idx["country"]] = st.country
            new_row[col_idx["time_zone"]] = config["timezone"]
            new_row[col_idx["is_city"]] = "f"
            new_row[col_idx["is_main_station"]] = "f"
            new_row[col_idx["is_airport"]] = "f"
            new_row[col_idx["is_suggestable"]] = "t"
            new_row[col_idx[config["id_column"]]] = st.operator_id

            rows.append(new_row)
            next_id += 1
            stats[f"{op_key}_added"] += 1

    # Sauvegarder
    backup_path = stations_csv + ".bak"
    shutil.copy(stations_csv, backup_path)

    with open(stations_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"\n✅ Modifications appliquées à stations.csv :")
    for key, count in sorted(stats.items()):
        print(f"   {key}: {count}")
    print(f"   Backup : {backup_path}")


def save_orphans_geojson(all_orphans: dict, output_path: str):
    """Sauvegarde toutes les orphelines en GeoJSON."""
    features = []

    for op_key, orphans in all_orphans.items():
        for st in orphans:
            if not st.lat or not st.lon:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
                "properties": {
                    "operator": op_key,
                    "name": st.name,
                    "operator_id": st.operator_id,
                    "uic": st.uic,
                    "country": st.country,
                }
            })

    geojson = {"type": "FeatureCollection", "features": features}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    print(f"\n🗺️  GeoJSON : {output_path} ({len(features)} gares)")


def main():
    parser = argparse.ArgumentParser(description="Enrichissement des gares de tous les opérateurs")
    parser.add_argument("--operator", choices=list(OPERATORS.keys()) + ["ALL"], default="ALL",
                        help="Opérateur à traiter (défaut: ALL)")
    parser.add_argument("--apply", action="store_true", help="Applique les modifications")
    args = parser.parse_args()

    print("=" * 60)
    print("🚂 Enrichissement des gares - Tous opérateurs")
    print("=" * 60)

    # Charger stations.csv
    ref = load_stations_csv()

    operators_to_process = [args.operator] if args.operator != "ALL" else list(OPERATORS.keys())

    all_matched = {}
    all_to_update = {}
    all_orphans = {}
    temp_dirs = []

    for op_key in operators_to_process:
        config = OPERATORS[op_key]
        print(f"\n{'─'*40}")
        print(f"🚄 {config['name']} ({config['country']})")
        print(f"{'─'*40}")

        # Obtenir le GTFS
        if "gtfs_path" in config:
            if not os.path.exists(config["gtfs_path"]):
                print(f"   ⚠️ {config['gtfs_path']} introuvable, ignoré")
                continue
            zip_path = config["gtfs_path"]
        else:
            zip_path = download_gtfs(config["gtfs_url"], config["name"])
            if not zip_path:
                continue
            temp_dirs.append(os.path.dirname(zip_path))

        # Extraire les gares
        stations = extract_stations_from_gtfs(zip_path, op_key)
        if not stations:
            continue

        # Matcher
        matched, to_update, orphans = match_stations(stations, ref, op_key)

        print(f"   ✅ Matchées : {len(matched)}")
        print(f"   📝 À enrichir : {len(to_update)}")
        print(f"   ❌ Orphelines : {len(orphans)}")

        all_matched[op_key] = matched
        all_to_update[op_key] = to_update
        all_orphans[op_key] = [o for o in orphans if o.lat and o.lon]

    # Résumé
    total_orphans = sum(len(o) for o in all_orphans.values())
    total_to_update = sum(len(u) for u in all_to_update.values())

    print(f"\n{'='*60}")
    print(f"📊 Résumé total :")
    print(f"   À enrichir : {total_to_update}")
    print(f"   Nouvelles gares : {total_orphans}")

    # Sauvegarder le GeoJSON
    if total_orphans > 0:
        save_orphans_geojson(all_orphans, OUTPUT_GEOJSON)

    # Appliquer
    if args.apply and (total_to_update > 0 or total_orphans > 0):
        apply_updates(all_matched, all_to_update, all_orphans, STATIONS_CSV)
    elif total_to_update > 0 or total_orphans > 0:
        print(f"\n💡 Pour appliquer : python {__file__} --apply")

    # Nettoyage
    for tmp in temp_dirs:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()
