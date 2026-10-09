"""
Référentiel des gares TrainNomad — opérateur Ouigo España (Madrid, Barcelone, Valence, Alicante,
Séville, Malaga, Valladolid...).

Source : OUIGO_ES/ouigo_es_gtfs.zip, le GTFS téléchargé par OUIGO_ES/ingest_ouigo_es.py (API du
ministère espagnol, avec clé). S'il est absent, la copie gardée dans
sources_officielles/ouigo_es_gtfs.zip sert à sa place : les gares changent peu d'une version à l'autre.
Le stop_id est l'UIC précédé de "00" : "007117000" = 7117000, Madrid-Chamartín. Ce sont les mêmes
UIC que Renfe : fusion.py réunit les deux opérateurs sur la même gare.

Noms : le flux les écrit en majuscules sans accents ("CORDOBA-CENTRAL") : c'est le nom
d'OpenStreetMap qui est gardé, et dans la base fusionnée celui de Renfe.

Sortie : referentiel/operateurs/OUIGO_ES/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_ouigo_es.py
    python referentiel/build_gares_ouigo_es.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import BASE_DIR, GTFS_DIR, default_names, run  # noqa: E402

OUIGO_ZIP = os.path.join(GTFS_DIR, "OUIGO_ES", "ouigo_es_gtfs.zip")
OUIGO_COPY = os.path.join(BASE_DIR, "sources_officielles", "ouigo_es_gtfs.zip")


def load_ouigo_es_stations() -> list[dict]:
    path = OUIGO_ZIP if os.path.exists(OUIGO_ZIP) else OUIGO_COPY
    if not os.path.exists(path):
        sys.exit(f"❌ {OUIGO_ZIP} absent (python OUIGO_ES/ingest_ouigo_es.py), et pas de copie dans {OUIGO_COPY}")
    logging.info(f"   source : {path}")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        uic = re.sub(r"\D", "", r.stop_id)[-7:]
        out.append({"cle": r.stop_id, "uic": uic if re.fullmatch(r"\d{7}", uic) else "",
                    "nom_gtfs": " ".join(r.stop_name.title().split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "OUIGO", "cars": "", "codes": [("OUIGO_ES", r.stop_id)]})
    logging.info(f"   {len(out)} gares Ouigo España")
    return out


if __name__ == "__main__":
    # nom d'OpenStreetMap d'abord : le flux est en majuscules sans accents
    run("OUIGO_ES", "Ouigo España", load_ouigo_es_stations, op_country="ES",
        names=lambda st, f, country: default_names(st["nom_gtfs"], f, country, ""))
