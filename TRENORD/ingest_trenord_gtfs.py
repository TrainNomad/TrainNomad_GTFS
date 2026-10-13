"""
Télécharge et prépare le GTFS de Trenord (Lombardie) : trains régionaux R et RegioExpress RE,
Malpensa Express.

Source : https://www.dati.lombardia.it/download/3z4k-mxz9/application%2Fzip (données ouvertes de la
Région Lombardie, ~1,7 Mo).

Structure du flux :
  - stops.txt  : stop_id = code de la gare en Italie ("S01700" = Milano Centrale), sans UIC
  - routes.txt : route_short_name = ligne (R4, RE8, S5...), "TN Bus" = cars de substitution
  - trips.txt  : trip_short_name = "RE_11 - 2153" (ligne et numéro du train)

Ce que fait le script :
  ✓ lignes R (Regionale) et RE (RegioExpress), et les trains sans ligne indiquée
  ✓ stop_code = UIC à 7 chiffres tiré du code de la gare ("S01700" -> 8301700), comme Trenitalia
  ✓ trip_short_name = numéro du train seul ("2153")
  ✓ route_short_name = type affiché par le moteur ("Trenord Regionale", "Trenord RegioExpress",
    "Trenord Malpensa Express") ; la ligne et son parcours passent dans route_long_name
  ✗ Banlieue : lignes S (S1 à S13 de Milan, S10 à S50 du Tessin), comme le RER en France
  ✗ Cars de substitution : ligne "TN Bus" et numéros terminés par une lettre ("1012A")
Les lignes R23 et RE4 sont déclarées "bus" dans le flux alors que ce sont des trains : leur type
est corrigé.
Les trains vers la Suisse (RE80 Milan - Locarno) sont aussi dans le flux suisse : build_network.py
écarte ceux qu'un flux chargé avant fournit déjà (même numéro, mêmes horaires).

Usage :
    python TRENORD/ingest_trenord_gtfs.py
    python TRENORD/ingest_trenord_gtfs.py --input trenord.zip     (fichier déjà téléchargé)
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
OUTPUT_ZIP = os.path.join(BASE_DIR, "trenord_gtfs.zip")
TRENORD_GTFS_URL = "https://www.dati.lombardia.it/download/3z4k-mxz9/application%2Fzip"

SUBURBAN_LINES = r"^S\d+$"
BUS_LINES = {"TN BUS"}
MALPENSA_LINES = {"RE51", "RE54"}


def download(url: str, dest: str) -> None:
    logging.info(f"📥 Téléchargement : {url}")
    with requests.get(url, stream=True, timeout=300, headers={"User-Agent": "Mozilla/5.0 TrainNomad"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f, 1024 * 1024)
    logging.info(f"   {os.path.getsize(dest) / 1e6:.1f} Mo")


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def display_name(line: str) -> str:
    """ "R4" -> "Trenord Regionale" ; "RE8" -> "Trenord RegioExpress" ; "RE54" -> "Trenord Malpensa Express" """
    line = line.strip().upper()
    if line in MALPENSA_LINES:
        return "Trenord Malpensa Express"
    if re.fullmatch(r"RE\d+", line):
        return "Trenord RegioExpress"
    return "Trenord Regionale"


def uic7(stop_id: str) -> str:
    """ "S01700" -> "8301700" ; un code d'une autre forme ("Z05304") n'a pas d'UIC """
    m = re.fullmatch(r"S(\d{5})", stop_id)
    return "83" + m.group(1) if m else ""


def build(src: str, dst: str) -> dict:
    stats = {}
    with zipfile.ZipFile(src) as z:
        agency, routes, trips = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "trips.txt")
        st, stops, cd = read(z, "stop_times.txt"), read(z, "stops.txt"), read(z, "calendar_dates.txt")
        feed_info = z.read("feed_info.txt") if "feed_info.txt" in z.namelist() else None
    stats["trajets_total"] = len(trips)

    # 1. lignes : ni banlieue ni cars de substitution
    line = routes["route_short_name"].str.strip().str.upper()
    suburban, bus = line.str.match(SUBURBAN_LINES), line.isin(BUS_LINES)
    stats["lignes_banlieue_exclues"], stats["lignes_bus_exclues"] = int(suburban.sum()), int(bus.sum())
    routes = routes[~suburban & ~bus].copy()
    known = routes["route_short_name"].str.match(r"^RE?\d+$")
    routes["route_long_name"] = (routes["route_short_name"].where(known, "") + " "
                                 + routes["route_long_name"].where(known, "")).str.strip()
    routes["route_short_name"] = routes["route_short_name"].map(display_name)
    routes["route_type"] = "2"

    # 2. trains : numéro seul, sans les cars (numéro terminé par une lettre)
    trips = trips[trips["route_id"].isin(routes["route_id"])].copy()
    trips["trip_short_name"] = trips["trip_short_name"].str.split(" - ").str[-1].str.strip()
    bus_trips = trips["trip_short_name"].str.contains(r"[A-Za-z]$")
    stats["trajets_bus_exclus"] = int(bus_trips.sum())
    trips = trips[~bus_trips]

    st = st[st["trip_id"].isin(set(trips["trip_id"]))].copy()
    st["stop_sequence"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "stop_sequence"])
    st = st[st.groupby("trip_id")["stop_id"].transform("size") >= 2]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]
    routes = routes[routes["route_id"].isin(trips["route_id"])]
    cd = cd[cd["service_id"].isin(set(trips["service_id"]))]

    # 3. gares desservies, avec leur UIC
    stops = stops[stops["stop_id"].isin(set(st["stop_id"]))].copy()
    stops["stop_code"] = stops["stop_id"].map(uic7)
    stops = stops[["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"]].sort_values("stop_id")
    stats["gares_sans_uic"] = int((stops["stop_code"] == "").sum())

    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as o:
        for name, df in (("agency.txt", agency), ("routes.txt", routes), ("stops.txt", stops),
                         ("trips.txt", trips), ("calendar_dates.txt", cd), ("stop_times.txt", st)):
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            o.writestr(name, buf.getvalue())
        if feed_info:
            o.writestr("feed_info.txt", feed_info)
    os.replace(tmp, dst)

    stats.update({"lignes": len(routes), "trajets": len(trips), "passages": len(st), "gares": len(stops),
                  "du": cd["date"].min(), "au": cd["date"].max(),
                  "types": trips["route_id"].map(routes.set_index("route_id")["route_short_name"])
                  .value_counts().to_dict()})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="zip Trenord déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚂 Ingestion GTFS Trenord (Lombardie) : trains R et RE")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            download(TRENORD_GTFS_URL, raw)
        stats = build(raw, OUTPUT_ZIP)
    finally:
        if not args.input and os.path.exists(raw):
            os.remove(raw)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
