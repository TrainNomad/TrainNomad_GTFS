"""
Patch pour ajouter la liaison Elvas - Badajoz au GTFS CP.

Cette liaison transfrontalière Portugal-Espagne n'est pas dans le GTFS officiel CP.
Les horaires sont extraits du PDF publié par CP.

Source : https://www.cp.pt/info/documents/d/cp/comboios-regionais-linha-leste-badajoz

Usage :
    python CP/patch_elvas_badajoz.py                    # Patch le GTFS CP existant
    python CP/patch_elvas_badajoz.py --input cp.zip    # Spécifier le fichier source
"""
import argparse
import csv
import io
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta

# Essayer d'importer pdfplumber pour parser le PDF
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
CP_GTFS = os.path.join(BASE_DIR, "cp_gtfs.zip") if os.path.exists(os.path.join(BASE_DIR, "cp_gtfs.zip")) else os.path.join(GTFS_DIR, "feeds", "CP.zip")
OUTPUT_GTFS = os.path.join(BASE_DIR, "cp_gtfs_patched.zip")

PDF_URL = "https://www.cp.pt/info/documents/d/cp/comboios-regionais-linha-leste-badajoz"

# Gare de Badajoz (Espagne) - données basées sur UIC espagnol
# UIC Badajoz = 7137606, on utilise le format CP : 71_37606
BADAJOZ_STOP = {
    "stop_id": "71_37606",  # Format CP avec UIC espagnol
    "stop_code": "7137606",  # Code UIC Badajoz (Espagne = 71)
    "stop_name": "Badajoz",
    "stop_lat": "38.890867",
    "stop_lon": "-6.981822",
    "location_type": "0",
    "parent_station": "",
}

# Gare d'Elvas (Portugal) - UIC 9457497, cp_id dans stations.csv
ELVAS_STOP_ID = "94_57497"  # Format CP vérifié dans stations.csv

# Horaires extraits manuellement du PDF (à mettre à jour si le PDF change)
# Format: [(train_number, [(stop_id, arrival, departure), ...]), ...]
# Les horaires sont en format HH:MM
TIMETABLE_ELVAS_BADAJOZ = [
    # Trains direction Badajoz (Elvas -> Badajoz)
    ("4521", [("94_57497", None, "06:35"), ("71_37606", "06:50", None)]),
    ("4523", [("94_57497", None, "08:55"), ("71_37606", "09:10", None)]),
    ("4525", [("94_57497", None, "12:55"), ("71_37606", "13:10", None)]),
    ("4527", [("94_57497", None, "17:25"), ("71_37606", "17:40", None)]),
    ("4529", [("94_57497", None, "19:35"), ("71_37606", "19:50", None)]),

    # Trains direction Elvas (Badajoz -> Elvas)
    ("4520", [("71_37606", None, "07:00"), ("94_57497", "07:15", None)]),
    ("4522", [("94_57497", None, "09:20"), ("71_37606", "09:35", None)]),
    ("4524", [("71_37606", None, "13:20"), ("94_57497", "13:35", None)]),
    ("4526", [("71_37606", None, "17:50"), ("94_57497", "18:05", None)]),
    ("4528", [("71_37606", None, "20:00"), ("94_57497", "20:15", None)]),
]

# Service : tous les jours sauf exceptions
# À adapter selon le calendrier réel
SERVICE_ID = "ELVAS_BADAJOZ_DAILY"


