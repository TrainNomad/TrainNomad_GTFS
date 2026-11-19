"""
Télécharge et prépare le GTFS ferroviaire danois : DSB (InterCity, InterCityLyn, Regionaltog, EuroCity-Express
Hambourg - Copenhague, Railjet Copenhague - Prague), DSB Vores Tog (lignes régionales du Jutland), les chemins de
fer locaux (Lokaltog, Midttrafik, Nordjyske Jernbaner), les Öresundståg de Skånetrafiken (Copenhague - Malmö -
Göteborg / Kalmar / Karlskrona) et le Snälltåget de nuit (Stockholm - Copenhague - Hambourg / Berlin).

Source : Rejseplanen Labs, GTFS national de tous les transports publics danois (ouvert, sans clé)
https://www.rejseplanen.info/labs/GTFS.zip. Copie de secours : Release sources-latest (dk_brut.zip).

Structure du flux :
  - stops.txt : stop_id = UIC à 7 chiffres précédé de zéros ("000008600626" = København H, "000007400003" =
    Malmö C, "000008002549" = Hamburg Hbf), aussi pour les gares étrangères
  - routes.txt : route_type 2 = trains ; 109 = S-tog de Copenhague (banlieue, non repris), 3 / 715 = bus,
    4 = bateaux, 0 / 1 = métro et tram (non repris)
  - trips.txt : trip_short_name = numéro du train ("000390" = EuroCity-Express 390), zéros de tête retirés

Ce que fait le script :
  ✓ trains seuls (route_type 2) : ni S-tog, ni métro, ni bus, ni bateaux
  ✓ type du train dans route_desc ("DSB InterCity", "Öresundståg", "Snälltåget"...), lu par build_network.py
  ✓ numéro du train sans zéros de tête
  ✓ trajets sans aucun jour de circulation à partir d'hier écartés ; points de simple passage retirés
  ✓ shapes.txt, frequencies.txt et transfers.txt non repris

Usage :
    python DENMARK/ingest_dk_gtfs.py
    python DENMARK/ingest_dk_gtfs.py --input GTFS.zip   (fichier déjà téléchargé)
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

OUTPUT_ZIP = os.path.join(BASE_DIR, "dk_gtfs.zip")
SOURCE_URL = "https://www.rejseplanen.info/labs/GTFS.zip"
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/dk_brut.zip"

RAIL_ROUTE_TYPES = {"2"}
# (agency_name, route_short_name) -> type affiché ; route_short_name absent de la table : type de la compagnie
DSB_CATEGORIES = {"IC": "DSB InterCity", "ICL": "DSB InterCityLyn", "RE": "DSB Regionaltog",
                  "ECE": "DSB EuroCity-Express", "RJ": "DSB Railjet", "RX": "DSB Regionaltog"}
AGENCY_TYPES = {"DSB": "DSB Train", "DSB Vores Tog": "DSB Regionaltog", "Lokaltog A/S": "Lokaltog",
                "Midttrafik": "Lokaltog", "NT": "Lokaltog", "Skånetrafiken": "Öresundståg",
                "Snälltåget AB": "Snälltåget"}

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]


def read(z: zipfile.ZipFile, name: str, usecols=None) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", usecols=usecols)


def running_services(cal: pd.DataFrame, cd: pd.DataFrame, since: str) -> set:
    """service_id ayant au moins un jour de circulation à partir de since (AAAAMMJJ)."""
    out = set(cal.loc[cal["end_date"] >= since, "service_id"]) if "end_date" in cal else set()
    if "date" in cd:
        out |= set(cd.loc[(cd["exception_type"] == "1") & (cd["date"] >= since), "service_id"])
    return out


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        agency, routes, stops = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "stops.txt")
        trips = read(z, "trips.txt")
        cal = read(z, "calendar.txt") if "calendar.txt" in names else pd.DataFrame(columns=["service_id"])
        cd = read(z, "calendar_dates.txt") if "calendar_dates.txt" in names else pd.DataFrame(columns=["service_id"])
        # 1. trains seuls (le flux complet compte surtout des bus)
        stats["lignes_total"] = len(routes)
        routes = routes[routes["route_type"].isin(RAIL_ROUTE_TYPES)].copy()
        trips = trips[trips["route_id"].isin(set(routes["route_id"]))]
        trips = trips[trips["service_id"].isin(running_services(cal, cd, since))].copy()
        keep = set(trips["trip_id"])
        # stop_times par morceaux : 226 Mo pour tout le pays
        parts = []
        for chunk in pd.read_csv(z.open("stop_times.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig",
                                 chunksize=1_000_000):
            parts.append(chunk[chunk["trip_id"].isin(keep)])
        st = pd.concat(parts) if parts else pd.DataFrame(columns=["trip_id", "stop_id"])
    stats["lignes_train"] = len(routes)

    # 2. type du train (route_desc) et numéro sans zéros de tête
    agency_names = dict(zip(agency["agency_id"], agency["agency_name"]))
    routes["agency_name"] = routes["agency_id"].map(agency_names).fillna("")
    short = routes["route_short_name"].str.strip()
    routes["route_desc"] = [
        DSB_CATEGORIES.get(s, AGENCY_TYPES["DSB"]) if a == "DSB" else AGENCY_TYPES.get(a, a or "Train")
        for a, s in zip(routes["agency_name"], short)]
    unknown = sorted(set(routes["agency_name"]) - set(AGENCY_TYPES))
    if unknown:
        logging.warning(f"   ⚠️ compagnies ferroviaires inconnues (type = leur nom) : {unknown}")
    trips = trips.merge(routes[["route_id", "route_desc"]], on="route_id", how="left")
    trips["trip_short_name"] = trips["trip_short_name"].str.lstrip("0")

    # 3. points de simple passage retirés ; trajets d'un seul arrêt écartés
    if "pickup_type" in st and "drop_off_type" in st:
        st = st[~((st["pickup_type"] == "1") & (st["drop_off_type"] == "1"))]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 4. tables réduites à ce qui sert
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""} if "parent_station" in stops else set()
    stops = stops[stops["stop_id"].isin(used | parents)]
    routes = routes[routes["route_id"].isin(set(trips["route_id"]))]
    agency = agency[agency["agency_id"].isin(set(routes["agency_id"]))]
    services = set(trips["service_id"])
    cal = cal[cal["service_id"].isin(services)]
    cd = cd[cd["service_id"].isin(services)]
    routes = routes[[c for c in ("route_id", "agency_id", "route_short_name", "route_long_name", "route_desc",
                                 "route_type") if c in routes]]
    trips = trips[[c for c in ("route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
                               "direction_id", "route_desc") if c in trips]]
    st = st[[c for c in ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type",
                         "drop_off_type") if c in st]]
    stops = stops[[c for c in ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type",
                               "parent_station", "stop_timezone") if c in stops]]

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
                  "types": trips["route_desc"].value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="GTFS Rejseplanen déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Danemark (DSB, Lokaltog, Öresundståg, Snälltåget)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(SOURCE_URL, raw)
            except Exception as e:
                logging.warning(f"⚠️ rejseplanen.info injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Danemark : Rejseplanen injoignable, repris de la Release sources-latest")
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
            print(f"::error::Danemark : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
