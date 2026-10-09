"""
Patch du GTFS CP : prolonge jusqu'à Badajoz les Regionais de la Linha do Leste.

Le GTFS officiel CP contient bien les trains Entroncamento <-> Badajoz (481/485 vers Badajoz, 482/486 depuis
Badajoz, route_id « 24-94_34009-71_37606 »), mais leurs horaires s'arrêtent à Elvas et la gare de Badajoz
manque dans stops.txt. Résultat : le moteur proposait une correspondance à Elvas qui n'existe pas.
Ce patch ajoute Badajoz à stops.txt et l'arrêt Badajoz au début / à la fin de ces trains (même trip,
même calendrier que CP : pas de changement de train à Elvas).

Source : https://www.cp.pt/info/documents/d/cp/comboios-regionais-linha-leste-badajoz
(horaire en vigueur depuis le 14/12/2025, version du 20/07/2026) :
    R 481  Entroncamento 09:06 -> Elvas 11:40 -> Badajoz 12:54 (heure espagnole)   quotidien
    R 485  Entroncamento 13:36 -> Elvas 16:16 -> Badajoz 17:30 (heure espagnole)   quotidien
    R 482  Badajoz 14:09 (heure espagnole) -> Elvas 13:25 -> Entroncamento 15:55   quotidien
    R 486  Badajoz 19:41 (heure espagnole) -> Elvas 18:57 -> Entroncamento 21:26   quotidien

Usage :
    python CP/patch_elvas_badajoz.py                   # patche CP/cp_gtfs.zip -> CP/cp_gtfs_patched.zip
    python CP/patch_elvas_badajoz.py --input cp.zip --output out.zip
"""
import argparse
import csv
import io
import os
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CP_GTFS = os.path.join(BASE_DIR, "cp_gtfs.zip")
OUTPUT_GTFS = os.path.join(BASE_DIR, "cp_gtfs_patched.zip")

PDF_URL = "https://www.cp.pt/info/documents/d/cp/comboios-regionais-linha-leste-badajoz"

ELVAS_ID = "94_57497"
BADAJOZ_ID = "71_37606"  # identifiant déjà utilisé par CP dans route_id ; UIC 7137606 = Badajoz dans stations.csv
BADAJOZ_STOP = {
    "stop_id": BADAJOZ_ID,
    "stop_code": "",
    "stop_name": "Badajoz",
    "stop_lat": "38.890867",
    "stop_lon": "-6.981822",
    "location_type": "0",
    "stop_timezone": "Europe/Madrid",
    "wheelchair_boarding": "0",
}

# Horaires à Badajoz du PDF, en heure espagnole (= heure portugaise + 1 h toute l'année).
BADAJOZ_TIMES_ES = {
    "481": "12:54",  # arrivée
    "485": "17:30",  # arrivée
    "482": "14:09",  # départ
    "486": "19:41",  # départ
}
SPAIN_OFFSET_MIN = 60
# Train absent du tableau (nouvel horaire CP) : temps de parcours Elvas <-> Badajoz relevés dans le PDF.
RUN_TO_BADAJOZ_MIN = 14
RUN_FROM_BADAJOZ_MIN = 16


def to_seconds(t: str) -> int:
    h, m, *s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + (s[0] if s else 0)


