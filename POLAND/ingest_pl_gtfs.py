"""
Télécharge et prépare le GTFS ferroviaire polonais : tous les trains de voyageurs du réseau PKP PLK, toutes
compagnies confondues (PKP Intercity, PolRegio, Koleje Mazowieckie, Koleje Śląskie, Koleje Dolnośląskie,
Koleje Wielkopolskie, Koleje Małopolskie, ŁKA, Arriva, RegioJet, Leo Express), trains internationaux compris.

Source : https://mkuran.pl/gtfs/polish_trains.zip, GTFS produit par Mikołaj Kuranowski à partir des données
ouvertes de PKP PLK (et de Koleje Mazowieckie), mis à jour chaque nuit, horaires sur un mois. Flux non officiel,
hébergé par un particulier et lent (environ 60 Ko/s) : quand il est injoignable, copie de la Release
sources-latest (comme la Tchéquie), à déposer à la main (polish_trains.zip renommé pl_brut.zip).

Structure du flux :
  - stops.txt : gare "33605" (location_type 1, code interne PLK, pas un UIC), voies "33605_RAIL_1_5",
    "33605_FALLBACK" ; gares étrangères (DE, CZ, SK, AT, HU, LT, UA) avec leur position, colonne country.
    Les gares ukrainiennes sont écrites en cyrillique
  - routes.txt : une ligne (route) par compagnie et produit ("IC_EIP", "PR_REG", "KM_R3"), bus de
    remplacement à part ("PR_REG_BUS", route_type 3, « Zastępcza Komunikacja Autobusowa »)
  - trips.txt : plk_category_code ("EIP", "IC", "EC/IC", "R", "OsP", "R3", "SKA2"), plk_train_number
    ("5406/7" : le numéro change en route, 5406 puis 5407), plk_train_name ("BYSTRZYCA") ; un train
    remplacé en partie par un bus est coupé en deux trajets "..._LEG0" (bus) et "..._LEG1" (train)
  - calendar_dates.txt seul

Ce que fait le script :
  ✓ numéro du train dans trip_short_name : catégorie + premier numéro commercial ("EIP 5406", "EN 406",
    "EC 113") ; c'est celui que publient les flux voisins (tchèque, allemand) : les doublons s'y retrouvent.
    Le numéro de sillon PLK (plk_train_number : 41010 pour l'EN 406 Chopin) ne sert qu'à défaut
  ✓ catégorie seule dans route_desc (la première : "EC/IC" -> "EC"), lue par build_network.py
  ✓ bus de remplacement d'un train gardés (route_type 3) ; lignes de bus régulières écartées (navette de
    l'aéroport de Modlin, lignes D31, D81, D92 de Koleje Dolnośląskie)
  ✗ trains urbains : SKM Warszawa et PKP SKM Trójmiasto (comme les S-Bahn allemands et le RER)
  ✓ noms des gares en cyrillique (Ukraine) écrits en alphabet latin (translittération officielle ukrainienne)
Les gares ne sont pas rapprochées ici : référentiel referentiel/build_gares_pkp.py (stop_id PLK stables).

Usage :
    python POLAND/ingest_pl_gtfs.py
    python POLAND/ingest_pl_gtfs.py --input polish_trains.zip     (fichier déjà téléchargé)
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

OUTPUT_ZIP = os.path.join(BASE_DIR, "pl_gtfs.zip")
URL = "https://mkuran.pl/gtfs/polish_trains.zip"
MIRROR_URL = "https://github.com/TrainNomad/TrainNomad_GTFS/releases/download/sources-latest/pl_brut.zip"

# trains urbains (comme les S-Bahn) : SKM Warszawa, PKP SKM Trójmiasto
EXCLUDED_AGENCIES = {"SKM", "SKMT"}
# bus de remplacement d'un train ; les autres lignes de bus sont écartées
REPLACEMENT_BUS = "Zastępcza Komunikacja Autobusowa"

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar_dates.txt", "stop_times.txt"]

# translittération officielle ukrainienne (2010), « Київ » -> « Kyiv » ; ы, э, ё (russe) en plus.
# Lettres en début de mot : є, ї, й, ю, я s'écrivent ye, yi, y, yu, ya ; ailleurs ie, i, i, iu, ia
UA_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
    "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "’": "", "ʼ": "",
    "ы": "y", "э": "e", "ё": "io", "ъ": "", "ў": "u",
}
UA_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def latin(name: str) -> str:
    """ "Київ-Пасажирський" -> "Kyiv-Pasazhyrskyi" ; un nom sans cyrillique est rendu tel quel."""
    if not re.search(r"[Ѐ-ӿ]", name):
        return name
    out = []
    for i, ch in enumerate(name):
        low = ch.lower()
        initial = i == 0 or not (name[i - 1].isalpha() or name[i - 1] in "'’ʼ")  # « Знам'янка » -> « Znamianka »
        if low == "з" and name[i + 1:i + 2].lower() == "г":
            t = "zgh"  # « Згурівка » -> « Zghurivka »
        elif low == "г" and i and name[i - 1].lower() == "з":
            continue
        else:
            t = (UA_INITIAL.get(low) if initial else None) or UA_LATIN.get(low, ch)
        out.append(t.capitalize() if ch != low and t else t)
    return "".join(out)


def read(z: zipfile.ZipFile, name: str, **kwargs) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", **kwargs)


def build(src: str, dst: str) -> dict:
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    with zipfile.ZipFile(src) as z:
        agency, routes, trips, stops = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "trips.txt"), read(z, "stops.txt")
        cd = read(z, "calendar_dates.txt")
        stats["trips_total"] = len(trips)

        # 1. jours de circulation à partir d'hier
        cd = cd[(cd["date"] >= since) & (cd["exception_type"] == "1")]
        stats["du"], stats["au"] = cd["date"].min(), cd["date"].max()

        # 2. trains hors réseaux urbains ; bus seulement en remplacement d'un train
        urban = routes["agency_id"].isin(EXCLUDED_AGENCIES)
        bus = routes["route_type"] == "3"
        other_bus = bus & ~routes["route_long_name"].str.contains(REPLACEMENT_BUS, regex=False)
        stats["trajets_urbains_exclus"] = int(trips["route_id"].isin(routes.loc[urban, "route_id"]).sum())
        stats["trajets_bus_reguliers_exclus"] = int(trips["route_id"].isin(routes.loc[other_bus & ~urban, "route_id"]).sum())
        routes = routes[~urban & ~other_bus]
        trips = trips[trips["service_id"].isin(set(cd["service_id"])) & trips["route_id"].isin(routes["route_id"])].copy()
        stats["trajets_en_cours"] = len(trips)

        # 3. numéro affiché : catégorie + premier numéro ("EIP 5406") ; catégorie seule dans route_desc.
        # Numéro commercial (trip_short_name : "406 Chopin", "113 Silesia", "1029") d'abord : plk_train_number
        # est le numéro de sillon du gestionnaire, différent pour les trains internationaux (41010 pour l'EN 406,
        # 41006 pour l'EC 113, 14022 pour le RJ 1029) ; à défaut (vide ou "0"), le numéro PLK
        commercial = trips["trip_short_name"].str.extract(r"^\s*(\d+)", expand=False).fillna("")
        plk = trips["plk_train_number"].str.extract(r"^\s*(\d+)", expand=False).fillna("")
        first = commercial.where(~commercial.isin(["", "0"]), plk)
        stats["numeros_commerciaux_differents_du_plk"] = int(((first != plk) & (plk != "")).sum())
        cat = trips["plk_category_code"].str.split("/").str[0].str.strip()
        # trajets de Koleje Mazowieckie sans catégorie PLK : la ligne ("R3")
        cat = cat.where(cat != "", trips["route_id"].map(routes.set_index("route_id")["route_short_name"]).fillna(""))
        trips["trip_short_name"] = (cat + " " + first).str.strip().where(first != "", "")
        trips["route_desc"] = cat
        stats["trajets_sans_numero"] = int((first == "").sum())

        # 4. horaires de ces trajets
        keep = set(trips["trip_id"])
        cols = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"]
        st = read(z, "stop_times.txt", usecols=lambda c: c in cols)
        st = st[st["trip_id"].isin(keep)]

    # 5. points de simple passage retirés (ni montée ni descente)
    for c in ("pickup_type", "drop_off_type"):
        if c not in st.columns:
            st[c] = ""
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    # 6. gares desservies et leurs gares mères, noms en alphabet latin
    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | parents)].copy()
    cyrillic = stops["stop_name"].str.contains(r"[Ѐ-ӿ]")
    stats["gares_translitterees"] = int((cyrillic & (stops["location_type"] == "1")).sum())
    stops["stop_name"] = stops["stop_name"].map(latin)
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station", "stop_timezone",
                   "platform_code", "country"]]

    # route_desc par trajet : une route a plusieurs catégories ("IC" et "EC/IC" sur IC_IC)
    trips = trips[["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name", "route_desc"]]
    routes = routes[routes["route_id"].isin(trips["route_id"])]
    routes = routes[["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]

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
    is_bus = trips["route_id"].map(routes.set_index("route_id")["route_type"]) == "3"
    stats.update({"trajets": len(trips), "dont_bus_de_remplacement": int(is_bus.sum()), "passages": len(st),
                  "gares": int((stops["location_type"] == "1").sum()),
                  "categories": trips["route_desc"].value_counts().head(20).to_dict(),
                  "compagnies": by_agency.value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip polish_trains déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion GTFS Pologne (PKP PLK : PKP Intercity, PolRegio, Koleje...)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            try:
                download(URL, raw)
            except Exception as e:
                logging.warning(f"⚠️ mkuran.pl injoignable ({e}) : copie de la Release sources-latest")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print("::warning::Pologne : mkuran.pl injoignable, flux repris de la Release sources-latest")
                download(MIRROR_URL, raw)
        stats = build(raw, OUTPUT_ZIP)
    finally:
        if not args.input and os.path.exists(raw):
            os.remove(raw)
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
            print(f"::error::Pologne : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
