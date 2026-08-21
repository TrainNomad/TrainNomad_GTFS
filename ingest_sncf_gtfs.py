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
OUTPUT_DB_PATH = os.path.join(BASE_DIR, "harmonized_gtfs.db")
OUTPUT_GZ_PATH = os.path.join(BASE_DIR, "harmonized_gtfs.db.gz")
REPORT_PATH = os.path.join(BASE_DIR, "harmonization_report.json")


def normalize_string(s: str) -> str:
    """Normalise une chaîne en minuscules sans accents ni mots vides."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn').lower()
    s = re.sub(r'[^a-z0-9]', ' ', s)
    stop_words = {'gare', 'de', 'du', 'des', 'la', 'le', 'l', 'saint', 'st'}
    words = [w for w in s.split() if w not in stop_words]
    return ' '.join(words)


def extract_uic(val: str) -> str:
    """Extrait la suite de 7 ou 8 chiffres UIC à partir d'un string (stop_id ou stop_code)."""
    if not isinstance(val, str):
        return ""
    m = re.search(r'(\d{7,8})', val)
    return m.group(1) if m else ""


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance en mètres entre deux coordonnées GPS."""
    if any(v is None or math.isnan(v) for v in [lat1, lon1, lat2, lon2]):
        return float('inf')
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def determine_train_type_eurostar(row) -> str:
    """Détermine le type de train Eurostar via la colonne agency_id."""
    agency_id = str(row.get("agency_id", "")).strip().upper() if pd.notnull(row.get("agency_id")) else ""
    
    if "THALYS" in agency_id:
        return "Eurostar (ex-Thalys)"
    elif "EUROSTAR" in agency_id:
        return "Eurostar"
    
    return "Eurostar"


def determine_train_type_sncf(row) -> str:
    """Détermine le type de train SNCF via le route_id, route_long_name ou route_short_name."""
    route_id = str(row.get("route_id", "")).upper() if pd.notnull(row.get("route_id")) else ""
    long_name = str(row.get("route_long_name", "")).upper() if pd.notnull(row.get("route_long_name")) else ""
    short_name = str(row.get("route_short_name", "")).upper() if pd.notnull(row.get("route_short_name")) else ""
    
    full_text = f"{route_id} {short_name} {long_name}"

    if "TGV INOUI" in full_text or "INOUI" in full_text or "TGV" in full_text:
        return "TGV InOui"
    elif "OUIGO" in full_text:
        return "OUIGO"
    elif "TER" in full_text:
        return "TER"
    elif "INTERCITES" in full_text or "INTERCITÉS" in full_text:
        return "Intercités"
    elif "ICE" in full_text:
        return "ICE"
    elif "TRANSILIEN" in full_text or "RER" in full_text:
        return "Transilien"
    
    return "Train SNCF"


class GTFSHarmonizer:
    def __init__(self):
        with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
            self.operators = json.load(f)
        
        self.stations_reference = {}  # uic_clean -> station_info
        self.raw_stops = []
        self.stop_map = {}            # (operator_id, raw_stop_id) -> canonical_stop_id
        self.canonical_stops = {}     # canonical_id -> dict
        
        self.stats = {
            "total_raw_stops": 0,
            "unique_canonical_stops": 0,
            "merges_uic": 0,
            "merges_geoloc": 0
        }

    def load_stations_reference(self):
        """Charge le fichier stations.csv pour en faire la référence officielle des noms de gares."""
        if not os.path.exists(STATIONS_CSV):
            logging.warning(f"⚠️ Fichier {STATIONS_CSV} non trouvé. Les noms GTFS bruts seront utilisés par défaut.")
            return

        logging.info("📖 Chargement du fichier de référence stations.csv...")
        df = pd.read_csv(STATIONS_CSV, sep=';', dtype=str)
        
        for _, row in df.iterrows():
            name = row.get('name')
            if not isinstance(name, str) or not name.strip():
                continue

            uic7 = str(row.get('uic', '')).strip() if pd.notnull(row.get('uic')) else ''
            uic8 = str(row.get('uic8_sncf', '')).strip() if pd.notnull(row.get('uic8_sncf')) else ''
            lat = float(row['latitude']) if pd.notnull(row.get('latitude')) else None
            lon = float(row['longitude']) if pd.notnull(row.get('longitude')) else None

            info = {'name': name, 'lat': lat, 'lon': lon, 'uic7': uic7, 'uic8': uic8}

            if uic7:
                self.stations_reference[uic7] = info
                if len(uic7) == 7:
                    self.stations_reference["8" + uic7] = info
            if uic8:
                self.stations_reference[uic8] = info
                if len(uic8) == 8 and uic8.startswith("8"):
                    self.stations_reference[uic8[1:]] = info

        logging.info(f"✅ {len(self.stations_reference)} clés UIC de référence chargées.")

    def fetch_stops(self):
        """Phase 1 : Extraction des gares GTFS (SNCF, Eurostar, etc.)."""
        logging.info("📥 Extraction des gares depuis les GTFS...")
        for op in self.operators:
            op_id = op["id"]
            if not op.get("enabled", True) or not op.get("gtfs_url"):
                continue

            logging.info(f" -> Téléchargement pour [{op_id}]...")
            res = requests.get(op["gtfs_url"], timeout=120)
            res.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                if 'stops.txt' in z.namelist():
                    df = pd.read_csv(z.open('stops.txt'), dtype=str)
                    for _, row in df.iterrows():
                        raw_id = str(row['stop_id'])
                        
                        # Extraction du code UIC depuis stop_code (Eurostar) ou stop_id (SNCF)
                        stop_code = str(row.get('stop_code', '')) if pd.notnull(row.get('stop_code')) else ''
                        uic = extract_uic(stop_code) or extract_uic(raw_id)

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
        logging.info(f"✅ {len(self.raw_stops)} arrêts bruts extraits.")

    def process_and_deduplicate(self):
        """Phase 2 : Déduplication et construction de la table canonique."""
        logging.info("⚡ Déduplication des gares et harmonisation des noms...")
        uic_index = {}  # uic -> canonical_id

        for stop in self.raw_stops:
            op_id = stop['operator_id']
            raw_id = stop['raw_stop_id']
            uic = stop['uic']
            lat, lon = stop['stop_lat'], stop['stop_lon']
            norm_name = stop['norm_name']

            matched_id = None

            # 1. Matching par UIC
            if uic and uic in uic_index:
                matched_id = uic_index[uic]
                self.stats["merges_uic"] += 1

            # 2. Matching par Géolocalisation (< 100m) + Nom similaire
            if not matched_id and lat is not None and lon is not None:
                for c_id, c_stop in self.canonical_stops.items():
                    dist = haversine_distance(lat, lon, c_stop['stop_lat'], c_stop['stop_lon'])
                    if dist <= 100.0 and (norm_name == c_stop['norm_name'] or norm_name in c_stop['norm_name'] or c_stop['norm_name'] in norm_name):
                        matched_id = c_id
                        self.stats["merges_geoloc"] += 1
                        break

            # 3. Création si nouvelle gare
            if not matched_id:
                matched_id = f"CANONICAL_UIC_{uic}" if uic else f"CANONICAL_STOP_{len(self.canonical_stops) + 1:06d}"
                
                # Nom officiel provenant en priorité de stations.csv
                official_name = stop['raw_name']
                if uic and uic in self.stations_reference:
                    official_name = self.stations_reference[uic]['name']

                self.canonical_stops[matched_id] = {
                    "stop_id": matched_id,
                    "stop_name": official_name,
                    "stop_lat": lat,
                    "stop_lon": lon,
                    "uic": uic,
                    "norm_name": norm_name,
                    "sncf": 0,
                    "eurostar": 0,
                    "source_stop_ids": []
                }
                if uic:
                    uic_index[uic] = matched_id

            # Mise à jour des indicateurs de compagnies (1 ou 0)
            target = self.canonical_stops[matched_id]
            if op_id.upper() == "SNCF":
                target["sncf"] = 1
            elif op_id.upper() == "EUROSTAR":
                target["eurostar"] = 1

            target["source_stop_ids"].append(f"{op_id}:{raw_id}")
            self.stop_map[(op_id, raw_id)] = matched_id

        self.stats["unique_canonical_stops"] = len(self.canonical_stops)
        logging.info(f"✅ Nombre de gares uniques : {len(self.canonical_stops)}")

    def build_sqlite(self):
        """Phase 3 : Écriture dans SQLite."""
        logging.info("💾 Génération de la base SQLite...")
        if os.path.exists(OUTPUT_DB_PATH):
            os.remove(OUTPUT_DB_PATH)

        conn = sqlite3.connect(OUTPUT_DB_PATH)
        cursor = conn.cursor()

        cursor.executescript("""
            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                stop_name TEXT,
                stop_lat REAL,
                stop_lon REAL,
                uic TEXT,
                sncf INTEGER DEFAULT 0,
                eurostar INTEGER DEFAULT 0,
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
                operator_id TEXT
            );
        """)

        # Insertion dans la table stops
        stops_rows = [
            (
                s["stop_id"],
                s["stop_name"],
                s["stop_lat"],
                s["stop_lon"],
                s["uic"],
                s["sncf"],
                s["eurostar"],
                json.dumps(s["source_stop_ids"])
            )
            for s in self.canonical_stops.values()
        ]

        cursor.executemany("INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?)", stops_rows)

        # Re-remplissage des tables liées avec réécriture des IDs
        for op in self.operators:
            op_id = op["id"]
            if not op.get("enabled", True) or not op.get("gtfs_url"):
                continue

            res = requests.get(op["gtfs_url"], timeout=120)
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                # 1. Stop Times
                if 'stop_times.txt' in z.namelist():
                    st = pd.read_csv(z.open('stop_times.txt'), usecols=['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'], dtype=str)
                    st['trip_id'] = op_id + "_" + st['trip_id']
                    st['operator_id'] = op_id
                    st['stop_id'] = st['stop_id'].apply(lambda x: self.stop_map.get((op_id, str(x)), str(x)))
                    st.to_sql('stop_times', conn, if_exists='append', index=False)

                # 2. Routes (Détermination dynamique du type de train)
                if 'routes.txt' in z.namelist():
                    rt = pd.read_csv(z.open('routes.txt'), dtype=str)
                    
                    if op_id.upper() == "EUROSTAR":
                        rt['train_type'] = rt.apply(determine_train_type_eurostar, axis=1)
                    else:
                        rt['train_type'] = rt.apply(determine_train_type_sncf, axis=1)
                        
                    rt['route_id'] = op_id + "_" + rt['route_id']
                    rt['operator_id'] = op_id
                    
                    routes_to_db = rt[['route_id', 'operator_id', 'train_type']]
                    routes_to_db.to_sql('routes', conn, if_exists='append', index=False)

                # 3. Trips
                if 'trips.txt' in z.namelist():
                    tp = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
                    tp['trip_id'] = op_id + "_" + tp['trip_id']
                    tp['route_id'] = op_id + "_" + tp['route_id']
                    tp['service_id'] = op_id + "_" + tp['service_id']
                    tp['operator_id'] = op_id
                    tp.to_sql('trips', conn, if_exists='append', index=False)

        cursor.executescript("""
            CREATE INDEX idx_stops_uic ON stops(uic);
            CREATE INDEX idx_st_stop ON stop_times(stop_id);
            CREATE INDEX idx_st_trip ON stop_times(trip_id);
        """)

        conn.commit()
        conn.close()
        logging.info("✅ Base SQLite créée avec succès.")

    def export(self):
        """Phase 4 : Compression finale et sauvegarde des statistiques."""
        with open(OUTPUT_DB_PATH, 'rb') as f_in:
            with gzip.open(OUTPUT_GZ_PATH, 'wb', compresslevel=9) as f_out:
                shutil.copyfileobj(f_in, f_out)

        with open(REPORT_PATH, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=2, ensure_ascii=False)

        logging.info(f"📦 Archive générée : {OUTPUT_GZ_PATH}")


if __name__ == '__main__':
    harmonizer = GTFSHarmonizer()
    harmonizer.load_stations_reference()
    harmonizer.fetch_stops()
    harmonizer.process_and_deduplicate()
    harmonizer.build_sqlite()
    harmonizer.export()