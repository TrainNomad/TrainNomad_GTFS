"""
Telecharge le GTFS Ouigo Espana via l'API NAP (Punto de Acceso Nacional).

L'API NAP du Ministere des Transports espagnol expose les donnees GTFS
de tous les operateurs de transport, y compris Ouigo Espana.

Endpoints utilises:
  - List: GET https://nap.transportes.gob.es/api/Fichero/GetList
  - Download: GET https://nap.transportes.gob.es/api/Fichero/download/{fileId}

Authentification:
  - Header: ApiKey: {cle-api}
  - Variable d'environnement: OUIGO_ES_API_KEY

ID du fichier Ouigo (GTFS-ZIP): 1766

Usage :
    python OUIGO_ES/ingest_ouigo_es.py

Avec cle API manuelle :
    OUIGO_ES_API_KEY="5c51e865-2f81-4215-a1f0-3b73985a31fa" python OUIGO_ES/ingest_ouigo_es.py
"""
import os
import shutil
import logging
import requests

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(BASE_DIR, "ouigo_es_gtfs.zip")

# NAP API Configuration
NAP_BASE_URL = "https://nap.transportes.gob.es/api"
OUIGO_GTFS_FILE_ID = "1766"
OUIGO_ES_DOWNLOAD_URL = f"{NAP_BASE_URL}/Fichero/download/{OUIGO_GTFS_FILE_ID}"
OUIGO_ES_LIST_URL = f"{NAP_BASE_URL}/Fichero/GetList"


def download_ouigo_es_gtfs() -> str:
    """Telecharge le GTFS officiel Ouigo Espana via l'API NAP."""
    api_key = os.environ.get("OUIGO_ES_API_KEY")

    print("=" * 60)
    print("Train GTFS Ouigo Espana (GTFS Ingestion)")
    print("=" * 60)
    print("[INFO] Telechargement du GTFS Ouigo Espana...")
    print(f"[INFO] ID fichier : {OUIGO_GTFS_FILE_ID}")

    if not api_key:
        logging.error("ERREUR: OUIGO_ES_API_KEY n'est pas defini")
        print("[ERROR] Cle API requise")
        raise ValueError("OUIGO_ES_API_KEY environment variable not set")

    # Headers correctes pour l'API NAP
    headers = {
        "User-Agent": "Mozilla/5.0",
        "ApiKey": api_key,
        "accept": "application/octet-stream"
    }

    print(f"[INFO] URL : {OUIGO_ES_DOWNLOAD_URL}")
    print("[INFO] Avec authentification API")

    tmp_path = OUTPUT_ZIP + ".tmp"

    try:
        # verify=False uniquement en dev local (OUIGO_ES_SKIP_SSL_VERIFY=1)
        # En production (GitHub Actions), toujours verifier les certificats
        verify_ssl = not (os.environ.get("OUIGO_ES_SKIP_SSL_VERIFY") == "1")

        with requests.get(OUIGO_ES_DOWNLOAD_URL, headers=headers, stream=True, timeout=120, verify=verify_ssl) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except requests.exceptions.HTTPError as e:
        logging.error(f"Erreur HTTP {e.response.status_code}: {e}")
        if e.response.status_code == 401:
            logging.error("Authentification echouee - Verifiez votre cle API")
        raise
    except requests.exceptions.RequestException as e:
        logging.error(f"Erreur de telechargement: {e}")
        raise

    size_mb = os.path.getsize(tmp_path) / 1024 / 1024
    print(f"[SUCCESS] Telecharge ({size_mb:.1f} Mo)")
    return tmp_path


def main():
    try:
        # Telecharger le GTFS
        tmp_gtfs = download_ouigo_es_gtfs()

        # Deplacer vers le fichier final
        if os.path.exists(OUTPUT_ZIP):
            os.remove(OUTPUT_ZIP)
        os.rename(tmp_gtfs, OUTPUT_ZIP)

        size_mb = os.path.getsize(OUTPUT_ZIP) / 1024 / 1024
        print(f"\n[SUCCESS] GTFS Ouigo Espana pret : {OUTPUT_ZIP}")
        print(f"[INFO] Taille : {size_mb:.1f} Mo")
        print("[SUCCESS] Termine !")
        return 0

    except Exception as e:
        logging.error(f"Erreur fatale: {e}")
        # Nettoyer le fichier temporaire
        tmp_path = OUTPUT_ZIP + ".tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print("\n[ERROR] Erreur lors du telechargement")
        return 1


if __name__ == "__main__":
    exit(main())
