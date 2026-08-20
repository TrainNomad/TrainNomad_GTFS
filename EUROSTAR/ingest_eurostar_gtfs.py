import gzip
import io
import os
import shutil
import sqlite3
import zipfile
import pandas as pd
import requests

GTFS_URL = "https://integration-storage.dm.eurostar.com/gtfs-prod/gtfs_static_commercial_v2.zip"  # Ajuster l'URL source GTFS Eurostar si besoin

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "eurostar_temp.db")
GZ_PATH = os.path.join(BASE_DIR, "eurostar.db.gz")

def map_train_type_from_agency(agency_id):
    """
    Mappe agency_id de routes.txt vers un train_type clair.
    """
    if not isinstance(agency_id, str):
        return "EUROSTAR"
    
    agency = agency_id.upper()
    if "CHANNEL" in agency:
        return "EUROSTAR CHANNEL"
    if "CONTINENTAL" in agency:
        return "EUROSTAR CONTINENTAL"
    return "EUROSTAR"

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
            train_type TEXT
        );

        CREATE TABLE trips (
            trip_id TEXT PRIMARY KEY,
            route_id TEXT,
            service_id TEXT,
            trip_headsign TEXT
        );

        CREATE TABLE stop_times (
            trip_id TEXT,
            arrival_time TEXT,
            departure_time TEXT,
            stop_id TEXT,
            stop_sequence INTEGER,
            dep_min INTEGER
        );

        CREATE TABLE calendar_dates (
            service_id TEXT,
            date TEXT,
            exception_type INTEGER
        );
    """)
    conn.commit()

def create_indexes(conn):
    print("⚡ Création des index SQL Eurostar...")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE INDEX idx_stop_times_search ON stop_times(stop_id, dep_min);
        CREATE INDEX idx_stop_times_trip ON stop_times(trip_id, stop_sequence);
        CREATE INDEX idx_calendar_search ON calendar_dates(service_id, date, exception_type);
        CREATE INDEX idx_trips_route ON trips(route_id);
    """)
    conn.commit()

def build_sqlite_gtfs():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if os.path.exists(GZ_PATH):
        os.remove(GZ_PATH)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print("1. Téléchargement du fichier GTFS Eurostar...")
    response = requests.get(GTFS_URL, stream=True, timeout=120)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        # 1. Stops
        print("2. Indexation des gares (stops)...")
        stops = pd.read_csv(z.open('stops.txt'), usecols=['stop_id', 'stop_name', 'stop_lat', 'stop_lon'], dtype=str)
        stops['clean_uic'] = stops['stop_id'].str.extract(r'(\d+)')
        stops['stop_lat'] = pd.to_numeric(stops['stop_lat'], errors='coerce')
        stops['stop_lon'] = pd.to_numeric(stops['stop_lon'], errors='coerce')
        stops[['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'clean_uic']].to_sql('stops', conn, if_exists='append', index=False)

        # 2. Routes (Détection directe via agency_id dans routes.txt)
        print("3. Indexation des lignes (routes via agency_id)...")
        routes = pd.read_csv(z.open('routes.txt'), usecols=['route_id', 'agency_id'], dtype=str)
        routes['train_type'] = routes['agency_id'].apply(map_train_type_from_agency)
        routes[['route_id', 'train_type']].to_sql('routes', conn, if_exists='append', index=False)

        # 3. Trips
        print("4. Indexation des trajets (trips)...")
        trips = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
        trips[['trip_id', 'route_id', 'service_id', 'trip_headsign']].to_sql('trips', conn, if_exists='append', index=False)

        # 4. Calendar Dates
        print("5. Indexation des dates de circulation (calendar_dates)...")
        calendar = pd.read_csv(z.open('calendar_dates.txt'), usecols=['service_id', 'date', 'exception_type'], dtype=str)
        calendar['date'] = pd.to_datetime(calendar['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
        calendar['exception_type'] = calendar['exception_type'].astype(int)
        calendar[['service_id', 'date', 'exception_type']].to_sql('calendar_dates', conn, if_exists='append', index=False)

        # 5. Stop Times
        print("6. Indexation des horaires (stop_times)...")
        chunksize = 100000
        use_cols = ['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence']
        
        for chunk in pd.read_csv(z.open('stop_times.txt'), usecols=use_cols, dtype=str, chunksize=chunksize):
            def to_min(t_str):
                if not isinstance(t_str, str) or ':' not in t_str:
                    return 0
                parts = t_str.split(':')
                return int(parts[0]) * 60 + int(parts[1])

            chunk['dep_min'] = chunk['departure_time'].apply(to_min)
            chunk['stop_sequence'] = chunk['stop_sequence'].astype(int)
            
            chunk[['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'dep_min']].to_sql(
                'stop_times', conn, if_exists='append', index=False
            )

    create_indexes(conn)

    print("7. Nettoyage et compactage SQL (VACUUM)...")
    conn.execute("VACUUM;")
    conn.execute("ANALYZE;")
    conn.close()

    print(f"8. Compression en fichier {GZ_PATH}...")
    with open(DB_PATH, 'rb') as f_in:
        with gzip.open(GZ_PATH, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    if not os.path.exists(GZ_PATH):
        raise FileNotFoundError(f"❌ Le fichier {GZ_PATH} n'a pas pu être créé !")

    gz_size_mb = os.path.getsize(GZ_PATH) / (1024 * 1024)
    print(f"✅ Base Eurostar générée avec succès : {GZ_PATH} ({gz_size_mb:.2f} Mo)")

if __name__ == '__main__':
    build_sqlite_gtfs()