"""
Référentiel des gares TrainNomad — Tchéquie (Správa železnic : ČD, RegioJet, Leo Express, Arriva, trilex...).

Source : CZECH/cz_gtfs.zip, le GTFS tchèque préparé par CZECH/ingest_cz_gtfs.py.

Une gare = une gare mère du flux ("-SR70ST-CZ-33615", location_type 1), ses voies sont ses arrêts enfants
("-SR70S-CZ-33615-6"). Le code SR70 donne l'UIC des gares tchèques : "54" + code (Bzenec 5433615), vrai
seulement en Tchéquie (uic_pays) : les gares étrangères du flux ("-SR70ST-DE-...") ont un code SR70 qui n'est
pas un UIC. Elles sont cherchées par leur nom dans OpenStreetMap, qui donne leur UIC : les gares déjà en base
(Bad Schandau, Gmünd NÖ, Kúty...) sont retrouvées à la fusion.

Sortie : referentiel/operateurs/CD/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_cd.py              (python CZECH/ingest_cz_gtfs.py d'abord si besoin)
    python referentiel/build_gares_cd.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

CD_ZIP = os.path.join(GTFS_DIR, "CZECH", "cz_gtfs.zip")


def category(number: str) -> str:
    """ "SC 516" -> "SC" ; "rj 578" -> "rj" (casse gardée : "RJ" est RegioJet) """
    m = re.match(r"[A-Za-z]+", (number or "").strip())
    return m.group(0) if m else ""


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : l'UIC de la gare (uic_ref d'OSM), puis son nom, puis son
    nom sans précision entre parenthèses ni suffixe de quartier (« Praha-Libeň » -> « Praha Libeň »)."""
    out = [{"uic_ref": st["uic"]}] if st["uic"] else []
    short = re.sub(r"\s*\(.*?\)$", "", st["nom_gtfs"]).strip()
    names = [st["nom_gtfs"], short, short.replace("-", " ")]
    return out + list(dict.fromkeys(n for n in names if len(n) > 2))


def load_cd_stations(path: str = CD_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python CZECH/ingest_cz_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "trip_short_name"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["trip_short_name"]).fillna("").map(category)
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    stops = stops.set_index("stop_id", drop=False)
    used = stops[stops["stop_id"].isin(cats.index)]
    parent = used["parent_station"].where(used["parent_station"].isin(stops.index), used["stop_id"])

    out = []
    for pid, grp in used.groupby(parent):
        p = stops.loc[pid]
        m = re.match(r"-SR70ST?-CZ-(\d{5})", pid)
        uic = f"54{m.group(1)}" if m else ""
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        out.append({"cle": pid, "uic": uic, "uic_pays": "CZ", "code_osm": ("uic_ref", uic) if uic else None,
                    "nom_gtfs": " ".join(p["stop_name"].split()),
                    "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]), "type": "gare",
                    "trains": "|".join(trains), "cars": "",
                    "codes": [("CD", c) for c in [pid, *children]]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic'])} tchèques avec UIC, "
                 f"{sum(1 for s in out if not s['uic'])} étrangères cherchées par leur nom)")
    return out


if __name__ == "__main__":
    run("CD", "Tchéquie (ČD, RegioJet, Leo Express...)", load_cd_stations, op_country="CZ", search_name=search_names)
