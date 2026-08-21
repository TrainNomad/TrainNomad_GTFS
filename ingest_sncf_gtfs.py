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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
OUTPUT_DB_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db")
OUTPUT_GZ_PATH = os.path.join(BASE_DIR, "gtfs_indexed.db.gz")


def time_to_minutes(time_str: str) -> int:
    if not isinstance(time_str, str) or not time_str:
        return 0
    try:
        parts = time_str.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 0


def extract_uic_numeric(val: str) -> int:
    if not isinstance(val, str):
        return 0
    m = re.search(r'(\d{7,8})', val)
    return int(m.group(1)) if m else 0


def build_denormalized_db():
    if os.path.exists(OUTPUT_DB_PATH):
        os.remove(OUTPUT_DB_PATH)

    conn = sqlite3.connect(OUTPUT_DB_PATH)
    cursor = conn.cursor()

    # Création exacte du schéma de votre .db
    cursor.executescript("""
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            origin_id INTEGER NOT NULL,
            origin_parent_id INTEGER NOT NULL,
            origin_name TEXT NOT NULL,
            origin_parent_name TEXT NOT NULL,
            origin_lat REAL,
            origin_lon REAL,
            destination_id INTEGER NOT NULL,
            destination_parent_id INTEGER NOT NULL,
            destination_name TEXT NOT NULL,
            destination_parent_name TEXT NOT NULL,
            dest_lat REAL,
            dest_lon REAL,
            departure_time TEXT NOT NULL,
            arrival_time TEXT NOT NULL,
            dep_min INTEGER NOT NULL,
            arr_min INTEGER NOT NULL,
            train_no TEXT,
            train_type TEXT NOT NULL
        );
    """)

    with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
        operators = json.load(f)

    for op in operators:
        op_id = op["id"]
        if not op.get("enabled", True) or not op.get("gtfs_url"):
            continue

        logging.info(f"📥 Traitement GTFS pour {op_id}...")
        res = requests.get(op["gtfs_url"], timeout=120)
        
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            stops_df = pd.read_csv(z.open('stops.txt'), dtype=str)
            stops_df['uic'] = stops_df['stop_id'].apply(extract_uic_numeric)
            stops_dict = {}
            for _, row in stops_df.iterrows():
                stops_dict[row['stop_id']] = {
                    "id": extract_uic_numeric(row['stop_id']),
                    "name": str(row.get('stop_name', '')),
                    "lat": float(row['stop_lat']) if pd.notnull(row.get('stop_lat')) else 0.0,
                    "lon": float(row['stop_lon']) if pd.notnull(row.get('stop_lon')) else 0.0,
                }

            cd_df = pd.read_csv(z.open('calendar_dates.txt'), dtype=str)
            cd_df = cd_df[cd_df['exception_type'] == '1']
            service_dates = cd_df.groupby('service_id')['date'].apply(list).to_dict()

            trips_df = pd.read_csv(z.open('trips.txt'), dtype=str)
            trips_dict = trips_df.set_index('trip_id').to_dict('index')

            st_df = pd.read_csv(z.open('stop_times.txt'), dtype=str)
            st_df['stop_sequence'] = st_df['stop_sequence'].astype(int)
            st_df['dep_min'] = st_df['departure_time'].apply(time_to_minutes)
            st_df['arr_min'] = st_df['arrival_time'].apply(time_to_minutes)

            grouped = st_df.groupby('trip_id')
            records = []

            for trip_id, group in grouped:
                if trip_id not in trips_dict:
                    continue
                
                t_info = trips_dict[trip_id]
                service_id = t_info['service_id']
                dates = service_dates.get(service_id, [])
                if not dates:
                    continue

                train_no = t_info.get('trip_headsign', '')
                train_type = "TGV InOui" if "TGV" in str(trip_id) else ("Eurostar" if op_id.upper() == "EUROSTAR" else "TER")

                group = group.sort_values('stop_sequence').to_dict('records')
                n = len(group)

                for i in range(n):
                    s1 = group[i]
                    st1_info = stops_dict.get(s1['stop_id'])
                    if not st1_info:
                        continue

                    for j in range(i + 1, n):
                        s2 = group[j]
                        st2_info = stops_dict.get(s2['stop_id'])
                        if not st2_info:
                            continue

                        # Formater la date en YYYY-MM-DD
                        for d in dates:
                            formatted_date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
                            records.append((
                                formatted_date,
                                st1_info['id'], st1_info['id'], st1_info['name'], st1_info['name'], st1_info['lat'], st1_info['lon'],
                                st2_info['id'], st2_info['id'], st2_info['name'], st2_info['name'], st2_info['lat'], st2_info['lon'],
                                s1['departure_time'], s2['arrival_time'], s1['dep_min'], s2['arr_min'],
                                train_no, train_type
                            ))

            cursor.executemany("""
                INSERT INTO trips (
                    date, origin_id, origin_parent_id, origin_name, origin_parent_name, origin_lat, origin_lon,
                    destination_id, destination_parent_id, destination_name, destination_parent_name, dest_lat, dest_lon,
                    departure_time, arrival_time, dep_min, arr_min, train_no, train_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)

    # Création des index exacts de la spec
    cursor.executescript("""
        CREATE INDEX idx_search_direct ON trips(date, origin_parent_id, destination_parent_id, dep_min);
        CREATE INDEX idx_search_transfer ON trips(date, origin_parent_id, dep_min, arr_min);
    """)

    conn.commit()
    conn.close()

    with open(OUTPUT_DB_PATH, 'rb') as f_in:
        with gzip.open(OUTPUT_GZ_PATH, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

    logging.info("✅ Génération de la base 'trips' terminée avec succès.")


if __name__ == '__main__':
    build_denormalized_db()