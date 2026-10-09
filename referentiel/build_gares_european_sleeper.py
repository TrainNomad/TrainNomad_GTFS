"""
Référentiel des gares TrainNomad — opérateur European Sleeper (trains de nuit Bruxelles / Amsterdam -
Berlin - Prague, Bruxelles - Milan, Paris - Berlin).

Source : feeds/EUROPEAN_SLEEPER.zip (GTFS European Sleeper). Le stop_id est l'UIC à 7 chiffres
("8727100" = Paris Gare du Nord, "5457076" = Praha hl.n.). La gare est cherchée dans OpenStreetMap
par UIC, sinon par nom.

European Sleeper n'a pas de pays : toutes ses gares portent le nom officiel d'OpenStreetMap, et
fusion.py les réunit avec celles des opérateurs nationaux.

Sortie : referentiel/operateurs/EUROPEAN_SLEEPER/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_european_sleeper.py
    python referentiel/build_gares_european_sleeper.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

SLEEPER_ZIP = os.path.join(GTFS_DIR, "feeds", "EUROPEAN_SLEEPER.zip")


def load_sleeper_stations(path: str = SLEEPER_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord l'ingestion des flux (feeds/EUROPEAN_SLEEPER.zip)")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        # "Milano Porta Garibaldi (superficie)" -> "Milano Porta Garibaldi"
        name = " ".join(re.sub(r"\s*\([^)]*\)\s*$", "", r.stop_name).split())
        out.append({"cle": r.stop_id, "uic": r.stop_id if re.fullmatch(r"\d{7}", r.stop_id) else "",
                    "nom_gtfs": name, "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "EUROPEAN SLEEPER", "cars": "", "codes": [("EUROPEAN_SLEEPER", r.stop_id)]})
    logging.info(f"   {len(out)} gares European Sleeper")
    return out


if __name__ == "__main__":
    run("EUROPEAN_SLEEPER", "European Sleeper", load_sleeper_stations, op_country="")
