"""
Telecharge et filtre le GTFS Suisse pour les operateurs de voyageurs:
  - Grands operateurs nationaux/regionaux
  - Compagnies a voie etroite et panoramiques
  - Trains de montagne touristiques majeurs

OPERATEURS CONSERVES:

Grands Acteurs Nationaux & Regionaux:
  SBB/CFF, BLS, SOB, THURBO, Regionalps, ZB

Compagnies a Voie Etroite / Panoramiques:
  RhB (Glacier Express, Bernina Express)
  MGB (Matterhorn Gotthard Bahn)
  MOB (Montreux-Oberland Bernois)
  CJ (Chemins de fer du Jura)
  TPF, TPC, TRAVYS, MBC, LEB, FART, FLP

Trains de Montagne / Funiculaires:
  BOB, JB, WAB, GGB, BRB, PB, AB, RBS, SZU

OPERATEURS SUPPRIMES:
  ✗ Musees/historiques (DFB, DVZO, etc.)
  ✗ Transports urbains (TL, TRN, etc.)
  ✗ Autres pays (DB Regio, SNCF, OeBB, etc.)
  ✗ Infrastructure/info (VHB, NeTS, KUBUS, SRT, etc.)

Usage:
    python SWISS/ingest_swiss_gtfs.py

Avec override SSL (dev local):
    SWISS_SKIP_SSL_VERIFY=1 python SWISS/ingest_swiss_gtfs.py
"""
import os
import shutil
import logging
import zipfile

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(BASE_DIR, "swiss_gtfs.zip")
SWISS_GTFS_URL = "https://gtfs.geops.ch/dl/gtfs_train.zip"

# Operateurs a conserver (agency_id)
OPERATORS_TO_KEEP = {
    # Grands acteurs
    "000011",  # SBB
    "L7____",  # SBB variant
    "000351",  # SBB (Grenzverkehr)
    "000033",  # BLS
    "000082",  # SOB
    "000065",  # THURBO
    "000074",  # Regionalps (RA)
    "000086",  # ZB

    # Voie etroite / Panoramiques
    "000072",  # RhB (Rhaetische Bahn)
    "000093",  # MGB
    "000048",  # MGB variant
    "000064",  # MOB
    "000043",  # CJ
    "000053",  # TPF
    "000023",  # TPC
    "000097",  # TRAVYS
    "000029",  # MBC
    "000055",  # LEB
    "000049",  # FART
    "000047",  # FLP

    # Trains de montagne
    "000035",  # BOB
    "000140",  # BOB variant
    "000124",  # JB
    "000157",  # WAB
    "000121",  # GGB
    "000104",  # BRB
    "000136",  # PB
    "000022",  # AB
    "000088",  # RBS
    "000078",  # SZU
}


def download_swiss_gtfs() -> str:
    """Telecharge le GTFS Suisse officiel."""
    print("=" * 70)
    print("[INFO] Ingestion GTFS Suisse - Operateurs de Voyageurs")
    print("=" * 70)
    print("[INFO] Telechargement depuis https://gtfs.geops.ch/...")

    tmp_path = OUTPUT_ZIP + ".tmp"
    headers = {"User-Agent": "Mozilla/5.0"}

    verify_ssl = not (os.environ.get("SWISS_SKIP_SSL_VERIFY") == "1")

    try:
        with requests.get(SWISS_GTFS_URL, headers=headers, stream=True, timeout=120, verify=verify_ssl) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except requests.exceptions.RequestException as e:
        logging.error(f"Erreur telechargement: {e}")
        raise

    size_mb = os.path.getsize(tmp_path) / 1024 / 1024
    print(f"[SUCCESS] Telecharge ({size_mb:.1f} Mo)")
    return tmp_path


