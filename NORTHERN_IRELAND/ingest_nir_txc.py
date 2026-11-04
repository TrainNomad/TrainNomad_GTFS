"""
Télécharge et convertit en GTFS les horaires de NI Railways (Translink, Irlande du Nord), publiés en TransXChange.

Source : OpenDataNI, jeu « Translink Northern Ireland Rail Timetable Data » (licence OGL), un ZIP contenant un
seul XML TransXChange 2.4, mis à jour à chaque changement d'horaire. Le nom du fichier change à chaque
publication (« nir-and-enterprise-10-sept-26.zip ») : son adresse est demandée à l'API CKAN du portail
(ressource RESOURCE_ID), qui renvoie vers un stockage Cloudflare R2.

Structure du XML :
  - StopPoints : un quai par arrêt ("9000BR4", "9000BR6" = Bangor quais 4 et 6 ; "70009000GCP1" = Belfast
    Grand Central quai 1), avec position et CommonName (« Bangor Train Station »)
  - Services : Bangor, Larne, Derry~Londonderry, Portadown (opérateur RailNI) et l'Enterprise (opérateur ENT)
  - JourneyPatternSections : suite de liens From -> To, RunTime, WaitTime (arrêt) sur le To, Activity
    (pickUp, setDown, pickUpAndSetDown, pass)
  - VehicleJourneys : heure de départ, JourneyPattern, OperatingProfile (jours de la semaine dans
    l'OperatingPeriod du service, moins les DaysOfNonOperation). Aucun numéro de train publié

Ce que fait le script :
  ✓ un arrêt GTFS par gare : quais réunis (numéro de quai retiré du code, position moyenne)
  ✓ horaires calculés lien par lien : arrivée = départ précédent + RunTime, départ = arrivée + WaitTime ;
    arrêts « pass » retirés ; pickUp seul = descente interdite, setDown seul = montée interdite
  ✓ jours de circulation en calendar_dates.txt, à partir d'hier
  ✓ Enterprise écarté : déjà dans le flux Irish Rail (IRELAND/ingest_ie_gtfs.py), avec ses numéros de train

Usage :
    python NORTHERN_IRELAND/ingest_nir_txc.py
    python NORTHERN_IRELAND/ingest_nir_txc.py --input nir.zip      (fichier déjà téléchargé)
"""
import argparse
import io
import json
import logging
import os
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta
import xml.etree.ElementTree as ET

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(GTFS_DIR, "GERMANY"))
from ingest_de_gtfs import download  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "nir_gtfs.zip")
CKAN_API = "https://admin.opendatani.gov.uk/api/3/action/resource_show?id="
RESOURCE_ID = "ff8c5dd8-dcfd-4141-a270-df242f114215"  # « NIR Rail TransXChange data »
TIMEZONE = "Europe/London"
AGENCY = {"agency_id": "NIR", "agency_name": "NI Railways", "agency_url": "https://www.translink.co.uk/",
          "agency_timezone": TIMEZONE, "agency_lang": "en"}
# opérateurs TransXChange écartés : Enterprise, déjà publié par Irish Rail
SKIPPED_OPERATORS = {"ENT"}
# CommonName (sans « Train Station ») -> nom de la gare
RENAME = {"Grand Central Station": "Belfast Grand Central", "Lanyon Place": "Belfast Lanyon Place",
          "Connolly": "Dublin Connolly"}

WEEKDAYS = {"Monday": [0], "Tuesday": [1], "Wednesday": [2], "Thursday": [3], "Friday": [4], "Saturday": [5],
            "Sunday": [6], "MondayToFriday": [0, 1, 2, 3, 4], "MondayToSaturday": [0, 1, 2, 3, 4, 5],
            "MondayToSunday": [0, 1, 2, 3, 4, 5, 6], "Weekend": [5, 6], "NotSaturday": [0, 1, 2, 3, 4, 6]}


def resource_url() -> str:
    """Adresse actuelle du ZIP TransXChange (son nom change à chaque publication)."""
    req = urllib.request.Request(CKAN_API + RESOURCE_ID, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)["result"]
    logging.info(f"   OpenDataNI : {res.get('name')} — {res.get('description')} (modifié le {res.get('last_modified')})")
    return res["url"]