def to_gtfs(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def badajoz_time_pt(train: str):
    """Heure du PDF convertie en heure portugaise (fuseau de l'agence CP, celui des stop_times)."""
    t = BADAJOZ_TIMES_ES.get(train)
    return to_seconds(t) - SPAIN_OFFSET_MIN * 60 if t else None


def read_csv(files: dict, name: str):
    text = files[name].decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames, list(reader)


def write_csv(files: dict, name: str, header: list, rows: list):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    files[name] = buf.getvalue().encode("utf-8")


def route_ends(route_id: str):
    """route_id CP = « <ligne>-<gare origine>-<gare terminus> » (ex. 24-94_34009-71_37606)."""
    parts = route_id.split("-")
    return (parts[1], parts[-1]) if len(parts) >= 3 else (None, None)


def patch_gtfs(input_zip: str, output_zip: str) -> bool:
    print(f"\n📦 Patch du GTFS : {input_zip}")
    if not os.path.exists(input_zip):
        print(f"   ❌ Fichier introuvable : {input_zip}")
        return False

    with zipfile.ZipFile(input_zip) as zin:
        files = {name: zin.read(name) for name in zin.namelist()}

    # 1. Gare de Badajoz
    stops_header, stops = read_csv(files, "stops.txt")
    if not any(s["stop_id"] == BADAJOZ_ID for s in stops):
        stops.append({c: BADAJOZ_STOP.get(c, "") for c in stops_header})
        write_csv(files, "stops.txt", stops_header, stops)
        print("   ✅ Badajoz ajouté à stops.txt (Europe/Madrid)")

    # 2. Trains dont la route va à / vient de Badajoz
    _, trips = read_csv(files, "trips.txt")
    targets = {}
    for t in trips:
        origin, dest = route_ends(t["route_id"])
        if BADAJOZ_ID in (origin, dest):
            targets[t["trip_id"]] = ("to" if dest == BADAJOZ_ID else "from", t.get("trip_short_name", ""))
    if not targets:
        print("   ⚠️ Aucun train vers/depuis Badajoz dans le GTFS CP : vérifier le PDF", PDF_URL)
        return False

    st_header, stop_times = read_csv(files, "stop_times.txt")
    by_trip = {}
    for r in stop_times:
        if r["trip_id"] in targets:
            by_trip.setdefault(r["trip_id"], []).append(r)

    added = []
    for trip_id, (direction, train) in sorted(targets.items()):
        rows = sorted(by_trip.get(trip_id, []), key=lambda r: int(r["stop_sequence"]))
        if not rows or any(r["stop_id"] == BADAJOZ_ID for r in rows):
            continue  # déjà jusqu'à Badajoz (CP a corrigé son GTFS) ou trip vide
        pdf_time = badajoz_time_pt(train)
        if direction == "to":
            last = rows[-1]
            if last["stop_id"] != ELVAS_ID:
                print(f"   ⚠️ {train} ({trip_id}) ne s'arrête pas à Elvas en dernier, ignoré")
                continue
            t = pdf_time if pdf_time is not None else to_seconds(last["departure_time"]) + RUN_TO_BADAJOZ_MIN * 60
            seq = int(last["stop_sequence"]) + 1
        else:
            first = rows[0]
            if first["stop_id"] != ELVAS_ID:
                print(f"   ⚠️ {train} ({trip_id}) ne part pas d'Elvas, ignoré")
                continue
            t = pdf_time if pdf_time is not None else to_seconds(first["arrival_time"]) - RUN_FROM_BADAJOZ_MIN * 60
            seq = int(first["stop_sequence"]) - 1
            if seq < 0:  # renumérote le trip pour garder des stop_sequence positives
                for r in rows:
                    r["stop_sequence"] = str(int(r["stop_sequence"]) + 1)
                seq = 0
        stop_times.append({c: "" for c in st_header} | {
            "trip_id": trip_id,
            "arrival_time": to_gtfs(t),
            "departure_time": to_gtfs(t),
            "stop_id": BADAJOZ_ID,
            "stop_sequence": str(seq),
            "pickup_type": "1" if direction == "to" else "0",    # terminus : pas de montée
            "drop_off_type": "0" if direction == "to" else "1",  # origine : pas de descente
        })
        es = to_gtfs(t + SPAIN_OFFSET_MIN * 60)[:5]
        src = "PDF" if pdf_time is not None else "estimé"
        added.append(f"{train} {'Elvas → Badajoz' if direction == 'to' else 'Badajoz → Elvas'} {es} heure espagnole ({src})")

    if not added:
        print("   ℹ️ Rien à ajouter")
        return False
    write_csv(files, "stop_times.txt", st_header, stop_times)
    for line in added:
        print(f"   ✅ {line}")

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, content in files.items():
            zout.writestr(name, content)
    print(f"\n✅ GTFS patché écrit : {output_zip}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Prolonge les trains CP de la Linha do Leste jusqu'à Badajoz")
    parser.add_argument("--input", default=CP_GTFS, help="GTFS CP source")
    parser.add_argument("--output", default=OUTPUT_GTFS, help="GTFS patché")
    args = parser.parse_args()
    patch_gtfs(args.input, args.output)


if __name__ == "__main__":
    main()
