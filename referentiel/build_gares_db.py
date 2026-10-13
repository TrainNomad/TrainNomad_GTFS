"""
Référentiel des gares TrainNomad — Allemagne, grandes lignes (ICE, IC, EC, ECE, EN, RJ de la DB et
des réseaux voisins).

Source : GERMANY/de_gtfs.zip, le GTFS gtfs.de préparé par GERMANY/ingest_de_gtfs.py. Les trains
régionaux (GERMANY/de_regio_gtfs.zip) sont un autre opérateur : build_gares_db_regio.py.

Une gare = une gare mère du flux (location_type 1), ses voies sont ses arrêts enfants. Le stop_id
est un numéro interne à gtfs.de, sans UIC ni numéro DB (EVA) ; la gare mère porte le nom de
l'arrêt de bus voisin ("Ahlen, Bahnhof", "Hauptbahnhof (oben)"), ses voies le nom de la DB
("Frankfurt(Main)Hbf") : c'est ce dernier qui est gardé.

Aucun fichier officiel allemand ne donne l'UIC (la DB publie son numéro EVA, son abréviation RL100
et le PLC européen, trois autres numérotations). L'UIC vient donc, dans l'ordre :
  1. de Wikidata (P722), sur la gare trouvée dans OpenStreetMap ;
  2. de stations.csv, pour la gare que GERMANY/ingest_de_gtfs.py a rapprochée (stop_code du flux) ;
  3. de l'uic_ref d'OpenStreetMap, s'il n'est pas le numéro EVA de la gare.
Le numéro EVA de stations.csv sert à trouver la gare : dans OpenStreetMap, l'uic_ref d'une gare
allemande est le plus souvent cet EVA (statut "code"). Il est gardé dans codes.csv (source IBNR).
Une gare sans UIC garde un identifiant propre (TNOSM...).

Sortie : referentiel/operateurs/DB/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_db.py              (python GERMANY/ingest_de_gtfs.py d'abord si besoin)
    python referentiel/build_gares_db.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, default_names, fold, run  # noqa: E402

DB_ZIP = os.path.join(GTFS_DIR, "GERMANY", "de_gtfs.zip")
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")
# banlieue, absente du moteur (GERMANY/ingest_de_gtfs.py) : S-Bahn, Regio-S-Bahn, métro
SUBURBAN_LINES = r"^(S|RS|U)\s*\d*[A-Za-z]?$"
REGIO_CATEGORIES = {"RE", "RB", "IRE", "MEX", "FEX", "RRX", "ALX", "BRB", "ERB", "HLB", "NWB", "ME", "TLX", "VIA"}


def db_name(name: str) -> str:
    """ "Frankfurt(Main)Hbf" -> "Frankfurt (Main) Hbf" ; "Storkow(Mark)" -> "Storkow (Mark)" """
    name = re.sub(r"\s*\(", " (", name or "")
    return " ".join(re.sub(r"\)(?=\w)", ") ", name).split())


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le numéro EVA de la gare (uic_ref d'OSM), puis ses
    noms (DB, stations.csv, gare mère du flux), avec "Hauptbahnhof" pour "Hbf" et sans la précision
    entre parenthèses ("Münster (Westf) Hbf" -> "Münster Hbf")."""
    out = [{"uic_ref": c} for c in st.get("ibnr", [])]
    names = []
    for name in [st["nom_gtfs"], *st.get("autres_noms", [])]:
        plain = " ".join(re.sub(r"\([^)]*\)", " ", name).split())
        # "S Olympiastadion" -> "Olympiastadion" ; "Ahlen, Bahnhof" -> "Ahlen"
        short = re.sub(r",?\s+(Bahnhof|Bf)$", "", re.sub(r"^(S\+U|S|U)\s+", "", plain)).strip()
        for n in (name, plain, short):
            names += [n, n.replace("Hbf", "Hauptbahnhof"), n.replace("Hauptbahnhof", "Hbf")]
    return out + list(dict.fromkeys(n for n in names if len(n) > 2))


def station_hints(path: str = STATIONS_CSV) -> dict:
    """{id de stations.csv: (nom, UIC, numéro EVA)} : le stop_code du flux préparé est cet id."""
    if not os.path.exists(path):
        logging.warning(f"   {path} absent : gares cherchées par leur nom seulement")
        return {}
    s = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, usecols=["id", "name", "uic", "db_id"])
    ok = lambda v: v if re.fullmatch(r"\d{7}", v) else ""  # noqa: E731
    return {r.id: (r.name, ok(r.uic), ok(r.db_id)) for r in s.itertuples(index=False)}


