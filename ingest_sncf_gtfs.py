import gzip
import io
import json
import os
import shutil
import sqlite3
import zipfile
import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
DB_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db")
GZ_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db.gz")

def load_operators():
    """Charge la liste des opérateurs et leurs configurations."""
    if not os.path.exists(OPERATORS_FILE):
        raise FileNotFoundError(f"Fichier introuvable: {OPERATORS_FILE}")
    with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def extract_train_type(agency_id: str, stop_id_str: str, default_types: list) -> str:
    """
    Déduit le type de train à partir de :
    1. agency_id (pour Eurostar / Thalys)
    2. stop_id (méthode SNCF)
    3. fallback
    """
    agency = str(agency_id).upper()
    
    # 1. Cas EUROSTAR / THALYS via agency_id
    if 'EUROSTAR_CONTINENTAL' in agency:
        return "Thalys"  # Ex-Thalys
    if 'EUROSTAR_CHANNEL' in agency:
        return "Eurostar" # Eurostar Transmanche
    
    # 2. Cas SNCF via stop_id (ou autres via chaîne)
    val = str(stop_id_str).upper()
    if "CAR TER" in val:
        return "Car TER"
    if "CAR À RÉSERVATION" in val or "CAR A RESERVATION" in val:
        return "Car à réservation"
    if "EUROSTAR" in val:
        return "Eurostar"
    if "ICE" in val:
        return "ICE"
    if "INTERCITÉS" in val or "INTERCITES" in val:
        return "INTERCITES"
    if "LYRIA" in val:
        return "Lyria"
    if "OUIGO" in val:
        return "OUIGO"
    if "TGV INOUI" in val or "INOUI" in val or "TGV" in val:
        return "TGV INOUI"
    if "TRAMTRAIN" in val or "TRAM TRAIN" in val:
        return "TramTrain"
    if "TER" in val or "TRAIN TER" in val:
        return "Train TER"
        
    return default_types[0] if default_types else "Train"

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
            trip_headsign TEXT,
            train_type TEXT,
            operator_id TEXT
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
    print("⚡ Création des index SQL...")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE INDEX idx_stops_name ON stops(stop_name);
        CREATE INDEX idx_stop_times_search ON stop_times(stop_id, dep_min);
        CREATE INDEX idx_stop_times_trip ON stop_times(trip_id, stop_sequence);
        CREATE INDEX idx_calendar_search ON calendar_dates(service_id, date, exception_type);
        CREATE INDEX idx_trips_route ON trips(route_id);
    """)
    conn.commit()

def process_operator(op: dict, conn: sqlite3.Connection):
    op_id = op.get("id", "UNKNOWN")
    gtfs_url = op.get("gtfs_url")
    transport_types = op.get("transport_types", ["Train"])
    is_enabled = op.get("enabled", True)
    
    if not is_enabled:
        print(f"⚠️ Opérateur {op_id} désactivé dans le json, ignoré.")
        return

    if not gtfs_url:
        print(f"⚠️ Aucun URL fourni pour l'opérateur {op_id}, ignoré.")
        return

    print(f"\n🚀 Traitement de l'opérateur [{op_id}] -> {gtfs_url}")
    response = requests.get(gtfs_url, stream=True, timeout=180)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        # --- 1. Stops ---
        if 'stops.txt' in z.namelist():
            print(f"  └ Indexation des gares (stops)...")
            stops = pd.read_csv(z.open('stops.txt'), usecols=['stop_id', 'stop_name', 'stop_lat', 'stop_lon'], dtype=str)
            stops['stop_id'] = op_id + "_" + stops['stop_id']
            stops['clean_uic'] = stops['stop_id'].str.extract(r'(\d+)')
            stops['stop_lat'] = pd.to_numeric(stops['stop_lat'], errors='coerce')
            stops['stop_lon'] = pd.to_numeric(stops['stop_lon'], errors='coerce')
            
            stops[['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'clean_uic']].to_sql(
                'stops', conn, if_exists='append', index=False
            )

        # --- 2. Routes ---
        route_agency_map = {} # Map route_id -> agency_id for trip train_type extraction
        if 'routes.txt' in z.namelist():
            print(f"  └ Indexation des lignes (routes)...")
            # We load agency_id if it exists to determine Eurostar/Thalys
            cols_to_use = ['route_id']
            sample_routes = pd.read_csv(z.open('routes.txt'), nrows=0)
            if 'agency_id' in sample_routes.columns:
                cols_to_use.append('agency_id')
                
            routes = pd.read_csv(z.open('routes.txt'), usecols=cols_to_use, dtype=str)
            
            # Map raw route_id to agency_id before modifying route_id
            if 'agency_id' in routes.columns:
                 route_agency_map = dict(zip(routes['route_id'], routes['agency_id']))
                 
            routes['route_id'] = op_id + "_" + routes['route_id']
            
            def resolve_route_type(row):
                 agency = row.get('agency_id', '')
                 return extract_train_type(agency, "", transport_types)
                 
            routes['train_type'] = routes.apply(resolve_route_type, axis=1)
            routes[['route_id', 'train_type']].to_sql('routes', conn, if_exists='append', index=False)

        # --- 3. Stop Times ---
        trip_type_map = {}
        if 'stop_times.txt' in z.namelist():
            print(f"  └ Indexation des horaires (stop_times)...")
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
                
                chunk['trip_id'] = op_id + "_" + chunk['trip_id']
                raw_stop_ids = chunk['stop_id'].copy()
                chunk['stop_id'] = op_id + "_" + chunk['stop_id']

                # SNCF style: Détection du type de transport à partir du stop_id brut
                if op_id == "SNCF":
                    for idx, row in chunk.iterrows():
                        t_id = row['trip_id']
                        if t_id not in trip_type_map:
                            trip_type_map[t_id] = extract_train_type("", raw_stop_ids[idx], transport_types)

                chunk[['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'dep_min']].to_sql(
                    'stop_times', conn, if_exists='append', index=False
                )

        # --- 4. Trips ---
        if 'trips.txt' in z.namelist():
            print(f"  └ Indexation des trajets (trips)...")
            trips = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
            
            def get_trip_train_type(row):
                 raw_route_id = row['route_id']
                 # Try SNCF logic first (from stop_times)
                 t_id = op_id + "_" + row['trip_id']
                 if t_id in trip_type_map:
                     return trip_type_map[t_id]
                 # Fallback Eurostar logic (from routes agency_id)
                 agency = route_agency_map.get(raw_route_id, "")
                 return extract_train_type(agency, "", transport_types)
                 
            trips['train_type'] = trips.apply(get_trip_train_type, axis=1)
            
            trips['trip_id'] = op_id + "_" + trips['trip_id']
            trips['route_id'] = op_id + "_" + trips['route_id']
            trips['service_id'] = op_id + "_" + trips['service_id']
            trips['operator_id'] = op_id

            trips[['trip_id', 'route_id', 'service_id', 'trip_headsign', 'train_type', 'operator_id']].to_sql(
                'trips', conn, if_exists='append', index=False
            )

        # --- 5. Calendar Dates ---
        if 'calendar_dates.txt' in z.namelist():
            print(f"  └ Indexation du calendrier (calendar_dates)...")
            calendar = pd.read_csv(z.open('calendar_dates.txt'), usecols=['service_id', 'date', 'exception_type'], dtype=str)
            calendar['service_id'] = op_id + "_" + calendar['service_id']
            calendar['date'] = pd.to_datetime(calendar['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
            calendar['exception_type'] = calendar['exception_type'].astype(int)

            calendar[['service_id', 'date', 'exception_type']].to_sql(
                'calendar_dates', conn, if_exists='append', index=False
            )

def build_sqlite_gtfs():
    operators = load_operators()
    print(f"📋 {len(operators)} opérateur(s) chargé(s) depuis {OPERATORS_FILE}.")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if os.path.exists(GZ_PATH):
        os.remove(GZ_PATH)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    for op in operators:
        try:
            process_operator(op, conn)
        except Exception as e:
            print(f"❌ Erreur lors du traitement de l'opérateur {op.get('id')}: {e}")

    create_indexes(conn)

    print("\n🧹 Nettoyage et compactage SQL (VACUUM)...")
    conn.execute("VACUUM;")
    conn.execute("ANALYZE;")
    conn.close()

    print("📦 Compression finale en .gz...")
    with open(DB_PATH, 'rb') as f_in:
        with gzip.open(GZ_PATH, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

    os.remove(DB_PATH)
    print("✅ Ingestion multi-opérateurs terminée avec succès !")

if __name__ == '__main__':
    build_sqlite_gtfs()