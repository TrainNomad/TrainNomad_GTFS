import csv
import json
import math
import os
import requests

# 📁 Chemins d'accès : stations.csv est dans le dossier parent (..)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIONS_CSV = os.path.abspath(os.path.join(BASE_DIR, "..", "stations.csv"))
OUTPUT_JSON = os.path.join(BASE_DIR, "datawiki.json")

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance en mètres entre deux points GPS."""
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def fetch_wikidata_pt_stations() -> dict:
    """Récupère toutes les gares du Portugal depuis Wikidata indexées par UIC."""
    query = """
    SELECT DISTINCT ?item ?itemLabel ?uic ?lat ?lon WHERE {
      ?item wdt:P31/wdt:279* wd:Q55488 ;
            wdt:P17 wd:Q45 .
      
      OPTIONAL { ?item wdt:P722 ?uic . }
      OPTIONAL { 
        ?item p:P625/psv:P625 ?node .
        ?node wikibase:geoLatitude ?lat ;
              wikibase:geoLongitude ?lon .
      }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "pt,fr,en". }
    }
    """
    headers = {
        "User-Agent": "StationAuditor/1.0 (https://github.com)",
        "Accept": "application/json",
    }

    print("📡 Interrogation de Wikidata pour le Portugal (Q45)...")
    response = requests.get(
        WIKIDATA_SPARQL_URL, params={"query": query, "format": "json"}, headers=headers
    )
    response.raise_for_status()
    data = response.json()

    wikidata_by_uic = {}
    for row in data["results"]["bindings"]:
        uic = row.get("uic", {}).get("value", "").strip()
        lat = row.get("lat", {}).get("value")
        lon = row.get("lon", {}).get("value")

        if uic:
            wikidata_by_uic[uic] = {
                "wikidata_id": row["item"]["value"].split("/")[-1],
                "wikidata_name": row.get("itemLabel", {}).get("value", ""),
                "uic": uic,
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
            }
    return wikidata_by_uic


def run_comparison():
    wikidata_map = fetch_wikidata_pt_stations()

    to_control_manually = []
    matched_count = 0
    missing_in_wikidata = 0

    print(f"📖 Lecture du fichier : {STATIONS_CSV}")
    if not os.path.exists(STATIONS_CSV):
        print(f"❌ Erreur : Fichier introuvable à l'emplacement {STATIONS_CSV}")
        return

    with open(STATIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            # Filtre uniquement sur le Portugal
            if row.get("country", "")[:2].upper() != "PT":
                continue

            local_uic = row.get("uic", "").strip()
            local_name = row.get("name", "").strip()
            local_id = row.get("id", "").strip()

            try:
                local_lat = float(row.get("latitude", 0))
                local_lon = float(row.get("longitude", 0))
            except ValueError:
                local_lat, local_lon = 0.0, 0.0

            # Si le code UIC local n'est pas présent sur Wikidata
            if not local_uic or local_uic not in wikidata_map:
                missing_in_wikidata += 1
                to_control_manually.append(
                    {
                        "local_id": local_id,
                        "local_name": local_name,
                        "local_uic": local_uic,
                        "reason": "uic_not_found_in_wikidata",
                        "details": {
                            "local_coords": {"lat": local_lat, "lon": local_lon}
                        },
                    }
                )
                continue

            wiki_data = wikidata_map[local_uic]
            wiki_lat = wiki_data["lat"]
            wiki_lon = wiki_data["lon"]

            issues = []

            # 1. Vérification de la distance (> 500 mètres)
            distance_m = None
            if local_lat and local_lon and wiki_lat and wiki_lon:
                distance_m = round(
                    haversine_distance(local_lat, local_lon, wiki_lat, wiki_lon), 2
                )
                if distance_m > 500:
                    issues.append("distance_exceeds_500m")
            else:
                issues.append("missing_coordinates")

            # 2. Vérification rapide des libellés (nom)
            name_clean_local = local_name.lower().replace("-", " ")
            name_clean_wiki = wiki_data["wikidata_name"].lower().replace("-", " ")

            if (
                name_clean_local not in name_clean_wiki
                and name_clean_wiki not in name_clean_local
            ):
                issues.append("name_mismatch")

            # Sauvegarde des données en cas d'anomalie
            if issues:
                to_control_manually.append(
                    {
                        "local_id": local_id,
                        "local_name": local_name,
                        "local_uic": local_uic,
                        "wikidata_id": wiki_data["wikidata_id"],
                        "wikidata_name": wiki_data["wikidata_name"],
                        "issues": issues,
                        "distance_meters": distance_m,
                        "coordinates": {
                            "local": {"lat": local_lat, "lon": local_lon},
                            "wikidata": {"lat": wiki_lat, "lon": wiki_lon},
                        },
                    }
                )
            else:
                matched_count += 1

    # Rapport JSON final
    output_data = {
        "summary": {
            "total_to_control": len(to_control_manually),
            "perfect_matches": matched_count,
            "missing_in_wikidata": missing_in_wikidata,
            "distance_threshold_meters": 500,
        },
        "items_to_control": to_control_manually,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as out:
        json.dump(output_data, out, ensure_ascii=False, indent=2)

    print(f"\n✅ Terminé !")
    print(f"📊 Valides : {matched_count}")
    print(f"⚠️ À contrôler : {len(to_control_manually)}")
    print(f"📁 Fichier créé : {OUTPUT_JSON}")


if __name__ == "__main__":
    run_comparison()