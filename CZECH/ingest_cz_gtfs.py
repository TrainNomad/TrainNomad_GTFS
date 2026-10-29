"""
Télécharge et prépare le GTFS ferroviaire tchèque : tous les trains de voyageurs du réseau de Správa železnic,
toutes compagnies confondues (České dráhy, RegioJet, Leo Express, Arriva, Die Länderbahn / trilex, GW Train
Regio...), trains internationaux compris jusqu'aux premières gares étrangères.

Source : https://data.jr.ggu.cz/results/latest/CZPTT_GTFS.zip, GTFS produit par JrUtil (projet Oběhy) à
partir des horaires officiels CZPTT de Správa železnic (licence CC0). Flux non officiel, hébergé par un
particulier : quand il est injoignable, copie de la Release sources-latest (comme Trenord), à déposer à la
main (CZPTT_GTFS.zip renommé cz_brut.zip).

Structure du flux :
  - stops.txt : gare "-SR70ST-CZ-33615" (location_type 1), voies "-SR70S-CZ-33615-6". SR70 = code tchèque
    de la gare : UIC = "54" + code (Bzenec 5433615). Gares étrangères proches de la frontière en
    "-SR70ST-DE-...", "-SR70ST-AT-..." : leur code n'est pas un UIC
  - routes.txt : une ligne par train, route_short_name = catégorie + numéro ("Os 24140", "rj 578",
    "RJ 1016"), route_long_name = nom du train ("Vindobona", "Pendolino") ; trip_short_name vide
  - calendar_dates.txt seul (600 Mo décompressés) : horaires de plusieurs années (2022 -> horaire en cours)

Ce que fait le script :
  ✓ trajets qui circulent à partir d'hier seulement : les horaires des années passées sont écartés
  ✓ numéro du train dans trip_short_name ("SC 516"), casse gardée : "rj" = railjet de ČD, "RJ" = RegioJet ;
    la catégorie affichée en vient (build_network.py, CD_CATEGORIES)
  ✓ tous les trains de voyageurs, omnibus (Os) compris : sur la plupart des lignes, ce sont les seuls trains
    (comme le Regionalzug en Autriche)
  ✗ trains de service (Sv) et sillons proposés sans train (« nabídková trasa »)
Les gares ne sont pas rapprochées ici : les stop_id (SR70) sont stables et portent l'UIC, le référentiel
(referentiel/build_gares_cd.py) les reprend tels quels dans codes.csv.

Usage :
    python CZECH/ingest_cz_gtfs.py
    python CZECH/ingest_cz_gtfs.py --input CZPTT_GTFS.zip     (fichier déjà téléchargé)
"""
import argparse
import io
import logging
import os
import re
import sys
import zipfile
from datetime import date, timedelta

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "cz_gtfs.zip")
URL = "https://data.jr.ggu.cz/results/latest/CZPTT_GTFS.zip"
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/cz_brut.zip"

# catégories écartées : trains de service (soupravový vlak)
EXCLUDED_CATEGORIES = {"Sv"}
# compagnies écartées : sillons proposés aux compagnies, sans train réel
EXCLUDED_AGENCIES = {"nabídková trasa"}
CHUNK = 2_000_000

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar_dates.txt", "stop_times.txt"]


def read(z: zipfile.ZipFile, name: str, **kwargs) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", **kwargs)


def category(short_name: str) -> str:
    """ "Os 24140" -> "Os" ; "rj 578" -> "rj" (casse gardée : "RJ" est RegioJet) """
    m = re.match(r"[A-Za-z]+", (short_name or "").strip())
    return m.group(0) if m else ""


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        agency, routes, trips, stops = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "trips.txt"), read(z, "stops.txt")
        stats["trips_total"] = len(trips)

        # 1. jours de circulation à partir d'hier (le fichier fait 600 Mo : lu par morceaux)
        parts = [c[(c["date"] >= since) & (c["exception_type"] == "1")]
                 for c in read(z, "calendar_dates.txt", usecols=["service_id", "date", "exception_type"], chunksize=CHUNK)]
        cd = pd.concat(parts, ignore_index=True)
        stats["du"], stats["au"] = cd["date"].min(), cd["date"].max()

        # 2. trains de voyageurs avec un numéro, hors trains de service et sillons sans train
        names = routes["agency_id"].map(agency.set_index("agency_id")["agency_name"]).fillna("")
        routes = routes[~names.isin(EXCLUDED_AGENCIES)]
        trips = trips[trips["service_id"].isin(set(cd["service_id"])) & trips["route_id"].isin(routes["route_id"])].copy()
        stats["trajets_en_cours"] = len(trips)
        trips["trip_short_name"] = trips["route_id"].map(routes.set_index("route_id")["route_short_name"]).fillna("")
        trips["trip_short_name"] = trips["trip_short_name"].str.split().str.join(" ")
        cats = trips["trip_short_name"].map(category)
        stats["trajets_sans_numero_exclus"] = int((cats == "").sum())
        stats.update({f"trajets_{c}_exclus": int((cats == c).sum()) for c in sorted(EXCLUDED_CATEGORIES)})
        trips = trips[(cats != "") & ~cats.isin(EXCLUDED_CATEGORIES)]

        # 3. horaires de ces trajets (237 Mo : lus par morceaux)
        keep = set(trips["trip_id"])
        cols = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"]
        st = pd.concat([c[c["trip_id"].isin(keep)] for c in read(z, "stop_times.txt", usecols=cols, chunksize=CHUNK)],
                       ignore_index=True)

    # 4. points de simple passage retirés (ni montée ni descente)
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 5. gares desservies et leurs gares mères
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | parents)]
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station", "stop_timezone",
                   "platform_code"]]

    routes = routes[routes["route_id"].isin(trips["route_id"])]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]
    trips = trips.drop(columns=[c for c in ("shape_id", "block_id") if c in trips.columns])

    tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
              "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    by_agency = trips["route_id"].map(routes.set_index("route_id")["agency_id"]).map(agency.set_index("agency_id")["agency_name"])
    stats.update({"trajets": len(trips), "passages": len(st),
                  "gares": int((stops["parent_station"] == "").sum()),
                  "categories": trips["trip_short_name"].map(category).value_counts().to_dict(),
                  "compagnies": by_agency.value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip CZPTT_GTFS déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Tchéquie (Správa železnic : ČD, RegioJet, Leo Express...)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(URL, raw)
            except Exception as e:
                logging.warning(f"⚠️ data.jr.ggu.cz injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Tchéquie : data.jr.ggu.cz injoignable, flux repris de la Release sources-latest")
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
            print(f"::error::Tchéquie : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
