"""
Référentiel des gares TrainNomad — Slovaquie (ŽSR : ZSSK, RegioJet, Leo Express).

Source : SLOVAKIA/sk_gtfs.zip, le GTFS slovaque préparé par SLOVAKIA/ingest_sk_gtfs.py.

Une gare = un arrêt du flux (pas de gares mères ni de voies), stop_id = UIC à 7 chiffres, en Slovaquie
("5613206") comme à l'étranger ("5433014" Albrechtice u Č.Těš., "8100025" Baden b.Wien) : la gare est
cherchée dans OpenStreetMap par cet UIC (uic_ref), puis par son nom ; les gares déjà en base (Wien Hbf,
Břeclav, Budapest-Keleti...) sont retrouvées à la fusion par le même UIC.

Sortie : referentiel/operateurs/ZSSK/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_zssk.py              (python SLOVAKIA/ingest_sk_gtfs.py d'abord si besoin)
    python referentiel/build_gares_zssk.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

ZSSK_ZIP = os.path.join(GTFS_DIR, "SLOVAKIA", "sk_gtfs.zip")


def category(number: str) -> str:
    """ "EC 120" -> "EC" ; "rj 1033" -> "rj" (casse gardée : "RJ" est RegioJet) """
    m = re.match(r"[A-Za-z]+", (number or "").strip())
    return m.group(0) if m else ""


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : l'UIC (uic_ref d'OSM), puis le nom, puis sans abréviation
    (« Bratislava hl.st. » -> « Bratislava hlavná stanica », « Baden b.Wien » -> « Baden bei Wien »)."""
    name = st["nom_gtfs"]
    full = (name.replace("hl.st.", "hlavná stanica").replace("hl.n.", "hlavní nádraží")
            .replace("b.Wien", "bei Wien").replace(" pu", " pályaudvar"))
    short = re.sub(r"\s*\(.*?\)$", "", full).strip()
    return [{"uic_ref": st["uic"]}] + list(dict.fromkeys(n for n in (name, full, short) if len(n) > 2))


def load_zssk_stations(path: str = ZSSK_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python SLOVAKIA/ingest_sk_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "trip_short_name"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["trip_short_name"]).fillna("").map(category)
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    out = []
    for s in stops[stops["stop_id"].isin(cats.index)].to_dict("records"):
        uic = s["stop_id"] if re.fullmatch(r"\d{7}", s["stop_id"]) else ""
        out.append({"cle": s["stop_id"], "uic": uic, "code_osm": ("uic_ref", uic) if uic else None,
                    "nom_gtfs": " ".join(s["stop_name"].split()),
                    "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"]), "type": "gare",
                    "trains": "|".join(sorted(cats[s["stop_id"]])), "cars": "",
                    "codes": [("ZSSK", s["stop_id"])]})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic'].startswith('56'))} en Slovaquie)")
    return out


if __name__ == "__main__":
    run("ZSSK", "Slovaquie (ZSSK, RegioJet, Leo Express)", load_zssk_stations, op_country="SK",
        search_name=search_names)
