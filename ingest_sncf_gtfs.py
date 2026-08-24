import gzip
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta
import pandas as pd
import requests
import unicodedata

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
STATIONS_CSV = os.path.join(BASE_DIR, "stations.csv")
OUTPUT_DB_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db")
OUTPUT_GZ_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db.gz")
REPORT_PATH = os.path.join(BASE_DIR, "harmonization_report.json")

# Plage glissante de 120 jours
TODAY = datetime.now()
DATE_START = TODAY.strftime("%Y-%m-%d")
DATE_END = (TODAY + timedelta(days=120)).strftime("%Y-%m-%d")


def normalize_string(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn').lower()
    s = re.sub(r'[^a-z0-9]', ' ', s)
    stop_words = {'gare', 'de', 'du', 'des', 'la', 'le', 'l', 'saint', 'st', 'estacion'}
    words = [w for w in s.split() if w not in stop_words]
    return ' '.join(words)

def extract_eurostar_train_no(trip_id: str) -> str:
    if not isinstance(trip_id, str):
        return ""
    match = re.search(r'(?:EUROSTAR_)?(\d{3,5})', trip_id, re.IGNORECASE)
    return match.group(1) if match else trip_id

def extract_renfe_train_no(trip_short_name: str, trip_id: str) -> str:
    if isinstance(trip_short_name, str) and trip_short_name.strip():
        clean_name = trip_short_name.strip()
        return str(int(clean_name)) if clean_name.isdigit() else clean_name
    
    if isinstance(trip_id, str):
        m = re.search(r'(\d{4,5})', trip_id)
        if m:
            return str(int(m.group(1)))
            
    return ""

def extract_uic(val: str, operator_id: str = "") -> str:
    if not isinstance(val, str):
        return ""
    
    if isinstance(operator_id, str) and operator_id.upper() == "SNCF":
        digits = re.sub(r'\D', '', val)
        if len(digits) >= 7:
            return digits[:7]
        return digits

    m = re.search(r'(\d{7,8})', val)
    if m:
        return m.group(1)
    
    m_short = re.search(r'\b(\d{5})\b', val)
    if m_short:
        return f"71{m_short.group(1)}"
        
    return ""

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    if any(v is None or math.isnan(v) for v in [lat1, lon1, lat2, lon2]):
        return float('inf')
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def extract_train_type_from_stop_id(stop_id: str) -> str:
    if not isinstance(stop_id, str):
        return ""
    
    sid = stop_id.upper()
    if "TGV INOUI" in sid or "INOUI" in sid or "TGV" in sid:
        return "TGV InOui"
    elif "OUIGO" in sid:
        return "OUIGO"
    elif "TER" in sid:
        return "TER"
    elif "INTERCITES" in sid or "INTERCITÉS" in sid:
        return "Intercités"
    elif "ICE" in sid:
        return "ICE"
    elif "TRANSILIEN" in sid or "RER" in sid:
        return "Transilien"
    elif "EUROSTAR" in sid or "THALYS" in sid:
        return "Eurostar"
    
    return ""

def parse_renfe_train_type(route_row: pd.Series) -> str:
    full_str = f"{route_row.get('route_short_name', '')} {route_row.get('route_long_name', '')} {route_row.get('route_id', '')}".upper()
    if "AVE" in full_str:
        return "AVE"
    elif "AVLO" in full_str:
        return "Avlo"
    elif "ALVIA" in full_str:
        return "Alvia"
    elif "EUROMED" in full_str:
        return "Euromed"
    elif "INTERCITY" in full_str:
        return "Intercity"
    elif "MD" in full_str or "MEDIA" in full_str:
        return "Media Distancia"
    elif "CERCANIAS" in full_str or "RODALIES" in full_str:
        return "Cercanías"
    elif "REGIONAL" in full_str:
        return "Regional"
    elif "TRENCELTA" in full_str:
        return "Tren Celta"
    
    val = str(route_row.get('route_long_name', '')).strip()
    return val if val else "Train Renfe"

def to_minutes(t_str: str) -> int:
    if not isinstance(t_str, str) or ':' not in t_str:
        return 0
    parts = t_str.split(':')
    return int(parts[0]) * 60 + int(parts[1])


class GTFSHarmonizer:
    def __init__(self):
        if os.path.exists(OPERATORS_FILE):
            with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
                self.operators = json.load(f)
        else:
            self.operators = [{
                "id": "SNCF",
                "enabled": True,
                "gtfs_url": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
            }]

        self.stations_reference = {}
        self.raw_stops = []
        self.stop_map = {}
        self.canonical_stops = {}
        self.uic_to_country = {}

        self.stats = {
            "total_raw_stops": 0,
            "unique_canonical_stops": 0,
            "merges_uic": 0,
            "merges_geoloc": 0
        }

    def load_stations_reference(self):
        if not os.path.exists(STATIONS_CSV):
            logging.warning(f"⚠️ Fichier {STATIONS_CSV} non trouvé.")
            return

        logging.info("📖 Chargement des correspondances depuis stations.csv...")
        df = pd.read_csv(STATIONS_CSV, sep=';', dtype=str)

        self.sncf_to_uic = {}
        self.renfe_to_uic = {}
        self.uic_to_country = {}

        for _, row in df.iterrows():
            real_uic = str(row.get('uic', '')).strip()
            if not real_uic:
                continue

            country = str(row.get('country', '')).strip().upper() if pd.notnull(row.get('country')) else ''
            if len(country) > 2:
                country = country[:2]

            self.uic_to_country[real_uic] = country

            name = str(row.get('name', '')).strip()
            parent_name = str(row.get('parent_station_name', '')).strip() or str(row.get('city', '')).strip() or name

            self.stations_reference[real_uic] = {
                'name': name,
                'parent_name': parent_name,
                'country': country,
                'lat': float(row['latitude']) if pd.notnull(row.get('latitude')) else None,
                'lon': float(row['longitude']) if pd.notnull(row.get('longitude')) else None,
                'uic': real_uic
            }

            uic8_sncf = str(row.get('uic8_sncf', '')).strip()
            if uic8_sncf:
                sncf_7 = extract_uic(uic8_sncf, operator_id="SNCF")
                self.sncf_to_uic[sncf_7] = real_uic

            renfe_id = str(row.get('renfe_id', '')).strip()
            if renfe_id:
                self.renfe_to_uic[renfe_id] = real_uic
                if len(renfe_id) == 5:
                    self.renfe_to_uic[f"71{renfe_id}"] = real_uic

        logging.info(f"✅ Chargé : {len(self.sncf_to_uic)} clés SNCF, {len(self.renfe_to_uic)} clés RENFE.")

    def fetch_stops(self):
        logging.info("📥 Extraction des gares depuis les GTFS...")
        for op in self.operators:
            op_id = op["id"]
            if not op.get("enabled", True) or not op.get("gtfs_url"):
                continue

            logging.info(f" -> Téléchargement pour [{op_id}]...")
            res = requests.get(op["gtfs_url"], timeout=180)
            res.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                if 'stops.txt' in z.namelist():
                    df = pd.read_csv(z.open('stops.txt'), dtype=str)
                    df.columns = df.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                    
                    for _, row in df.iterrows():
                        raw_id = str(row['stop_id']).strip()
                        stop_code = str(row.get('stop_code', '')).strip() if pd.notnull(row.get('stop_code')) else ''
                        
                        extracted = extract_uic(stop_code, op_id) or extract_uic(raw_id, op_id)

                        uic = ""
                        if op_id.upper() == "SNCF":
                            uic = self.sncf_to_uic.get(extracted, extracted)
                        elif op_id.upper() == "RENFE":
                            uic = self.renfe_to_uic.get(extracted, extracted)
                        else:
                            uic = extracted

                        lat = float(row['stop_lat']) if pd.notnull(row.get('stop_lat')) else None
                        lon = float(row['stop_lon']) if pd.notnull(row.get('stop_lon')) else None
                        raw_name = str(row.get('stop_name', ''))

                        self.raw_stops.append({
                            'operator_id': op_id,
                            'raw_stop_id': raw_id,
                            'stop_code': stop_code,
                            'raw_name': raw_name,
                            'stop_lat': lat,
                            'stop_lon': lon,
                            'uic': uic,
                            'norm_name': normalize_string(raw_name)
                        })

        self.stats["total_raw_stops"] = len(self.raw_stops)

    def process_and_deduplicate(self):
        logging.info("⚡ Déduplication des gares...")
        uic_index = {}

        for stop in self.raw_stops:
            op_id = stop['operator_id']
            raw_id = stop['raw_stop_id']
            uic = stop['uic']
            lat, lon = stop['stop_lat'], stop['stop_lon']
            norm_name = stop['norm_name']

            matched_id = None

            if uic and uic in uic_index:
                matched_id = uic_index[uic]
                self.stats["merges_uic"] += 1

            if not matched_id and lat is not None and lon is not None:
                for c_id, c_stop in self.canonical_stops.items():
                    dist = haversine_distance(lat, lon, c_stop['stop_lat'], c_stop['stop_lon'])
                    if dist <= 100.0 and (norm_name == c_stop['norm_name'] or norm_name in c_stop['norm_name'] or c_stop['norm_name'] in norm_name):
                        matched_id = c_id
                        self.stats["merges_geoloc"] += 1
                        break

            if not matched_id:
                matched_id = f"CANONICAL_UIC_{uic}" if uic else f"CANONICAL_STOP_{len(self.canonical_stops) + 1:06d}"

                official_name = stop['raw_name']
                parent_name = stop['raw_name']
                country = self.uic_to_country.get(uic, "")

                if uic and uic in self.stations_reference:
                    ref = self.stations_reference[uic]
                    official_name = ref['name']
                    parent_name = ref['parent_name']

                self.canonical_stops[matched_id] = {
                    "stop_id": matched_id,
                    "stop_name": official_name,
                    "parent_name": parent_name,
                    "country": country,
                    "stop_lat": lat,
                    "stop_lon": lon,
                    "uic": uic,
                    "norm_name": norm_name,
                    "sncf": 0,
                    "eurostar": 0,
                    "renfe": 0,
                    "source_stop_ids": []
                }
                if uic:
                    uic_index[uic] = matched_id

            target = self.canonical_stops[matched_id]
            if op_id.upper() == "SNCF":
                target["sncf"] = 1
            elif op_id.upper() == "EUROSTAR":
                target["eurostar"] = 1
            elif op_id.upper() == "RENFE":
                target["renfe"] = 1

            target["source_stop_ids"].append(f"{op_id}:{raw_id}")
            self.stop_map[(op_id, raw_id)] = matched_id

        self.stats["unique_canonical_stops"] = len(self.canonical_stops)

    def build_sqlite(self):
        logging.info("💾 Génération de la base SQLite...")
        if os.path.exists(OUTPUT_DB_PATH):
            os.remove(OUTPUT_DB_PATH)

        conn = sqlite3.connect(OUTPUT_DB_PATH)
        cursor = conn.cursor()

        cursor.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;

            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                stop_name TEXT,
                parent_name TEXT,
                country TEXT,
                stop_lat REAL,
                stop_lon REAL,
                uic TEXT,
                sncf INTEGER DEFAULT 0,
                eurostar INTEGER DEFAULT 0,
                renfe INTEGER DEFAULT 0,
                source_stop_ids TEXT
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

        stops_rows = [
            (
                s["stop_id"],
                s["stop_name"],
                s["parent_name"],
                s["country"],
                s["stop_lat"],
                s["stop_lon"],
                s["uic"],
                s["sncf"],
                s["eurostar"],
                s["renfe"],
                json.dumps(s["source_stop_ids"])
            )
            for s in self.canonical_stops.values()
        ]
        cursor.executemany("INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", stops_rows)

        logging.info(f"📅 Traitement des calendriers du {DATE_START} au {DATE_END}...")

        for op in self.operators:
            op_id = op["id"]
            if not op.get("enabled", True) or not op.get("gtfs_url"):
                continue

            res = requests.get(op["gtfs_url"], timeout=180)
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:

                calendar_entries = []
                active_services = set()

                # --- 1. PARSING DE CALENDAR.TXT (Si disponible) ---
                if 'calendar.txt' in z.namelist():
                    cal_df = pd.read_csv(z.open('calendar.txt'), dtype=str)
                    cal_df.columns = cal_df.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()

                    if 'start_date' in cal_df.columns and 'end_date' in cal_df.columns:
                        days_map = {0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday', 4: 'friday', 5: 'saturday', 6: 'sunday'}
                        
                        dt_start = datetime.strptime(DATE_START, "%Y-%m-%d")
                        dt_end = datetime.strptime(DATE_END, "%Y-%m-%d")

                        for _, row in cal_df.iterrows():
                            srv_id = op_id + "_" + str(row['service_id']).strip()
                            s_start = datetime.strptime(str(row['start_date']).strip(), "%Y%m%d")
                            s_end = datetime.strptime(str(row['end_date']).strip(), "%Y%m%d")

                            curr = max(dt_start, s_start)
                            limit = min(dt_end, s_end)

                            while curr <= limit:
                                day_name = days_map[curr.weekday()]
                                if str(row.get(day_name, '0')).strip() == '1':
                                    calendar_entries.append((srv_id, curr.strftime("%Y-%m-%d"), 1, op_id))
                                    active_services.add(srv_id)
                                curr += timedelta(days=1)

                # --- 2. PARSING DE CALENDAR_DATES.TXT ET PURGE DES SUPPRESSIONS ---
                if 'calendar_dates.txt' in z.namelist():
                    cd_df = pd.read_csv(z.open('calendar_dates.txt'), dtype=str)
                    cd_df.columns = cd_df.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                    
                    if 'exception_type' not in cd_df.columns:
                        cd_df['exception_type'] = '1'

                    cd_df['date'] = pd.to_datetime(cd_df['date'].str.strip(), format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
                    cd_df = cd_df[(cd_df['date'] >= DATE_START) & (cd_df['date'] <= DATE_END)]

                    exceptions = {}
                    for _, row in cd_df.iterrows():
                        srv_id = op_id + "_" + str(row['service_id']).strip()
                        dt_str = row['date']
                        if pd.notnull(dt_str):
                            exceptions[(srv_id, dt_str)] = int(row['exception_type'])

                    # Filtrage : On retire les dates annulées (type 2) issues de calendar.txt
                    filtered_calendar = []
                    for srv_id, dt_str, exc_type, op_name in calendar_entries:
                        if (srv_id, dt_str) in exceptions:
                            if exceptions[(srv_id, dt_str)] == 2:
                                continue  # Annulation appliquée
                        filtered_calendar.append((srv_id, dt_str, 1, op_name))

                    # Ajout des exceptions d'ajout manuel (type 1)
                    for (srv_id, dt_str), exc_type in exceptions.items():
                        if exc_type == 1:
                            filtered_calendar.append((srv_id, dt_str, 1, op_id))
                            active_services.add(srv_id)

                    calendar_entries = filtered_calendar

                if calendar_entries:
                    cursor.executemany(
                        "INSERT INTO calendar_dates VALUES (?, ?, ?, ?)",
                        calendar_entries
                    )

                # --- 3. INGESTION DES TRIPS, ROUTES ET STOP_TIMES ---
                if op_id.upper() in ["RENFE", "EUROSTAR"]:
                    if 'stop_times.txt' in z.namelist():
                        st = pd.read_csv(z.open('stop_times.txt'), usecols=['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'], dtype=str)
                        st.columns = st.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        st['dep_min'] = st['departure_time'].apply(to_minutes)
                        st['stop_sequence'] = st['stop_sequence'].astype(int)
                        st['trip_id'] = op_id + "_" + st['trip_id'].str.strip()
                        st['operator_id'] = op_id
                        st['stop_id'] = st['stop_id'].apply(lambda x: self.stop_map.get((op_id, str(x).strip()), str(x).strip()))
                        st[['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'dep_min', 'operator_id']].to_sql(
                            'stop_times', conn, if_exists='append', index=False
                        )

                    if 'routes.txt' in z.namelist():
                        rt = pd.read_csv(z.open('routes.txt'), dtype=str)
                        rt.columns = rt.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        if op_id.upper() == "RENFE":
                            rt['train_type'] = rt.apply(parse_renfe_train_type, axis=1)
                        else:
                            rt['train_type'] = rt.apply(lambda r: "Eurostar (ex-Thalys)" if "THALYS" in str(r.get("agency_id", "")).upper() else "Eurostar", axis=1)
                        
                        rt['route_id'] = op_id + "_" + rt['route_id'].str.strip()
                        rt['operator_id'] = op_id
                        rt[['route_id', 'operator_id', 'train_type']].to_sql('routes', conn, if_exists='append', index=False)

                    if 'trips.txt' in z.namelist():
                        cols_to_read = ['trip_id', 'route_id', 'service_id']
                        header_cols = [c.strip().replace('\ufeff', '').replace('\xa0', '').lower() for c in pd.read_csv(z.open('trips.txt'), nrows=1).columns]
                        if 'trip_short_name' in header_cols:
                            cols_to_read.append('trip_short_name')

                        tp = pd.read_csv(z.open('trips.txt'), dtype=str)
                        tp.columns = tp.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        tp = tp[[c for c in cols_to_read if c in tp.columns]]

                        tp['service_id'] = op_id + "_" + tp['service_id'].str.strip()
                        tp['trip_id'] = op_id + "_" + tp['trip_id'].str.strip()
                        tp['route_id'] = op_id + "_" + tp['route_id'].str.strip()
                        tp['operator_id'] = op_id

                        if active_services:
                            tp = tp[tp['service_id'].isin(active_services)]

                        if op_id.upper() == "RENFE":
                            tp['trip_headsign'] = tp.apply(lambda r: extract_renfe_train_no(r.get('trip_short_name'), r.get('trip_id')), axis=1)
                        else:
                            tp['trip_headsign'] = tp['trip_id'].apply(extract_eurostar_train_no)

                        tp[['trip_id', 'route_id', 'service_id', 'trip_headsign', 'operator_id']].to_sql(
                            'trips', conn, if_exists='append', index=False
                        )

                else:
                    stop_type_map = {}
                    if 'stops.txt' in z.namelist():
                        stops_df = pd.read_csv(z.open('stops.txt'), usecols=['stop_id'], dtype=str)
                        stops_df.columns = stops_df.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        stops_df['train_type'] = stops_df['stop_id'].apply(extract_train_type_from_stop_id)
                        stop_type_map = stops_df[stops_df['train_type'] != ""].set_index('stop_id')['train_type'].to_dict()

                    trip_type_map = {}
                    if 'stop_times.txt' in z.namelist():
                        st = pd.read_csv(z.open('stop_times.txt'), usecols=['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'], dtype=str)
                        st.columns = st.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        st['raw_type'] = st['stop_id'].map(stop_type_map)
                        trip_type_map = st.dropna(subset=['raw_type']).groupby('trip_id')['raw_type'].first().to_dict()

                        st['dep_min'] = st['departure_time'].apply(to_minutes)
                        st['stop_sequence'] = st['stop_sequence'].astype(int)
                        st['trip_id'] = op_id + "_" + st['trip_id'].str.strip()
                        st['operator_id'] = op_id
                        st['stop_id'] = st['stop_id'].apply(lambda x: self.stop_map.get((op_id, str(x).strip()), str(x).strip()))
                        st[['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'dep_min', 'operator_id']].to_sql(
                            'stop_times', conn, if_exists='append', index=False
                        )

                    route_type_map = {}
                    if 'trips.txt' in z.namelist():
                        tp = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
                        tp.columns = tp.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        tp['raw_type'] = tp['trip_id'].map(trip_type_map)
                        route_type_map = tp.dropna(subset=['raw_type']).groupby('route_id')['raw_type'].first().to_dict()

                        tp['service_id'] = op_id + "_" + tp['service_id'].str.strip()
                        tp['trip_id'] = op_id + "_" + tp['trip_id'].str.strip()
                        tp['route_id'] = op_id + "_" + tp['route_id'].str.strip()
                        tp['operator_id'] = op_id

                        if active_services:
                            tp = tp[tp['service_id'].isin(active_services)]

                        tp[['trip_id', 'route_id', 'service_id', 'trip_headsign', 'operator_id']].to_sql(
                            'trips', conn, if_exists='append', index=False
                        )

                    if 'routes.txt' in z.namelist():
                        rt = pd.read_csv(z.open('routes.txt'), dtype=str)
                        rt.columns = rt.columns.str.strip().str.replace('\ufeff', '').str.replace('\xa0', '').str.lower()
                        rt['train_type'] = rt['route_id'].map(route_type_map).fillna("Train SNCF")
                        rt['route_id'] = op_id + "_" + rt['route_id'].str.strip()
                        rt['operator_id'] = op_id
                        rt[['route_id', 'operator_id', 'train_type']].to_sql('routes', conn, if_exists='append', index=False)

        logging.info("⚡ Création des index SQL...")
        cursor.executescript("""
            CREATE INDEX idx_stops_uic ON stops(uic);
            CREATE INDEX idx_stops_parent ON stops(parent_name);
            CREATE INDEX idx_st_search ON stop_times(stop_id, dep_min);
            CREATE INDEX idx_st_trip ON stop_times(trip_id, stop_sequence);
            CREATE INDEX idx_calendar_search ON calendar_dates(service_id, date, exception_type);
            CREATE INDEX idx_trips_route ON trips(route_id);
        """)

        conn.execute("VACUUM;")
        conn.execute("ANALYZE;")
        conn.close()
        logging.info("✅ Base SQLite optimisée créée avec succès.")

    def export(self):
        with open(OUTPUT_DB_PATH, 'rb') as f_in:
            with gzip.open(OUTPUT_GZ_PATH, 'wb', compresslevel=9) as f_out:
                shutil.copyfileobj(f_in, f_out)

        raw_size = os.path.getsize(OUTPUT_DB_PATH) / (1024 * 1024)
        gz_size = os.path.getsize(OUTPUT_GZ_PATH) / (1024 * 1024)
        logging.info(f"📊 Taille SQLite brute: {raw_size:.2f} Mo | Compression .gz: {gz_size:.2f} Mo")

        with open(REPORT_PATH, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    harmonizer = GTFSHarmonizer()
    harmonizer.load_stations_reference()
    harmonizer.fetch_stops()
    harmonizer.process_and_deduplicate()
    harmonizer.build_sqlite()
    harmonizer.export()