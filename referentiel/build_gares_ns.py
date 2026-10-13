"""
Référentiel des gares TrainNomad — Pays-Bas (NS, Arriva, Keolis, Qbuzz et trains internationaux).

Source : NETHERLANDS/nl_gtfs.zip, le GTFS OVapi réduit aux trains par NETHERLANDS/ingest_nl_gtfs.py.
Le stop_id est le code de la gare aux Pays-Bas ("rtd" = Rotterdam Centraal, "asd" = Amsterdam
Centraal), sans UIC. L'UIC vient de la liste des gares de Rijden de Treinen, tirée des données NS
(sources_officielles/stations_nl.csv, téléchargée au premier lancement) : rtd -> 8400530. Elle donne
aussi le vrai UIC des gares étrangères du flux (Hannover Hbf : 8013552, pas son numéro DB).
Les quelques gares étrangères absentes de la liste prennent l'UIC de leur objet OpenStreetMap.

Le flux place quelques gares étrangères au point 0,0 (München Ost, Salzburg Hbf, St. Pölten Hbf) :
leurs coordonnées viennent alors de la même liste, sinon de COMPLEMENTS.

La gare est cherchée dans OpenStreetMap par son UIC, sinon par son nom, confirmé par son code
(railway:ref = "Rtd").

Sortie : referentiel/operateurs/NS/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_ns.py              (python NETHERLANDS/ingest_nl_gtfs.py d'abord si besoin)
    python referentiel/build_gares_ns.py --offline
"""
import logging
import os
import re
import sys
import urllib.request
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import BASE_DIR, GTFS_DIR, run  # noqa: E402

NL_ZIP = os.path.join(GTFS_DIR, "NETHERLANDS", "nl_gtfs.zip")
STATIONS_NL = os.path.join(BASE_DIR, "sources_officielles", "stations_nl.csv")
# Gares que le flux place au point 0,0 et que la liste ne connaît pas : code -> (UIC, lat, lon)
COMPLEMENTS = {"mop": ("8020234", 48.127721, 11.605519)}  # München Ost
STATIONS_NL_URL = "https://opendata.rijdendetreinen.nl/public/stations/stations-2023-09.csv"


def station_list(path: str = STATIONS_NL) -> dict:
    """{code de gare en minuscules: (UIC à 7 chiffres ou "", lat, lon)}"""
    if not os.path.exists(path):
        logging.info(f"   téléchargement de la liste des gares : {STATIONS_NL_URL}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            req = urllib.request.Request(STATIONS_NL_URL, headers={"User-Agent": "TrainNomad"})
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
                f.write(r.read())
        except OSError as e:
            logging.warning(f"   liste des gares indisponible ({e}) : gares néerlandaises sans UIC")
            return {}
    s = pd.read_csv(path, dtype=str, keep_default_na=False)
    out = {r.code.lower(): (r.uic if re.fullmatch(r"\d{7}", r.uic) else "", float(r.geo_lat), float(r.geo_lng))
           for r in s.itertuples(index=False)}
    return {**out, **COMPLEMENTS}


def search_names(st) -> list:
    """Le nom du flux, puis sans la précision entre parenthèses, puis avec "Hauptbahnhof" pour
    "Hbf" (gares allemandes), puis le code de la gare comme référence OSM."""
    name = st["nom_gtfs"]
    plain = " ".join(re.sub(r"\([^)]*\)", " ", name).split())
    out = [name, plain, name.replace("Hbf", "Hauptbahnhof"), plain.replace("Hbf", "Hauptbahnhof")]
    return list(dict.fromkeys(n for n in out if n)) + [{"ref": st["cle"].capitalize()}]


def load_ns_stations(path: str = NL_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python NETHERLANDS/ingest_nl_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "trip_long_name"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["trip_long_name"]).fillna("")
    cats = st.groupby("stop_id")["cat"].agg(lambda s: sorted(set(s) - {""}))

    known = station_list()
    out = []
    for r in stops[stops["stop_id"].isin(cats.index)].itertuples(index=False):
        uic, lat, lon = known.get(r.stop_id, ("", 0.0, 0.0))
        if abs(float(r.stop_lat)) > 1 or abs(float(r.stop_lon)) > 1:
            lat, lon = float(r.stop_lat), float(r.stop_lon)
        elif not lat and not lon:
            logging.warning(f"   {r.stop_id} ({r.stop_name}) : sans coordonnées, à ajouter à COMPLEMENTS")
        out.append({"cle": r.stop_id, "uic": uic, "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": lat, "lon": lon, "type": "gare",
                    "code_osm": ("railway:ref", r.stop_id),
                    "trains": "|".join(c.upper() for c in cats[r.stop_id]), "cars": "",
                    "codes": [("NS", r.stop_id)]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic'].startswith('84'))} aux Pays-Bas, "
                 f"{sum(1 for s in out if not s['uic'])} sans UIC dans la liste)")
    return out


if __name__ == "__main__":
    run("NS", "Pays-Bas (NS)", load_ns_stations, op_country="NL", search_name=search_names)