def filter_gtfs_by_operators(input_zip: str, output_zip: str):
    """
    Filtre le GTFS pour ne garder que les operateurs specifiques.

    Logique:
      1. Lire agency.txt et identifier les operateurs a conserver
      2. Lire trips.txt et identifier les trips pour ces operateurs
      3. Lire stop_times.txt et identifier les stops utilises
      4. Re-generer les fichiers filtrés
    """
    print("\n[INFO] Filtrage GTFS par operateurs...")

    with zipfile.ZipFile(input_zip, 'r') as z_in:
        # Etape 1: Charger toutes les donnees
        print("  [1] Chargement donnees...")
        agency = pd.read_csv(z_in.open("agency.txt"), dtype=str, low_memory=False)
        trips = pd.read_csv(z_in.open("trips.txt"), dtype={"trip_id": str, "route_id": str}, low_memory=False)
        routes = pd.read_csv(z_in.open("routes.txt"), dtype={"route_id": str}, low_memory=False)
        stops = pd.read_csv(z_in.open("stops.txt"), dtype={"stop_id": str}, low_memory=False)
        stop_times = pd.read_csv(z_in.open("stop_times.txt"), dtype={"stop_id": str, "trip_id": str}, low_memory=False)
        calendar = pd.read_csv(z_in.open("calendar.txt"), dtype=str, low_memory=False)
        calendar_dates = pd.read_csv(z_in.open("calendar_dates.txt"), dtype=str, low_memory=False)

        try:
            transfers = pd.read_csv(z_in.open("transfers.txt"), dtype={"from_stop_id": str, "to_stop_id": str}, low_memory=False)
        except:
            transfers = None

        print(f"      Agency: {len(agency)}")
        print(f"      Trips: {len(trips)}")
        print(f"      Stops: {len(stops)}")

        # Etape 2: Filtrer agency
        print("  [2] Filtrage operateurs...")
        agency_orig = len(agency)
        agency = agency[agency["agency_id"].isin(OPERATORS_TO_KEEP)]
        print(f"      {agency_orig} → {len(agency)} operateurs")

        # Etape 3: Filtrer routes par les operateurs conserves
        print("  [3] Filtrage routes...")
        routes_orig = len(routes)
        routes = routes[routes["agency_id"].isin(OPERATORS_TO_KEEP)]
        print(f"      {routes_orig} → {len(routes)} routes")

        # Etape 4: Filtrer trips par les routes conservees
        print("  [4] Filtrage trajets...")
        trips_orig = len(trips)
        trips = trips[trips["route_id"].isin(routes["route_id"])]
        print(f"      {trips_orig} → {len(trips)} trajets")

        # Etape 5: Filtrer stop_times par les trajets conserves
        print("  [5] Filtrage horaires...")
        stop_times_orig = len(stop_times)
        stop_times = stop_times[stop_times["trip_id"].isin(trips["trip_id"])]
        print(f"      {stop_times_orig} → {len(stop_times)} horaires")

        # Etape 6: Filtrer stops par les stop_times
        print("  [6] Filtrage arrets...")
        stops_with_trips = set(stop_times["stop_id"].unique())
        stops_orig = len(stops)
        stops = stops[stops["stop_id"].isin(stops_with_trips)]
        print(f"      {stops_orig} → {len(stops)} arrets")

        # Etape 7: Filtrer transfers
        if transfers is not None:
            print("  [7] Filtrage transferts...")
            transfers = transfers[
                (transfers["from_stop_id"].isin(stops_with_trips)) &
                (transfers["to_stop_id"].isin(stops_with_trips))
            ]

        # Etape 8: Ecrire nouveau ZIP
        print("  [8] Ecriture GTFS filtre...")
        with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as z_out:
            z_out.writestr("agency.txt", agency.to_csv(index=False))
            z_out.writestr("calendar.txt", calendar.to_csv(index=False))
            z_out.writestr("calendar_dates.txt", calendar_dates.to_csv(index=False))
            z_out.writestr("routes.txt", routes.to_csv(index=False))
            z_out.writestr("stops.txt", stops.to_csv(index=False))
            z_out.writestr("stop_times.txt", stop_times.to_csv(index=False))
            z_out.writestr("trips.txt", trips.to_csv(index=False))

            if transfers is not None:
                z_out.writestr("transfers.txt", transfers.to_csv(index=False))

            try:
                feed_info = pd.read_csv(z_in.open("feed_info.txt"), dtype=str)
                z_out.writestr("feed_info.txt", feed_info.to_csv(index=False))
            except:
                pass

    print(f"      Filtre complete")


def analyze_and_report(input_zip: str):
    """Analyse et genere un rapport."""
    print("\n" + "=" * 70)
    print("[INFO] Rapport GTFS Suisse Filtre")
    print("=" * 70)

    with zipfile.ZipFile(input_zip, 'r') as z:
        agency = pd.read_csv(z.open("agency.txt"), dtype=str, low_memory=False)
        routes = pd.read_csv(z.open("routes.txt"), dtype=str, low_memory=False)
        stops = pd.read_csv(z.open("stops.txt"), dtype=str, low_memory=False)
        trips = pd.read_csv(z.open("trips.txt"), dtype=str, low_memory=False)
        stop_times = pd.read_csv(z.open("stop_times.txt"), dtype=str, low_memory=False)

        print(f"[INFO] Operateurs: {len(agency)}")
        print(f"[INFO] Routes: {len(routes)}")
        print(f"[INFO] Arrets: {len(stops)}")
        print(f"[INFO] Trajets: {len(trips)}")
        print(f"[INFO] Horaires: {len(stop_times)}")

        print(f"\n[INFO] Operateurs conserves:")
        for _, row in agency[["agency_id", "agency_name"]].iterrows():
            print(f"      {row['agency_id']:15s} | {row['agency_name']}")


def main():
    try:
        # Telecharger
        tmp_gtfs = download_swiss_gtfs()

        # Filtrer par operateurs
        tmp_filtered = OUTPUT_ZIP + ".tmp.filtered"
        filter_gtfs_by_operators(tmp_gtfs, tmp_filtered)

        # Analyser
        analyze_and_report(tmp_filtered)

        # Finaliser
        if os.path.exists(OUTPUT_ZIP):
            os.remove(OUTPUT_ZIP)
        os.rename(tmp_filtered, OUTPUT_ZIP)

        size_mb = os.path.getsize(OUTPUT_ZIP) / 1024 / 1024
        print(f"\n[SUCCESS] GTFS Suisse filtre pret: {OUTPUT_ZIP}")
        print(f"[INFO] Taille: {size_mb:.1f} Mo")
        print(f"[SUCCESS] Termine !")

        # Nettoyage
        if os.path.exists(tmp_gtfs):
            os.remove(tmp_gtfs)

        return 0

    except Exception as e:
        logging.error(f"Erreur: {e}")
        import traceback
        traceback.print_exc()

        # Nettoyage
        for path in [OUTPUT_ZIP + ".tmp", OUTPUT_ZIP + ".tmp.filtered"]:
            if os.path.exists(path):
                os.remove(path)

        print("\n[ERROR] Erreur ingestion")
        return 1


if __name__ == "__main__":
    exit(main())
