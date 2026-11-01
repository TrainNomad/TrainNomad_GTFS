"""
Télécharge et prépare le GTFS ferroviaire slovaque : tous les trains de voyageurs du réseau ŽSR (ZSSK, RegioJet,
Leo Express), trains internationaux compris jusqu'à leur terminus (Praha, Wien, Budapest...).

Source : « Grafikon vlakovej dopravy vo formáte GTFS » de ŽSR (iTranSys), publié dans le catalogue national des
données ouvertes (https://data.slovensko.sk/datasety/ebeeedf1-aca2-451a-bdc0-35d536714888). L'adresse du
fichier (download?id=...) change à chaque publication : elle est demandée au catalogue (SPARQL) à chaque
passage, l'ancienne adresse de www.zsr.sk ne renvoyant plus qu'une page d'information.

Structure du flux :
  - stops.txt : un arrêt par gare, stop_id = UIC à 7 chiffres ("5613206" ; "5433014" en Tchéquie, "8100025"
    en Autriche) : build_network.py les rapproche de stations.csv par l'UIC, sans référentiel
  - routes.txt : une ligne par train, route_short_name = catégorie + numéro ("EC 120", "Os 2401", "rj 1033"),
    route_long_name = nom du train ("TATRAN") ou "NAD" (bus de remplacement, náhradná autobusová doprava)
  - calendar.txt + calendar_dates.txt : horaire annuel (jusqu'au changement de service de décembre)

Ce que fait le script :
  ✓ trajets qui circulent à partir d'hier
  ✓ numéro du train dans trip_short_name ("EC 120"), casse gardée : "rj" = railjet de ČD, "RJ" = RegioJet
  ✓ bus de remplacement (NAD) gardés : ils remplacent un train
  ✓ points de simple passage retirés

Usage :
    python SLOVAKIA/ingest_sk_gtfs.py
    python SLOVAKIA/ingest_sk_gtfs.py --input GTFS_2026_10_28.zip     (fichier déjà téléchargé)
"""
import argparse
import io
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date, timedelta

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "sk_gtfs.zip")
DATASET = "ebeeedf1-aca2-451a-bdc0-35d536714888"
SPARQL = "https://data.slovensko.sk/api/sparql"
# adresse connue le 2026-10-31, si le catalogue ne répond pas
FALLBACK_URL = "https://data.slovensko.sk/download?id=fa56545f-a4c2-4eb9-bd26-b1c350991293"
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/sk_brut.zip"

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]


def catalog_url() -> str:
    """Adresse du fichier GTFS publiée dans le catalogue national (dcat:downloadURL de la distribution)."""
    query = f"""PREFIX dcat: <http://www.w3.org/ns/dcat#>
SELECT DISTINCT ?dl WHERE {{
  ?ds dcat:distribution ?d . ?d dcat:downloadURL ?dl .
  FILTER(CONTAINS(STR(?ds), "{DATASET}"))
}}"""
    req = urllib.request.Request(f"{SPARQL}?{urllib.parse.urlencode({'query': query})}",
                                 headers={"Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        urls = sorted({b["dl"]["value"] for b in json.load(r)["results"]["bindings"]})
    if len(urls) != 1:
        raise RuntimeError(f"{len(urls)} fichiers GTFS dans le catalogue : {urls}")
    return urls[0]


def read(z: zipfile.ZipFile, name: str, **kwargs) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", **kwargs)


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        tables = {name: read(z, name) for name in FILES_KEPT}
    agency, routes, trips, stops = (tables[n] for n in ("agency.txt", "routes.txt", "trips.txt", "stops.txt"))
    cal, cd, st = tables["calendar.txt"], tables["calendar_dates.txt"], tables["stop_times.txt"]
    stats["trips_total"] = len(trips)
    stats["du"], stats["au"] = cal["start_date"].min(), cal["end_date"].max()

    # 1. trajets qui circulent à partir d'hier : période de calendar.txt ou jour ajouté
    active = set(cal.loc[cal["end_date"] >= since, "service_id"]) \
        | set(cd.loc[(cd["date"] >= since) & (cd["exception_type"] == "1"), "service_id"])
    trips = trips[trips["service_id"].isin(active)].copy()
    stats["trajets_en_cours"] = len(trips)

    # 2. numéro affiché : catégorie + numéro de la route ("EC 120")
    trips["trip_short_name"] = trips["route_id"].map(routes.set_index("route_id")["route_short_name"]).fillna("")
    trips["trip_short_name"] = trips["trip_short_name"].str.split().str.join(" ")

    # 3. points de simple passage retirés (ni montée ni descente)
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    stops = stops[stops["stop_id"].isin(set(st["stop_id"]))]
    routes = routes[routes["route_id"].isin(trips["route_id"])]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    cal = cal[cal["service_id"].isin(set(trips["service_id"]))]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]
    trips = trips.drop(columns=[c for c in ("shape_id", "block_id") if c in trips.columns])

    out_tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
                  "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            out_tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    by_agency = trips["route_id"].map(routes.set_index("route_id")["agency_id"]).map(agency.set_index("agency_id")["agency_name"])
    nad = trips["route_id"].map(routes.set_index("route_id")["route_long_name"]) == "NAD"
    stats.update({"trajets": len(trips), "dont_bus_de_remplacement": int(nad.sum()), "passages": len(st),
                  "gares": len(stops),
                  "categories": trips["trip_short_name"].str.extract(r"^([A-Za-z]+)", expand=False).value_counts().to_dict(),
                  "compagnies": by_agency.value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip GTFS de ŽSR déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Slovaquie (ŽSR : ZSSK, RegioJet, Leo Express)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                try:
                    url = catalog_url()
                except Exception as e:
                    logging.warning(f"⚠️ catalogue data.slovensko.sk sans réponse ({e}) : dernière adresse connue")
                    url = FALLBACK_URL
                download(url, raw)
            except Exception as e:
                logging.warning(f"⚠️ data.slovensko.sk injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Slovaquie : data.slovensko.sk injoignable, flux repris de la Release sources-latest")
                download(MIRROR_URL, raw)
        stats = build(raw, OUTPUT_ZIP)
    finally:
        if not args.input and os.path.exists(raw):
            os.remove(raw)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as e:
        # message visible dans le résumé du workflow (sinon seulement « code 1 » au fond du journal)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::Slovaquie : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
