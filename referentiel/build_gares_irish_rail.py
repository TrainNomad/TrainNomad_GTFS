"""
Référentiel des gares TrainNomad — Irlande (Iarnród Éireann / Irish Rail, Enterprise Dublin - Belfast).

Source : IRELAND/ie_gtfs.zip, le GTFS irlandais préparé par IRELAND/ingest_ie_gtfs.py.

Une gare = un arrêt du flux ("8220IR0132", sans gare mère ni UIC). La gare est cherchée dans OpenStreetMap
par son nom, nettoyé des écritures du flux :
  - nom commémoratif entre parenthèses accolé au nom de la ville, comme sur les panneaux et dans OSM :
    « Cork (Kent) » -> « Cork Kent », « Galway (Ceannt) » -> « Galway Ceannt »
  - « Stn », « Station » retirés (« Enfield Stn », « Tralee Casement Station »)
  - quelques noms incomplets du flux complétés (RENAME : « Connolly » -> « Dublin Connolly »)
Les gares d'Irlande du Nord desservies par l'Enterprise (Belfast, Lisburn, Lurgan, Portadown, Newry) sont au
Royaume-Uni (GB) : nom officiel d'OSM, et fusion.py les réunit avec celles d'un futur flux Translink.

Sortie : referentiel/operateurs/IRISH_RAIL/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_irish_rail.py      (python IRELAND/ingest_ie_gtfs.py d'abord si besoin)
    python referentiel/build_gares_irish_rail.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

IE_ZIP = os.path.join(GTFS_DIR, "IRELAND", "ie_gtfs.zip")

# noms du flux incomplets ou mal écrits -> nom de la gare
RENAME = {
    "Connolly": "Dublin Connolly",
    "Belfast": "Belfast Grand Central",
    "Muine Bheag (Bagenalstown)": "Bagenalstown",
    "Wexford (O Hanrahan)": "Wexford O'Hanrahan",
    "Dun Laoghaire (Mallin)": "Dún Laoghaire Mallin",
    "Leixlip(Confey)": "Leixlip Confey",
    "Leixlip(Louisa Bridge)": "Leixlip Louisa Bridge",
    "Tralee Casement Station": "Tralee Casement",
}


def clean_name(name: str) -> str:
    """ "Cork (Kent)" -> "Cork Kent" ; "Enfield Stn" -> "Enfield" ; "Connolly" -> "Dublin Connolly" """
    name = " ".join(name.split())
    if name in RENAME:
        return RENAME[name]
    name = re.sub(r"\s+(Stn|Station)$", "", name)
    name = re.sub(r"\s*\(([^)]*)\)", r" \1", name)
    return " ".join(name.split())


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom nettoyé, le nom sans le nom commémoratif (« Cork »),
    puis le nom brut du flux."""
    clean = st["nom_gtfs"]
    short = re.sub(r"\s*\([^)]*\)", "", st["nom_brut"]).strip()
    short = re.sub(r"\s+(Stn|Station)$", "", short)
    return list(dict.fromkeys(n for n in (clean, short, st["nom_brut"]) if len(n) > 2))


def load_ie_stations(path: str = IE_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python IRELAND/ingest_ie_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_desc"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["route_desc"]).fillna("")
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    out = []
    for r in stops[stops["stop_id"].isin(cats.index)].itertuples(index=False):
        out.append({"cle": r.stop_id, "uic": "", "nom_brut": r.stop_name, "nom_gtfs": clean_name(r.stop_name),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "|".join(sorted(cats[r.stop_id])), "cars": "",
                    "codes": [("IRISH_RAIL", r.stop_id)]})
    logging.info(f"   {len(out)} gares Irish Rail")
    return out


if __name__ == "__main__":
    run("IRISH_RAIL", "Irlande (Irish Rail, Enterprise)", load_ie_stations, op_country="IE",
        search_name=search_names, chercher_uic=False)
