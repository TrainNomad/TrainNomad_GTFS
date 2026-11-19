"""
Référentiel des gares TrainNomad — ports d'Irish Ferries vers la France (Dublin, Cherbourg).

Source : FERRIES/irish_ferries_gtfs.zip, préparé par FERRIES/ingest_ferries.py (depuis le CSV tiré des PDF de la
compagnie, FERRIES/irish_ferries_pdf.py).

Deux ports, de type « port » : pas de recherche dans OpenRailwayMap, position du flux, ville par géocodage.
Dublin : terminal 1 de Dublin Port (Alexandra Road), distinct du terminal Stena (« Dublin Port Stena » de
National Rail). Cherbourg : même gare maritime que Brittany Ferries ; fusion.py rattache les deux ports à la
ville de Cherbourg.

Sortie : referentiel/operateurs/IRISH_FERRIES/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_irish_ferries.py    (python FERRIES/ingest_ferries.py d'abord si besoin)
    python referentiel/build_gares_irish_ferries.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

IF_ZIP = os.path.join(GTFS_DIR, "FERRIES", "irish_ferries_gtfs.zip")

# stop_id -> (nom français, nom anglais)
PORTS = {
    "IE DUB": ("Dublin Port (terminal 1)", "Dublin Port Terminal 1"),
    "FR CER": ("Cherbourg (port)", "Cherbourg Port"),
}


def names(st, f, country):
    fr, en = PORTS.get(st["cle"], (st["nom_gtfs"], st["nom_gtfs"]))
    return fr, en, en if country == "IE" else fr


def load_if_ports(path: str = IF_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python FERRIES/ingest_ferries.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])
    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        if r.stop_id not in PORTS:
            logging.warning(f"   ⚠️ port inconnu de PORTS, nom du flux gardé : {r.stop_id} {r.stop_name}")
        out.append({"cle": r.stop_id, "uic": "", "nom_gtfs": PORTS.get(r.stop_id, (r.stop_name,))[0],
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "port", "trains": "",
                    "cars": "FERRY", "codes": [("IRISH_FERRIES", r.stop_id)]})
    logging.info(f"   {len(out)} ports Irish Ferries")
    return out


if __name__ == "__main__":
    run("IRISH_FERRIES", "Ports Irish Ferries (Dublin, Cherbourg)", load_if_ports, op_country="IE", names=names,
        chercher_uic=False)
