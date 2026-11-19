"""
Référentiel des gares TrainNomad — opérateur ZOU ! Intermétropole (trains Marseille - Toulon - Nice exploités
par Transdev Rail Sud Intermétropole pour la Région Sud).

Source : feeds/ZOU.zip (GTFS Transdev, transport.data.gouv.fr). Le stop_id est l'UIC à 8 chiffres
("87751008" = Marseille-St-Charles), le stop_code le code gare SNCF à trois lettres ("MSC"). La gare est
cherchée dans OpenStreetMap par UIC (7 premiers chiffres), sinon par nom.

Sortie : referentiel/operateurs/ZOU/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_zou.py
    python referentiel/build_gares_zou.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

ZOU_ZIP = os.path.join(GTFS_DIR, "feeds", "ZOU.zip")


def load_zou_stations(path: str = ZOU_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord l'ingestion des flux (feeds/ZOU.zip)")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        uic = r.stop_id[:7] if re.fullmatch(r"\d{8}", r.stop_id) else ""
        out.append({"cle": r.stop_id, "uic": uic, "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "ZOU! INTERMETROPOLE", "cars": "", "codes": [("ZOU", r.stop_id)]})
    logging.info(f"   {len(out)} gares ZOU ! Intermétropole")
    return out


if __name__ == "__main__":
    run("ZOU", "ZOU ! Intermétropole", load_zou_stations, op_country="FR")
