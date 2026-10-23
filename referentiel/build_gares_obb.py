"""
Référentiel des gares TrainNomad — Autriche (ÖBB : Railjet, Nightjet, EuroCity, Cityjet, Regionalexpress...).

Source : AUSTRIA/at_gtfs.zip, le GTFS ÖBB préparé par AUSTRIA/ingest_at_gtfs.py.

Une gare = une gare mère du flux ("Pat:49:1349", location_type 1), ses voies sont ses arrêts enfants
("at:49:1349:0:3"). Les identifiants sont autrichiens (DHID), sans UIC : AUSTRIA/ingest_at_gtfs.py a
rapproché chaque gare de stations.csv (stop_code = id Trainline), qui donne l'UIC de secours. Les gares
déjà en base (Wien Hbf, Salzburg Hbf... venues des flux allemand, suisse ou italien) sont retrouvées par
cet UIC à la fusion : elles gagnent les codes ÖBB, sans créer de doublon.

Sortie : referentiel/operateurs/OBB/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_obb.py              (python AUSTRIA/ingest_at_gtfs.py d'abord si besoin)
    python referentiel/build_gares_obb.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_db import station_hints  # noqa: E402
from commun import GTFS_DIR, run  # noqa: E402

OBB_ZIP = os.path.join(GTFS_DIR, "AUSTRIA", "at_gtfs.zip")


def category(number: str) -> str:
    """ "RJX 162" -> "RJX" """
    m = re.match(r"[A-Za-z]+", (number or "").strip())
    return m.group(0).upper() if m else ""


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : l'UIC de la gare (uic_ref d'OSM), puis son nom, sans
    « Bahnhof » / « Bahnhst » (« Kapfenberg Bahnhof » -> « Kapfenberg ») et avec « Hbf » pour « Hauptbahnhof »."""
    out = [{"uic_ref": st["uic_secours"]}] if st["uic_secours"] else []
    names = []
    for name in [st["nom_gtfs"], *st.get("autres_noms", [])]:
        short = re.sub(r"\s+(Bahnhof|Bahnhst|Haltestelle|Bf)$", "", name).strip()
        for n in (name, short):
            names += [n, n.replace("Hauptbahnhof", "Hbf")]
    return out + list(dict.fromkeys(n for n in names if len(n) > 2))


def load_obb_stations(path: str = OBB_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python AUSTRIA/ingest_at_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "trip_short_name"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["trip_short_name"]).fillna("").map(category)
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    stops = stops.set_index("stop_id", drop=False)
    used = stops[stops["stop_id"].isin(cats.index)]
    parent = used["parent_station"].where(used["parent_station"].isin(stops.index), used["stop_id"])
    hints = station_hints()

    out = []
    for pid, grp in used.groupby(parent):
        p = stops.loc[pid]
        code = next((c for c in grp["stop_code"] if c), "")
        hint_name, uic, _ = hints.get(code, ("", "", ""))
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        out.append({"cle": pid, "uic": "", "uic_secours": uic, "ibnr": [],
                    "code_osm": ("uic_ref", uic) if uic else None,
                    "nom_gtfs": " ".join(p["stop_name"].split()), "autres_noms": [hint_name] if hint_name else [],
                    "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]), "type": "gare",
                    "trains": "|".join(trains), "cars": "",
                    "codes": [("OBB", c) for c in [pid, *children]]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic_secours'])} avec UIC de secours dans stations.csv)")
    return out


if __name__ == "__main__":
    run("OBB", "Autriche (ÖBB)", load_obb_stations, op_country="AT", search_name=search_names)
