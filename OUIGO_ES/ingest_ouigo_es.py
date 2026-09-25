"""
Télécharge le GTFS Ouigo España via l'API Renfe (NAP) avec authentification.

Ouigo España est fourni par Renfe via le Punto de Acceso Nacional (NAP) à :
  https://nap.transportes.gob.es/api/v1/dataset/1515/download

L'accès authentifié permet de récupérer les données mises à jour régulièrement.
La clé API est stockée dans la variable GitHub Actions : OUIGO_ES_API_KEY

Usage :
    python OUIGO_ES/ingest_ouigo_es.py

Ou avec clé API manuelle :
    OUIGO_ES_API_KEY="..." python OUIGO_ES/ingest_ouigo_es.py
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

# API publique (fallback sans authentification)
OUIGO_ES_PUBLIC_URL = "https://nap.transportes.gob.es/api/v1/dataset/1515/download"

# L'URL avec authentification (optionnel)
OUIGO_ES_AUTH_URL = OUIGO_ES_PUBLIC_URL


def download_ouigo_es_gtfs() -> str:
    """Télécharge le GTFS officiel Ouigo España."""
    api_key = os.environ.get("OUIGO_ES_API_KEY")

    print("=" * 60)
    print("🚂 Ingestion GTFS Ouigo España")
    print("=" * 60)
    print(f"📥 Téléchargement du GTFS Ouigo España...")

    headers = {"User-Agent": "Mozilla/5.0"}

    if api_key:
        print(f"   Avec authentification API")
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-KEY"] = api_key
        url = OUIGO_ES_AUTH_URL
    else:
        print(f"   Mode public (sans clé API)")
        url = OUIGO_ES_PUBLIC_URL

    print(f"   URL : {url}")

    tmp_path = OUTPUT_ZIP + ".tmp"

    try:
        with requests.get(url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except requests.exceptions.HTTPError as e:
        logging.error(f"Erreur HTTP {e.response.status_code}: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logging.error(f"Erreur de téléchargement: {e}")
        raise

    size_mb = os.path.getsize(tmp_path) / 1024 / 1024
    print(f"   ✅ Téléchargé ({size_mb:.1f} Mo)")
    return tmp_path


def main():
    try:
        # Télécharger le GTFS
        tmp_gtfs = download_ouigo_es_gtfs()

        # Déplacer vers le fichier final
        if os.path.exists(OUTPUT_ZIP):
            os.remove(OUTPUT_ZIP)
        os.rename(tmp_gtfs, OUTPUT_ZIP)

        size_mb = os.path.getsize(OUTPUT_ZIP) / 1024 / 1024
        print(f"\n✅ GTFS Ouigo España prêt : {OUTPUT_ZIP}")
        print(f"   Taille : {size_mb:.1f} Mo")
        print("✅ Terminé !")
        return 0

    except Exception as e:
        logging.error(f"Erreur fatale: {e}")
        # Nettoyer le fichier temporaire
        tmp_path = OUTPUT_ZIP + ".tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print("\n❌ Erreur lors du téléchargement")
        return 1


if __name__ == "__main__":
    exit(main())
