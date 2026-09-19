"""
Détecte les gares orphelines ou problématiques dans stations.csv.

Gares orphelines = gares qui :
- N'ont pas de coordonnées GPS
- N'ont pas de code UIC
- Ont des coordonnées invalides (0,0 ou hors d'Europe)
- Sont référencées par un GTFS mais absentes du CSV

Usage :
    python stations_enrichment/find_orphan_stations.py
    python stations_enrichment/find_orphan_stations.py --country PT  # Filtrer par pays
    python stations_enrichment/find_orphan_stations.py --gtfs CP     # Vérifier vs GTFS CP
"""
import argparse
import csv
import json
import os
import tempfile
import shutil
import zipfile
from dataclasses import dataclass

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")
OUTPUT_GEOJSON = os.path.join(BASE_DIR, "orphan_stations.geojson")

# URLs des GTFS pour vérification
GTFS_URLS = {
    "CP": "https://publico.cp.pt/gtfs/gtfs.zip",
    "RENFE": "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip",
    "SNCF": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip",
}

# Limites géographiques de l'Europe
EUROPE_BOUNDS = {
    "lat_min": 35.0,
    "lat_max": 72.0,
    "lon_min": -25.0,
    "lon_max": 45.0,
}


@dataclass
class OrphanStation:
    id: str
    name: str
    country: str
    lat: float
    lon: float
    uic: str
    issues: list


def load_stations_csv(country_filter: str = None) -> list[dict]:
    """Charge stations.csv et retourne les lignes."""
    stations = []

    with open(STATIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if country_filter:
                if row.get("country", "")[:2].upper() != country_filter.upper():
                    continue
            stations.append(row)

    print(f"📖 {len(stations)} gares chargées" + (f" (filtre: {country_filter})" if country_filter else ""))
    return stations


def analyze_station(row: dict) -> list[str]:
    """Analyse une gare et retourne la liste des problèmes."""
    issues = []

    # 1. Vérifier coordonnées GPS
    try:
        lat = float(row.get("latitude", 0) or 0)
        lon = float(row.get("longitude", 0) or 0)
    except ValueError:
        lat, lon = 0, 0

    if lat == 0 and lon == 0:
        issues.append("no_coords")
    elif lat != 0 or lon != 0:
        if not (EUROPE_BOUNDS["lat_min"] <= lat <= EUROPE_BOUNDS["lat_max"]):
            issues.append("lat_out_of_bounds")
        if not (EUROPE_BOUNDS["lon_min"] <= lon <= EUROPE_BOUNDS["lon_max"]):
            issues.append("lon_out_of_bounds")

    # 2. Vérifier UIC
    uic = row.get("uic", "").strip()
    if not uic:
        issues.append("no_uic")
    elif not uic.isdigit() or len(uic) != 7:
        issues.append("invalid_uic")

    # 3. Vérifier Nom
    name = row.get("name", "").strip()
    if not name:
        issues.append("no_name")

    # 4. Vérifier Parent Station
    is_city = row.get("is_city", "").strip().lower() in ("t", "true", "1")
    parent_id = row.get("parent_station_id", "").strip()
    
    # Une gare qui n'est ni une entité ville (is_city=t) ni rattachée à un parent est orpheline
    if not is_city and not parent_id:
        issues.append("no_parent_station")

    return issues

def find_orphans(stations: list[dict]) -> list[OrphanStation]:
    """Trouve toutes les gares avec des problèmes."""
    orphans = []

    for row in stations:
        issues = analyze_station(row)
        if issues:
            try:
                lat = float(row.get("latitude", 0) or 0)
                lon = float(row.get("longitude", 0) or 0)
            except ValueError:
                lat, lon = 0, 0

            orphans.append(OrphanStation(
                id=row.get("id", ""),
                name=row.get("name", ""),
                country=row.get("country", "")[:2].upper(),
                lat=lat,
                lon=lon,
                uic=row.get("uic", ""),
                issues=issues
            ))

    return orphans


def download_gtfs_stops(gtfs_name: str) -> set[str]:
    """Télécharge un GTFS et retourne les stop_ids."""
    if gtfs_name not in GTFS_URLS:
        print(f"❌ GTFS inconnu : {gtfs_name}")
        return set()

    url = GTFS_URLS[gtfs_name]
    print(f"📥 Téléchargement du GTFS {gtfs_name}...")

    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, "gtfs.zip")

    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)

        stop_ids = set()
        with zipfile.ZipFile(zip_path) as z:
            with z.open("stops.txt") as f:
                reader = csv.DictReader(line.decode("utf-8-sig") for line in f)
                for row in reader:
                    stop_ids.add(row.get("stop_id", "").strip())

        print(f"   {len(stop_ids)} arrêts dans le GTFS")
        return stop_ids

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def save_geojson(orphans: list[OrphanStation], output_path: str):
    """Sauvegarde les orphelines en GeoJSON."""
    features = []

    for st in orphans:
        # Ne pas inclure les gares sans coordonnées valides
        if st.lat == 0 and st.lon == 0:
            continue

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [st.lon, st.lat]
            },
            "properties": {
                "id": st.id,
                "name": st.name,
                "country": st.country,
                "uic": st.uic,
                "issues": ", ".join(st.issues)
            }
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    print(f"\n🗺️  GeoJSON : {output_path} ({len(features)} gares avec coordonnées)")


def print_summary(orphans: list[OrphanStation]):
    """Affiche un résumé des problèmes."""
    from collections import Counter

    issue_counts = Counter()
    country_counts = Counter()

    for o in orphans:
        for issue in o.issues:
            issue_counts[issue] += 1
        country_counts[o.country] += 1

    print(f"\n📊 Résumé ({len(orphans)} gares avec problèmes) :")

    print("\n   Par type de problème :")
    for issue, count in issue_counts.most_common():
        label = {
            "no_coords": "Sans coordonnées",
            "no_uic": "Sans code UIC",
            "invalid_uic": "Code UIC invalide",
            "lat_out_of_bounds": "Latitude hors Europe",
            "lon_out_of_bounds": "Longitude hors Europe",
            "no_name": "Sans nom",
            "no_parent_station": "Sans station parente",
        }.get(issue, issue)
        print(f"      {label}: {count}")

    print("\n   Par pays (top 10) :")
    for country, count in country_counts.most_common(10):
        print(f"      {country}: {count}")


def main():
    parser = argparse.ArgumentParser(description="Détecte les gares orphelines dans stations.csv")
    parser.add_argument("--country", help="Filtrer par code pays (ex: PT, ES, FR)")
    parser.add_argument("--gtfs", help="Vérifier vs un GTFS (CP, RENFE, SNCF)")
    parser.add_argument("--output", default=OUTPUT_GEOJSON, help="Fichier GeoJSON de sortie")
    args = parser.parse_args()

    print("=" * 60)
    print("🔍 Recherche des gares orphelines")
    print("=" * 60)

    # Charger stations.csv
    stations = load_stations_csv(args.country)

    # Analyser
    orphans = find_orphans(stations)

    # Afficher résumé
    print_summary(orphans)

    # Sauvegarder GeoJSON
    if orphans:
        save_geojson(orphans, args.output)

    # Vérifier vs GTFS si demandé
    if args.gtfs:
        gtfs_stops = download_gtfs_stops(args.gtfs.upper())
        if gtfs_stops:
            # Comparer avec stations.csv
            csv_ids = {row.get("id", "") for row in stations}
            # Note: ceci nécessite un mapping stop_id → csv_id pour être utile
            print(f"\n   {len(gtfs_stops)} arrêts GTFS à matcher avec {len(csv_ids)} gares CSV")

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()
