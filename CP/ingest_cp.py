"""
Télécharge le GTFS CP et applique le patch Elvas-Badajoz.

La liaison transfrontalière Elvas (PT) - Badajoz (ES) n'est pas publiée dans le GTFS officiel CP.
Ce script télécharge le GTFS et injecte les horaires extraits du PDF publié par CP.

Usage :
    python CP/ingest_cp.py
"""
import os
import shutil
import tempfile

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


def main():
    print("=" * 60)
    print("🚂 Ingestion GTFS CP (Comboios de Portugal)")
    print("=" * 60)

    # 1. Télécharger le GTFS officiel
    tmp_gtfs = download_cp_gtfs()

    # 2. Importer et appliquer le patch Elvas-Badajoz
    from patch_elvas_badajoz import patch_gtfs, TIMETABLE_ELVAS_BADAJOZ

    print("\n🔧 Application du patch Elvas - Badajoz...")
    success = patch_gtfs(tmp_gtfs, OUTPUT_ZIP, TIMETABLE_ELVAS_BADAJOZ)

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
