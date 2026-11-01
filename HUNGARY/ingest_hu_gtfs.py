"""
Télécharge et prépare le GTFS ferroviaire hongrois : MÁV (MÁV Személyszállítási Zrt.) et GYSEV (ouest du pays
et lignes vers l'Autriche), réunis en un seul flux.

Sources : https://jbb.ghsq.de/gtfs/ (même hébergeur que le flux European Sleeper), GTFS publiés pour le
calculateur Transitous à partir des horaires de MÁV et GYSEV. Flux non officiels, comparés le 2026-10-31 au
calculateur de MÁV (jegy.mav.hu) : mêmes trains, mêmes horaires à la minute sur Budapest - Debrecen, Pécs et
Szeged. MÁV délivre aussi son propre GTFS, sur demande (https://www.mavcsoport.hu/gtfs-igenybejelento).
Copies de secours dans la Release sources-latest (hu_mav_brut.zip, hu_gysev_brut.zip).

Structure des flux :
  - stops.txt : gare "1:005513912" (location_type 1), voies "1:005513912_4" ; les chiffres après "00" sont
    l'UIC ("5513912" Debrecen ; "81..." en Autriche, "53..." en Roumanie ; "43..." : gares GYSEV en Hongrie).
    Arrêts de bus de remplacement en "1:v.0055...". Mêmes identifiants dans les deux flux
  - trips.txt : trip_short_name = catégorie en toutes lettres + numéro ("InterCity 950", "gyorsvonat 822",
    "SZ 7221", "xpress 165") ; route_type 714 = bus de remplacement (« Pótló »)
  - calendar_dates.txt seul, environ six mois d'horaires

Ce que fait le script :
  ✓ numéro du train dans trip_short_name : catégorie abrégée + numéro ("IC 950", "Gy 822", "Sz 7221") ;
    catégorie seule dans route_desc, lue par build_network.py
  ✓ trains MÁV coupés à la première gare d'un pays déjà publié par un autre flux (Autriche, Slovaquie,
    Tchéquie, Pologne, Allemagne, Suisse) : leurs horaires y diffèrent parfois du flux officiel du pays
    (jusqu'à 16 minutes à Praha hl.n.). Les parties roumaine, serbe, croate, ukrainienne... sont gardées.
    Les trains GYSEV restent entiers : ÖBB ne publie pas les trains de GYSEV / Raaberbahn en Autriche
  ✓ un train publié deux fois (« InterCity 720 » et « gyorsvonat 720 » : deux classes de voitures d'un même
    train, ou un train présent dans les deux flux) n'est gardé qu'une fois
  ✓ bus de remplacement gardés

Usage :
    python HUNGARY/ingest_hu_gtfs.py
    python HUNGARY/ingest_hu_gtfs.py --input-mav mav.zip --input-gysev gysev.zip   (fichiers déjà téléchargés)
"""
import argparse
import io
import logging
import os
import re
import sys
import zipfile
from datetime import date, timedelta

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "hu_gtfs.zip")
SOURCES = {
    "mav": ("https://jbb.ghsq.de/gtfs/hu-m%C3%A1v_szem%C3%A9lysz%C3%A1ll%C3%ADt%C3%A1si_zrt..gtfs.zip",
            "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/hu_mav_brut.zip"),
    "gysev": ("https://jbb.ghsq.de/gtfs/hu-gysev_zrt..gtfs.zip",
              "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/hu_gysev_brut.zip"),
}

