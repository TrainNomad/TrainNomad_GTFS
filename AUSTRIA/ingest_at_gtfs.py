"""
Télécharge et prépare le GTFS d'ÖBB (Autriche) : Railjet, Nightjet, EuroCity, Intercity, Cityjet,
Regionalexpress et Regionalzug, trains internationaux compris.

Source : https://static.web.oebb.at/open-data/soll-fahrplan-gtfs/GTFS_Fahrplan_<année>.zip
(données ouvertes ÖBB, ~175 Mo zippé dont 666 Mo de tracés « shapes » qui ne sont pas lus).
Un fichier par année d'horaire : celui de l'année N couvre du changement d'horaire de décembre N-1
à celui de décembre N. L'année est choisie d'après la date du jour (FAHRPLAN_CHANGE).

Structure du flux :
  - fichiers dans un dossier "GTFS_Fahrplan_2026/" à l'intérieur du zip
  - stops.txt : gare mère "Pat:41:3087" (location_type 1), voies en arrêts enfants "at:41:3087:0:1",
    identifiants autrichiens (DHID) : aucun UIC. Gares étrangères en "it:...", "de:..."
  - routes.txt : ligne ("A10", "REX41"), qui mélange plusieurs catégories de trains
  - trips.txt : trip_short_name = catégorie + numéro du train ("RJ 658", "NJ 420", "REX 1775")
  - un train est publié en plusieurs morceaux (RJX 162 : Wien -> Kufstein, Kufstein -> Wörgl...),
    chacun avec ses jours de circulation, la numérotation des arrêts se poursuivant d'un morceau à l'autre

Ce que fait le script :
  ✓ morceaux d'un même train recollés (même numéro, même gare, départ 0 à 5 min après l'arrivée) :
    sinon le moteur verrait un changement de train à Kufstein ou à Buchs
  ✓ trains uniquement (route_type 2), avec leur numéro, gardé tel quel (« RJ 658 ») : la catégorie
    affichée en vient (build_network.py, OBB_CATEGORIES)
  ✓ gares rapprochées de stations.csv comme pour l'Allemagne (StationMatcher de GERMANY/ingest_de_gtfs.py,
    OpenStreetMap en secours) : id Trainline dans stop_code, rapport dans AUSTRIA/stations_report.csv
  ✗ S-Bahn (catégorie S), comme le RER en France et la S-Bahn en Allemagne
  ✗ cars de remplacement (lignes « SV », sans numéro) et navettes autos des Tauern (ATB)
  ✗ tracés (shapes), cheminements (pathways, levels)
Les trains internationaux (ICE, EuroCity, Nightjet) sont gardés : build_network.py écarte ceux qu'un
flux chargé avant fournit déjà (même numéro, mêmes horaires) et retire des flux allemands ceux
qu'ÖBB publie (mêmes gares, mêmes horaires).

Usage :
    python AUSTRIA/ingest_at_gtfs.py
    python AUSTRIA/ingest_at_gtfs.py --input GTFS_Fahrplan_2026.zip     (fichier déjà téléchargé)
"""
import argparse
import io
import logging
import os
import re
import sys
import zipfile
from datetime import date

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, GTFS_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from build_network import StationRef, STATIONS_CSV  # noqa: E402
from ingest_de_gtfs import StationMatcher, download, map_stations, merge_border_splits, service_dates  # noqa: E402
from osm_lookup import OsmLookup, write_extra  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "at_gtfs.zip")
REPORT_CSV = os.path.join(BASE_DIR, "stations_report.csv")
URL = "https://static.web.oebb.at/open-data/soll-fahrplan-gtfs/GTFS_Fahrplan_{year}.zip"
# changement d'horaire européen : deuxième samedi de décembre, au plus tard le 14
FAHRPLAN_CHANGE = (12, 14)

# catégories écartées : S-Bahn (banlieue), navettes autos (Autoschleuse Tauernbahn)
EXCLUDED_CATEGORIES = {"S", "ATB"}

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]


