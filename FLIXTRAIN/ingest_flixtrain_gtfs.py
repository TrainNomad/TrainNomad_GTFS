"""
Télécharge le GTFS officiel Flix (FlixBus + FlixTrain) et n'en garde que les trains FlixTrain.

Source : https://gtfs.gis.flix.tech/gtfs_generic_eu.zip (~30 Mo, dont 150 Mo de shapes ignorés)

Particularités :
  - FlixBus (route_type 3) et FlixTrain (route_type 2) dans le même flux : on ne garde que le 2
  - gares "Berlin Central Station (FlixTrain)" sans UIC : rapprochées de stations.csv comme pour
    l'Allemagne (GERMANY/ingest_de_gtfs.py : distance + nom, choix manuels dans
    GERMANY/stations_overrides.csv) ; rapport dans FLIXTRAIN/stations_report.csv
  - pas de numéro de train ; horaires en UTC (agency_timezone), chaque gare a son stop_timezone

Usage :
    python FLIXTRAIN/ingest_flixtrain_gtfs.py
"""
import io
import logging
import os
import sys
import zipfile

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import StationMatcher, StationRef, STATIONS_CSV, download, map_stations, read  # noqa: E402
from osm_lookup import OsmLookup, write_extra  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "flixtrain_gtfs.zip")
REPORT_CSV = os.path.join(BASE_DIR, "stations_report.csv")
FLIX_GTFS_URL = "https://gtfs.gis.flix.tech/gtfs_generic_eu.zip"
FILES_KEPT = ["agency.txt", "feed_info.txt", "routes.txt", "stops.txt", "trips.txt",
              "calendar.txt", "calendar_dates.txt", "stop_times.txt"]


def build(src: str, dst: str, matcher: StationMatcher) -> dict:
    with zipfile.ZipFile(src) as z:
        tables = {n: read(z, n) for n in FILES_KEPT if n in z.namelist()}
    routes, trips, st = tables["routes.txt"], tables["trips.txt"], tables["stop_times.txt"]
    stats = {"trips_total": len(trips)}

    # trains uniquement (route_type 2 = rail ; 100-117 = types étendus ferroviaires)
    rt = pd.to_numeric(routes["route_type"], errors="coerce").fillna(-1).astype(int)
    routes = routes[(rt == 2) | rt.between(100, 117)]
    trips = trips[trips["route_id"].isin(routes["route_id"])]
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
    services = set(trips["service_id"])

    stops = tables["stops.txt"]
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | parents)]
    stops, report = map_stations(stops, used, matcher)
    report.sort_values(["status", "gtfs_name"]).to_csv(REPORT_CSV, sep=";", index=False, encoding="utf-8")
    stats.update({f"stations_{k}": int(v) for k, v in report["status"].value_counts().items()})

    tables.update({
        "routes.txt": routes, "trips.txt": trips, "stop_times.txt": st, "stops.txt": stops,
        "agency.txt": tables["agency.txt"][tables["agency.txt"]["agency_id"].isin(routes["agency_id"])],
    })
    for n in ("calendar.txt", "calendar_dates.txt"):
        if n in tables:
            tables[n] = tables[n][tables[n]["service_id"].isin(services)]
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name, df in tables.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    stats.update({"trips_kept": len(trips), "stop_times": len(st)})
    return stats


def main():
    print("=" * 60)
    print("🚆 Ingestion GTFS FlixTrain")
    print("=" * 60)
    raw = OUTPUT_ZIP + ".raw"
    try:
        download(FLIX_GTFS_URL, raw)
        osm = OsmLookup()
        matcher = StationMatcher(StationRef(STATIONS_CSV), osm)
        stats = build(raw, OUTPUT_ZIP, matcher)
        osm.save()
        write_extra(matcher.extra)
    finally:
        if os.path.exists(raw):
            os.remove(raw)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ GTFS FlixTrain prêt : {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")
    print(f"📋 Rapport des gares : {REPORT_CSV}")


if __name__ == "__main__":
    main()
