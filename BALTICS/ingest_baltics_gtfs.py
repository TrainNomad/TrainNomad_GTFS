"""
Télécharge et prépare les GTFS ferroviaires des pays baltes, un opérateur par pays :
  LTG_LINK (Lituanie) : LTG Link, trains intérieurs et Vilnius - Riga
  VIVI     (Lettonie) : Vivi (Pasažieru vilciens) et le Gulbenes-Alūksnes bānītis, tirés du GTFS national letton
  ELRON    (Estonie)  : Elron, tous les trains de voyageurs estoniens

Sources officielles uniquement :
  - Lituanie : GTFS national de la LTSA (Lietuvos transporto saugos administracija),
    https://www.visimarsrutai.lt/gtfs/gtfs_all.zip (≈ 75 Mo, surtout des bus) : seuls les trains LTG Link sont repris
  - Lettonie : GTFS de Vivi (Pasažieru vilciens), https://vivi.lv/uploads/GTFS.zip, publié en CC0 sur data.gov.lv
    par l'ATD (Autotransporta direkcija) ; + le Gulbenes-Alūksnes bānītis, tiré du GTFS national de l'ATD
    (https://www.atd.lv/sites/default/files/GTFS/gtfs-latvia-lv.zip, ≈ 30 Mo, sinon des bus)
  - Elron : https://eu-gtfs.remix.com/elron.zip (Transpordiamet)

Particularités :
  - aucun UIC dans les flux : les gares sont rapprochées par referentiel/build_gares_<opérateur>.py (OSM, nom)
  - numéro de train : LTG Link n'a pas de trip_short_name, le numéro est dans route_short_name ("889/890" :
    deux numéros selon le sens, le premier est gardé) ; Vivi : trip_short_name ("693") ; Elron : trip_short_name
    est un texte ("Tallinn -Turba"), la ligne (route_short_name "R12", "RE34") est prise à la place
  - type affiché dans route_desc ("LTG Link", "Vivi", "Elron"), lu par build_network.py

Ce que fait le script, pour chaque flux :
  ✓ trains seuls ; trajets sans aucun jour de circulation à partir d'hier écartés ; points de simple passage retirés
  ✓ shapes.txt, transfers.txt et frequencies.txt non repris

Usage :
    python BALTICS/ingest_baltics_gtfs.py              (les trois)
    python BALTICS/ingest_baltics_gtfs.py lt ee        (seulement ceux-là)
"""
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

RAIL_ROUTE_TYPES = {"2", "100", "101", "102", "103", "106"}
# sources : (adresse, préfixe des identifiants, obligatoire). Plusieurs sources sont réunies en un GTFS ; le
# préfixe évite les collisions d'identifiants (trajets, gares, services) entre elles. Une source facultative en
# panne est sautée (le pays est publié sans elle).
FEEDS = {
    "lt": {"sources": [("https://www.visimarsrutai.lt/gtfs/gtfs_all.zip", "", True)],
           "out": os.path.join(BASE_DIR, "lt_gtfs.zip"), "type": "LTG Link", "number": "route_unique",
           "agencies": {"UAB LTG Link"}},
    "lv": {"sources": [("https://vivi.lv/uploads/GTFS.zip", "", True),
                       ("https://www.atd.lv/sites/default/files/GTFS/gtfs-latvia-lv.zip", "atd:", False)],
           "out": os.path.join(BASE_DIR, "lv_gtfs.zip"), "type": "Vivi", "number": "trip"},
    "ee": {"sources": [("https://eu-gtfs.remix.com/elron.zip", "", True)],
           "out": os.path.join(BASE_DIR, "ee_gtfs.zip"), "type": "Elron", "number": "route"},
}
# Lettonie : compagnie -> type affiché ; « AS Pasažieru vilciens », « Vivi »... -> Vivi ; les autres gardent leur nom
LV_TYPES = {"Gulbenes-Alūksnes bānītis": "Bānītis"}


def lv_type(agency_name: str) -> str:
    if re.search(r"vivi|pasa[žz]ieru vilciens", agency_name, re.I):
        return "Vivi"
    return LV_TYPES.get(agency_name, agency_name or "Vivi")

FILES_KEPT = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "calendar.txt", "calendar_dates.txt",
              "stop_times.txt"]


