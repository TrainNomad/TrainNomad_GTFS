"""
Référentiel des gares TrainNomad — Royaume-Uni (National Rail : LNER, Avanti West Coast, GWR,
ScotRail, Northern, Southeastern, Caledonian Sleeper...).

Source : UK/uk_gtfs.zip, le GTFS produit par UK/ingest_uk_gtfs.py à partir des horaires National Rail.
Le stop_id est le code CRS à 3 lettres ("EDB" = Edinburgh), sans UIC. L'UIC vient du fichier officiel
CORPUS de Network Rail (sources_officielles/CORPUSExtract.json.gz) : UIC = "70" + son champ UIC
(EDB -> 93280 -> 7093280). Les gares récentes absentes de CORPUS (Cambridge South, Edinburgh
Gateway) prennent l'id de leur objet OpenStreetMap.

OpenStreetMap porte rarement l'UIC des gares britanniques mais presque toujours leur code CRS
(ref:crs) : la gare est cherchée par son nom et confirmée par ce code (statut "code"), ce qui
évite une requête par gare et accepte un nom écrit autrement ; si le nom ne donne rien, elle est
cherchée par ce code ("London Kings Cross" / "London King's Cross").

Types : gare (train ou métro), arrêt de car (bus seulement : "Keswick Bus"), port (ferry seulement).
Les catégories de trains sont les compagnies (LNER, ScotRail...).

Sortie : referentiel/operateurs/NATIONAL_RAIL/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_uk.py              (python UK/ingest_uk_gtfs.py d'abord si besoin)
    python referentiel/build_gares_uk.py --offline
"""
import gzip
import json
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import BASE_DIR, GTFS_DIR, run  # noqa: E402

UK_ZIP = os.path.join(GTFS_DIR, "UK", "uk_gtfs.zip")
CORPUS = os.path.join(BASE_DIR, "sources_officielles", "CORPUSExtract.json.gz")
MODES = {"1": "metro", "2": "train", "3": "bus", "4": "ferry"}


def corpus_uic(path: str = CORPUS) -> dict:
    """{CRS: UIC à 7 chiffres} d'après CORPUS (Network Rail)."""
    if not os.path.exists(path):
        logging.warning(f"   {path} absent : gares britanniques sans UIC")
        return {}
    out = {}
    for r in json.load(gzip.open(path, "rt", encoding="utf-8"))["TIPLOCDATA"]:
        crs, uic = (r.get("3ALPHA") or "").strip(), (r.get("UIC") or "").strip()
        if crs and re.fullmatch(r"\d{5}", uic):
            out.setdefault(crs, "70" + uic)
    return out


def clean_uk_name(name: str) -> str:
    """ "Maidenhead Station" -> "Maidenhead" ; les arrêts de car gardent leur nom ("Keswick Bus")."""
    return " ".join(re.sub(r"\s+(Rail\s+)?Station$", "", name or "").split())


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom du flux, le nom sans sa précision
    ("Mossley (Grtr Manchester)" -> "Mossley"), puis le code CRS comme référence de la gare
    ("London Kings Cross" s'écrit "London King's Cross" dans OSM : introuvable par son nom ;
    "Dunkeld & Birnam" -> "Dunkeld Birnam")."""
    name = st["nom_gtfs"]
    out = [name]
    # sans la précision, puis sans "&" ni ponctuation (la recherche ne trouve rien avec "&")
    # et enfin le début du nom : aucun nom contenant "&" n'est trouvé, "Dunkeld" trouve "Dunkeld & Birnam"
    for n in (name.split("(")[0].strip(), " ".join(re.sub(r"[^\w\s]", " ", name).split()),
              re.split(r"\s*[&(-]", name)[0].strip()):
        if n and n not in out:
            out.append(n)
    return out + [{"ref": st["cle"]}]


def load_uk_stations(path: str = UK_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python UK/ingest_uk_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False).set_index("route_id")
    agency = pd.read_csv(z.open("agency.txt"), dtype=str, keep_default_na=False).set_index("agency_id")

    route = st["trip_id"].map(trips.set_index("trip_id")["route_id"])
    st["mode"] = route.map(routes["route_type"]).map(MODES).fillna("")
    st["cat"] = route.map(routes["agency_id"]).map(agency["agency_name"]).fillna("")
    modes = st.groupby("stop_id")["mode"].agg(lambda s: set(s) - {""})
    rail = st[st["mode"].isin(["train", "metro"])].groupby("stop_id")["cat"].agg(lambda s: sorted(set(s) - {""}))

    uic = corpus_uic()
    out = []
    for r in stops[stops["stop_id"].isin(modes.index)].itertuples(index=False):
        m = modes[r.stop_id]
        type_ = "gare" if m & {"train", "metro"} else ("port" if m == {"ferry"} else "arret_car")
        out.append({"cle": r.stop_id, "uic": uic.get(r.stop_id, ""), "nom_gtfs": clean_uk_name(r.stop_name),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": type_,
                    "code_osm": ("ref:crs", r.stop_id),
                    "trains": "|".join(rail.get(r.stop_id, [])),
                    "cars": "|".join(x for x, k in (("BUS", "bus"), ("FERRY", "ferry")) if k in m),
                    "codes": [("CRS", r.stop_id)]})
    n = {t: sum(1 for s in out if s["type"] == t) for t in ("gare", "arret_car", "port")}
    logging.info(f"   {len(out)} arrêts au Royaume-Uni ({n['gare']} gares, {n['arret_car']} arrêts de car, "
                 f"{n['port']} ports, {sum(1 for s in out if not s['uic'])} sans UIC)")
    return out


if __name__ == "__main__":
    run("NATIONAL_RAIL", "Royaume-Uni (National Rail)", load_uk_stations, op_country="GB", chercher_uic=False,
        search_name=search_names)
