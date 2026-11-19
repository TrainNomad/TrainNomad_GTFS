"""
Télécharge et prépare les GTFS des ferries qui relient les îles Britanniques au continent :
  - BRITTANY_FERRIES : Portsmouth / Poole / Plymouth <-> Caen, Cherbourg, Le Havre, Saint-Malo, Roscoff ;
    Rosslare <-> Cherbourg, Cork <-> Roscoff ; Portsmouth / Plymouth / Rosslare <-> Bilbao, Santander
  - TRANSMANCHE : Dieppe <-> Newhaven (DFDS pour le syndicat mixte Transmanche)
  - IRISH_FERRIES : Dublin <-> Cherbourg
Les traversées Holyhead / Fishguard <-> Irlande, Cairnryan <-> Belfast, Harwich <-> Hoek van Holland et vers
l'île de Man sont déjà dans le flux National Rail (UK/ingest_uk_gtfs.py).

Sources : transport.data.gouv.fr (point d'accès national français), adresses de téléchargement stables.
Irish Ferries ne publie ni GTFS ni NeTEx : FERRIES/irish_ferries_pdf.py transcrit ses PDF d'horaires dans
FERRIES/irish_ferries_traversees.csv (versionné), dont ce script fait le GTFS. Pas de téléchargement : le CSV
est à régénérer à la main quand un nouveau PDF paraît (fin des horaires connus affichée à chaque passage).

Fuseau des horaires : la norme GTFS veut toutes les heures dans le fuseau de l'agence (Europe/Paris ici).
Brittany Ferries la respecte ; Transmanche écrit chaque heure dans le fuseau du port (Newhaven en heure
anglaise : « Dieppe 06:00 -> Newhaven 09:00 », 4 h de traversée et non 3). Le script le détecte au lieu de
le supposer : pour chaque paire de ports dans deux fuseaux, la durée aller moins la durée retour vaut
2 x l'écart des fuseaux si les heures sont locales, 0 sinon. Heures locales -> ramenées au fuseau de l'agence.
Cas douteux (entre les deux) : erreur, le flux publié précédemment reste en place.

Ce que fait le script :
  ✓ heures ramenées au fuseau de l'agence si le flux les écrit en heure locale de chaque port
  ✓ shapes.txt, transfers.txt retirés ; stop_desc (longs textes commerciaux) vidé

Usage :
    python FERRIES/ingest_ferries.py
    python FERRIES/ingest_ferries.py --input-brittany bf.zip --input-transmanche tm.zip
"""
import argparse
import csv
import io
import logging
import os
import statistics
import sys
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

FEEDS = {
    "brittany": {
        "url": "https://transport.data.gouv.fr/resources/83427/download",
        "out": os.path.join(BASE_DIR, "brittany_ferries_gtfs.zip"),
        "stop_tz": {},  # stop_timezone fourni par le flux
    },
    "transmanche": {
        "url": "https://transport.data.gouv.fr/resources/83981/download",
        "out": os.path.join(BASE_DIR, "transmanche_gtfs.zip"),
        # le flux ne donne pas stop_timezone
        "stop_tz": {"FR:0:ZE:NEWHAVEN:ATOUMOD0118": "Europe/London",
                    "FR:76217:ZE:DIEPPE:ATOUMOD0118": "Europe/Paris"},
    },
    "irish": {
        # pas de GTFS publié : construit depuis le CSV tiré des PDF (FERRIES/irish_ferries_pdf.py)
        "csv": os.path.join(BASE_DIR, "irish_ferries_traversees.csv"),
        "out": os.path.join(BASE_DIR, "irish_ferries_gtfs.zip"),
        "stop_tz": {},
        "heures_agence": True,  # make_irish_gtfs écrit déjà tout dans le fuseau de l'agence
    },
}

# Irish Ferries : Dublin Port, terminal 1 (Alexandra Road) ; Cherbourg, gare maritime (même terminal et même
# code que Brittany Ferries, « FR CER »)
IF_AGENCY_TZ = "Europe/Dublin"
IF_STOPS = {
    "IE DUB": ("Dublin Port (Terminal 1)", 53.34659, -6.19805, "Europe/Dublin"),  # OSM
    "FR CER": ("Cherbourg", 49.64485, -1.611565, "Europe/Paris"),
}
IF_DIRECTIONS = {"DUB-CER": ("IE DUB", "FR CER", 0), "CER-DUB": ("FR CER", "IE DUB", 1)}
IF_SHIPS = {"WBY": "W.B. Yeats", "IIS": "Isle of Inisheer", "JJ": "James Joyce"}
DROPPED = {"shapes.txt", "transfers.txt"}


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def minutes(t: str) -> int:
    h, m, *_ = t.split(":")
    return int(h) * 60 + int(m)


