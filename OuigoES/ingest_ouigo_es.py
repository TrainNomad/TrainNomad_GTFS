"""
Télécharge le GTFS de Ouigo España depuis le NAP espagnol (Ministerio de Transportes).

Sortie : OuigoES/ouigo_es_gtfs.zip, lu par build_network.py

Le NAP espagnol nécessite une authentification par clé API.
Les identifiants sont lus depuis les variables d'environnement :
  - NAP_ES_API_KEY : clé API

Usage :
  python OuigoES/ingest_ouigo_es.py
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
OUT_ZIP = os.path.join(BASE_DIR, "ouigo_es_gtfs.zip")
REPORT_PATH = os.path.join(BASE_DIR, "ouigo_es_gtfs_report.json")

NAP_DETAIL_URL = "https://nap.transportes.gob.es/Files/Detail/1515"
NAP_DOWNLOAD_BASE = "https://nap.transportes.gob.es/api/v1/dataset"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def get_api_key() -> str:
    key = os.environ.get("NAP_ES_API_KEY")
    if not key:
        key = "5c51e865-2f81-4215-a1f0-3b73985a31fa"
    return key


def download_gtfs(dest: str, attempts: int = 4):
    api_key = get_api_key()
    download_url = f"{NAP_DOWNLOAD_BASE}/1515/download"

    logging.info(f"📥 Téléchargement du GTFS Ouigo España depuis le NAP")

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,
        "Accept": "application/zip, application/octet-stream, */*",
    }

    for attempt in range(1, attempts + 1):
        tmp = dest + ".part"
        try:
            with requests.get(download_url, headers=headers, stream=True, timeout=300, allow_redirects=True) as r:
                if r.status_code == 401:
                    logging.error("❌ Authentification échouée - vérifiez NAP_ES_API_KEY")
                    raise SystemExit(1)
                if r.status_code == 403:
                    logging.error("❌ Accès refusé - clé API invalide ou expirée")
                    raise SystemExit(1)
                r.raise_for_status()

                content_type = r.headers.get("Content-Type", "")
                if "html" in content_type.lower():
                    logging.warning("  Réponse HTML reçue, tentative alternative...")
                    try_alternative_download(dest, api_key, attempts - attempt)
                    return

                with open(tmp, "wb") as f:
                    shutil.copyfileobj(r.raw, f)

            if os.path.getsize(tmp) < 1000:
                with open(tmp, "r", errors="ignore") as f:
                    content = f.read(500)
                if "<html" in content.lower() or "<!doctype" in content.lower():
                    logging.warning("  Contenu HTML reçu au lieu du ZIP")
                    try_alternative_download(dest, api_key, attempts - attempt)
                    return

            os.replace(tmp, dest)
            logging.info(f"  {os.path.getsize(dest) / 1e6:.1f} Mo reçus")
            return

        except requests.RequestException as e:
            if attempt == attempts:
                raise
            logging.warning(f"  Échec ({e}), nouvel essai {attempt + 1}/{attempts} dans {10 * attempt} s")
            time.sleep(10 * attempt)


def try_alternative_download(dest: str, api_key: str, remaining_attempts: int):
    alt_urls = [
        "https://nap.transportes.gob.es/api/v1/dataset/1515/resource/download",
        "https://nap.transportes.gob.es/api/dataset/1515/gtfs",
        "https://nap.transportes.gob.es/Files/Download/1515",
    ]

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,
    }

    for url in alt_urls:
        logging.info(f"  Essai URL alternative : {url}")
        try:
            tmp = dest + ".part"
            with requests.get(url, headers=headers, stream=True, timeout=300, allow_redirects=True) as r:
                if r.status_code in (401, 403):
                    continue
                r.raise_for_status()

                content_type = r.headers.get("Content-Type", "")
                if "html" in content_type.lower():
                    continue

                with open(tmp, "wb") as f:
                    shutil.copyfileobj(r.raw, f)

                if os.path.getsize(tmp) > 1000:
                    os.replace(tmp, dest)
                    logging.info(f"  {os.path.getsize(dest) / 1e6:.1f} Mo reçus")
                    return
        except requests.RequestException:
            continue

    logging.error("❌ Impossible de télécharger le GTFS depuis le NAP espagnol")
    logging.error("   Vérifiez manuellement : https://nap.transportes.gob.es/Files/Detail/1515")
    raise SystemExit(1)


def validate_gtfs(path: str) -> dict:
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
        "source": NAP_DETAIL_URL,
        "zip_mb": round(os.path.getsize(OUT_ZIP) / 1e6, 2),
        "stats": stats,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logging.info(f"📊 {json.dumps(stats, ensure_ascii=False)}")
    logging.info(f"✅ GTFS téléchargé : {OUT_ZIP} ({report['zip_mb']} Mo)")


if __name__ == "__main__":
    main()
