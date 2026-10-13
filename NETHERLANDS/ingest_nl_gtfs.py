"""
Télécharge et réduit le GTFS des Pays-Bas (OVapi) aux trains : NS (Intercity, Sprinter), Arriva,
Keolis, Qbuzz et les trains internationaux (ICE, EuroCity, Nightjet).

Source : https://gtfs.ovapi.nl/nl/gtfs-nl.zip (~260 Mo zippé, tous les transports publics du pays ;
stop_times.txt fait 1,2 Go, lu par morceaux). Les trains y viennent du fichier national IFF.

Structure du flux :
  - stops.txt : gare mère "stoparea:17894" (stop_code = code de la gare : "rtd" = Rotterdam Centraal),
                voies en arrêts enfants (zone_id "IFF:rtd"). Aucun UIC.
  - routes.txt : route_short_name = catégorie, parfois suivie de la ligne ("Stoptrein RS11")
  - trips.txt  : trip_short_name = numéro du train (2835), trip_long_name = catégorie

Ce que fait le script :
  ✓ trains uniquement (route_type 2) : ni bus, ni tram, ni métro, ni ferry
  ✓ une gare = un arrêt, identifié par son code ("rtd") : les voies sont ramenées à leur gare.
    Ce code est stable d'une version à l'autre, contrairement aux numéros d'arrêt du flux ;
    referentiel/build_gares_ns.py en tire l'UIC.
  ✓ route_short_name = compagnie + catégorie ("NS Intercity", "Arriva Stoptrein"), affiché tel quel
    par le moteur ; la ligne ("RS11") passe dans route_long_name
  ✗ Eurostar et European Sleeper : déjà fournis par leur propre GTFS
  ✗ trains touristiques (Miljoenenlijn) et trains spéciaux
  ✗ tracés (shapes) et correspondances garanties (transfers), inutiles au moteur
Les autres trains internationaux (ICE, EuroCity, trains belges et allemands) sont gardés :
build_network.py écarte ceux qu'un flux chargé avant fournit déjà (même numéro, mêmes horaires).

Pas de banlieue à retirer : les Pays-Bas n'ont pas de RER, le Sprinter est leur train régional.

Usage :
    python NETHERLANDS/ingest_nl_gtfs.py
    python NETHERLANDS/ingest_nl_gtfs.py --input gtfs-nl.zip     (fichier déjà téléchargé)
"""
import argparse
import io
import logging
import os
import re
import shutil
import zipfile

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(BASE_DIR, "nl_gtfs.zip")
NL_GTFS_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
TIMEZONE = "Europe/Amsterdam"

# Compagnies (agency_name) et catégories (route_short_name) écartées
EXCLUDED_AGENCIES = {"European Sleeper", "Miljoenenlijn"}
EXCLUDED_CATEGORIES = {"Eurostar", "European Sleeper", "Speciale trein"}
# Compagnie dont la catégorie porte déjà le nom : "GoVolta", pas "GoVolta GoVolta". Les trains
# internationaux de NS gardent le préfixe ("NS ICE", "NS EuroCity", "NS Nightjet"), comme ceux de la
# SNCB : le site choisit le logo d'après la fin du nom.
INTERNATIONAL = {"GoVolta"}
# Marque affichée : les concessions régionales portent le nom de leur réseau ("RRReis Arriva")
BRANDS = (("Arriva", "Arriva"), ("Keolis", "Keolis"), ("Qbuzz", "Qbuzz"), ("NMBS", "SNCB"), ("NS", "NS"))
CHUNK = 2_000_000


