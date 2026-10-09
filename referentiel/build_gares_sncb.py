"""
Référentiel des gares TrainNomad — opérateur SNCB/NMBS (Belgique).

Source : SNCB/sncb_gtfs.zip, le GTFS SNCB filtré (trains uniquement) produit par
SNCB/ingest_sncb_gtfs.py. L'UIC est dans le stop_id : gare "gs:nmbssncb:S8814001",
quai "gs:nmbssncb:8814001_12". La gare est cherchée dans OpenStreetMap par UIC (exact), sinon par nom.

Noms : le GTFS SNCB est en français ("Bruxelles-Midi", "Anvers-Central", "Saint-Trond") : c'est le
nom_fr des gares belges ; OSM donne le nom local (néerlandais en Flandre). Gares étrangères
("Paris Nord (FR)") : nom officiel local d'OSM.

Sortie : referentiel/operateurs/SNCB/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_sncb.py              (python SNCB/ingest_sncb_gtfs.py d'abord si besoin)
    python referentiel/build_gares_sncb.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

SNCB_ZIP = os.path.join(GTFS_DIR, "SNCB", "sncb_gtfs.zip")


def clean_sncb_name(name: str) -> str:
    """ "Paris Nord (FR)" -> "Paris Nord", "Lille Flandres (FR)" -> "Lille Flandres" """
    return " ".join(re.sub(r"\s*\((FR|DE|NL|LU|GB|AT|CH)\)\s*$", "", name or "").split())


def load_sncb_stations(path: str = SNCB_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python SNCB/ingest_sncb_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False)

    # catégories de trains (IC, L, P, S1 -> S...) qui desservent chaque arrêt
    cat = trips.set_index("trip_id")["route_id"].map(routes.set_index("route_id")["route_short_name"])
    st["cat"] = st["trip_id"].map(cat).fillna("").str.upper().str.replace(r"^S\d+$", "S", regex=True)
    stops["uic"] = stops["stop_id"].str.extract(r"nmbssncb:S?(\d{7})")[0]
    used = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    out = []
    for uic, g in stops[stops["uic"].notna()].groupby("uic"):
        served = set().union(*[used.get(i, set()) for i in g["stop_id"]])
        if not any(i in used.index for i in g["stop_id"]):
            continue  # arrêt sans train
        parent = g[g["stop_id"].str.match(r"gs:nmbssncb:S\d{7}$")]
        src = parent.iloc[0] if len(parent) else g.iloc[0]
        out.append({"cle": uic, "uic": uic, "nom_gtfs": clean_sncb_name(src["stop_name"]),
                    "lat": float(src["stop_lat"]), "lon": float(src["stop_lon"]), "type": "gare",
                    "trains": "|".join(sorted(served)), "cars": "",
                    "codes": [("SNCB", i) for i in sorted(g["stop_id"])]})
    logging.info(f"   {len(out)} gares SNCB ({sum(1 for s in out if s['uic'].startswith('88'))} en Belgique)")
    return out


if __name__ == "__main__":
    run("SNCB", "SNCB / NMBS (Belgique)", load_sncb_stations, op_country="BE")
