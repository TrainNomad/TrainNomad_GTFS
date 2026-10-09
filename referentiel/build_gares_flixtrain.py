"""
Référentiel des gares TrainNomad — opérateur FlixTrain (Allemagne, Bâle).

Source : FLIXTRAIN/flixtrain_gtfs.zip, le GTFS Flix réduit aux trains par
FLIXTRAIN/ingest_flixtrain_gtfs.py. Le stop_id est un identifiant Flix, sans UIC, et les noms sont
en anglais : "Cologne Central Station (FlixTrain)". Le nom est remis en allemand ("Köln Hbf") pour
chercher la gare dans OpenStreetMap ; la gare prend l'UIC de son objet OSM, et fusion.py la réunit
avec celle des autres opérateurs.

FlixTrain n'a pas de pays : toutes ses gares portent le nom officiel d'OpenStreetMap.

Sortie : referentiel/operateurs/FLIXTRAIN/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_flixtrain.py       (python FLIXTRAIN/ingest_flixtrain_gtfs.py d'abord si besoin)
    python referentiel/build_gares_flixtrain.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

FLIXTRAIN_ZIP = os.path.join(GTFS_DIR, "FLIXTRAIN", "flixtrain_gtfs.zip")
GERMAN_CITY = {"Cologne": "Köln", "Hanover": "Hannover", "Munich": "München", "Nuremberg": "Nürnberg"}


def german_name(name: str) -> str:
    """ "Cologne Central Station (FlixTrain)" -> "Köln Hbf" ; "Berlin Südkreuz (FlixTrain)" -> "Berlin Südkreuz" """
    name = re.sub(r"\s*\(FlixTrain\)\s*$", "", name or "").replace("Central Station", "Hbf")
    for en, de in GERMAN_CITY.items():
        name = re.sub(rf"^{en}\b", de, name)
    return " ".join(name.split())


def search_names(st) -> list[str]:
    """ "Essen Hbf", puis "Essen Hauptbahnhof" (OSM écrit l'un ou l'autre), puis sans la précision
    ("Münster (Westf) Hbf" -> "Münster Hbf")."""
    name = st["nom_gtfs"]
    out = [name, name.replace("Hbf", "Hauptbahnhof")]
    plain = " ".join(re.sub(r"\([^)]*\)", " ", name).split())
    out += [plain, plain.replace("Hbf", "Hauptbahnhof")]
    return list(dict.fromkeys(n for n in out if n))


def load_flixtrain_stations(path: str = FLIXTRAIN_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python FLIXTRAIN/ingest_flixtrain_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        out.append({"cle": r.stop_id, "uic": "", "nom_gtfs": german_name(r.stop_name),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "FLIXTRAIN", "cars": "", "codes": [("FLIXTRAIN", r.stop_id)]})
    logging.info(f"   {len(out)} gares FlixTrain")
    return out


if __name__ == "__main__":
    run("FLIXTRAIN", "FlixTrain", load_flixtrain_stations, op_country="", search_name=search_names)
