"""
Référentiel des gares TrainNomad — opérateur DSB (trains danois du GTFS Rejseplanen, avec les Öresundståg vers
la Suède et le Snälltåget Stockholm - Hambourg / Berlin).

Source : DENMARK/dk_gtfs.zip (DENMARK/ingest_dk_gtfs.py). Le stop_id est l'UIC à 7 chiffres précédé de zéros
("000008600626" = København H, "000007400003" = Malmö C). La gare est cherchée dans OpenStreetMap par UIC,
sinon par nom.

Le flux passe par trois pays (Danemark, Suède, Allemagne) : pas de pays imposé, fusion.py réunit ses gares
avec celles des opérateurs nationaux.

Sortie : referentiel/operateurs/DSB/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_dsb.py
    python referentiel/build_gares_dsb.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

DK_ZIP = os.path.join(GTFS_DIR, "DENMARK", "dk_gtfs.zip")


def load_dsb_stations(path: str = DK_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python DENMARK/ingest_dk_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        m = re.fullmatch(r"0*(\d{7})", r.stop_id)
        # "Odense St." -> "Odense" : le suffixe danois de gare n'aide pas la recherche par nom
        name = re.sub(r"\s+St\.$", "", " ".join(r.stop_name.split()))
        out.append({"cle": r.stop_id, "uic": m.group(1) if m else "", "nom_gtfs": name,
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "DSB", "cars": "", "codes": [("DSB", r.stop_id)]})
    logging.info(f"   {len(out)} gares du flux danois")
    return out


if __name__ == "__main__":
    run("DSB", "DSB (Danemark)", load_dsb_stations, op_country="")
