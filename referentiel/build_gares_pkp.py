"""
Référentiel des gares TrainNomad — Pologne (PKP PLK : PKP Intercity, PolRegio, Koleje Mazowieckie...).

Source : POLAND/pl_gtfs.zip, le GTFS polonais préparé par POLAND/ingest_pl_gtfs.py.

Une gare = une gare mère du flux ("33605", location_type 1), ses voies sont ses arrêts enfants
("33605_RAIL_1_5", "33605_FALLBACK"). Le code est interne à PKP PLK, pas un UIC : la gare est cherchée par son
nom dans OpenRailwayMap, au plus près de sa position (toutes les gares du flux en ont une, gares étrangères
comprises), et prend l'UIC d'OpenStreetMap : les gares déjà en base (Kraków Główny, Frankfurt (Oder), Bohumín...
venues des flux allemand ou tchèque) sont retrouvées à la fusion. Les gares ukrainiennes arrivent en alphabet
latin (translittérées par l'ingestion).

Sortie : referentiel/operateurs/PKP/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_pkp.py              (python POLAND/ingest_pl_gtfs.py d'abord si besoin)
    python referentiel/build_gares_pkp.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

PKP_ZIP = os.path.join(GTFS_DIR, "POLAND", "pl_gtfs.zip")


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom, puis sans précision entre parenthèses ni abréviation
    (« Grodzisk Wlkp. » -> « Grodzisk Wielkopolski », « Frankfurt (Oder) » -> « Frankfurt »)."""
    name = st["nom_gtfs"]
    full = name.replace("Wlkp.", "Wielkopolski").replace("Maz.", "Mazowiecki").replace("Śl.", "Śląski")
    short = re.sub(r"\s*\(.*?\)$", "", full).strip()
    return list(dict.fromkeys(n for n in (name, full, short, short.replace("-", " ")) if len(n) > 2))


def load_pkp_stations(path: str = PKP_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python POLAND/ingest_pl_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_desc"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["route_desc"]).fillna("")
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    stops = stops.set_index("stop_id", drop=False)
    used = stops[stops["stop_id"].isin(cats.index)]
    parent = used["parent_station"].where(used["parent_station"].isin(stops.index), used["stop_id"])

    out = []
    for pid, grp in used.groupby(parent):
        p = stops.loc[pid]
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        out.append({"cle": pid, "uic": "", "code_osm": None,
                    "nom_gtfs": " ".join(p["stop_name"].split()),
                    "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]), "type": "gare",
                    "trains": "|".join(trains), "cars": "",
                    "codes": [("PKP", c) for c in [pid, *children]]})
    foreign = sum(1 for s in out if stops.loc[s["cle"], "country"] not in ("", "PL"))
    logging.info(f"   {len(out)} gares ({foreign} hors de Pologne)")
    return out


if __name__ == "__main__":
    run("PKP", "Pologne (PKP Intercity, PolRegio, Koleje...)", load_pkp_stations, op_country="PL",
        search_name=search_names)
