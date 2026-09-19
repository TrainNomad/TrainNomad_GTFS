"""
Télécharge le GTFS de CP (Comboios de Portugal).

Sortie : CP/cp_gtfs.zip, lu par build_network.py

Usage :
  python CP/ingest_cp.py
"""
import json
import logging
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ZIP = os.path.join(BASE_DIR, "cp_gtfs.zip")
REPORT_PATH = os.path.join(BASE_DIR, "cp_gtfs_report.json")

GTFS_URL = "https://publico.cp.pt/gtfs/gtfs.zip"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def download_gtfs(dest: str, attempts: int = 4):
    logging.info(f"📥 Téléchargement du GTFS CP : {GTFS_URL}")
    for attempt in range(1, attempts + 1):
        tmp = dest + ".part"
        try:
            with requests.get(GTFS_URL, headers={"User-Agent": USER_AGENT}, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            os.replace(tmp, dest)
            logging.info(f"  {os.path.getsize(dest) / 1e6:.1f} Mo reçus")
            return
        except requests.RequestException as e:
            if attempt == attempts:
                raise
            logging.warning(f"  Échec ({e}), nouvel essai {attempt + 1}/{attempts} dans {10 * attempt} s")
            time.sleep(10 * attempt)


def validate_gtfs(path: str) -> dict:
    """Vérifie que le zip contient les fichiers GTFS requis et compte les lignes."""
    stats = {}
    required = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
    with zipfile.ZipFile(path, "r") as z:
        names = set(z.namelist())
        missing = required - names
        if missing:
            raise ValueError(f"Fichiers GTFS manquants : {missing}")
        for name in names:
            if name.endswith(".txt"):
                with z.open(name) as f:
                    lines = sum(1 for _ in f) - 1
                    stats[name.replace(".txt", "")] = lines
    return stats


def main():
    download_gtfs(OUT_ZIP)
    stats = validate_gtfs(OUT_ZIP)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": GTFS_URL,
        "zip_mb": round(os.path.getsize(OUT_ZIP) / 1e6, 2),
        "stats": stats,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logging.info(f"📊 {json.dumps(stats, ensure_ascii=False)}")
    logging.info(f"✅ GTFS téléchargé : {OUT_ZIP} ({report['zip_mb']} Mo)")


if __name__ == "__main__":
    main()