def seconds(duration: str | None) -> int:
    """Durée ISO 8601 de TransXChange : "PT2M" -> 120, "PT1H5M30S" -> 3930."""
    if not duration:
        return 0
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        raise ValueError(f"durée TransXChange illisible : {duration}")
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def hms(t: int) -> str:
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def station_code(atco: str) -> str:
    """Quai -> gare : "9000BR4" -> "9000BR", "70009000GCP1" -> "70009000GCP"."""
    return re.sub(r"\d+$", "", atco)


def to_date(s: str) -> date:
    return date.fromisoformat(s)


def operating_days(profile, start: date, end: date, unknown: set) -> set:
    """Jours de circulation d'un OperatingProfile entre start et end (inclus)."""
    weekdays = set()
    for d in profile.findall("RegularDayType/DaysOfWeek/*"):
        if d.tag not in WEEKDAYS:
            unknown.add(d.tag)
        weekdays.update(WEEKDAYS.get(d.tag, []))
    days = {start + timedelta(i) for i in range((end - start).days + 1)}
    days = {d for d in days if d.weekday() in weekdays}
    for r in profile.findall("SpecialDaysOperation/DaysOfNonOperation/DateRange"):
        a, b = to_date(r.findtext("StartDate")), to_date(r.findtext("EndDate"))
        days = {d for d in days if not a <= d <= b}
    for r in profile.findall("SpecialDaysOperation/DaysOfOperation/DateRange"):
        a, b = to_date(r.findtext("StartDate")), to_date(r.findtext("EndDate"))
        days |= {a + timedelta(i) for i in range((b - a).days + 1) if start <= a + timedelta(i) <= end}
    for child in profile:
        if child.tag not in ("RegularDayType", "SpecialDaysOperation"):
            unknown.add(child.tag)  # BankHolidayOperation... : non géré, signalé
    return days