# catégorie en toutes lettres -> abrégée (affichée devant le numéro, comme sur les billets MÁV)
CATEGORIES = {
    "InterCity": "IC", "IR": "IR", "gyorsvonat": "Gy", "SV": "S", "Expresszvonat": "Ex", "SZ": "Sz",
    "EuroRegio": "ER", "EC": "EC", "EuroNight": "EN", "railjet": "RJ", "xpress": "RJX", "TramTrain": "TT",
    "S-Bahn": "S-Bahn", "autóbusz": "Bus", "busz": "Bus",
}
# train publié deux fois sous deux catégories : celle gardée (la plus haute)
RANK = ["RJX", "RJ", "EC", "EN", "IC", "ER", "Ex", "IR", "Gy", "S", "S-Bahn", "Sz", "TT", "Bus"]
# pays (deux premiers chiffres de l'UIC) dont un autre flux publie déjà les trains : Autriche, Slovaquie,
# Tchéquie, Pologne, Allemagne, Suisse
TRIM_COUNTRIES = {"81", "56", "54", "51", "80", "85"}
HU_COUNTRIES = {"55", "43"}

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar_dates.txt", "stop_times.txt"]


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def country(stop_id: str) -> str:
    """ "1:005513912_4" -> "55" ; "1:v.008100025" -> "81" """
    m = re.match(r"1:(?:v\.)?00(\d\d)", stop_id)
    return m.group(1) if m else ""


def station(stop_id: str) -> str:
    """ "1:005513912_4" -> "1:005513912" (gare mère) """
    return stop_id.split("_")[0]


def load(path: str, feed: str) -> dict:
    with zipfile.ZipFile(path) as z:
        t = {name: read(z, name) for name in FILES_KEPT}
    # service_id communs aux deux flux (« 1:6005 ») : préfixés ; trip_id et route_id aussi, par précaution
    for name, cols in (("routes.txt", ["route_id"]), ("trips.txt", ["trip_id", "route_id", "service_id"]),
                       ("calendar_dates.txt", ["service_id"]), ("stop_times.txt", ["trip_id"])):
        for c in cols:
            t[name][c] = f"{feed}:" + t[name][c]
    t["trips.txt"]["feed"] = feed
    return t


def trim(st: pd.DataFrame, trips: pd.DataFrame) -> pd.DataFrame:
    """Trajets MÁV coupés à la première gare d'un pays de TRIM_COUNTRIES (gardée : la correspondance s'y fait)."""
    st = st.assign(seq=st["stop_sequence"].astype(int)).sort_values(["trip_id", "seq"])
    mav = st["trip_id"].map(trips.set_index("trip_id")["feed"]) == "mav"
    foreign = st["stop_id"].map(country).isin(TRIM_COUNTRIES) & mav
    # arrêt gardé : hors de ces pays, ou voisin sur le trajet d'un arrêt hors de ces pays
    prev_ok = (~foreign).groupby(st["trip_id"]).shift(1, fill_value=False)
    next_ok = (~foreign).groupby(st["trip_id"]).shift(-1, fill_value=False)
    return st[~foreign | prev_ok | next_ok].drop(columns="seq")


