"""
Référentiel des gares TrainNomad — opérateur Eurostar (Londres, Paris, Bruxelles, Amsterdam, Cologne,
trains des neiges vers la Tarentaise).

Source : feeds/EUROSTAR.zip (GTFS Eurostar). Chaque gare est une zone ("paris_nord_station_area")
dont les quais sont les arrêts desservis ("paris_nord_9") ; le stop_code est l'UIC à 7 chiffres.
La gare est cherchée dans OpenStreetMap par UIC, sinon par nom.

Eurostar n'a pas de pays : toutes ses gares portent le nom officiel d'OpenStreetMap, et fusion.py
les réunit avec celles de la SNCF, de la SNCB ou du Royaume-Uni. Londres St Pancras a deux UIC
(7015400 chez Eurostar, 7015550 côté britannique) : c'est le même objet OSM, donc une seule gare.

Sortie : referentiel/operateurs/EUROSTAR/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_eurostar.py
    python referentiel/build_gares_eurostar.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

EUROSTAR_ZIP = os.path.join(GTFS_DIR, "feeds", "EUROSTAR.zip")


def load_eurostar_stations(path: str = EUROSTAR_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord l'ingestion des flux (feeds/EUROSTAR.zip)")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False, encoding="utf-8")
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])

    # un quai appartient à sa zone de gare (parent_station) ; un arrêt sans zone est sa propre gare
    stops["gare"] = stops["parent_station"].where(stops["parent_station"] != "", stops["stop_id"])
    by_id = stops.set_index("stop_id")
    out = []
    for gare, g in stops.groupby("gare"):
        if not (set(g["stop_id"]) & used):
            continue  # gare sans train
        src = by_id.loc[gare] if gare in by_id.index else g.iloc[0]
        code = src["stop_code"] or next((c for c in g["stop_code"] if c), "")
        out.append({"cle": gare, "uic": code if re.fullmatch(r"\d{7}", code) else "",
                    "nom_gtfs": " ".join(src["stop_name"].split()),
                    "lat": float(src["stop_lat"]), "lon": float(src["stop_lon"]), "type": "gare",
                    "trains": "EUROSTAR", "cars": "",
                    "codes": [("EUROSTAR", i) for i in sorted(set(g["stop_id"]) | {gare})]})
    logging.info(f"   {len(out)} gares Eurostar")
    return out


if __name__ == "__main__":
    run("EUROSTAR", "Eurostar", load_eurostar_stations, op_country="")
