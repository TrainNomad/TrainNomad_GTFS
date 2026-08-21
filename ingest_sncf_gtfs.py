import gzip
import io
import json
import logging
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
OUTPUT_DB_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db")
OUTPUT_GZ_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db.gz")

# Plage de 60 jours glissants à partir d'aujourd'hui
TODAY = datetime.now()
DATE_START = TODAY.strftime("%Y-%m-%d")
DATE_END = (TODAY + timedelta(days=60)).strftime("%Y-%m-%d")


def detect_train_type(row):
    # Analyse combinée des noms courts et longs pour isoler le type
    name = f"{row.get('route_long_name', '')} {row.get('route_short_name', '')}".upper()
    if "OUIGO" in name:
        return "OUIGO"
    if "TER" in name:
        return "TER"
    if "INTERCITÉS" in name or "INTERCITES" in name or "IC" in name:
        return "INTERCITÉS"
    if "TGV" in name or "INOUI" in name:
        return "TGV INOUI"
    if "EUROSTAR" in name:
        return "EUROSTAR"
    return "TRAIN"


def init_db(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = OFF;")
    cursor.execute("PRAGMA synchronous = OFF;")

    cursor.executescript("""
        DROP TABLE IF EXISTS stops;
        DROP TABLE IF EXISTS routes;
        DROP TABLE IF EXISTS trips;
        DROP TABLE IF EXISTS stop_times;
        DROP TABLE IF EXISTS calendar_dates;

        CREATE TABLE stops (
            stop_id TEXT PRIMARY KEY,
            stop_name TEXT,
            stop_lat REAL,
            stop_lon REAL,
            clean_uic TEXT
        );

        CREATE TABLE routes (
            route_id TEXT PRIMARY KEY,
            operator_id TEXT,
            train_type TEXT
        );

        CREATE TABLE trips (
            trip_id TEXT PRIMARY KEY,
            route_id TEXT,
            service_id TEXT,
            trip_headsign TEXT,
            operator_id TEXT
        );

        CREATE TABLE stop_times (
            trip_id TEXT,
            arrival_time TEXT,
            departure_time TEXT,
            stop_id TEXT,
            stop_sequence INTEGER,
            dep_min INTEGER,
            operator_id TEXT
        );

        CREATE TABLE calendar_dates (
            service_id TEXT,
            date TEXT,
            exception_type INTEGER,
            operator_id TEXT
        );
    """)
    conn.commit()


def create_indexes(conn):
    logging.info("⚡ Création des index SQL...")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_stops_uic ON stops(clean_uic);
        CREATE INDEX IF NOT EXISTS idx_stop_times_search ON stop_times(stop_id, dep_min);
        CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON stop_times(trip_id, stop_sequence);
        CREATE INDEX IF NOT EXISTS idx_calendar_search ON calendar_dates(service_id, date, exception_type);
        CREATE INDEX IF NOT EXISTS idx_trips_route ON trips(route_id);
    """)
    conn.commit()


def build_sqlite_gtfs():
    if os.path.exists(OUTPUT_DB_PATH):
        os.remove(OUTPUT_DB_PATH)
    if os.path.exists(OUTPUT_GZ_PATH):
        os.remove(OUTPUT_GZ_PATH)

    conn = sqlite3.connect(OUTPUT_DB_PATH)
    init_db(conn)

    if os.path.exists(OPERATORS_FILE):
        with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
            operators = json.load(f)
    else:
        operators = [{
            "id": "SNCF",
            "enabled": True,
            "gtfs_url": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
        }]

    logging.info(f"📅 Dates conservées : {DATE_START} à {DATE_END}")

    for op in operators:
        if not op.get("enabled", True):
            continue

        op_id = op["id"]
        url = op["gtfs_url"]
        logging.info(f"📥 Téléchargement GTFS {op_id}...")

        res = requests.get(url, stream=True, timeout=180)
        res.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            # 1. STOPS (Conservation intégrale de tous les arrêts)
            logging.info(f"[{op_id}] 1/5 Indexation des gares (stops)...")
            stops = pd.read_csv(z.open('stops.txt'), usecols=['stop_id', 'stop_name', 'stop_lat', 'stop_lon'], dtype=str)
            stops['clean_uic'] = stops['stop_id'].str.extract(r'(\d+)')
            stops['stop_lat'] = pd.to_numeric(stops['stop_lat'], errors='coerce')
            stops['stop_lon'] = pd.to_numeric(stops['stop_lon'], errors='coerce')
            stops.drop_duplicates(subset=['stop_id'], inplace=True)
            stops[['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'clean_uic']].to_sql(
                'stops', conn, if_exists='append', index=False
            )

            # 2. ROUTES (Détection exacte du type de train)
            logging.info(f"[{op_id}] 2/5 Indexation des lignes (routes)...")
            routes = pd.read_csv(z.open('routes.txt'), usecols=['route_id', 'route_short_name', 'route_long_name'], dtype=str)
            routes['operator_id'] = op_id
            routes['train_type'] = routes.apply(detect_train_type, axis=1)
            routes[['route_id', 'operator_id', 'train_type']].to_sql('routes', conn, if_exists='append', index=False)

            # 3. CALENDAR_DATES (Restreint aux 60 prochains jours)
            logging.info(f"[{op_id}] 3/5 Indexation de calendar_dates...")
            calendar = pd.read_csv(z.open('calendar_dates.txt'), usecols=['service_id', 'date', 'exception_type'], dtype=str)
            calendar['date'] = pd.to_datetime(calendar['date'], format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
            calendar = calendar[(calendar['date'] >= DATE_START) & (calendar['date'] <= DATE_END)]
            calendar['exception_type'] = calendar['exception_type'].astype(int)
            calendar['operator_id'] = op_id
            calendar[['service_id', 'date', 'exception_type', 'operator_id']].to_sql('calendar_dates', conn, if_exists='append', index=False)

            active_services = set(calendar['service_id'].unique())

            # 4. TRIPS (Liés aux services des 60 jours)
            logging.info(f"[{op_id}] 4/5 Indexation des trajets (trips)...")
            trips = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
            trips = trips[trips['service_id'].isin(active_services)]
            trips['operator_id'] = op_id
            trips[['trip_id', 'route_id', 'service_id', 'trip_headsign', 'operator_id']].to_sql('trips', conn, if_exists='append', index=False)

            active_trips = set(trips['trip_id'].unique())

            # 5. STOP_TIMES
            logging.info(f"[{op_id}] 5/5 Indexation des horaires (stop_times)...")
            def to_min(t_str):
                if not isinstance(t_str, str) or ':' not in t_str:
                    return 0
                parts = t_str.split(':')
                return int(parts[0]) * 60 + int(parts[1])

            chunksize = 200000
            use_cols = ['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence']

            for chunk in pd.read_csv(z.open('stop_times.txt'), usecols=use_cols, dtype=str, chunksize=chunksize):
                chunk = chunk[chunk['trip_id'].isin(active_trips)]
                if chunk.empty:
                    continue
                chunk['dep_min'] = chunk['departure_time'].apply(to_min)
                chunk['stop_sequence'] = chunk['stop_sequence'].astype(int)
                chunk['operator_id'] = op_id

                chunk[['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'dep_min', 'operator_id']].to_sql(
                    'stop_times', conn, if_exists='append', index=False
                )

    create_indexes(conn)

    logging.info("🧹 Nettoyage SQL (VACUUM)...")
    conn.execute("VACUUM;")
    conn.execute("ANALYZE;")
    conn.close()

    raw_size_mb = os.path.getsize(OUTPUT_DB_PATH) / (1024 * 1024)
    logging.info(f"📊 Taille SQLite brute : {raw_size_mb:.2f} Mo")

    logging.info("📦 Compression gzippée...")
    with open(OUTPUT_DB_PATH, 'rb') as f_in:
        with gzip.open(OUTPUT_GZ_PATH, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

    gz_size_mb = os.path.getsize(OUTPUT_GZ_PATH) / (1024 * 1024)
    logging.info(f"✅ Fichier gzippé : {OUTPUT_GZ_PATH} ({gz_size_mb:.2f} Mo)")


if __name__ == '__main__':
    build_sqlite_gtfs()