def hms(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}:00"


def utc_offset(tz: str) -> int:
    """Décalage UTC (minutes) d'un fuseau ; Europe/London, Dublin, Paris, Madrid changent d'heure aux mêmes
    dates : l'écart entre deux de ces fuseaux est le même toute l'année (vérifié en janvier et en juillet)."""
    offs = {int(ZoneInfo(tz).utcoffset(datetime(2026, m, 15, 12)).total_seconds() // 60) for m in (1, 7)}
    return min(offs)  # heure d'hiver ; seul l'écart entre fuseaux sert


def local_time_score(st: pd.DataFrame, tz_of: dict, agency_tz: str) -> tuple[float | None, list]:
    """Score ~1 si les heures sont locales à chaque port, ~0 si elles sont dans le fuseau de l'agence."""
    st = st.assign(q=st["stop_sequence"].astype(int)).sort_values(["trip_id", "q"])
    g = st.groupby("trip_id")
    trips = pd.DataFrame({"a": g["stop_id"].first(), "b": g["stop_id"].last(),
                          "d": g["arrival_time"].last().map(minutes) - g["departure_time"].first().map(minutes)})
    # traversée rapide de chaque sens (10e centile) : le même type de navire à l'aller et au retour. La médiane
    # mélange les traversées de nuit (Le Havre -> Portsmouth, 10 h) et de jour (6 h) ; le minimum, les trajets
    # aberrants du flux (un Bilbao -> Portsmouth en 3 h 45)
    med = trips.groupby(["a", "b"])["d"].quantile(0.1)
    scores, detail = [], []
    for (a, b), d_ab in med.items():
        if a >= b or (b, a) not in med:
            continue
        gap = utc_offset(tz_of.get(b, agency_tz)) - utc_offset(tz_of.get(a, agency_tz))
        if gap == 0:
            continue
        s = (d_ab - med[(b, a)]) / (2 * gap)
        scores.append(s)
        detail.append(f"{a} -> {b} {d_ab:.0f} min / retour {med[(b, a)]:.0f} min (score {s:.2f})")
    return (statistics.median(scores) if scores else None), detail


def make_irish_gtfs(csv_path: str, dest: str) -> None:
    """CSV des traversées (date, sens, navire, départ et arrivée le lendemain, en heure locale de chaque port)
    -> GTFS brut, heures dans le fuseau de l'agence (Europe/Dublin). Un service par combinaison sens / navire /
    horaires, ses jours dans calendar_dates."""
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{csv_path} vide")
    last = max(r["date"] for r in rows)
    if last < date.today().isoformat():
        raise ValueError(f"{csv_path} : horaires terminés le {last} : relancer FERRIES/irish_ferries_pdf.py")
    logging.info(f"   [irish] horaires connus jusqu'au {last}")

    def to_agency(hhmm: str, stop: str, next_day: bool) -> str:
        shift = utc_offset(IF_AGENCY_TZ) - utc_offset(IF_STOPS[stop][3])  # Cherbourg : -60
        return hms(minutes(hhmm) + shift + (24 * 60 if next_day else 0))

    services: dict[tuple, list[str]] = {}
    for r in rows:
        services.setdefault((r["sens"], r["navire"], r["depart"], r["arrivee"]), []).append(r["date"].replace("-", ""))
    trips, stop_times, cal = [], [], []
    for (sens, ship, dep, arr), dates in sorted(services.items()):
        a, b, direction = IF_DIRECTIONS[sens]
        sid = f"{sens}_{ship}_{dep.replace(':', '')}_{arr.replace(':', '')}"
        trips.append({"route_id": "DUB-CER", "service_id": sid, "trip_id": sid, "direction_id": direction,
                      "trip_headsign": IF_STOPS[b][0], "trip_short_name": IF_SHIPS.get(ship, ship)})
        t_dep, t_arr = to_agency(dep, a, False), to_agency(arr, b, True)
        stop_times += [{"trip_id": sid, "arrival_time": t_dep, "departure_time": t_dep, "stop_id": a,
                        "stop_sequence": 1},
                       {"trip_id": sid, "arrival_time": t_arr, "departure_time": t_arr, "stop_id": b,
                        "stop_sequence": 2}]
        cal += [{"service_id": sid, "date": d, "exception_type": 1} for d in sorted(dates)]
    tables = {
        "agency.txt": [{"agency_id": "IF", "agency_name": "Irish Ferries", "agency_url": "https://www.irishferries.com",
                        "agency_timezone": IF_AGENCY_TZ, "agency_lang": "en"}],
        "stops.txt": [{"stop_id": k, "stop_name": n, "location_type": 0, "stop_lat": lat, "stop_lon": lon,
                       "stop_timezone": tz} for k, (n, lat, lon, tz) in IF_STOPS.items()],
        "routes.txt": [{"route_id": "DUB-CER", "agency_id": "IF", "route_short_name": "IE DUB-FR CER",
                        "route_long_name": "DUBLIN-CHERBOURG", "route_type": 4}],
        "trips.txt": trips, "stop_times.txt": stop_times, "calendar_dates.txt": cal,
    }
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zo:
        for n, recs in tables.items():
            buf = io.StringIO()
            pd.DataFrame(recs).to_csv(buf, index=False, lineterminator="\n")
            zo.writestr(n, buf.getvalue())


def build(name: str, cfg: dict, src: str) -> dict:
    stats = {}
    with zipfile.ZipFile(src) as z:
        tables = {n: read(z, n) for n in z.namelist() if n.endswith(".txt") and n not in DROPPED}
    agency_tz = tables["agency.txt"]["agency_timezone"].iloc[0]
    stops, st = tables["stops.txt"], tables["stop_times.txt"]
    tz_of = dict(cfg["stop_tz"])
    if "stop_timezone" in stops:
        tz_of.update({s: tz for s, tz in zip(stops["stop_id"], stops["stop_timezone"]) if tz})
    if "stop_desc" in stops:
        stops["stop_desc"] = ""

    score, detail = (None, []) if cfg.get("heures_agence") else local_time_score(st, tz_of, agency_tz)
    for line in detail:
        logging.info(f"   {line}")
    stats["score_heures_locales"] = None if score is None else round(score, 2)
    if score is not None and 0.25 < score < 0.75:
        raise ValueError(f"{name} : fuseau des horaires indécidable (score {score:.2f}) : vérifier le flux à la main")
    if score is not None and score >= 0.75:
        # heure locale du port -> heure de l'agence
        shift = st["stop_id"].map(lambda s: utc_offset(agency_tz) - utc_offset(tz_of.get(s, agency_tz)))
        for col in ("arrival_time", "departure_time"):
            st[col] = [hms(minutes(t) + d) if t else t for t, d in zip(st[col], shift)]
        stats["heures_ramenees_au_fuseau_agence"] = int((shift != 0).sum())
        # port à l'est du fuseau de l'agence, départ juste après minuit : heure négative, non représentable
        if any(t.startswith("-") for col in ("arrival_time", "departure_time") for t in st[col]):
            raise ValueError(f"{name} : heure négative après correction du fuseau")
        tables["stop_times.txt"] = st

    out = cfg["out"]
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zo:
        for n, df in tables.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            zo.writestr(n, buf.getvalue())
    os.replace(tmp, out)
    stats.update({"fuseau_agence": agency_tz, "trajets": len(tables["trips.txt"]), "ports": len(stops)})
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-brittany", help="GTFS Brittany Ferries déjà téléchargé")
    ap.add_argument("--input-transmanche", help="GTFS Transmanche déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("⛴️  Ingestion GTFS ferries (Brittany Ferries, Transmanche, Irish Ferries)")
    print("=" * 60)
    given = {"brittany": args.input_brittany, "transmanche": args.input_transmanche, "irish": None}
    failed = []
    for name, cfg in FEEDS.items():
        raw = given[name] or cfg["out"] + ".raw"
        try:
            if "csv" in cfg:
                make_irish_gtfs(cfg["csv"], raw)
            elif not given[name]:
                download(cfg["url"], raw)
            for k, v in build(name, cfg, raw).items():
                logging.info(f"   [{name}] {k} : {v}")
            print(f"✅ {cfg['out']} ({os.path.getsize(cfg['out']) / 1e6:.2f} Mo)")
        except Exception as e:
            # une compagnie en échec n'empêche pas l'autre ; le pipeline refuse le GTFS absent ou périmé
            logging.error(f"❌ {name} : {type(e).__name__} : {e}")
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::error::Ferries ({name}) : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
            failed.append(name)
            if os.path.exists(cfg["out"]):
                os.remove(cfg["out"])  # pas de version précédente publiée par erreur
        finally:
            if not given[name] and os.path.exists(raw):
                os.remove(raw)
    if len(failed) == len(FEEDS):
        sys.exit(1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
