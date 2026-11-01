"""
Référentiel des gares TrainNomad — Hongrie (MÁV, GYSEV).

Source : HUNGARY/hu_gtfs.zip, le GTFS hongrois préparé par HUNGARY/ingest_hu_gtfs.py.

Une gare = une gare mère du flux ("1:005513912", location_type 1), ses voies sont ses arrêts enfants
("1:005513912_4"). Les chiffres après "00" sont l'UIC (Debrecen 5513912 ; "81..." en Autriche, "53..." en
Roumanie) : la gare est cherchée dans OpenStreetMap par cet UIC (uic_ref), puis par son nom ; les gares déjà
en base (Wien Hbf, Štúrovo, Bratislava...) sont retrouvées à la fusion par le même UIC. Les gares du réseau
GYSEV ont un code "43..." qui n'est pas toujours leur uic_ref dans OSM : confirmé par OSM avant d'être gardé.
Les arrêts de bus de remplacement ("1:v.0055...") sont des arrêts, sans UIC.

Sortie : referentiel/operateurs/MAV/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_mav.py              (python HUNGARY/ingest_hu_gtfs.py d'abord si besoin)
    python referentiel/build_gares_mav.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

MAV_ZIP = os.path.join(GTFS_DIR, "HUNGARY", "hu_gtfs.zip")


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : l'UIC (uic_ref d'OSM), puis le nom, puis sans précision
    (« Budapest-Keleti » -> « Budapest Keleti », « Sárrétudvari/Községháza » -> « Sárrétudvari »)."""
    name = st["nom_gtfs"]
    short = re.sub(r"\s*\(.*?\)$", "", name.split("/")[0]).strip()
    out = [{"uic_ref": st["uic"]}] if st["uic"] else []
    return out + list(dict.fromkeys(n for n in (name, short, short.replace("-", " ")) if len(n) > 2))


def load_mav_stations(path: str = MAV_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python HUNGARY/ingest_hu_gtfs.py")
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
        m = re.fullmatch(r"1:00(\d{7})", pid)
        uic = m.group(1) if m else ""
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        out.append({"cle": pid, "uic": uic, "uic_a_confirmer": uic.startswith("43"),
                    "code_osm": ("uic_ref", uic) if uic else None,
                    "nom_gtfs": " ".join(p["stop_name"].split()),
                    "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]),
                    "type": "gare" if uic else "arret_car",
                    "trains": "|".join(trains) if uic else "", "cars": "BUS" if not uic else "",
                    "codes": [("MAV", c) for c in [pid, *children]]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic'][:2] in ('55', '43'))} en Hongrie, "
                 f"{sum(1 for s in out if not s['uic'])} arrêts de bus de remplacement)")
    return out


if __name__ == "__main__":
    run("MAV", "Hongrie (MÁV, GYSEV)", load_mav_stations, op_country="HU", search_name=search_names)