def read(z: zipfile.ZipFile, name: str, **kw):
    # skipinitialspace : le GTFS de l'ATD écrit « 23, "SIA GALSS BUSS" » (espace après chaque virgule)
    df = pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig", skipinitialspace=True, **kw)
    if isinstance(df, pd.DataFrame):
        df.columns = [c.strip() for c in df.columns]
    return df


# identifiants préfixés quand plusieurs sources sont réunies
ID_COLUMNS = ("agency_id", "route_id", "service_id", "trip_id", "stop_id", "parent_station")


def prefixed(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if prefix:
        for c in ID_COLUMNS:
            if c in df:
                df[c] = df[c].map(lambda v: prefix + v if v else v)
    return df


def read_rail(src: str, prefix: str, since: str, agencies: set | None = None) -> dict:
    """Tables d'un GTFS réduites aux trains qui circulent encore (à partir de since), identifiants préfixés."""
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        agency, routes, stops, trips = read(z, "agency.txt"), read(z, "routes.txt"), read(z, "stops.txt"), read(z, "trips.txt")
        cal = read(z, "calendar.txt") if "calendar.txt" in names else pd.DataFrame(columns=["service_id"])
        cd = read(z, "calendar_dates.txt") if "calendar_dates.txt" in names else pd.DataFrame(columns=["service_id"])
        if "agency_id" not in routes:
            routes["agency_id"] = agency["agency_id"].iloc[0] if "agency_id" in agency and len(agency) else ""
        routes = routes[routes["route_type"].isin(RAIL_ROUTE_TYPES)].copy()
        if agencies:  # GTFS national lituanien : bus et trains, seule LTG Link est gardée
            ids = set(agency.loc[agency["agency_name"].str.strip().isin(agencies), "agency_id"])
            routes = routes[routes["agency_id"].isin(ids)]
        trips = trips[trips["route_id"].isin(set(routes["route_id"]))]
        trips = trips[trips["service_id"].isin(running_services(cal, cd, since))].copy()
        keep = set(trips["trip_id"])
        parts = [c[c["trip_id"].str.strip().isin(keep)] for c in read(z, "stop_times.txt", chunksize=1_000_000)]
        st = pd.concat(parts) if parts else pd.DataFrame(columns=["trip_id", "stop_id"])
        st.columns = [c.strip() for c in st.columns]
    services = set(trips["service_id"])
    tables = {"agency": agency, "routes": routes, "stops": stops, "trips": trips,
              "cal": cal[cal["service_id"].isin(services)], "cd": cd[cd["service_id"].isin(services)], "st": st}
    return {k: prefixed(v.copy(), prefix) for k, v in tables.items()}


def running_services(cal: pd.DataFrame, cd: pd.DataFrame, since: str) -> set:
    """service_id ayant au moins un jour de circulation à partir de since (AAAAMMJJ)."""
    out = set(cal.loc[cal["end_date"] >= since, "service_id"]) if "end_date" in cal else set()
    if "date" in cd:
        out |= set(cd.loc[(cd["exception_type"] == "1") & (cd["date"] >= since), "service_id"])
    return out


def build(srcs: list[tuple[str, str]], dst: str, feed: dict) -> dict:
    """srcs : [(GTFS téléchargé, préfixe des identifiants)], réunis en un seul GTFS de trains."""
    stats = {}
    since = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    parts = [read_rail(path, prefix, since, feed.get("agencies")) for path, prefix in srcs]
    agency, routes, stops, trips, cal, cd, st = (pd.concat([p[k] for p in parts], ignore_index=True)
                                                 for k in ("agency", "routes", "stops", "trips", "cal", "cd", "st"))
    stats["lignes_train"] = len(routes)

    # type affiché (route_desc) et numéro du train
    agency["agency_name"] = agency["agency_name"].str.strip()
    agency_names = dict(zip(agency.get("agency_id", pd.Series(dtype=str)), agency["agency_name"]))
    routes["route_desc"] = [lv_type(agency_names.get(a, "")) if feed["type"] == "Vivi" else feed["type"]
                            for a in routes["agency_id"]]
    short = dict(zip(routes["route_id"], routes["route_short_name"].str.strip()))
    if "trip_short_name" not in trips:
        trips["trip_short_name"] = ""
    if feed["number"] in ("route", "route_unique"):
        # LTG Link : "889/890" -> "889" (numéro changé à la frontière) ; Elron : ligne "R12", "RE34"
        trips["trip_short_name"] = trips["route_id"].map(short).fillna("").map(lambda s: s.split("/")[0].strip())
    if feed["number"] == "route_unique":
        # GTFS national lituanien : route_short_name est le numéro d'une « ligne » qui regroupe souvent plusieurs
        # trains (« 833 » : 20 trains Vilnius - Kaunas par jour, chacun avec son propre numéro). Le numéro n'est
        # gardé que si la ligne ne compte qu'un train (une seule heure de départ) : 889 Vilnius - Riga, 31 Mockava
        first_dep = st.sort_values("stop_sequence", key=lambda c: c.astype(int)).groupby("trip_id")["departure_time"].first()
        departures = trips["trip_id"].map(first_dep).groupby(trips["route_id"]).nunique()
        shared = set(departures[departures > 1].index)
        trips.loc[trips["route_id"].isin(shared), "trip_short_name"] = ""
        stats["numeros_ambigus_retires"] = int(trips["route_id"].isin(shared).sum())
    else:
        trips["trip_short_name"] = trips["trip_short_name"].str.strip().map(lambda s: s if re.fullmatch(r"\d+", s) else "")
    trips = trips.merge(routes[["route_id", "route_desc"]], on="route_id", how="left")

    # points de simple passage retirés ; trajets d'un seul arrêt écartés
    if "pickup_type" in st and "drop_off_type" in st:
        st = st[~((st["pickup_type"] == "1") & (st["drop_off_type"] == "1"))]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]

    used = set(st["stop_id"])
    parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""} if "parent_station" in stops else set()
    stops = stops[stops["stop_id"].isin(used | parents)]
    routes = routes[routes["route_id"].isin(set(trips["route_id"]))]
    if "agency_id" in agency:
        agency = agency[agency["agency_id"].isin(set(routes["agency_id"]))]
    services = set(trips["service_id"])
    cal = cal[cal["service_id"].isin(services)]
    cd = cd[cd["service_id"].isin(services)]
    routes = routes[[c for c in ("route_id", "agency_id", "route_short_name", "route_long_name", "route_desc",
                                 "route_type") if c in routes]]
    trips = trips[[c for c in ("route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
                               "direction_id", "route_desc") if c in trips]]
    st = st[[c for c in ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type",
                         "drop_off_type") if c in st]]
    stops = stops[[c for c in ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type",
                               "parent_station", "stop_timezone") if c in stops]]
    if trips.empty:
        raise ValueError("aucun train dans le flux")

    tables = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
              "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name, df in tables.items():
            if name in ("calendar.txt", "calendar_dates.txt") and df.empty:
                continue
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)

    days = pd.concat([cal.get("start_date", pd.Series(dtype=str)), cal.get("end_date", pd.Series(dtype=str)),
                      cd.get("date", pd.Series(dtype=str))])
    stats.update({"du": days.min() if len(days) else "", "au": days.max() if len(days) else "",
                  "trajets": len(trips), "passages": len(st), "gares": len(stops),
                  "types": trips["route_desc"].value_counts().to_dict()})
    return stats


