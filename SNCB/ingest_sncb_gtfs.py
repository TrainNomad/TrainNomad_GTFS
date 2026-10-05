"""
Télécharge et filtre le GTFS SNCB/NMBS (Belgique) pour ne garder que les trains.

Source : https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static
(accès libre, ~30 Mo zippé, ~435 Mo décompressé)

Structure du flux SNCB :
  - stops.txt : gares  "gs:nmbssncb:S8814001"   (S + UIC à 7 chiffres, 88xxxxx en Belgique)
                quais  "gs:nmbssncb:8814001_12" (UIC + "_" + voie), parent_station = la gare
                stop_times.txt référence les quais -> l'UIC est toujours lisible dans le stop_id
  - routes.txt : route_short_name = catégorie du train (IC, L, P, S1..S64, EC, ICE...), BUS = car
  - trips.txt  : trip_short_name = numéro commercial du train (ex. 2838)

Filtrage :
  ✗ Bus de substitution (route_type 3 / "BUS")
  ✗ Trains déjà fournis par un autre flux (sinon doublons dans les résultats) :
      OUIGO Train Classique Paris - Bruxelles ("OTC")      -> GTFS SNCF
      Eurostar / TGV INOUI (numéros 9xxx)                  -> GTFS Eurostar et SNCF
      European Sleeper Bruxelles - Berlin (452/453/474/475) -> GTFS European Sleeper
  ✗ Points de passage sans montée ni descente (frontières, bifurcations) : pickup = drop_off = 1
  ✗ Dates de calendrier passées et traductions (inutiles au moteur)

Catégorie "TRN" : la SNCB l'utilise comme catégorie générique pour certaines variantes de
circulation (IC, ICE, Nightjet, Eurostar...). On la résout, dans l'ordre :
  1. catégorie portée par les autres circulations du même numéro de train ;
  2. numéros 9xxx -> grande vitesse d'un autre opérateur (HST, écartée) ;
  3. numéros European Sleeper -> ES (écartée) ;
  4. relations vers Frankfurt / Köln -> ICE (DB) ;
  5. sinon reste TRN ("SNCB Train").
Les routes concernées sont dupliquées avec la catégorie résolue (route_id suffixé).

Usage :
    python SNCB/ingest_sncb_gtfs.py
"""
import io
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(BASE_DIR, "sncb_gtfs.zip")
SNCB_GTFS_URL = "https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static"

# Catégories (route_short_name) écartées
EXCLUDED_CATEGORIES = {
    "BUS",  # bus de substitution
    "OTC",  # OUIGO Train Classique : déjà dans le GTFS SNCF
    "HST",  # Eurostar / TGV (TRN 9xxx) : déjà dans les GTFS Eurostar et SNCF
    "ES",   # European Sleeper : déjà dans son propre GTFS
}
EUROPEAN_SLEEPER_NUMBERS = {"452", "453", "474", "475"}
GENERIC_CATEGORY = "TRN"
KEEP_DAYS_BEFORE = 2  # dates de calendrier conservées avant aujourd'hui

FILES_KEPT = ["agency.txt", "feed_info.txt", "routes.txt", "stops.txt", "trips.txt",
              "calendar.txt", "calendar_dates.txt", "stop_times.txt", "transfers.txt"]


def download(url: str, dest: str) -> None:
    logging.info(f"📥 Téléchargement {url}")
    with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f)
    zipfile.ZipFile(dest).testzip()
    logging.info(f"   ✅ {os.path.getsize(dest) / 1e6:.1f} Mo")


def read(z: zipfile.ZipFile, name: str, usecols=None) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", usecols=usecols)


