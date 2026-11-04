"""
Référentiel des gares TrainNomad — ports de Brittany Ferries (Royaume-Uni, Irlande, France, Espagne).

Source : FERRIES/brittany_ferries_gtfs.zip, préparé par FERRIES/ingest_ferries.py.

Un port = un arrêt du flux ("GB PME" Portsmouth, "IE ROE" Rosslare, code pays + code du port), de type
« port » : il n'est pas cherché dans OpenRailwayMap, sa position est celle du flux et sa ville est trouvée par
géocodage. Le flux ne donne que le nom de la ville (« Cork », « Cherbourg ») : PORTS donne le nom du port
(Cork = Ringaskiddy, à 20 km de la ville ; Bilbao = Santurtzi). fusion.py le réunit avec le même port d'un
autre flux (Rosslare Harbour de National Rail) ; la correspondance avec la gare voisine passe par la ville.

Sortie : referentiel/operateurs/BRITTANY_FERRIES/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_brittany_ferries.py     (python FERRIES/ingest_ferries.py d'abord si besoin)
    python referentiel/build_gares_brittany_ferries.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

BF_ZIP = os.path.join(GTFS_DIR, "FERRIES", "brittany_ferries_gtfs.zip")

# stop_id -> (nom français, nom anglais)
PORTS = {
    "GB PME": ("Portsmouth (port international)", "Portsmouth International Port"),
    "GB POO": ("Poole (port)", "Poole Port"),
    "GB PLY": ("Plymouth (Millbay)", "Plymouth Millbay"),
    "IE ROE": ("Rosslare Europort", "Rosslare Europort"),
    "IE ORK": ("Cork (Ringaskiddy)", "Cork Ringaskiddy"),
    "FR OUI": ("Caen - Ouistreham (port)", "Caen Ouistreham Port"),
    "FR CER": ("Cherbourg (port)", "Cherbourg Port"),
    "FR LEH": ("Le Havre (terminal transmanche)", "Le Havre Ferry Terminal"),
    "FR SML": ("Saint-Malo (terminal du Naye)", "Saint-Malo Ferry Terminal"),
    "FR ROS": ("Roscoff (port du Bloscon)", "Roscoff Bloscon"),
    "ES BIO": ("Bilbao (Santurtzi)", "Bilbao Santurtzi"),
    "ES SDR": ("Santander (port)", "Santander Port"),
}


def names(st, f, country):
    fr, en = PORTS.get(st["cle"], (st["nom_gtfs"], st["nom_gtfs"]))
    return fr, en, en if country in ("GB", "IE") else fr


def load_bf_ports(path: str = BF_ZIP) -> list[dict]:
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
                    "cars": "FERRY", "codes": [("BRITTANY_FERRIES", r.stop_id)]})
    logging.info(f"   {len(out)} ports Brittany Ferries")
    return out


if __name__ == "__main__":
    run("BRITTANY_FERRIES", "Ports Brittany Ferries", load_bf_ports, op_country="FR", names=names,
        chercher_uic=False)