def download(url: str, dest: str) -> None:
    logging.info(f"📥 Téléchargement : {url}")
    with requests.get(url, stream=True, timeout=600, headers={"User-Agent": "TrainNomad"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f, 1024 * 1024)
    logging.info(f"   {os.path.getsize(dest) / 1e6:.0f} Mo")


def read(z: zipfile.ZipFile, name: str, usecols=None) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", usecols=usecols)


def category(route_short_name: str) -> str:
    """ "Stoptrein RS11" -> "Stoptrein" ; "Intercity direct" -> "Intercity direct" """
    return re.sub(r"\s+(RS|RE|RB|IC)\s*\d+$", "", (route_short_name or "").strip())


def brand(agency_name: str) -> str:
    for word, name in BRANDS:
        if word in agency_name.split():
            return name
    return agency_name


def display_name(agency_name: str, cat: str) -> str:
    """ "NS Intercity", "Arriva Stoptrein", "NS ICE", "GoVolta" """
    return cat if cat in INTERNATIONAL or not cat else f"{brand(agency_name)} {cat}"


def build(src: str, dst: str) -> dict:
    stats = {}
    with zipfile.ZipFile(src) as z:
        agency, routes = read(z, "agency.txt"), read(z, "routes.txt")
        stats["routes_total"] = len(routes)

        # 1. trains uniquement, sans ceux d'un autre flux ni les trains touristiques
        routes = routes[routes["route_type"] == "2"].copy()
        names = routes["agency_id"].map(agency.set_index("agency_id")["agency_name"]).fillna("")
        cat = routes["route_short_name"].map(category)
        routes = routes[~names.isin(EXCLUDED_AGENCIES) & ~cat.isin(EXCLUDED_CATEGORIES)].copy()
        names, cat = names[routes.index], cat[routes.index]
        line = routes["route_short_name"].str.extract(r"((?:RS|RE|RB|IC)\s*\d+)$")[0].fillna("")
        routes["route_long_name"] = (line + " " + routes["route_long_name"]).str.strip()
        routes["route_short_name"] = [display_name(a, c) for a, c in zip(names, cat)]
        agency = agency[agency["agency_id"].isin(routes["agency_id"])]

        trips = read(z, "trips.txt", usecols=["route_id", "service_id", "trip_id", "trip_headsign",
                                               "trip_short_name", "trip_long_name"])
        trips = trips[trips["route_id"].isin(routes["route_id"])]
        trip_ids = set(trips["trip_id"])

        # 2. horaires des trains, lus par morceaux (1,2 Go)
        parts = []
        for chunk in pd.read_csv(z.open("stop_times.txt"), dtype=str, keep_default_na=False, chunksize=CHUNK,
                                 usecols=["trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time",
                                          "pickup_type", "drop_off_type"]):
            parts.append(chunk[chunk["trip_id"].isin(trip_ids)])
        st = pd.concat(parts, ignore_index=True)

        # 3. une gare = un arrêt, identifié par son code ("rtd")
        stops = read(z, "stops.txt").set_index("stop_id", drop=False)
        used = stops.loc[stops.index.intersection(st["stop_id"].unique())]
        parent = used["parent_station"].where(used["parent_station"].isin(stops.index), "")
        code = parent.map(stops["stop_code"]).fillna("")
        code = code.where(code != "", used["zone_id"].str.replace(r"^IFF:", "", regex=True))
        code = code.where(code != "", used["stop_id"]).str.lower()
        stats["arrets_sans_code_de_gare"] = int((code == used["stop_id"].str.lower()).sum())
        st["stop_id"] = st["stop_id"].map(code)
        station = parent.where(parent != "", used["stop_id"])
        rows = stops.loc[station.values, ["stop_name", "stop_lat", "stop_lon"]].reset_index(drop=True)
        out_stops = rows.assign(stop_id=code.values, stop_code=code.values, stop_timezone=TIMEZONE,
                                location_type="0").drop_duplicates("stop_id")
        out_stops = out_stops[["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type",
                               "stop_timezone"]].sort_values("stop_id")

        # un trajet doit garder au moins deux arrêts
        st["stop_sequence"] = st["stop_sequence"].astype(int)
        st = st.sort_values(["trip_id", "stop_sequence"])
        n = st.groupby("trip_id")["stop_id"].transform("size")
        st = st[n >= 2]
        trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]
        routes = routes[routes["route_id"].isin(trips["route_id"])]

        cd = read(z, "calendar_dates.txt")
        cd = cd[cd["service_id"].isin(set(trips["service_id"]))]
        feed_info = z.read("feed_info.txt") if "feed_info.txt" in z.namelist() else None

    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as o:
        for name, df in (("agency.txt", agency), ("routes.txt", routes), ("stops.txt", out_stops),
                         ("trips.txt", trips), ("calendar_dates.txt", cd), ("stop_times.txt", st)):
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            o.writestr(name, buf.getvalue())
        if feed_info:
            o.writestr("feed_info.txt", feed_info)
    os.replace(tmp, dst)

    stats.update({"routes": len(routes), "trajets": len(trips), "passages": len(st), "gares": len(out_stops),
                  "jours": cd["date"].nunique(), "du": cd["date"].min(), "au": cd["date"].max(),
                  "categories": trips["route_id"].map(routes.set_index("route_id")["route_short_name"])
                  .value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="gtfs-nl.zip déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚂 Ingestion GTFS Pays-Bas (OVapi) : trains uniquement")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            download(NL_GTFS_URL, raw)
        stats = build(raw, OUTPUT_ZIP)
    finally:
        if not args.input and os.path.exists(raw):
            os.remove(raw)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