def download_pdf(url: str, dest: str) -> bool:
    """Télécharge le PDF des horaires."""
    print(f"📥 Téléchargement du PDF : {url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        print(f"   {os.path.getsize(dest) / 1024:.1f} Ko téléchargés")
        return True
    except Exception as e:
        print(f"   ❌ Erreur : {e}")
        return False


def parse_pdf_timetable(pdf_path: str) -> list:
    """Parse le PDF pour extraire les horaires (nécessite pdfplumber)."""
    if not HAS_PDFPLUMBER:
        print("   ⚠️ pdfplumber non installé, utilisation des horaires codés en dur")
        return TIMETABLE_ELVAS_BADAJOZ

    print(f"📄 Parsing du PDF...")
    try:
        trains = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    # Parser les tableaux d'horaires
                    # Structure typique : lignes avec heures
                    for row in table:
                        # Chercher des patterns d'horaires (HH:MM)
                        times = [cell for cell in row if cell and re.match(r"\d{1,2}[:\.]?\d{2}", str(cell))]
                        if len(times) >= 2:
                            print(f"   Trouvé : {times}")

        if not trains:
            print("   ⚠️ Parsing automatique incomplet, utilisation des horaires codés en dur")
            return TIMETABLE_ELVAS_BADAJOZ

        return trains
    except Exception as e:
        print(f"   ❌ Erreur parsing PDF : {e}")
        return TIMETABLE_ELVAS_BADAJOZ


def format_time(t: str) -> str:
    """Formate l'heure en HH:MM:SS pour GTFS."""
    if not t:
        return ""
    t = t.replace(".", ":")
    parts = t.split(":")
    h, m = int(parts[0]), int(parts[1])
    return f"{h:02d}:{m:02d}:00"


def patch_gtfs(input_zip: str, output_zip: str, timetable: list):
    """Patche le GTFS CP avec la liaison Elvas-Badajoz."""
    print(f"\n📦 Patch du GTFS : {input_zip}")

    if not os.path.exists(input_zip):
        print(f"   ❌ Fichier introuvable : {input_zip}")
        return False

    # Lire le GTFS existant
    with zipfile.ZipFile(input_zip, "r") as zin:
        files = {name: zin.read(name) for name in zin.namelist()}

    # 1. Ajouter Badajoz à stops.txt
    stops_content = files.get("stops.txt", b"").decode("utf-8-sig")
    stops_lines = stops_content.strip().split("\n")
    stops_header = stops_lines[0] if stops_lines else ""

    # Vérifier si Badajoz est déjà présent
    badajoz_exists = any("Badajoz" in line or BADAJOZ_STOP["stop_id"] in line for line in stops_lines)

    if not badajoz_exists:
        # Construire la ligne Badajoz selon le header existant
        header_cols = stops_header.split(",")
        badajoz_row = []
        for col in header_cols:
            col = col.strip().lower()
            if col in BADAJOZ_STOP:
                badajoz_row.append(BADAJOZ_STOP[col])
            else:
                badajoz_row.append("")

        stops_lines.append(",".join(badajoz_row))
        files["stops.txt"] = "\n".join(stops_lines).encode("utf-8")
        print(f"   ✅ Badajoz ajouté à stops.txt")
    else:
        print(f"   ℹ️ Badajoz déjà présent dans stops.txt")

    # 2. Vérifier/Ajouter le service dans calendar_dates.txt ou calendar.txt
    # Pour simplifier, on utilise calendar_dates avec les prochains 90 jours
    calendar_dates_content = files.get("calendar_dates.txt", b"").decode("utf-8-sig")
    calendar_dates_lines = calendar_dates_content.strip().split("\n") if calendar_dates_content else ["service_id,date,exception_type"]

    # Vérifier si notre service existe
    service_exists = any(SERVICE_ID in line for line in calendar_dates_lines)

    if not service_exists:
        # Ajouter 90 jours de service
        today = datetime.now()
        for i in range(90):
            date = today + timedelta(days=i)
            date_str = date.strftime("%Y%m%d")
            calendar_dates_lines.append(f"{SERVICE_ID},{date_str},1")

        files["calendar_dates.txt"] = "\n".join(calendar_dates_lines).encode("utf-8")
        print(f"   ✅ Service {SERVICE_ID} ajouté (90 jours)")
    else:
        print(f"   ℹ️ Service {SERVICE_ID} déjà présent")

    # 3. Ajouter la route si nécessaire
    routes_content = files.get("routes.txt", b"").decode("utf-8-sig")
    routes_lines = routes_content.strip().split("\n")

    route_id = "ELVAS_BADAJOZ"
    route_exists = any(route_id in line for line in routes_lines)

    if not route_exists:
        # Ajouter la route
        routes_header = routes_lines[0] if routes_lines else "route_id,agency_id,route_short_name,route_long_name,route_type"
        header_cols = routes_header.split(",")

        route_data = {
            "route_id": route_id,
            "agency_id": "CP",
            "route_short_name": "R",
            "route_long_name": "Regional Elvas - Badajoz",
            "route_type": "2",  # Rail
            "route_color": "1E90FF",
            "route_text_color": "FFFFFF",
        }

        route_row = []
        for col in header_cols:
            col = col.strip().lower()
            route_row.append(route_data.get(col, ""))

        routes_lines.append(",".join(route_row))
        files["routes.txt"] = "\n".join(routes_lines).encode("utf-8")
        print(f"   ✅ Route {route_id} ajoutée")
    else:
        print(f"   ℹ️ Route {route_id} déjà présente")

    # 4. Ajouter les trips
    trips_content = files.get("trips.txt", b"").decode("utf-8-sig")
    trips_lines = trips_content.strip().split("\n")
    trips_header = trips_lines[0] if trips_lines else "route_id,service_id,trip_id,trip_headsign,direction_id"
    header_cols = trips_header.split(",")

    existing_trip_ids = {line.split(",")[header_cols.index("trip_id") if "trip_id" in header_cols else 2]
                         for line in trips_lines[1:] if line}

    new_trips = []
    for train_num, stops in timetable:
        trip_id = f"EB_{train_num}"
        if trip_id not in existing_trip_ids:
            # Déterminer la direction (0 = vers Badajoz, 1 = vers Elvas)
            first_stop = stops[0][0]
            direction = "0" if first_stop == ELVAS_STOP_ID else "1"
            headsign = "Badajoz" if direction == "0" else "Elvas"

            trip_data = {
                "route_id": route_id,
                "service_id": SERVICE_ID,
                "trip_id": trip_id,
                "trip_headsign": headsign,
                "direction_id": direction,
                "trip_short_name": train_num,
            }

            trip_row = []
            for col in header_cols:
                col = col.strip().lower()
                trip_row.append(trip_data.get(col, ""))

            new_trips.append(",".join(trip_row))

    if new_trips:
        trips_lines.extend(new_trips)
        files["trips.txt"] = "\n".join(trips_lines).encode("utf-8")
        print(f"   ✅ {len(new_trips)} trips ajoutés")

    # 5. Ajouter les stop_times
    stop_times_content = files.get("stop_times.txt", b"").decode("utf-8-sig")
    stop_times_lines = stop_times_content.strip().split("\n")
    stop_times_header = stop_times_lines[0] if stop_times_lines else "trip_id,arrival_time,departure_time,stop_id,stop_sequence"
    header_cols = stop_times_header.split(",")

    new_stop_times = []
    for train_num, stops in timetable:
        trip_id = f"EB_{train_num}"
        for seq, (stop_id, arr, dep) in enumerate(stops, 1):
            arr_time = format_time(arr) if arr else format_time(dep)
            dep_time = format_time(dep) if dep else format_time(arr)

            st_data = {
                "trip_id": trip_id,
                "arrival_time": arr_time,
                "departure_time": dep_time,
                "stop_id": stop_id,
                "stop_sequence": str(seq),
                "pickup_type": "0",
                "drop_off_type": "0",
            }

            st_row = []
            for col in header_cols:
                col = col.strip().lower()
                st_row.append(st_data.get(col, ""))

            new_stop_times.append(",".join(st_row))

    if new_stop_times:
        stop_times_lines.extend(new_stop_times)
        files["stop_times.txt"] = "\n".join(stop_times_lines).encode("utf-8")
        print(f"   ✅ {len(new_stop_times)} stop_times ajoutés")

    # 6. Écrire le GTFS patché
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, content in files.items():
            zout.writestr(name, content)

    print(f"\n✅ GTFS patché écrit : {output_zip}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Patch GTFS CP avec liaison Elvas-Badajoz")
    parser.add_argument("--input", default=CP_GTFS, help="GTFS CP source")
    parser.add_argument("--output", default=OUTPUT_GTFS, help="GTFS patché")
    parser.add_argument("--pdf", help="PDF des horaires (optionnel, sinon téléchargé)")
    parser.add_argument("--update-timetable", action="store_true",
                        help="Tente de parser le PDF pour mettre à jour les horaires")
    args = parser.parse_args()

    print("=" * 60)
    print("🚂 Patch GTFS CP : Liaison Elvas - Badajoz")
    print("=" * 60)

    timetable = TIMETABLE_ELVAS_BADAJOZ

    # Optionnel : parser le PDF pour mettre à jour les horaires
    if args.update_timetable:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = args.pdf if args.pdf else os.path.join(tmp_dir, "horaires.pdf")
            if not args.pdf:
                if download_pdf(PDF_URL, pdf_path):
                    timetable = parse_pdf_timetable(pdf_path)
            else:
                timetable = parse_pdf_timetable(pdf_path)

    # Patcher le GTFS
    if patch_gtfs(args.input, args.output, timetable):
        print("\n💡 Pour utiliser le GTFS patché :")
        print(f"   mv {args.output} {args.input}")

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()