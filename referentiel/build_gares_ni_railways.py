"""
Référentiel des gares TrainNomad — Irlande du Nord (NI Railways, Translink).

Source : NORTHERN_IRELAND/nir_gtfs.zip, le GTFS converti du TransXChange de Translink par
NORTHERN_IRELAND/ingest_nir_txc.py.

Une gare = un arrêt du flux ("9000BR" Bangor, "70009000GCP" Belfast Grand Central : quais déjà réunis par le
script d'ingestion), sans UIC. La gare est cherchée dans OpenStreetMap par son nom ; « Derry~Londonderry »
(nom de la gare chez Translink) est aussi essayé en « Londonderry » et « Derry ». Les gares partagées avec
l'Enterprise d'Irish Rail (Belfast Grand Central, Lisburn, Lurgan, Portadown, Newry) sont réunies par fusion.py.

Sortie : referentiel/operateurs/NI_RAILWAYS/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_ni_railways.py    (python NORTHERN_IRELAND/ingest_nir_txc.py d'abord si besoin)
    python referentiel/build_gares_ni_railways.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

NIR_ZIP = os.path.join(GTFS_DIR, "NORTHERN_IRELAND", "nir_gtfs.zip")


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom du flux, puis ses variantes (« Derry~Londonderry »)."""
    name = st["nom_gtfs"]
    out = [name, *name.split("~")]
    if name.startswith("Belfast "):
        out.append(name[len("Belfast "):])
    return list(dict.fromkeys(n for n in out if len(n) > 2))


def load_nir_stations(path: str = NIR_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python NORTHERN_IRELAND/ingest_nir_txc.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    used = set(pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"])
    out = []
    for r in stops[stops["stop_id"].isin(used)].itertuples(index=False):
        out.append({"cle": r.stop_id, "uic": "", "nom_gtfs": r.stop_name, "lat": float(r.stop_lat),
                    "lon": float(r.stop_lon), "type": "gare", "trains": "NI Railways", "cars": "",
                    "codes": [("NI_RAILWAYS", r.stop_id)]})
    logging.info(f"   {len(out)} gares NI Railways")
    return out


if __name__ == "__main__":
    run("NI_RAILWAYS", "Irlande du Nord (NI Railways)", load_nir_stations, op_country="GB",
        search_name=search_names, chercher_uic=False)
