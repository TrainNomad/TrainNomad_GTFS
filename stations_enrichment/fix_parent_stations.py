"""
Rattache automatiquement les gares orphelines à leur station parente.

Règles de rattachement :
1. Recherche de la ville correspondante (is_city=t) dans le même pays via le nom ou la proximité géographique (< 30km).
2. Si aucune ville n'est trouvée, recherche de la gare principale (is_main_station=t) la plus proche (< 20km).
3. Sauvegarde des modifications dans stations.csv avec création d'un fichier .bak de précaution.

Usage :
    python stations_enrichment/fix_parent_stations.py          # Mode simulation (preview)
    python stations_enrichment/fix_parent_stations.py --apply  # Appliquer directement dans stations.csv
"""
import argparse
import csv
import os
import re
import shutil
import unicodedata
from collections import defaultdict
from math import radians, sin, cos, sqrt, atan2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")


def normalize(s: str) -> str:
    """Normalise une chaîne (sans accents, minuscules, caractères spéciaux nettoyés)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s.lower())
    return " ".join(s.split())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance en km entre deux points GPS."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def load_stations() -> tuple[list[dict], list[str]]:
    """Charge les données du CSV."""
    rows = []
    with open(STATIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)
    return rows, fieldnames


def find_parents(rows: list[dict]) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    """Identifie les gares orphelines et recherche leur parent idéal."""
    # Découpage des gares parentes potentielles (villes et gares principales)
    cities = defaultdict(list)
    main_stations = defaultdict(list)

    for r in rows:
        country = r.get("country", "")[:2].upper()
        is_city = r.get("is_city", "").strip().lower() in ("t", "true", "1")
        is_main = r.get("is_main_station", "").strip().lower() in ("t", "true", "1")

        if is_city:
            cities[country].append(r)
        if is_main:
            main_stations[country].append(r)

    updates = {}  # {station_id: parent_station_id}
    logs = []     # [(station_id, station_name, parent_id, parent_name)]

    for r in rows:
        st_id = r.get("id", "")
        is_city = r.get("is_city", "").strip().lower() in ("t", "true", "1")
        parent_id = r.get("parent_station_id", "").strip()

        # Ne traiter que les gares qui ne sont ni des villes ni déjà rattachées
        if is_city or parent_id:
            continue

        st_name = r.get("name", "").strip()
        st_norm = normalize(st_name)
        country = r.get("country", "")[:2].upper()

        try:
            st_lat = float(r.get("latitude", 0) or 0)
            st_lon = float(r.get("longitude", 0) or 0)
        except ValueError:
            st_lat, st_lon = 0.0, 0.0

        best_parent_id = None
        best_parent_name = None

        # 1. Tenter un matching par le nom exact de la ville dans le même pays
        for c in cities.get(country, []):
            c_norm = normalize(c.get("name", ""))
            if c_norm and (c_norm in st_norm or st_norm in c_norm):
                best_parent_id = c.get("id")
                best_parent_name = c.get("name")
                break

        # 2. Si pas trouvé par nom, tenter par proximité géographique avec une ville (< 30km)
        if not best_parent_id and st_lat != 0 and st_lon != 0:
            min_dist = 30.0
            for c in cities.get(country, []):
                try:
                    clat = float(c.get("latitude", 0) or 0)
                    clon = float(c.get("longitude", 0) or 0)
                    if clat != 0 and clon != 0:
                        dist = haversine_km(st_lat, st_lon, clat, clon)
                        if dist < min_dist:
                            min_dist = dist
                            best_parent_id = c.get("id")
                            best_parent_name = c.get("name")
                except ValueError:
                    pass

        # 3. Dernier recours : gare principale la plus proche (< 20km)
        if not best_parent_id and st_lat != 0 and st_lon != 0:
            min_dist = 20.0
            for m in main_stations.get(country, []):
                if m.get("id") == st_id:
                    continue
                try:
                    mlat = float(m.get("latitude", 0) or 0)
                    mlon = float(m.get("longitude", 0) or 0)
                    if mlat != 0 and mlon != 0:
                        dist = haversine_km(st_lat, st_lon, mlat, mlon)
                        if dist < min_dist:
                            min_dist = dist
                            best_parent_id = m.get("id")
                            best_parent_name = m.get("name")
                except ValueError:
                    pass

        if best_parent_id:
            updates[st_id] = best_parent_id
            logs.append((st_id, st_name, best_parent_id, best_parent_name))

    return updates, logs


def apply_changes(updates: dict[str, str], fieldnames: list[str], rows: list[dict]):
    """Applique les modifications et écrit dans stations.csv."""
    backup_path = STATIONS_CSV + ".bak"
    shutil.copy(STATIONS_CSV, backup_path)

    for r in rows:
        st_id = r.get("id", "")
        if st_id in updates:
            r["parent_station_id"] = updates[st_id]

    with open(STATIONS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✅ {len(updates)} gares ont été rattachées avec succès dans stations.csv !")
    print(f"   Backup sauvegardé dans : {backup_path}")


def main():
    parser = argparse.ArgumentParser(description="Corrige les parent_station_id manquants dans stations.csv")
    parser.add_argument("--apply", action="store_true", help="Applique les modifications au fichier CSV")
    args = parser.parse_args()

    print("=" * 60)
    print("🔗 Correction automatique des stations parentes")
    print("=" * 60)

    rows, fieldnames = load_stations()
    updates, logs = find_parents(rows)

    print(f"\n📊 {len(logs)} gares orphelines à rattacher identifiées.")

    if logs:
        print("\n👀 Aperçu des 15 premiers raccordements :")
        for st_id, st_name, parent_id, parent_name in logs[:15]:
            print(f"   • [{st_id}] {st_name} ➔ [{parent_id}] {parent_name}")

        if len(logs) > 15:
            print(f"   ... et {len(logs) - 15} autres gares.")

    if args.apply and updates:
        apply_changes(updates, fieldnames, rows)
    elif updates:
        print("\n💡 Pour enregistrer ces modifications dans le CSV, lance :")
        print("   python stations_enrichment/fix_parent_stations.py --apply")


if __name__ == "__main__":
    main()