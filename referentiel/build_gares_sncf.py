"""
Référentiel des gares TrainNomad — opérateur SNCF (gares et arrêts de cars TER du GTFS SNCF).

Le stop_id SNCF contient l'UIC à 8 chiffres (StopPoint:OCETGV INOUI-87686006) : la gare est cherchée
dans OpenStreetMap par UIC (exact), sinon par son nom. Enrichissement et fichiers : voir commun.py.

Sortie : referentiel/operateurs/SNCF/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_sncf.py              (feeds/SNCF.zip, téléchargé si absent)
    python referentiel/build_gares_sncf.py --offline    (uniquement le cache : aucune requête)
"""
import logging
import os
import re
import shutil
import sys
import zipfile

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

SNCF_ZIP = os.path.join(GTFS_DIR, "feeds", "SNCF.zip")
SNCF_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

# types de StopPoint SNCF assurés par des cars : un arrêt qui n'a que ceux-là est un "arret_car"
ROAD_TYPES = {"Car TER", "Car à réservation", "Car"}


def clean_sncf_name(name: str) -> str:
    """ "Paris Gare de Lyon Hall 1 - 2" -> "Paris Gare de Lyon" """
    name = re.sub(r"\s+Hall\s+\d+(\s*-\s*\d+)?$", "", name or "").strip()
    return " ".join(name.split())


def load_sncf_stations(path: str = SNCF_ZIP) -> list[dict]:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        logging.info(f"📥 Téléchargement {SNCF_URL}")
        with requests.get(SNCF_URL, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    sp = stops[stops["stop_id"].isin(used)].copy()
    sp["uic8"] = sp["stop_id"].str.extract(r"(\d{8})$")[0]
    sp["type"] = sp["stop_id"].str.extract(r"StopPoint:OCE(.*)-\d{8}$")[0].fillna("")
    sp = sp[sp["uic8"].notna()]
    areas = stops[stops["stop_id"].str.startswith("StopArea:")].set_index("stop_id")

    out = []
    for uic8, g in sp.groupby("uic8"):
        area_id = "StopArea:OCE" + uic8
        area = areas.loc[area_id] if area_id in areas.index else None
        src = area if area is not None else g.iloc[0]
        trains = sorted(set(g["type"]) - ROAD_TYPES)
        cars = sorted(set(g["type"]) & ROAD_TYPES)
        ids = sorted(set(g["stop_id"]) | ({area_id} if area is not None else set()))
        out.append({"cle": uic8, "uic": uic8[:7], "uic8": uic8, "nom_gtfs": clean_sncf_name(src["stop_name"]),
                    "lat": float(src["stop_lat"]), "lon": float(src["stop_lon"]),
                    "type": "gare" if trains else "arret_car", "trains": "|".join(trains), "cars": "|".join(cars),
                    "codes": [("SNCF", i) for i in ids]})
    n_gares = sum(1 for s in out if s["type"] == "gare")
    logging.info(f"   {n_gares} gares (trains) et {len(out) - n_gares} arrêts de cars")
    return out


if __name__ == "__main__":
    run("SNCF", "SNCF", load_sncf_stations, op_country="FR")
