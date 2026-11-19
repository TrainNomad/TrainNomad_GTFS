"""
Référentiel des gares TrainNomad — opérateur Travelski Night Express (trains de nuit d'hiver Paris-Austerlitz
et Amsterdam / Rotterdam / Anvers / Bruxelles -> Albertville, Moûtiers, Aime, Landry, Bourg-Saint-Maurice).

Source : feeds/TRAVELSKI.zip (GTFS communautaire data.gouv.fr). Le stop_id est un code court ("PAZ", "XBM"),
l'UIC à 7 chiffres est dans stop_code ("8754700" = Paris Austerlitz). La gare est cherchée dans OpenStreetMap
par UIC, sinon par nom.

Travelski n'a pas de pays : toutes ses gares portent le nom officiel d'OpenStreetMap, et fusion.py les réunit
avec celles des opérateurs nationaux.

Sortie : referentiel/operateurs/TRAVELSKI/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_travelski.py
    python referentiel/build_gares_travelski.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

TRAVELSKI_ZIP = os.path.join(GTFS_DIR, "feeds", "TRAVELSKI.zip")


def load_travelski_stations(path: str = TRAVELSKI_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord l'ingestion des flux (feeds/TRAVELSKI.zip)")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        code = getattr(r, "stop_code", "")
        out.append({"cle": r.stop_id, "uic": code if re.fullmatch(r"\d{7}", code) else "",
                    "nom_gtfs": " ".join(r.stop_name.split()), "lat": float(r.stop_lat), "lon": float(r.stop_lon),
                    "type": "gare", "trains": "TRAVELSKI NIGHT EXPRESS", "cars": "",
                    "codes": [("TRAVELSKI", r.stop_id)]})
    logging.info(f"   {len(out)} gares Travelski Night Express")
    return out


if __name__ == "__main__":
    run("TRAVELSKI", "Travelski Night Express", load_travelski_stations, op_country="")
