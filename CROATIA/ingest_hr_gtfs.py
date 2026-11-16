"""
Télécharge et prépare le GTFS ferroviaire croate : tous les trains de voyageurs HŽPP (HŽ Putnički prijevoz),
bus de remplacement compris.

Source : GTFS officiel de HŽPP (https://www.hzpp.hr/GTFS_files.zip), référencé par la Mobility Database
(mdb-3043). Le flux ne couvre que la Croatie : les trains internationaux (Zagreb - Ljubljana, Zagreb -
Budapest, Rijeka / Zagreb - Villach) sont déjà publiés en entier par les flux ÖBB et MÁV ; leur partie
croate, présente ici sous le même numéro, est écartée par build_network.py (dédoublonnage par numéro).

Structure du flux :
  - stops.txt : une gare par arrêt, stop_id interne ("i-o523" = Zagreb Glavni kolodvor), sans UIC :
    les gares sont rapprochées par referentiel/build_gares_hzpp.py (codes.csv, source HZPP)
  - routes.txt : une relation par ligne ("Zagreb Glavni kolodvor - Slavonski Brod"), route_type 2 (train)
    ou 3 (bus de remplacement), sans catégorie
  - trips.txt : trip_short_name = numéro du train ("1840"), un trajet par semaine de circulation
  - calendar.txt : horaire annuel (jusqu'au changement de service de décembre)

Ce que fait le script :
  ✓ trajets qui circulent à partir d'hier
  ✓ bus de remplacement gardés : ils remplacent un train
  ✓ numéro du train : chiffres seuls (le flux ne publie pas la catégorie IC / putnički)

Usage :
    python CROATIA/ingest_hr_gtfs.py
    python CROATIA/ingest_hr_gtfs.py --input GTFS_files.zip     (fichier déjà téléchargé)
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

OUTPUT_ZIP = os.path.join(BASE_DIR, "hr_gtfs.zip")
SOURCE_URL = "https://www.hzpp.hr/GTFS_files.zip"
# copie du flux brut, à déposer à la main dans la Release sources-latest (GTFS_files.zip renommé hr_brut.zip)
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/hr_brut.zip"

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "stop_times.txt"]
OPTIONAL = ["calendar_dates.txt"]


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        tables = {name: read(z, name) for name in FILES_KEPT}
        cd = read(z, "calendar_dates.txt") if "calendar_dates.txt" in z.namelist() \
            else pd.DataFrame(columns=["service_id", "date", "exception_type"])
    agency, routes, trips, stops = (tables[n] for n in ("agency.txt", "routes.txt", "trips.txt", "stops.txt"))
    cal, st = tables["calendar.txt"], tables["stop_times.txt"]
    stats["trips_total"] = len(trips)
    stats["du"], stats["au"] = cal["start_date"].min(), cal["end_date"].max()

    # 1. trajets qui circulent à partir d'hier : période de calendar.txt ou jour ajouté
    active = set(cal.loc[cal["end_date"] >= since, "service_id"]) \
        | set(cd.loc[(cd["date"] >= since) & (cd["exception_type"] == "1"), "service_id"])
    trips = trips[trips["service_id"].isin(active)].copy()
    stats["trajets_en_cours"] = len(trips)

    # 2. numéro du train : chiffres seuls ("1840")
    trips["trip_short_name"] = trips["trip_short_name"].str.strip()

    # 3. trajets d'au moins deux arrêts
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
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
        for name in FILES_KEPT + OPTIONAL:
            buf = io.StringIO()
            out_tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    bus = trips["route_id"].map(routes.set_index("route_id")["route_type"]) == "3"
    stats.update({"trajets": len(trips), "dont_bus_de_remplacement": int(bus.sum()), "passages": len(st),
                  "gares": len(stops), "trains_distincts": trips.loc[~bus, "trip_short_name"].nunique()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip GTFS de HŽPP déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Croatie (HŽPP)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(SOURCE_URL, raw)
                zipfile.ZipFile(raw).close()  # page d'erreur HTML au lieu du zip
            except Exception as e:
                logging.warning(f"⚠️ hzpp.hr injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Croatie : hzpp.hr injoignable, flux repris de la Release sources-latest")
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
            print(f"::error::Croatie : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