def main():
    chosen = [a for a in sys.argv[1:] if a in FEEDS] or list(FEEDS)
    print("=" * 60)
    print("🚆 Ingestion GTFS pays baltes (LTG Link, Vivi, Elron)")
    print("=" * 60)
    failed = []
    for name in chosen:
        feed = FEEDS[name]
        raws = []
        try:
            srcs = []
            for i, (url, prefix, required) in enumerate(feed["sources"]):
                raw = f"{feed['out']}.{i}.raw"
                raws.append(raw)
                try:
                    download(url, raw)
                    srcs.append((raw, prefix))
                except Exception as e:
                    if required:
                        raise
                    logging.warning(f"⚠️ [{name}] source facultative indisponible ({url}) : {e}")
            stats = build(srcs, feed["out"], feed)
            for k, v in stats.items():
                logging.info(f"   [{name}] {k} : {v}")
            print(f"✅ {feed['out']} ({os.path.getsize(feed['out']) / 1e6:.1f} Mo)")
        except Exception as e:
            # un pays en panne n'empêche pas les autres ; la version déjà publiée du pays en panne reste en place
            logging.error(f"❌ [{name}] {type(e).__name__} : {e}")
            failed.append(name)
        finally:
            for raw in raws:
                if os.path.exists(raw):
                    os.remove(raw)
    if failed:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::Pays baltes : échec pour {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
