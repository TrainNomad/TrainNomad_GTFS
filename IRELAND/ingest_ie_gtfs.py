"""
Télécharge et prépare le GTFS ferroviaire irlandais : Iarnród Éireann / Irish Rail (InterCity, Commuter, DART)
et l'Enterprise Dublin - Belfast, exploité avec Translink (NI Railways) et publié dans ce même flux.

Source : National Transport Authority (Transport for Ireland), flux officiel
https://www.transportforireland.ie/transitData/Data/GTFS_Irish_Rail.zip (mis à jour à chaque changement
d'horaire, environ six semaines d'horaires). Copie de secours : Mobility Database (mdb-2637, même fichier).
Les trains internes à l'Irlande du Nord (Belfast - Derry, Bangor, Larne, Portrush) ne sont pas dans ce flux :
Translink ne les publie qu'en TransXChange (OpenDataNI).

Structure du flux :
  - stops.txt : 150 gares environ, sans gare mère ni UIC ; stop_id "8220IR0132", stop_code "999xxx"
  - routes.txt : une ligne par axe ("DUB-BFT-O" Dublin - Belfast, "BRAY-HOWTH-I" DART...), route_type 2
  - trips.txt : trip_short_name = code du train Irish Rail ("A101", "E215", "P570"). Première lettre :
    A = InterCity (et Enterprise sur Dublin - Belfast), E = DART, B / D / P = Commuter
  - calendar.txt + calendar_dates.txt
  - agency_timezone "Europe/London" : même heure que Dublin toute l'année, remplacé par "Europe/Dublin"

Ce que fait le script :
  ✓ catégorie du train dans route_desc (Enterprise, InterCity, Commuter, DART), lue par build_network.py
  ✓ fuseau de l'agence : Europe/Dublin
  ✓ trajets sans aucun jour de circulation à partir d'hier écartés ; points de simple passage retirés
  ✓ shapes.txt et translations.txt non repris (tracés calculés par traces/build_traces.py)

Usage :
    python IRELAND/ingest_ie_gtfs.py
    python IRELAND/ingest_ie_gtfs.py --input GTFS_Irish_Rail.zip   (fichier déjà téléchargé)
"""
import argparse
import io
import logging
import os
import sys
import zipfile
from datetime import date, timedelta

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "ie_gtfs.zip")
SOURCE_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Irish_Rail.zip"
MIRROR_URL = "https://files.mobilitydatabase.org/mdb-2637/latest.zip"
TIMEZONE = "Europe/Dublin"

# première lettre du code du train -> catégorie
CATEGORIES = {"A": "InterCity", "E": "DART", "B": "Commuter", "D": "Commuter", "P": "Commuter"}
# lignes Dublin - Belfast : les InterCity y sont l'Enterprise
ENTERPRISE_ROUTES = {"DUB-BFT-O", "DUB-BFT-I"}

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def running_services(cal: pd.DataFrame, cd: pd.DataFrame, since: str) -> set:
    """service_id ayant au moins un jour de circulation à partir de since (AAAAMMJJ)."""
    out = set(cal.loc[cal["end_date"] >= since, "service_id"])
    out |= set(cd.loc[(cd["exception_type"] == "1") & (cd["date"] >= since), "service_id"])
    return out


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        t = {n: read(z, n) for n in FILES_KEPT if n in names}
    for n in ("calendar.txt", "calendar_dates.txt"):
        if n not in t:
            t[n] = pd.DataFrame(columns=["service_id"])
    agency, routes, stops, trips, cal, cd, st = (t[n] for n in FILES_KEPT)
    stats["trips_total"] = len(trips)

    # 1. trajets qui circulent encore
    trips = trips[trips["service_id"].isin(running_services(cal, cd, since))].copy()
    stats["trajets_en_cours"] = len(trips)

    # 2. catégorie du train (route_desc), d'après la 1re lettre de son code
    letter = trips["trip_short_name"].str[:1].str.upper()
    trips["route_desc"] = letter.map(CATEGORIES).fillna("Train")
    trips.loc[trips["route_id"].isin(ENTERPRISE_ROUTES) & (letter == "A"), "route_desc"] = "Enterprise"
    unknown = sorted(set(letter[~letter.isin(CATEGORIES)]) - {""})
    if unknown:
        logging.warning(f"   ⚠️ codes de train inconnus (première lettre), classés « Train » : {unknown}")

    # 3. points de simple passage retirés ; trajets d'un seul arrêt écartés
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
    if "pickup_type" in st and "drop_off_type" in st:
        st = st[~((st["pickup_type"] == "1") & (st["drop_off_type"] == "1"))]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 4. tables réduites à ce qui sert
    agency = agency.assign(agency_timezone=TIMEZONE)
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""} if "parent_station" in stops else set()
    stops = stops[stops["stop_id"].isin(used | parents)]
    routes = routes[routes["route_id"].isin(set(trips["route_id"]))]
    services = set(trips["service_id"])
    cal = cal[cal["service_id"].isin(services)]
    cd = cd[cd["service_id"].isin(services)]
    trips = trips[[c for c in ("route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
                               "direction_id", "route_desc") if c in trips]]
    st = st[[c for c in ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type",
                         "drop_off_type") if c in st]]

    tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
              "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name, df in tables.items():
            if name in ("calendar.txt", "calendar_dates.txt") and df.empty:
                continue
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)

    days = pd.concat([cal.get("start_date", pd.Series(dtype=str)), cal.get("end_date", pd.Series(dtype=str)),
                      cd.get("date", pd.Series(dtype=str))])
    stats.update({"du": days.min() if len(days) else "", "au": days.max() if len(days) else "",
                  "trajets": len(trips), "passages": len(st), "gares": len(stops),
                  "categories": trips["route_desc"].value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="GTFS Irish Rail déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Irlande (Irish Rail, Enterprise)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(SOURCE_URL, raw)
            except Exception as e:
                logging.warning(f"⚠️ transportforireland.ie injoignable ({e}) : copie de la Mobility Database")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Irlande : flux TFI injoignable, repris de la Mobility Database (mdb-2637)")
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
            print(f"::error::Irlande : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