def timetable_year(today: date) -> int:
    """Année d'horaire en cours : celle de l'an prochain après le changement de décembre."""
    return today.year + 1 if (today.month, today.day) >= FAHRPLAN_CHANGE else today.year


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    """Fichier du flux, qu'il soit à la racine du zip ou dans son dossier "GTFS_Fahrplan_<année>/"."""
    path = next(n for n in z.namelist() if n == name or n.endswith("/" + name))
    return pd.read_csv(z.open(path), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def category(short_name: str) -> str:
    """ "RJ 658" -> "RJ" ; "REX 1775" -> "REX" ; car de remplacement sans numéro -> "" """
    m = re.match(r"[A-Za-z]+", (short_name or "").strip())
    return m.group(0).upper() if m else ""


def build(src: str, dst: str, matcher: StationMatcher) -> dict:
    stats = {}
    with zipfile.ZipFile(src) as z:
        agency, routes, trips = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "trips.txt")
        cal, cd = read(z, "calendar.txt"), read(z, "calendar_dates.txt")
        stops, st = read(z, "stops.txt"), read(z, "stop_times.txt")
    stats["trips_total"] = len(trips)
    stats["du"], stats["au"] = cal["start_date"].min(), cal["end_date"].max()

    # 1. trains avec un numéro, hors banlieue et navettes autos
    routes = routes[routes["route_type"] == "2"]
    trips = trips[trips["route_id"].isin(routes["route_id"])].copy()
    cats = trips["trip_short_name"].map(category)
    stats["trajets_sans_numero_exclus"] = int((cats == "").sum())
    stats.update({f"trajets_{c}_exclus": int((cats == c).sum()) for c in sorted(EXCLUDED_CATEGORIES)})
    trips = trips[(cats != "") & ~cats.isin(EXCLUDED_CATEGORIES)]
    trips["trip_short_name"] = trips["trip_short_name"].str.split().str.join(" ")
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]

    # 2. morceaux d'un même train recollés (merge_border_splits de l'Allemagne, sur le numéro du train)
    days = service_dates(cal, cd)
    trips, st, new_services, merged = merge_border_splits(trips, routes, st, stops, days, same_number=True)
    stats["trajets_recolles"] = merged
    if new_services:
        # jours des trajets recollés et des morceaux qui restent seuls : calendar_dates seulement
        extra = [{"service_id": sid, "date": d.strftime("%Y%m%d"), "exception_type": "1"}
                 for sid, ds in new_services.items() for d in sorted(ds)]
        cd = pd.concat([cd, pd.DataFrame(extra)], ignore_index=True)

    # 3. points de simple passage retirés (ni montée ni descente)
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 4. gares -> stations.csv (gare mère du flux ; ses voies prennent le même stop_code)
    used = set(st["stop_id"])
    keep = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | keep)]
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station", "platform_code"]]
    stops, report = map_stations(stops, used, matcher)
    report.sort_values(["status", "gtfs_name"]).to_csv(REPORT_CSV, sep=";", index=False, encoding="utf-8")
    stats.update({f"gares_{k}": int(v) for k, v in report["status"].value_counts().items()})

    routes = routes[routes["route_id"].isin(trips["route_id"])]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    services = set(trips["service_id"])
    cal, cd = cal[cal["service_id"].isin(services)], cd[cd["service_id"].isin(services)]
    trips = trips.drop(columns=[c for c in ("shape_id", "block_id") if c in trips.columns])
    st = st.drop(columns=[c for c in ("shape_dist_traveled", "stop_headsign") if c in st.columns])

    tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
              "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    stats.update({"trajets": len(trips), "passages": len(st), "gares": len(report),
                  "categories": trips["trip_short_name"].map(category).value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip ÖBB déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚄 Ingestion GTFS Autriche (ÖBB)")
    print("=" * 60)
    osm = OsmLookup()
    matcher = StationMatcher(StationRef(STATIONS_CSV), osm)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            year = timetable_year(date.today())
            try:
                download(URL.format(year=year), raw)
            except requests.HTTPError:
                # fichier de la nouvelle année pas encore publié au moment du changement d'horaire
                logging.warning(f"   horaire {year} indisponible : horaire {year - 1}")
                download(URL.format(year=year - 1), raw)
        stats = build(raw, OUTPUT_ZIP, matcher)
    finally:
        if not args.input and os.path.exists(raw):
            os.remove(raw)
        osm.save()
    write_extra(matcher.extra)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")
    print(f"📋 Rapport des gares : {REPORT_CSV}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as e:
        # message visible dans le résumé du workflow (sinon seulement « code 1 » au fond du journal)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::ÖBB : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
