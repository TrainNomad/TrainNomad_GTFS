"""
Télécharge et prépare le GTFS ferroviaire slovène : tous les trains de voyageurs SŽ (SŽ - Potniški promet),
bus de remplacement compris.

Source : GTFS national des transports publics slovènes (IJPP), publié par DUJPP (Družba za upravljanje
javnega potniškega prometa) sur le point d'accès national nap.si, licence CC BY-SA 4.0 :
https://dujpp.si/gtfs/dujpp-ijpp.zip. Le flux contient aussi les bus (LPP, Nomago, Arriva...) : seule
l'agence SŽ est gardée. Il ne couvre que la Slovénie : les trains internationaux (Ljubljana - Zagreb,
Villach, Graz, Budapest) sont déjà publiés en entier par les flux ÖBB et MÁV ; leur partie slovène, présente
ici sous le même numéro, est écartée par build_network.py (dédoublonnage par numéro).

Structure du flux (agence SŽ) :
  - stops.txt : stop_id national à 7 chiffres ("1122804" = Ljubljana), qui n'est PAS un UIC (les UIC slovènes
    commencent par 79) : les gares sont rapprochées par referentiel/build_gares_sz.py (codes.csv, source SZ)
  - routes.txt : une route par train, route_short_name = "Ž" + numéro ("Ž20"), route_type 2 (train) ou 3
    (bus de remplacement), sans catégorie
  - calendar.txt + calendar_dates.txt : horaire annuel (jusqu'au changement de service de décembre)

Ce que fait le script :
  ✓ trains et bus de remplacement de SŽ seulement
  ✓ trajets qui circulent à partir d'hier
  ✓ numéro du train dans trip_short_name ("20"), sans le préfixe "Ž" ; pas de numéro pour les bus
    (numéros internes, qui ne sont pas ceux d'un train)

Usage :
    python SLOVENIA/ingest_si_gtfs.py
    python SLOVENIA/ingest_si_gtfs.py --input dujpp-ijpp.zip     (fichier déjà téléchargé)
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

OUTPUT_ZIP = os.path.join(BASE_DIR, "si_gtfs.zip")
SOURCE_URL = "https://dujpp.si/gtfs/dujpp-ijpp.zip"
# copie du flux brut, à déposer à la main dans la Release sources-latest (dujpp-ijpp.zip renommé si_brut.zip)
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/si_brut.zip"

SZ_AGENCY_NAME = "SŽ - Potniški promet d.o.o."
FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]
COLUMNS = {  # colonnes utiles (le flux national porte aussi shapes, réseaux tarifaires...)
    "trips.txt": ["trip_id", "route_id", "service_id"],
    "stop_times.txt": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
}


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    cols = COLUMNS.get(name)
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig",
                       usecols=(lambda c: c in cols) if cols else None)


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        tables = {name: read(z, name) for name in FILES_KEPT}
    agency, routes, trips, stops = (tables[n] for n in ("agency.txt", "routes.txt", "trips.txt", "stops.txt"))
    cal, cd, st = tables["calendar.txt"], tables["calendar_dates.txt"], tables["stop_times.txt"]

    # 1. agence SŽ seulement (par son nom : l'agency_id est un numéro interne du registre)
    agency = agency[agency["agency_name"].str.strip() == SZ_AGENCY_NAME]
    if len(agency) != 1:
        raise RuntimeError(f"agence « {SZ_AGENCY_NAME} » introuvable dans le flux DUJPP "
                           f"(agences : {', '.join(tables['agency.txt']['agency_name'])})")
    routes = routes[routes["agency_id"].isin(agency["agency_id"])].copy()
    trips = trips[trips["route_id"].isin(routes["route_id"])].copy()
    stats["trips_total"] = len(trips)
    cal_sz = cal[cal["service_id"].isin(set(trips["service_id"]))]
    stats["du"], stats["au"] = cal_sz["start_date"].min(), cal_sz["end_date"].max()

    # 2. trajets qui circulent à partir d'hier : période de calendar.txt ou jour ajouté
    active = set(cal.loc[cal["end_date"] >= since, "service_id"]) \
        | set(cd.loc[(cd["date"] >= since) & (cd["exception_type"] == "1"), "service_id"])
    trips = trips[trips["service_id"].isin(active)]
    stats["trajets_en_cours"] = len(trips)

    # 3. numéro du train : route_short_name sans le "Ž" ("Ž20" -> "20") ; pas de numéro pour les bus
    route = routes.set_index("route_id")
    number = trips["route_id"].map(route["route_short_name"]).str.replace(r"^\D+", "", regex=True)
    is_bus = trips["route_id"].map(route["route_type"]) == "3"
    trips["trip_short_name"] = number.where(~is_bus, "")

    # 4. trajets d'au moins deux arrêts
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    stops = stops[stops["stop_id"].isin(set(st["stop_id"]))]
    routes = routes[routes["route_id"].isin(trips["route_id"])]
    cal = cal[cal["service_id"].isin(set(trips["service_id"]))]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]

    out_tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
                  "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            out_tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    bus = trips["trip_short_name"] == ""
    stats.update({"trajets": len(trips), "dont_bus_de_remplacement": int(bus.sum()), "passages": len(st),
                  "gares": len(stops)})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip GTFS DUJPP déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Slovénie (SŽ)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(SOURCE_URL, raw)
                zipfile.ZipFile(raw).close()  # page d'erreur HTML au lieu du zip
            except Exception as e:
                logging.warning(f"⚠️ dujpp.si injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Slovénie : dujpp.si injoignable, flux repris de la Release sources-latest")
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
            print(f"::error::Slovénie : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
