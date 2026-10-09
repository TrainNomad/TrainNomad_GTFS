"""
Référentiel des gares TrainNomad — opérateur Trenitalia (Italie : Frecciarossa, Frecciargento,
Frecciabianca, Intercity, Intercity Notte, Eurocity, Euronight, Regionale, Regionale Veloce...).

Source : Trenitalia/trenitalia_gtfs.zip, le GTFS produit par Trenitalia/ingest_trenitalia.py.
Le stop_code est l'UIC à 7 chiffres ("8301700" = Milano Centrale ; 85, 81, 87, 80, 79 pour les gares
étrangères). La gare est cherchée dans OpenStreetMap par UIC, sinon par nom.

Noms : le flux les écrit en majuscules et en abrégé ("S.GIOVANNI", "MUENCHEN HBF") : c'est le nom
d'OpenStreetMap qui est gardé ; une gare absente d'OSM garde le nom du flux remis en minuscules
("Marina di Cerveteri"), corrigeable dans la colonne nom_force de gares.csv.
Les arrêts desservis seulement par les autobus de substitution sont des arrêts de car.

Sortie : referentiel/operateurs/TRENITALIA/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_trenitalia.py      (python Trenitalia/ingest_trenitalia.py d'abord si besoin)
    python referentiel/build_gares_trenitalia.py --offline
"""
import logging
import os
import re
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, default_names, run  # noqa: E402

TRENITALIA_ZIP = os.path.join(GTFS_DIR, "Trenitalia", "trenitalia_gtfs.zip")
SMALL_WORDS = {"di", "de", "del", "della", "dello", "dei", "degli", "delle", "da", "dal", "in", "al", "alla",
               "a", "e", "sul", "sulla", "nel", "per", "con", "d", "dell"}


def titre(name: str) -> str:
    """ "MARINA DI CERVETERI" -> "Marina di Cerveteri" ; un nom déjà en minuscules est gardé tel quel."""
    name = " ".join((name or "").split())
    if name != name.upper():
        return name
    parts = re.split(r"([^\w]+)", name.lower())
    return "".join(p if (i and p in SMALL_WORDS) else p.capitalize() for i, p in enumerate(parts))


def search_names(st) -> list[str]:
    """Noms à essayer dans OpenRailwayMap : celui du flux, puis sans les abréviations ni la ponctuation
    ("S.Antonino Vaie" -> "Antonino Vaie", "Ponte S.Marco-Calcinato" -> "Ponte Marco Calcinato")."""
    name = st["nom_gtfs"]
    plain = " ".join(re.sub(r"[^\w\s]", " ", re.sub(r"\b\w{1,3}\.", " ", name)).split())
    out = [name]
    for n in (name.split("(")[0].strip(), plain):  # "Acerra (nuova stazione)" -> "Acerra"
        if n and n not in out:
            out.append(n)
    return out


def italian_names(st, f, country):
    """Nom d'OpenStreetMap d'abord (le flux abrège) ; sans gare OSM, le nom du flux."""
    return default_names(st["nom_gtfs"], f, country, "")


def load_italian_stations(path: str, source: str, label: str) -> list[dict]:
    """Arrêts d'un GTFS italien (Trenitalia, Italo) : UIC dans stop_code, catégories dans route_short_name."""
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python Trenitalia/ingest_trenitalia.py")
    z = zipfile.ZipFile(path)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id"])
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, keep_default_na=False, usecols=["trip_id", "route_id"])
    routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False).set_index("route_id")

    route = st["trip_id"].map(trips.set_index("trip_id")["route_id"])
    st["bus"] = route.map(routes["route_type"]).isin(["3", "700"])
    st["cat"] = route.map(routes["route_short_name"]).fillna("").str.upper()
    trains = st[~st["bus"]].groupby("stop_id")["cat"].agg(lambda s: sorted(set(s) - {""}))
    cars = set(st.loc[st["bus"], "stop_id"])

    out = []
    for r in stops[stops["stop_id"].isin(set(trains.index) | cars)].itertuples(index=False):
        uic = r.stop_code if re.fullmatch(r"\d{7}", r.stop_code) else ""
        # uic_pays : Trenitalia donne aussi un code italien (83...) à des gares étrangères (Tende, Breil,
        # Chiasso, Modane) : hors d'Italie il est écarté, la gare prend l'UIC de son objet OSM
        # "Tende (It)" -> "Tende"
        name = re.sub(r"\s*\((It|I|CH|F|A|D)\)$", "", titre(r.stop_name), flags=re.I)
        out.append({"cle": r.stop_id, "uic": uic, "uic_pays": "IT" if uic.startswith("83") else "",
                    "nom_gtfs": name,
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon),
                    "type": "gare" if r.stop_id in trains.index else "arret_car",
                    "trains": "|".join(trains.get(r.stop_id, [])), "cars": "BUS" if r.stop_id in cars else "",
                    "codes": [(source, r.stop_id)]})
    logging.info(f"   {len(out)} arrêts {label} ({sum(1 for s in out if s['type'] == 'gare')} gares, "
                 f"{sum(1 for s in out if not s['uic'])} sans UIC)")
    return out


if __name__ == "__main__":
    run("TRENITALIA", "Trenitalia (Italie)",
        lambda: load_italian_stations(TRENITALIA_ZIP, "TRENITALIA", "Trenitalia"),
        op_country="IT", names=italian_names, search_name=search_names)