def build(src: str, dst: str) -> dict:
    stats = defaultdict(int)
    since = date.today() - timedelta(days=1)
    with zipfile.ZipFile(src) as z:
        xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
        if len(xmls) != 1:
            raise ValueError(f"un XML TransXChange attendu dans le ZIP, trouvé : {z.namelist()}")
        txt = z.read(xmls[0]).decode("utf-8-sig")
    txt = re.sub(r'\sxmlns="[^"]+"', "", txt, count=1)  # espace de noms par défaut retiré : chemins lisibles
    root = ET.fromstring(txt)
    unknown = set()

    # gares : quais réunis
    platforms = defaultdict(list)
    names = {}
    for p in root.find("StopPoints"):
        code = station_code(p.findtext("AtcoCode"))
        platforms[code].append((float(p.findtext("Place/Location/Latitude")), float(p.findtext("Place/Location/Longitude"))))
        name = re.sub(r"\s+(Train|Rail)\s+Station$", "", " ".join(p.findtext("Descriptor/CommonName").split()))
        names[code] = RENAME.get(name, name)

    operators = {o.get("id"): o.findtext("OperatorCode") for o in root.find("Operators")}
    services = {}
    for sv in root.find("Services"):
        op = operators.get(sv.findtext("RegisteredOperatorRef"), "")
        services[sv.findtext("ServiceCode")] = {
            "op": op, "line": sv.findtext("Lines/Line/LineName") or sv.findtext("ServiceCode"),
            "start": to_date(sv.findtext("OperatingPeriod/StartDate")),
            "end": to_date(sv.findtext("OperatingPeriod/EndDate")),
            "profile": sv.find("OperatingProfile"),
            "patterns": {jp.get("id"): jp.findtext("JourneyPatternSectionRefs") for jp in sv.iter("JourneyPattern")},
        }
        stats[f"service {sv.findtext('Lines/Line/LineName')} ({op})"] = 0
    sections = {s.get("id"): list(s) for s in root.find("JourneyPatternSections")}

    routes, trips, stop_times, dates = {}, [], [], {}
    service_ids = {}
    for vj in root.find("VehicleJourneys"):
        sv = services[vj.findtext("ServiceRef")]
        stats[f"service {sv['line']} ({sv['op']})"] += 1
        if sv["op"] in SKIPPED_OPERATORS:
            stats["trajets_enterprise_ecartes"] += 1
            continue
        profile = vj.find("OperatingProfile")
        profile = profile if profile is not None else sv["profile"]
        days = {d for d in operating_days(profile, sv["start"], sv["end"], unknown) if d >= since}
        if not days:
            stats["trajets_sans_jour_a_venir"] += 1
            continue
        key = tuple(sorted(days))
        sid = service_ids.setdefault(key, f"S{len(service_ids) + 1}")
        dates[sid] = key

        # horaires, lien par lien
        links = []
        for ref in sv["patterns"][vj.findtext("JourneyPatternRef")].split():
            links += sections[ref]
        h, mi, s = (int(x) for x in vj.findtext("DepartureTime").split(":"))  # "07:17:00"
        t = h * 3600 + mi * 60 + s
        calls = []  # (gare, arrivée, départ, activité)
        first = links[0].find("From")
        calls.append([first.findtext("StopPointRef"), t, t + seconds(first.findtext("WaitTime")), first.findtext("Activity")])
        t = calls[-1][2]
        for link in links:
            to = link.find("To")
            arr = t + seconds(link.findtext("RunTime"))
            dep = arr + seconds(to.findtext("WaitTime"))
            calls.append([to.findtext("StopPointRef"), arr, dep, to.findtext("Activity")])
            t = dep
        calls = [c for c in calls if c[3] != "pass"]
        if len(calls) < 2:
            stats["trajets_trop_courts"] += 1
            continue

        tid = vj.findtext("VehicleJourneyCode")
        rid = re.sub(r"\W+", "_", sv["line"]).strip("_")
        routes[rid] = sv["line"]
        last = names[station_code(calls[-1][0])]
        trips.append({"route_id": rid, "service_id": sid, "trip_id": tid, "trip_headsign": last,
                      "trip_short_name": "", "route_desc": sv["line"]})
        for i, (atco, arr, dep, act) in enumerate(calls, 1):
            stop_times.append({"trip_id": tid, "arrival_time": hms(arr), "departure_time": hms(dep),
                               "stop_id": station_code(atco), "stop_sequence": i,
                               "pickup_type": 1 if act == "setDown" or i == len(calls) else 0,
                               "drop_off_type": 1 if act == "pickUp" or i == 1 else 0})
    if unknown:
        logging.warning(f"   ⚠️ éléments TransXChange non gérés (jours peut-être faux) : {sorted(unknown)}")

    used = {s["stop_id"] for s in stop_times}
    stops = pd.DataFrame([{"stop_id": c, "stop_name": names[c],
                           "stop_lat": round(sum(p[0] for p in platforms[c]) / len(platforms[c]), 6),
                           "stop_lon": round(sum(p[1] for p in platforms[c]) / len(platforms[c]), 6),
                           "location_type": 0} for c in sorted(used)])
    tables = {
        "agency.txt": pd.DataFrame([AGENCY]),
        "routes.txt": pd.DataFrame([{"route_id": r, "agency_id": "NIR", "route_short_name": n, "route_long_name": n,
                                     "route_type": 2} for r, n in sorted(routes.items())]),
        "stops.txt": stops,
        "trips.txt": pd.DataFrame(trips),
        "calendar_dates.txt": pd.DataFrame([{"service_id": sid, "date": d.strftime("%Y%m%d"), "exception_type": 1}
                                            for sid, ds in dates.items() if sid in {t["service_id"] for t in trips}
                                            for d in ds]),
        "stop_times.txt": pd.DataFrame(stop_times),
    }
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name, df in tables.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    cd = tables["calendar_dates.txt"]
    stats.update({"du": cd["date"].min(), "au": cd["date"].max(), "trajets": len(trips),
                  "passages": len(stop_times), "gares": len(stops),
                  "lignes": tables["trips.txt"]["route_desc"].value_counts().to_dict()})
    return dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="ZIP TransXChange déjà téléchargé")
    args = ap.parse_args()
    print("=" * 60)
    print("🚆 Ingestion TransXChange Irlande du Nord (NI Railways)")
    print("=" * 60)
    raw = args.input or OUTPUT_ZIP + ".raw"
    try:
        if not args.input:
            download(resource_url(), raw)
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
            print(f"::error::Irlande du Nord : {type(e).__name__} : {' '.join(str(e).split())[:500]}")
        raise
