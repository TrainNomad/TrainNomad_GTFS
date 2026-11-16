"""
Référentiel des gares TrainNomad — Croatie (HŽPP).

Source : CROATIA/hr_gtfs.zip, le GTFS croate préparé par CROATIA/ingest_hr_gtfs.py.

Une gare = un arrêt du flux ("i-o523" = Zagreb Glavni kolodvor), sans gare mère ni UIC. La gare est cherchée
dans OpenStreetMap par son nom (« Zagreb Glavni kolodvor », puis sans « kolodvor ») ; les gares déjà en base
par les flux ÖBB et MÁV (Zagreb, Koprivnica, Rijeka...) sont retrouvées à la fusion. Un arrêt desservi
seulement par des bus de remplacement (route_type 3) est un arrêt de car.

Sortie : referentiel/operateurs/HZPP/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_hzpp.py      (python CROATIA/ingest_hr_gtfs.py d'abord si besoin)
    python referentiel/build_gares_hzpp.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

HR_ZIP = os.path.join(GTFS_DIR, "CROATIA", "hr_gtfs.zip")


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom du flux, puis sans « kolodvor » / « stajalište »
    (« Zagreb Glavni kolodvor » -> « Zagreb Glavni »)."""
    name = st["nom_gtfs"]
    short = re.sub(r"\s+(glavni\s+)?(kolodvor|stajalište|stajaliste)$", "", name, flags=re.I).strip()
    return list(dict.fromkeys(n for n in (name, short, short.replace("-", " ")) if len(n) > 2))


def load_stations(path: str, op: str) -> list[dict]:
    """Gares d'un GTFS sans gare mère ni UIC (Croatie, Slovénie) : catégorie "Vlak" ou "Bus" par arrêt."""
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord le script d'ingestion du flux")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False, usecols=["route_id", "route_type"])
    rtype = trips.set_index("trip_id")["route_id"].map(routes.set_index("route_id")["route_type"])
    st["cat"] = st["trip_id"].map(rtype).map({"3": "Bus"}).fillna("Vlak")
    cats = st.groupby("stop_id")["cat"].agg(set)

    out = []
    for r in stops[stops["stop_id"].isin(cats.index)].itertuples(index=False):
        train = "Vlak" in cats[r.stop_id]
        out.append({"cle": r.stop_id, "uic": "", "nom_brut": r.stop_name, "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare" if train else "arret_car",
                    "trains": "Vlak" if train else "", "cars": "" if train else "BUS",
                    "codes": [(op, r.stop_id)]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['type'] == 'arret_car')} desservies seulement "
                 f"par des bus de remplacement)")
    return out


if __name__ == "__main__":
    run("HZPP", "Croatie (HŽPP)", lambda: load_stations(HR_ZIP, "HZPP"), op_country="HR",
        search_name=search_names, chercher_uic=False)
