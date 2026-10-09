"""
Référentiel des gares TrainNomad — Suisse (CFF/SBB/FFS, BLS, SOB, RhB, MGB, MOB, Thurbo, zb...).

Source : SWISS/swiss_gtfs.zip, le GTFS des trains suisses (geOps) produit par SWISS/ingest_swiss_gtfs.py.
Le stop_id est l'UIC à 7 chiffres, suivi du quai : "8503000:9" = Zürich HB voie 9, "8768600" = Paris
Gare de Lyon. Les arrêts "00..." sont des tronçons de ligne (Lötschberg-Basistunnel), pas des gares.
La gare est cherchée dans OpenStreetMap par UIC, sinon par nom.

Noms : le flux donne le nom officiel suisse dans la langue du lieu ("Genève", "Zürich HB", "Lugano") :
c'est le nom des gares suisses ; les gares étrangères ("Domodossola (I)", "Konstanz") prennent le
nom officiel d'OpenStreetMap.

Sortie : referentiel/operateurs/SWISS/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_swiss.py           (python SWISS/ingest_swiss_gtfs.py d'abord si besoin)
    python referentiel/build_gares_swiss.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

SWISS_ZIP = os.path.join(GTFS_DIR, "SWISS", "swiss_gtfs.zip")


def clean_swiss_name(name: str) -> str:
    """ "Domodossola (I)" -> "Domodossola" ; "Münster (Westf)" est gardé."""
    return " ".join(re.sub(r"\s*\((I|D|F|A|FL|CH)\)\s*$", "", name or "").split())


def search_names(st) -> list[str]:
    """Noms à essayer dans OpenRailwayMap quand l'UIC ne donne rien : celui du flux, puis sans
    abréviation ni ponctuation ("Sesto S. Giovanni" -> "Sesto Giovanni"), puis le début du nom
    ("Ittigen bei Bern" -> "Ittigen", "Bergün/Bravuogn" -> "Bergün")."""
    name = st["nom_gtfs"]
    plain = " ".join(re.sub(r"[^\w\s]", " ", re.sub(r"\b\w{1,3}\.", " ", name)).split())
    start = re.split(r"\s+bei\s+|[/,(]", name)[0].strip()
    return list(dict.fromkeys(n for n in (name, plain, start) if n))


def load_swiss_stations(path: str = SWISS_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python SWISS/ingest_swiss_gtfs.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False,
                         usecols=["route_id", "route_short_name"]).set_index("route_id")

    # catégories (S, IR, IC, RE, EC, TGV...) : "IC61" -> "IC", "S21" -> "S"
    cat = routes["route_short_name"].str.replace(r"\d+", "", regex=True).str.strip().str.upper()
    pairs = st.assign(route_id=st["trip_id"].map(trips.set_index("trip_id")["route_id"]))[
        ["stop_id", "route_id"]].drop_duplicates()
    pairs["cat"] = pairs["route_id"].map(cat).fillna("")
    pairs["uic"] = pairs["stop_id"].str.split(":").str[0]
    served = pairs.groupby("uic")["cat"].agg(lambda s: sorted(set(s) - {""}))

    stops["uic"] = stops["stop_id"].str.split(":").str[0]
    out = []
    for uic, g in stops.groupby("uic"):
        if uic not in served.index or not re.fullmatch(r"\d{7}", uic) or uic.startswith("00"):
            continue  # arrêt sans train, ou tronçon de ligne
        src = g[g["stop_id"] == uic].iloc[0] if (g["stop_id"] == uic).any() else g.iloc[0]
        out.append({"cle": uic, "uic": uic, "nom_gtfs": clean_swiss_name(src["stop_name"]),
                    "lat": float(src["stop_lat"]), "lon": float(src["stop_lon"]), "type": "gare",
                    "trains": "|".join(served[uic]), "cars": "",
                    "codes": [("SWISS", i) for i in sorted(set(g["stop_id"]) | {uic})]})
    logging.info(f"   {len(out)} gares du flux suisse ({sum(1 for s in out if s['uic'].startswith('85'))} en Suisse)")
    return out


if __name__ == "__main__":
    run("SWISS", "Suisse (CFF / SBB / FFS et compagnies privées)", load_swiss_stations, op_country="CH",
        search_name=search_names)