def category(short_name: str, regio: bool) -> str:
    """ "ICE 82" -> "ICE" ; "RE1" -> "RE" ; ligne régionale sans catégorie connue ("12N") -> "REGIO" """
    m = re.match(r"[A-Za-z]+", (short_name or "").strip())
    cat = m.group(0).upper() if m else ""
    if regio:
        return cat if cat in REGIO_CATEGORIES else "REGIO"
    return cat


def load_german_stations(path: str, source: str, regio: bool = False) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python GERMANY/ingest_de_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False).set_index("route_id")

    line = st["trip_id"].map(trips.set_index("trip_id")["route_id"]).map(routes["route_short_name"]).fillna("")
    if regio:  # le zip peut dater d'avant le retrait de la banlieue
        st, line = st[~line.str.strip().str.match(SUBURBAN_LINES)], line[~line.str.strip().str.match(SUBURBAN_LINES)]
    st = st.assign(cat=line.map(lambda v: category(v, regio)))
    cats = st.groupby("stop_id")["cat"].agg(lambda s: set(s) - {""})

    stops = stops.set_index("stop_id", drop=False)
    used = stops[stops["stop_id"].isin(cats.index)]
    parent = used["parent_station"].where(used["parent_station"].isin(stops.index), used["stop_id"])
    hints = station_hints()
    # deux gares du flux rapprochées de la même gare de stations.csv (Francfort Aéroport, gare grandes
    # lignes et gare régionale) : ni EVA ni UIC de secours, elles prendraient le même identifiant
    code_of = {pid: next((c for c in grp["stop_code"] if c), "") for pid, grp in used.groupby(parent)}
    shared = {c for c in code_of.values() if c and list(code_of.values()).count(c) > 1}

    out = []
    for pid, grp in used.groupby(parent):
        p = stops.loc[pid]
        name = db_name(grp["stop_name"].mode().iloc[0])
        hint_name, uic, eva = hints.get(code_of[pid], ("", "", ""))
        if code_of[pid] in shared:
            uic, eva = "", ""
        trains = sorted(set().union(*(cats[c] for c in grp["stop_id"])))
        children = [c for c in grp["stop_id"] if c != pid]
        out.append({"cle": pid, "uic": "", "uic_secours": uic, "ibnr": [eva] if eva else [],
                    "code_osm": ("uic_ref", eva) if eva else None,
                    "nom_gtfs": name, "autres_noms": [n for n in (hint_name, db_name(p["stop_name"])) if n],
                    "lat": float(p["stop_lat"]), "lon": float(p["stop_lon"]), "type": "gare",
                    "trains": "|".join(trains), "cars": "",
                    "codes": [(source, c) for c in [pid, *children]] + ([("IBNR", eva)] if eva else [])})
    logging.info(f"   {len(out)} gares ({sum(1 for s in out if s['ibnr'])} avec numéro EVA, "
                 f"{sum(1 for s in out if s['uic_secours'])} avec UIC de secours dans stations.csv)")
    return out


def names(st, f, country):
    """En Allemagne, le nom de la DB s'il est celui d'OpenStreetMap ("Bochum Hbf" pour "Bochum
    Hauptbahnhof"), sinon le nom d'OpenStreetMap : le flux abrège ("Weilheim (Oberbay)", "Frankfurt(M)
    Flughafen Fernbf"). Une gare au nom bilingue garde le nom de la DB ("Hoyerswerda - Wojerecy").
    À l'étranger, le nom officiel d'OpenStreetMap (comme FlixTrain)."""
    nom_fr, nom_en, local = default_names(st["nom_gtfs"], f, country, "")
    if country == "DE" and f:
        same = fold(local) == fold(st["nom_gtfs"]) or " - " in local
        nom_fr = st["nom_gtfs"] if same else local
        nom_en = f.get("name:en") or nom_fr
    return nom_fr, nom_en, local


if __name__ == "__main__":
    run("DB", "Allemagne, grandes lignes (DB)", lambda: load_german_stations(DB_ZIP, "DB"), op_country="DE",
        search_name=search_names, names=names)
