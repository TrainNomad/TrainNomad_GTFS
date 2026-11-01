"""
Référentiel des gares TrainNomad — Tchéquie (Správa železnic : ČD, RegioJet, Leo Express, Arriva, trilex...).

Source : CZECH/cz_gtfs.zip, le GTFS tchèque préparé par CZECH/ingest_cz_gtfs.py.

Une gare = une gare mère du flux ("-SR70ST-CZ-33615", location_type 1), ses voies sont ses arrêts enfants
("-SR70S-CZ-33615-6"). Le code SR70 donne l'UIC des gares tchèques : "54" + code (Bzenec 5433615), vrai
seulement en Tchéquie (uic_pays) : les gares étrangères du flux ("-SR70ST-DE-...") ont un code SR70 qui n'est
pas un UIC. Le flux ne leur donne pas non plus de position (0, 0) : elle est cherchée par leur nom dans
OpenRailwayMap, au plus près des gares tchèques voisines sur les trajets (locate_foreign), puis leur UIC vient
d'OpenStreetMap : les gares déjà en base (Bad Schandau, Gmünd NÖ, Kúty...) sont retrouvées à la fusion.

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
from commun import BASE_DIR, GTFS_DIR, RAILWAY_KINDS, fold, km, run  # noqa: E402
from sources import OpenRailwayMap  # noqa: E402

CD_ZIP = os.path.join(GTFS_DIR, "CZECH", "cz_gtfs.zip")
FOREIGN_MAX_KM = 60.0  # gare étrangère : à moins de 60 km d'une gare tchèque voisine sur un trajet


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


def neighbours(st: pd.DataFrame, stops: pd.DataFrame) -> dict:
    """Arrêt sans position -> positions des derniers arrêts placés avant et après lui sur ses trajets (Retz :
    Znojmo, par-delà Šatov et Unterretzbach, elle-même sans position)."""
    pos = stops[stops["stop_lat"].astype(float) != 0][["stop_lat", "stop_lon"]].astype(float)
    st = st.assign(seq=st["stop_sequence"].astype(int)).sort_values(["trip_id", "seq"])
    st["placed"] = st["stop_id"].where(st["stop_id"].isin(pos.index))
    st["prev"] = st.groupby("trip_id")["placed"].ffill()
    st["next"] = st.groupby("trip_id")["placed"].bfill()
    out = {}
    lost = st[st["placed"].isna()]
    for sid, grp in lost.groupby("stop_id"):
        near = set(grp["prev"].dropna()) | set(grp["next"].dropna())
        out[sid] = [tuple(pos.loc[n]) for n in near if n in pos.index]
    return out


def foreign_names(name: str) -> list:
    """Écritures à chercher pour une gare étrangère, le flux tchèque abrégeant leurs noms : « Brestovec z »
    (zastávka), « Krippen Hp », « Grossschönau/S. », « Rathmannsdorf(Pirna) », « Szkl. Poręba Huta »."""
    short = re.sub(r"\s+(z|Hp)$", "", name)
    short = re.sub(r"\s+zast(ávka|\.)?$", "", short)
    short = re.sub(r"/S\.$", "", short)
    short = re.sub(r"\s*\(.*?\)$", "", short).replace("Szkl. ", "Szklarska ").strip()
    # « Grossschönau » : « Großschönau » dans OSM
    return list(dict.fromkeys(n for n in (name, short, short.replace("-", " "), short.replace("ss", "ß"))
                              if len(n) > 2))


def locate_foreign(st: dict, anchors: list, orm: OpenRailwayMap, known: list) -> tuple | None:
    """Position d'une gare étrangère sans coordonnées : la gare OSM de même nom (ou nom contenu) la plus
    proche des gares voisines du trajet, sinon une gare de même nom déjà dans le référentiel commun."""
    names = foreign_names(st["nom_gtfs"])
    folded = {fold(n) for n in names}
    best = None
    for name in names:
        res = orm.facility(name=name)
        if res is None:  # hors ligne et pas en cache
            break
        for f in res:
            o = fold(f.get("name", ""))
            if f.get("railway") not in RAILWAY_KINDS or f.get("latitude") is None or not o \
                    or not any(o == g or o in g or g in o for g in folded):
                continue
            d = min(km(a[0], a[1], f["latitude"], f["longitude"]) for a in anchors)
            if d <= FOREIGN_MAX_KM and (best is None or (o not in folded, d) < best[0]):
                best = ((o not in folded, d), (f["latitude"], f["longitude"]))
        if best:
            return best[1]
    for k in known:
        if fold(k["nom_fr"]) in folded or fold(k["nom_local"]) in folded:
            d = min(km(a[0], a[1], k["lat"], k["lon"]) for a in anchors)
            if d <= FOREIGN_MAX_KM and (best is None or d < best[0]):
                best = (d, (k["lat"], k["lon"]))
    return best[1] if best else None


def load_cd_stations(path: str = CD_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python CZECH/ingest_cz_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id", "stop_sequence"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "trip_short_name"])
    st["cat"] = st["trip_id"].map(trips.set_index("trip_id")["trip_short_name"]).fillna("").map(category)
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    stops = stops.set_index("stop_id", drop=False)
    used = stops[stops["stop_id"].isin(cats.index)]
    parent = used["parent_station"].where(used["parent_station"].isin(stops.index), used["stop_id"])
    # arrêt isolé "-SR70S-DE-15614" à côté de sa gare mère "-SR70ST-DE-15614" (Klingenthal) : une seule gare
    mother = parent.str.replace("-SR70S-", "-SR70ST-", regex=False)
    parent = mother.where(parent.str.fullmatch(r"-SR70S-[A-Z]{2}-\d{5}") & mother.isin(stops.index), parent)
    near = neighbours(st, stops)
    orm = OpenRailwayMap()
    gares_csv = os.path.join(BASE_DIR, "gares.csv")
    known = pd.read_csv(gares_csv, sep=";", dtype=str, keep_default_na=False) if os.path.exists(gares_csv) else None
    known = [] if known is None else [
        {**k, "lat": float(k["lat"]), "lon": float(k["lon"])} for k in known.to_dict("records")
        if k["id"].startswith("TN") and not k["id"].startswith("TNCD-") and float(k["lat"] or 0) != 0]

    out, lost = [], []
    for pid, grp in used.groupby(parent):
        p = stops.loc[pid]
        m = re.match(r"-SR70ST?-CZ-(\d{5})", pid)
        uic = f"54{m.group(1)}" if m else ""
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        g = {"cle": pid, "uic": uic, "uic_pays": "CZ", "code_osm": ("uic_ref", uic) if uic else None,
             "nom_gtfs": " ".join(p["stop_name"].split()),
             "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]), "type": "gare",
             "trains": "|".join(trains), "cars": "",
             "codes": [("CD", c) for c in [pid, *children]]}
        if g["lat"] == 0 and g["lon"] == 0:
            anchors = [a for c in grp["stop_id"] for a in near.get(c, [])]
            found = locate_foreign(g, anchors, orm, known) if anchors else None
            if not found:
                lost.append(g["nom_gtfs"])
                continue  # sans position, la gare tracerait des lignes jusqu'au golfe de Guinée
            g["lat"], g["lon"] = found
        out.append(g)
    orm.save()
    if lost:
        logging.warning(f"   ⚠️ {len(lost)} gares étrangères sans position, laissées de côté : {', '.join(lost)}")
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['uic'])} tchèques avec UIC, "
                 f"{sum(1 for s in out if not s['uic'])} étrangères placées par leur nom)")
    return out


if __name__ == "__main__":
    run("CD", "Tchéquie (ČD, RegioJet, Leo Express...)", load_cd_stations, op_country="CZ", search_name=search_names)
