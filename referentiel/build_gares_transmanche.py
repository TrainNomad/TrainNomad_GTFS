"""
Référentiel des gares TrainNomad — ports de Transmanche Ferries / DFDS (Dieppe, Newhaven).

Source : FERRIES/transmanche_gtfs.zip, préparé par FERRIES/ingest_ferries.py.

Deux ports, de type « port » : pas de recherche dans OpenRailwayMap, position du flux, ville par géocodage.
La gare de Newhaven Harbour est à côté du port de Newhaven (correspondance à pied) ; à Dieppe, la gare est à
environ 1,5 km du terminal (correspondance par la ville).

Sortie : referentiel/operateurs/TRANSMANCHE/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_transmanche.py      (python FERRIES/ingest_ferries.py d'abord si besoin)
    python referentiel/build_gares_transmanche.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

TM_ZIP = os.path.join(GTFS_DIR, "FERRIES", "transmanche_gtfs.zip")

# stop_id -> (nom français, nom anglais)
PORTS = {
    "FR:76217:ZE:DIEPPE:ATOUMOD0118": ("Dieppe (terminal transmanche)", "Dieppe Ferry Terminal"),
    "FR:0:ZE:NEWHAVEN:ATOUMOD0118": ("Newhaven (terminal ferry)", "Newhaven Ferry Terminal"),
}


def names(st, f, country):
    fr, en = PORTS.get(st["cle"], (st["nom_gtfs"], st["nom_gtfs"]))
    return fr, en, en if country == "GB" else fr


def load_tm_ports(path: str = TM_ZIP) -> list[dict]:
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
                    "cars": "FERRY", "codes": [("TRANSMANCHE", r.stop_id)]})
    logging.info(f"   {len(out)} ports Transmanche")
    return out


if __name__ == "__main__":
    run("TRANSMANCHE", "Ports Transmanche (Dieppe, Newhaven)", load_tm_ports, op_country="FR", names=names,
        chercher_uic=False)
