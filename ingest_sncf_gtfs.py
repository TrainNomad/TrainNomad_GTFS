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
from typing import Dict, List, Tuple
import pandas as pd
import requests
import unicodedata
# --- CONFIGURATION LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
OUTPUT_DB_PATH = os.path.join(BASE_DIR, "harmonized_gtfs.db")
OUTPUT_GZ_PATH = os.path.join(BASE_DIR, "harmonized_gtfs.db.gz")
REPORT_PATH = os.path.join(BASE_DIR, "harmonization_report.json")

# --- OUTILS DE NORMALISATION ET CALCUL ---

def normalize_string(s: str) -> str:
    """Normalise une chaîne (minuscules, sans accent, caractères alphnumériques uniquement)."""
    if not isinstance(s, str):
        return ""
    # Supprime les accents via la décomposition NFD
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn').lower()
    s = re.sub(r'[^a-z0-9]', ' ', s)
    return ' '.join(s.split())

def extract_uic(stop_id: str) -> str:
    """Extrait le code UIC à 7 ou 8 chiffres s'il est présent dans le stop_id."""
    if not isinstance(stop_id, str):
        return ""
    match = re.search(r'(\d{7,8})', stop_id)
    return match.group(1) if match else ""

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance en mètres entre deux points GPS (formule de Haversine)."""
    if any(v is None or math.isnan(v) for v in [lat1, lon1, lat2, lon2]):
        return float('inf')
    R = 6371000.0  # Rayon de la Terre en mètres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

# --- LOGIQUE D'HARMONISATION ---

class GTFSHarmonizer:
    def __init__(self, config_file: str):
        with open(config_file, "r", encoding="utf-8") as f:
            self.operators = json.load(f)
        
        self.raw_stops = []
        self.stop_map = {}  # (op_id, raw_stop_id) -> canonical_stop_id
        self.canonical_stops = {} # canonical_id -> dict data
        self.report_stats = {
            "total_stops_processed": 0,
            "unique_canonical_stops": 0,
            "merges_by_uic": 0,
            "merges_by_geoloc": 0,
            "operators": []
        }

    def fetch_and_extract_all_stops(self):
        """Phase 1 : Extraction de tous les stops de tous les opérateurs activés."""
        logging.info("📥 Extraction des gares à partir des sources GTFS...")
        for op in self.operators:
            op_id = op["id"]
            if not op.get("enabled", True):
                continue

            url = op.get("gtfs_url")
            if not url:
                continue

            logging.info(f" -> Téléchargement GTFS pour [{op_id}]...")
            res = requests.get(url, timeout=120)
            res.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                if 'stops.txt' in z.namelist():
                    df = pd.read_csv(z.open('stops.txt'), dtype=str)
                    for _, row in df.iterrows():
                        raw_id = str(row['stop_id'])
                        uic = extract_uic(raw_id)
                        lat = float(row['stop_lat']) if pd.notnull(row.get('stop_lat')) else None
                        lon = float(row['stop_lon']) if pd.notnull(row.get('stop_lon')) else None
                        
                        self.raw_stops.append({
                            'operator_id': op_id,
                            'raw_stop_id': raw_id,
                            'stop_name': str(row['stop_name']),
                            'stop_lat': lat,
                            'stop_lon': lon,
                            'uic': uic,
                            'norm_name': normalize_string(str(row['stop_name']))
                        })
            self.report_stats["operators"].append(op_id)
        
        self.report_stats["total_stops_processed"] = len(self.raw_stops)
        logging.info(f"✅ {len(self.raw_stops)} arrêts bruts chargés au total.")

    def deduplicate_stops(self):
        """Phase 2 : Harmonisation, déduplication et attribution de clés canoniques."""
        logging.info("⚡ Déduplication des gares et fusion inter-opérateurs...")
        
        uic_index: Dict[str, str] = {} # uic -> canonical_id
        
        for stop in self.raw_stops:
            op_id = stop['operator_id']
            raw_id = stop['raw_stop_id']
            uic = stop['uic']
            lat, lon = stop['stop_lat'], stop['stop_lon']
            norm_name = stop['norm_name']
            
            matched_canonical_id = None
            merge_reason = None
            confidence = 1.0

            # 1. Matching par Code UIC exact
            if uic and uic in uic_index:
                matched_canonical_id = uic_index[uic]
                merge_reason = "UIC_MATCH"
                self.report_stats["merges_by_uic"] += 1
            
            # 2. Matching par géolocalisation (< 100m) + nom normalisé similaire
            if not matched_canonical_id and lat is not None and lon is not None:
                for c_id, c_stop in self.canonical_stops.items():
                    dist = haversine_distance(lat, lon, c_stop['stop_lat'], c_stop['stop_lon'])
                    if dist <= 100.0 and (norm_name == c_stop['norm_name'] or norm_name in c_stop['norm_name'] or c_stop['norm_name'] in norm_name):
                        matched_canonical_id = c_id
                        merge_reason = "GEOLOC_NAME_MATCH"
                        confidence = round(max(0.5, 1.0 - (dist / 200.0)), 2)
                        self.report_stats["merges_by_geoloc"] += 1
                        break

            # 3. Création d'une nouvelle gare canonique si aucune correspondance
            if not matched_canonical_id:
                if uic:
                    matched_canonical_id = f"CANONICAL_UIC_{uic}"
                else:
                    matched_canonical_id = f"CANONICAL_STOP_{len(self.canonical_stops) + 1:06d}"

                self.canonical_stops[matched_canonical_id] = {
                    "stop_id": matched_canonical_id,
                    "stop_name": stop['stop_name'],
                    "stop_lat": lat,
                    "stop_lon": lon,
                    "uic": uic,
                    "source_stop_ids": [],
                    "companies": set(),
                    "norm_name": norm_name,
                    "merge_confidence": confidence,
                    "is_merged": False
                }
                if uic:
                    uic_index[uic] = matched_canonical_id

            # Mise à jour des informations de la gare canonique retenue
            target = self.canonical_stops[matched_canonical_id]
            target["source_stop_ids"].append(f"{op_id}:{raw_id}")
            target["companies"].add(op_id)
            if len(target["source_stop_ids"]) > 1:
                target["is_merged"] = True

            # Mapping pour réécriture des IDs enfants
            self.stop_map[(op_id, raw_id)] = matched_canonical_id

        self.report_stats["unique_canonical_stops"] = len(self.canonical_stops)
        logging.info(f"✅ Gares uniques générées : {len(self.canonical_stops)} (Réduction de {len(self.raw_stops) - len(self.canonical_stops)} doublons).")

    def build_database(self):
        """Phase 3 : Écriture de la base SQLite finale et réécriture des références."""
        logging.info("💾 Écriture dans la base SQLite harmonisée...")
        if os.path.exists(OUTPUT_DB_PATH):
            os.remove(OUTPUT_DB_PATH)

        conn = sqlite3.connect(OUTPUT_DB_PATH)
        cursor = conn.cursor()

        # Schéma optimisé
        cursor.executescript("""
            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                stop_name TEXT,
                stop_lat REAL,
                stop_lon REAL,
                uic TEXT,
                companies TEXT,
                source_stop_ids TEXT,
                is_merged INTEGER,
                merge_confidence REAL
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

        # Insertion des gares harmonisées
        stops_rows = []
        for c_id, s in self.canonical_stops.items():
            stops_rows.append((
                c_id,
                s["stop_name"],
                s["stop_lat"],
                s["stop_lon"],
                s["uic"],
                ",".join(sorted(list(s["companies"]))), # Liste des compagnies présentes
                json.dumps(s["source_stop_ids"]),
                1 if s["is_merged"] else 0,
                s["merge_confidence"]
            ))

        cursor.executemany("""
            INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, stops_rows)

        # Rechargement et ré-indexation des stop_times, routes, trips
        for op in self.operators:
            op_id = op["id"]
            url = op.get("gtfs_url")
            if not op.get("enabled", True) or not url:
                continue

            res = requests.get(url, timeout=120)
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                # Stop times avec mapping des arrêt canoniques
                if 'stop_times.txt' in z.namelist():
                    st_df = pd.read_csv(z.open('stop_times.txt'), usecols=['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'], dtype=str)
                    st_df['trip_id'] = op_id + "_" + st_df['trip_id']
                    st_df['operator_id'] = op_id
                    
                    # Remplacement de l'ID d'arrêt par la clé canonique
                    st_df['stop_id'] = st_df['stop_id'].apply(lambda x: self.stop_map.get((op_id, str(x)), str(x)))
                    st_df.to_sql('stop_times', conn, if_exists='append', index=False)

                # Routes
                if 'routes.txt' in z.namelist():
                    r_df = pd.read_csv(z.open('routes.txt'), usecols=['route_id'], dtype=str)
                    r_df['route_id'] = op_id + "_" + r_df['route_id']
                    r_df['operator_id'] = op_id
                    r_df['train_type'] = "Train"
                    r_df.to_sql('routes', conn, if_exists='append', index=False)

                # Trips
                if 'trips.txt' in z.namelist():
                    t_df = pd.read_csv(z.open('trips.txt'), usecols=['trip_id', 'route_id', 'service_id', 'trip_headsign'], dtype=str)
                    t_df['trip_id'] = op_id + "_" + t_df['trip_id']
                    t_df['route_id'] = op_id + "_" + t_df['route_id']
                    t_df['service_id'] = op_id + "_" + t_df['service_id']
                    t_df['operator_id'] = op_id
                    t_df.to_sql('trips', conn, if_exists='append', index=False)

        # Indexation finale
        cursor.executescript("""
            CREATE INDEX idx_stops_uic ON stops(uic);
            CREATE INDEX idx_st_stop ON stop_times(stop_id);
            CREATE INDEX idx_st_trip ON stop_times(trip_id);
        """)

        conn.commit()
        conn.close()
        logging.info("✅ Base SQLite générée et indexée.")

    def export_and_compress(self):
        """Phase 4 : Compression final en .gz et sauvegarde du rapport JSON."""
        logging.info("📦 Compression du fichier final...")
        with open(OUTPUT_DB_PATH, 'rb') as f_in:
            with gzip.open(OUTPUT_GZ_PATH, 'wb', compresslevel=9) as f_out:
                shutil.copyfileobj(f_in, f_out)

        with open(REPORT_PATH, 'w', encoding='utf-8') as f:
            json.dump(self.report_stats, f, indent=2, ensure_ascii=False)

        logging.info(f"✨ Procédure achevée. Base enregistrée dans {OUTPUT_GZ_PATH}")

# --- EXECUTION ---

if __name__ == '__main__':
    harmonizer = GTFSHarmonizer(OPERATORS_FILE)
    harmonizer.fetch_and_extract_all_stops()
    harmonizer.deduplicate_stops()
    harmonizer.build_database()
    harmonizer.export_and_compress()