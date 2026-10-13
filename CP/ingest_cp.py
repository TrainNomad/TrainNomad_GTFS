"""
Télécharge le GTFS CP, retire la banlieue et prolonge les trains de la Linha do Leste jusqu'à Badajoz.

Le GTFS officiel CP arrête à Elvas les Regionais Entroncamento <-> Badajoz (481/482/485/486).
patch_elvas_badajoz.py ajoute l'arrêt Badajoz d'après le PDF publié par CP.

Banlieue retirée, comme le RER en France : les trains urbains de Lisbonne et de Porto, que le flux
range sous le nom de leur ligne ("Linha de Sintra", "Linha de Braga"...) ou sous la catégorie "U".
Restent : Alfa Pendular (AP), Intercidades (IC), InterRegional (IR), Regional (R).

Usage :
    python CP/ingest_cp.py
"""
import csv
import io
import os
import shutil
import tempfile
import zipfile

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(BASE_DIR, "cp_gtfs.zip")
CP_GTFS_URL = "https://publico.cp.pt/gtfs/gtfs.zip"


def download_cp_gtfs() -> str:
    """Télécharge le GTFS officiel CP."""
    print(f"📥 Téléchargement du GTFS CP...")
    print(f"   URL : {CP_GTFS_URL}")

    tmp_path = OUTPUT_ZIP + ".tmp"
    headers = {"User-Agent": "Mozilla/5.0"}

    with requests.get(CP_GTFS_URL, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

    size_mb = os.path.getsize(tmp_path) / 1024 / 1024
    print(f"   ✅ Téléchargé ({size_mb:.1f} Mo)")
    return tmp_path


def is_suburban(route_short_name: str) -> bool:
    name = (route_short_name or "").strip()
    return name.upper() in ("U", "S") or name.lower().startswith("linha ")


def remove_suburban(path: str) -> None:
    """Retire du zip les lignes urbaines, leurs trajets, leurs horaires et les arrêts devenus inutiles."""
    with zipfile.ZipFile(path) as z:
        files = {name: z.read(name) for name in z.namelist()}

    def rows(name):
        reader = csv.DictReader(io.StringIO(files[name].decode("utf-8-sig")))
        return reader.fieldnames, list(reader)

    def write(name, header, data):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(data)
        files[name] = buf.getvalue().encode("utf-8")

    h_routes, routes = rows("routes.txt")
    kept_routes = [r for r in routes if not is_suburban(r["route_short_name"])]
    route_ids = {r["route_id"] for r in kept_routes}
    h_trips, trips = rows("trips.txt")
    kept_trips = [t for t in trips if t["route_id"] in route_ids]
    trip_ids = {t["trip_id"] for t in kept_trips}
    h_st, st = rows("stop_times.txt")
    kept_st = [r for r in st if r["trip_id"] in trip_ids]
    used = {r["stop_id"] for r in kept_st}
    h_stops, stops = rows("stops.txt")
    used |= {s.get("parent_station", "") for s in stops if s["stop_id"] in used}
    kept_stops = [s for s in stops if s["stop_id"] in used]
    for name, header, data in (("routes.txt", h_routes, kept_routes), ("trips.txt", h_trips, kept_trips),
                               ("stop_times.txt", h_st, kept_st), ("stops.txt", h_stops, kept_stops)):
        write(name, header, data)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    print(f"   ✅ Banlieue retirée : {len(routes) - len(kept_routes)} lignes, {len(trips) - len(kept_trips)} trajets, "
          f"{len(stops) - len(kept_stops)} arrêts ; restent {len(kept_trips)} trajets")


def main():
    print("=" * 60)
    print("🚂 Ingestion GTFS CP (Comboios de Portugal)")
    print("=" * 60)

    # 1. Télécharger le GTFS officiel
    tmp_gtfs = download_cp_gtfs()

    # 2. Retirer la banlieue de Lisbonne et de Porto
    print("\n✂️ Retrait des trains urbains...")
    remove_suburban(tmp_gtfs)

    # 3. Prolonger les trains de la Linha do Leste jusqu'à Badajoz
    from patch_elvas_badajoz import patch_gtfs

    print("\n🔧 Prolongement des trains Elvas -> Badajoz...")
    success = patch_gtfs(tmp_gtfs, OUTPUT_ZIP)

    # Nettoyer le fichier temporaire
    if os.path.exists(tmp_gtfs):
        os.remove(tmp_gtfs)

    if success:
        print(f"\n✅ GTFS CP prêt : {OUTPUT_ZIP}")
        print(f"   Taille : {os.path.getsize(OUTPUT_ZIP) / 1024 / 1024:.1f} Mo")
    else:
        # En cas d'échec du patch, utiliser le GTFS non patché
        print("\n⚠️ Patch échoué, utilisation du GTFS original")
        shutil.move(tmp_gtfs, OUTPUT_ZIP)

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()