def resolve_generic(routes: pd.DataFrame, trips: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Remplace la catégorie générique TRN par la catégorie réelle du train (même numéro)."""
    cat = trips["route_id"].map(routes.set_index("route_id")["route_short_name"])
    known = trips.loc[cat != GENERIC_CATEGORY].assign(cat=cat[cat != GENERIC_CATEGORY])
    by_number = known.groupby("trip_short_name")["cat"].agg(lambda s: s.value_counts().index[0])

    long_name = trips["route_id"].map(routes.set_index("route_id")["route_long_name"])
    generic = cat == GENERIC_CATEGORY
    numbers = trips.loc[generic, "trip_short_name"]
    resolved = numbers.map(by_number)

    def fallback(i):
        n = numbers[i]
        if re.fullmatch(r"9\d{3}", n):
            return "HST"
        if n in EUROPEAN_SLEEPER_NUMBERS:
            return "ES"
        if re.search(r"Frankfurt|K\wln", long_name[i]):
            return "ICE"
        return GENERIC_CATEGORY
    resolved = resolved.where(resolved.notna(), pd.Series({i: fallback(i) for i in numbers.index}, dtype=object))

    trips = trips.copy()
    changed = generic & (resolved.reindex(trips.index) != GENERIC_CATEGORY)
    new_cat = resolved[changed[changed].index]
    trips.loc[new_cat.index, "route_id"] = trips.loc[new_cat.index, "route_id"] + ":" + new_cat

    extra = []
    base = routes.set_index("route_id")
    for rid in trips.loc[new_cat.index, "route_id"].unique():
        orig, c = rid.rsplit(":", 1)
        row = base.loc[orig].copy()
        row["route_short_name"] = c
        row.name = rid
        extra.append(row)
    if extra:
        routes = pd.concat([routes, pd.DataFrame(extra).reset_index(names="route_id")], ignore_index=True)
    logging.info(f"   Catégorie {GENERIC_CATEGORY} : {int(generic.sum())} trajets, "
                 f"{len(new_cat)} résolus {new_cat.value_counts().to_dict()}")
    return routes, trips


def filter_feed(src: str, dst: str, today=None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    min_date = (today - timedelta(days=KEEP_DAYS_BEFORE)).strftime("%Y%m%d")
    stats = {}
    with zipfile.ZipFile(src) as z:
        routes = read(z, "routes.txt")
        trips = read(z, "trips.txt")
        stats["trips_total"] = len(trips)

        # 1. trains uniquement (route_type 2 = rail ; 100-117 = types étendus ferroviaires)
        rt = pd.to_numeric(routes["route_type"], errors="coerce").fillna(-1).astype(int)
        routes = routes[(rt == 2) | rt.between(100, 117)]
        trips = trips[trips["route_id"].isin(routes["route_id"])]

        # 2. catégorie générique -> catégorie réelle, puis exclusions
        routes, trips = resolve_generic(routes, trips)
        routes = routes[~routes["route_short_name"].str.upper().isin(EXCLUDED_CATEGORIES)]
        trips = trips[trips["route_id"].isin(routes["route_id"])]
        routes = routes[routes["route_id"].isin(trips["route_id"])]

        # 3. calendriers : services utilisés, dates à venir
        services = set(trips["service_id"])
        cal = read(z, "calendar.txt")
        cal = cal[cal["service_id"].isin(services) & (cal["end_date"] >= min_date)]
        cd = read(z, "calendar_dates.txt")
        cd = cd[cd["service_id"].isin(services) & (cd["date"] >= min_date)]
        active = set(cal["service_id"]) | set(cd.loc[cd["exception_type"] == "1", "service_id"])
        trips = trips[trips["service_id"].isin(active)]
        routes = routes[routes["route_id"].isin(trips["route_id"])]

        # 4. horaires : trajets retenus, sans les points de simple passage
        st = read(z, "stop_times.txt")
        st = st[st["trip_id"].isin(set(trips["trip_id"]))]
        passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
        stats["stop_times_passing_removed"] = int(passing.sum())
        st = st[~passing]
        st = st.drop(columns=[c for c in ("shape_dist_traveled", "stop_headsign") if c in st.columns])
        # un trajet doit garder au moins deux arrêts
        counts = st.groupby("trip_id").size()
        st = st[st["trip_id"].isin(counts[counts >= 2].index)]
        trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

        # 5. arrêts utilisés + leurs gares parentes
        stops = read(z, "stops.txt")
        used = set(st["stop_id"])
        parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
        stops = stops[stops["stop_id"].isin(used | parents)]
        transfers = read(z, "transfers.txt")
        transfers = transfers[transfers["from_stop_id"].isin(stops["stop_id"]) & transfers["to_stop_id"].isin(stops["stop_id"])]

        tables = {
            "agency.txt": read(z, "agency.txt"), "feed_info.txt": read(z, "feed_info.txt"),
            "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
            "calendar.txt": cal[cal["service_id"].isin(set(trips["service_id"]))],
            "calendar_dates.txt": cd[cd["service_id"].isin(set(trips["service_id"]))],
            "stop_times.txt": st, "transfers.txt": transfers,
        }

    cat = trips["route_id"].map(routes.set_index("route_id")["route_short_name"])
    stats.update({
        "trips_kept": len(trips), "routes": len(routes), "stops": len(stops),
        "stop_times": len(st), "categories": cat.value_counts().to_dict(),
    })
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            tables[name].to_csv(buf, index=False)
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    return stats


def main():
    print("=" * 60)
    print("🚂 Ingestion GTFS SNCB / NMBS (Belgique)")
    print("=" * 60)
    raw = OUTPUT_ZIP + ".raw"
    try:
        download(SNCB_GTFS_URL, raw)
        logging.info("🔧 Filtrage (trains uniquement)")
        stats = filter_feed(raw, OUTPUT_ZIP)
    finally:
        if os.path.exists(raw):
            os.remove(raw)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ GTFS SNCB prêt : {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