def build(paths: dict, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    feeds = [load(p, feed) for feed, p in paths.items()]
    t = {name: pd.concat([f[name] for f in feeds], ignore_index=True) for name in FILES_KEPT}
    agency, routes, stops, trips, cd, st = (t[n] for n in FILES_KEPT)
    stops = stops.drop_duplicates("stop_id")
    agency = agency.drop_duplicates("agency_id")
    stats["trips_total"] = len(trips)

    # 1. jours de circulation à partir d'hier
    cd = cd[(cd["date"] >= since) & (cd["exception_type"] == "1")]
    stats["du"], stats["au"] = cd["date"].min(), cd["date"].max()
    trips = trips[trips["service_id"].isin(set(cd["service_id"]))].copy()
    stats["trajets_en_cours"] = len(trips)

    # 2. numéro affiché : catégorie abrégée + numéro ("IC 950") ; catégorie seule dans route_desc
    word = trips["trip_short_name"].str.extract(r"^(\S+)", expand=False).fillna("")
    num = trips["trip_short_name"].str.extract(r"(\d+)\s*$", expand=False).fillna("")
    trips["route_desc"] = word.map(CATEGORIES).fillna(word)
    unknown = sorted(set(word[~word.isin(CATEGORIES)]) - {""})
    if unknown:
        logging.warning(f"   ⚠️ catégories inconnues, gardées telles quelles : {unknown}")
    trips["trip_short_name"] = (trips["route_desc"] + " " + num).str.strip()

    # 3. trains MÁV coupés à l'entrée des pays déjà publiés ; points de simple passage retirés
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]
    before = st.groupby("trip_id").size()
    st = trim(st, trips)
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    stats["trajets_coupes_a_la_frontiere"] = int((counts < before.reindex(counts.index)).sum())
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    stats["trajets_hors_hongrie_ecartes"] = int(len(trips) - st["trip_id"].nunique())
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 4. train publié deux fois (deux catégories, ou dans les deux flux) : mêmes gares, horaires et jours
    st = st.assign(seq=st["stop_sequence"].astype(int)).sort_values(["trip_id", "seq"]).drop(columns="seq")
    sig = (st["stop_id"].map(station) + "@" + st["arrival_time"] + "-" + st["departure_time"]).groupby(st["trip_id"]).agg("|".join)
    days = cd.groupby("service_id")["date"].agg(lambda d: ",".join(sorted(d)))
    trips["sig"] = trips["trip_id"].map(sig) + "#" + trips["service_id"].map(days) + "#" \
        + trips["trip_short_name"].str.extract(r"(\d+)$", expand=False).fillna("")
    trips["rank"] = trips["route_desc"].map({c: i for i, c in enumerate(RANK)}).fillna(len(RANK))
    trips["feed_rank"] = (trips["feed"] != "gysev").astype(int)  # train des deux flux : celui de GYSEV
    kept = trips.sort_values(["rank", "feed_rank"]).drop_duplicates("sig")
    stats["trajets_en_double_ecartes"] = len(trips) - len(kept)
    trips = trips[trips["trip_id"].isin(set(kept["trip_id"]))]
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]

    # 5. gares desservies et leurs gares mères
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | parents)]
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station", "stop_timezone",
                   "platform_code"]]
    trips = trips[["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name", "route_desc"]]
    routes = routes[routes["route_id"].isin(trips["route_id"])]
    routes = routes[["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]
    st = st[["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"]]

    tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
              "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            tables[name].to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    by_agency = trips["route_id"].map(routes.set_index("route_id")["agency_id"]).map(agency.set_index("agency_id")["agency_name"])
    is_bus = trips["route_id"].map(routes.set_index("route_id")["route_type"]) == "714"
    stats.update({"trajets": len(trips), "dont_bus_de_remplacement": int(is_bus.sum()), "passages": len(st),
                  "gares": int((stops["location_type"] == "1").sum()),
                  "categories": trips["route_desc"].value_counts().to_dict(),
                  "compagnies": by_agency.value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-mav", help="GTFS MÁV déjà téléchargé")
    ap.add_argument("--input-gysev", help="GTFS GYSEV déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Hongrie (MÁV, GYSEV)")
    print("=" * 60)
    given = {"mav": args.input_mav, "gysev": args.input_gysev}
    paths = {}
    try:
        for feed, (url, mirror) in SOURCES.items():
            if given[feed]:
                paths[feed] = given[feed]
                continue
            paths[feed] = OUTPUT_ZIP + f".{feed}.raw"
            try:
                download(url, paths[feed])
            except Exception as e:
                logging.warning(f"⚠️ {feed} : jbb.ghsq.de injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print(f"::warning::Hongrie : flux {feed} injoignable, repris de la Release sources-latest")
                download(mirror, paths[feed])
        stats = build(paths, OUTPUT_ZIP)
    finally:
        for feed, p in paths.items():
            if not given[feed] and os.path.exists(p):
                os.remove(p)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as e:
        # message visible dans le résumé du workflow (sinon seulement « code 1 » au fond du journal)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::Hongrie : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
