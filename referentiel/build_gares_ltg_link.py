"""
Référentiel des gares TrainNomad — opérateur LTG_LINK : LTG Link (Lituanie).

Source : BALTICS/lt_gtfs.zip (BALTICS/ingest_baltics_gtfs.py). Le flux n'a pas d'UIC : la gare est cherchée dans
OpenStreetMap par nom et position.

Sortie : referentiel/operateurs/LTG_LINK/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_ltg_link.py
    python referentiel/build_gares_ltg_link.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

ZIP = os.path.join(GTFS_DIR, "BALTICS", "lt_gtfs.zip")


def load_stations(path: str = ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python BALTICS/ingest_baltics_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])
    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        out.append({"cle": r.stop_id, "uic": "", "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "LTG_LINK", "cars": "", "codes": [("LTG_LINK", r.stop_id)]})
    logging.info(f"   {len(out)} gares LTG Link (Lituanie)")
    return out


if __name__ == "__main__":
    # Vilnius - Riga : LTG Link dessert aussi la Lettonie, pas de pays imposé pour la Lituanie
    run("LTG_LINK", "LTG Link (Lituanie)", load_stations, op_country="")
