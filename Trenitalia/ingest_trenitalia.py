"""
Télécharge le flux NeTEx de Trenitalia (point d'accès national italien, CCISS) et le convertit en GTFS.

Sortie : Trenitalia/trenitalia_gtfs.zip, lu par build_network.py (opérateur TRENITALIA de operators.json)
et fusionné avec les autres réseaux (SNCF, Eurostar, Renfe, National Rail...).

Pipeline :
  1. Téléchargement du NeTEx (IT-IT-TRENITALIA_L1.xml.gz : ~9 Mo compressé, ~300 Mo de XML)
  2. Lecture en flux (iterparse) : gares (StopPlace), catégories commerciales (Line : FR, IC, REG...),
     parcours (ServiceJourneyPattern), calendriers (UicOperatingPeriod) et trains (ServiceJourney)
  3. Gares : stop_code = code UIC à 7 chiffres (830000070 -> 8300070), colonne trenitalia_id de stations.csv
  4. Horaires : DayOffset -> heures GTFS au-delà de 24:00, ForBoarding/ForAlighting -> pickup_type/drop_off_type
  5. Calendriers : ValidDayBits identiques = un seul service, écrits en calendar_dates
  6. Écriture du GTFS + rapport Trenitalia/trenitalia_gtfs_report.json

Usage :
  python Trenitalia/ingest_trenitalia.py                                  # télécharge le flux
  python Trenitalia/ingest_trenitalia.py --input IT-IT-TRENITALIA_L1.xml.gz
"""
import argparse
import csv
import gzip
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ZIP_TRENITALIA = os.path.join(BASE_DIR, "trenitalia_gtfs.zip")
OUT_ZIP_ITALO = os.path.join(BASE_DIR, "italo_gtfs.zip")
REPORT_PATH = os.path.join(BASE_DIR, "italy_gtfs_report.json")

# Sources NeTEx italiennes (CCISS - Point d'accès national)
NETEX_SOURCES = {
    "TRENITALIA": {
        "url": "https://www.cciss.it/nap/mmtis/public/api/v1/download/blob/Asset/1080596/resource",
        "name": "Trenitalia",
        "website": "https://www.trenitalia.com",
        "out_zip": OUT_ZIP_TRENITALIA,
    },
    "ITALO": {
        "url": "https://www.cciss.it/nap/mmtis/public/api/v1/download/blob/Asset/1814124/resource",
        "name": "Italo - Nuovo Trasporto Viaggiatori",
        "website": "https://www.italotreno.it",
        "out_zip": OUT_ZIP_ITALO,
    },
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEZONE = "Europe/Rome"

NS = "{http://www.netex.org.uk/netex}"
GTFS_BUS = 3
GTFS_RAIL = 2


# ---------------------------------------------------------------------------
# Téléchargement
# ---------------------------------------------------------------------------

def download_netex(dest: str, url: str, name: str, attempts: int = 4):
    logging.info(f"📥 Téléchargement du NeTEx {name} : {url}")
    for attempt in range(1, attempts + 1):
        tmp = dest + ".part"
        try:
            # Utiliser curl directement (plus robuste pour les gros fichiers)
            logging.info(f"  Tentative {attempt}/{attempts} avec curl...")
            result = subprocess.run(
                ["curl", "-sSfL", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "5", "-o", tmp, url],
                check=True, timeout=600, capture_output=True
            )
            os.replace(tmp, dest)
            logging.info(f"  ✅ {os.path.getsize(dest) / 1e6:.1f} Mo reçus")
            return
        except subprocess.SubprocessError as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == attempts:
                raise RuntimeError(f"Échec téléchargement {name} après {attempts} tentatives: {e}")
            wait = 15 * attempt
            logging.warning(f"  ❌ Échec ({e}), nouvel essai dans {wait}s...")
            time.sleep(wait)


def open_xml(path: str):
    """Flux XML brut, qu'il soit livré en .gz, en .zip ou non compressé."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if magic == b"PK\x03\x04":
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        return z.open(name)
    return open(path, "rb")


# ---------------------------------------------------------------------------
# Lecture NeTEx
# ---------------------------------------------------------------------------

def text(elem, path, default=""):
    v = elem.findtext(path)
    return v.strip() if v else default


def ref(elem, path):
    child = elem.find(path)
    return child.get("ref", "") if child is not None else ""


class NetexReader:
    def __init__(self):
        self.stop_places = {}     # StopPlace id -> dict
        self.ssp_fallback = {}    # ScheduledStopPoint id -> dict (si aucune affectation de gare)
        self.ssp_to_place = {}    # ScheduledStopPoint id -> StopPlace id
        self.lines = {}           # Line id -> dict
        self.periods = {}         # UicOperatingPeriod id -> (date de début, ValidDayBits)
        self.day_type_period = {} # DayType id -> UicOperatingPeriod id
        self.patterns = {}        # ServiceJourneyPattern id -> (Line id, {point id: (ssp, montée, descente)})
        self.journeys = []        # (id, nom, pattern, day type, [(point, arr, arr_off, dep, dep_off)])
        self.publication = ""
        self.valid_between = ("", "")

    def parse(self, stream):
        logging.info("🔎 Lecture du NeTEx")
        handlers = {
            "StopPlace": self.on_stop_place,
            "ScheduledStopPoint": self.on_scheduled_stop_point,
            "PassengerStopAssignment": self.on_stop_assignment,
            "Line": self.on_line,
            "UicOperatingPeriod": self.on_operating_period,
            "DayTypeAssignment": self.on_day_type_assignment,
            "ServiceJourneyPattern": self.on_pattern,
            "ServiceJourney": self.on_journey,
        }
        # éléments volumineux sans intérêt pour le GTFS (géométries des tronçons...)
        discard = {"ServiceLink", "DayType", "Route", "Operator"}
        for _, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag.rpartition("}")[2]
            handler = handlers.get(tag)
            if handler:
                handler(elem)
                elem.clear()
            elif tag in discard:
                elem.clear()
            elif tag == "PublicationTimestamp":
                self.publication = (elem.text or "").strip()
            elif tag == "ValidBetween" and not self.valid_between[0]:
                self.valid_between = (text(elem, NS + "FromDate")[:10], text(elem, NS + "ToDate")[:10])

        logging.info(
            f"   {len(self.stop_places)} gares, {len(self.lines)} catégories, {len(self.patterns)} parcours, "
            f"{len(self.journeys)} trains, {len(self.periods)} calendriers"
        )

    @staticmethod
    def location(elem):
        loc = elem.find(".//" + NS + "Location")
        if loc is None:
            return None, None
        try:
            return float(text(loc, NS + "Latitude")), float(text(loc, NS + "Longitude"))
        except ValueError:
            return None, None

    def on_stop_place(self, e):
        lat, lon = self.location(e.find(NS + "Centroid") if e.find(NS + "Centroid") is not None else e)
        self.stop_places[e.get("id")] = {
            "name": text(e, NS + "Name"), "code": text(e, NS + "PrivateCode"), "lat": lat, "lon": lon,
        }

    def on_scheduled_stop_point(self, e):
        lat, lon = self.location(e)
        self.ssp_fallback[e.get("id")] = {
            "name": text(e, NS + "Name"), "code": text(e, NS + "PrivateCode") or text(e, NS + "PublicCode"),
            "lat": lat, "lon": lon,
        }

    def on_stop_assignment(self, e):
        ssp, place = ref(e, NS + "ScheduledStopPointRef"), ref(e, NS + "StopPlaceRef")
        if ssp and place:
            self.ssp_to_place[ssp] = place

    def on_line(self, e):
        self.lines[e.get("id")] = {
            "code": text(e, NS + "PrivateCode") or e.get("id").rpartition(":")[2],
            "short": text(e, NS + "PublicCode") or text(e, NS + "ShortName"),
            "name": text(e, NS + "Name"),
            "mode": text(e, NS + "TransportMode"),
        }

    def on_operating_period(self, e):
        self.periods[e.get("id")] = (date.fromisoformat(text(e, NS + "FromDate")[:10]), text(e, NS + "ValidDayBits"))

    def on_day_type_assignment(self, e):
        day_type, period = ref(e, NS + "DayTypeRef"), ref(e, NS + "OperatingPeriodRef")
        if day_type and period:
            self.day_type_period[day_type] = period

    def on_pattern(self, e):
        points = {}
        for p in e.iter(NS + "StopPointInJourneyPattern"):
            points[p.get("id")] = (
                ref(p, NS + "ScheduledStopPointRef"),
                text(p, NS + "ForBoarding", "true") != "false",
                text(p, NS + "ForAlighting", "true") != "false",
            )
        self.patterns[e.get("id")] = (ref(e, ".//" + NS + "LineRef"), points)

    def on_journey(self, e):
        passing = []
        for pt in e.iter(NS + "TimetabledPassingTime"):
            passing.append((
                ref(pt, NS + "StopPointInJourneyPatternRef"),
                text(pt, NS + "ArrivalTime"), text(pt, NS + "ArrivalDayOffset", "0"),
                text(pt, NS + "DepartureTime"), text(pt, NS + "DepartureDayOffset", "0"),
            ))
        self.journeys.append((
            e.get("id"), text(e, NS + "Name"), ref(e, NS + "ServiceJourneyPatternRef"),
            ref(e, NS + "dayTypes/" + NS + "DayTypeRef"), passing,
        ))


# ---------------------------------------------------------------------------
# Conversion GTFS
# ---------------------------------------------------------------------------

def gtfs_time(hms: str, day_offset: str) -> str:
    if not hms:
        return ""
    h, m, s = (hms.split(":") + ["0", "0"])[:3]
    return f"{int(h) + 24 * int(day_offset or 0):02d}:{int(m):02d}:{int(float(s)):02d}"


def uic7(code: str) -> str:
    """Code Trenitalia à 9 chiffres (pays + 00 + gare) -> code UIC à 7 chiffres : 830000070 -> 8300070."""
    return code[:2] + code[-5:] if len(code) == 9 and code.isdigit() else ""


def clean_name(name: str) -> str:
    return name.replace("`", "'").strip()


class GtfsBuilder:
    def __init__(self, netex: NetexReader):
        self.n = netex
        self.stats = Counter()
        self.stops = {}          # stop_id GTFS -> ligne stops.txt
        self.ssp_stop = {}       # ScheduledStopPoint id -> stop_id GTFS
        self.services = {}       # frozenset de dates -> service_id
        self.trips, self.stop_times = [], []
        self.used_routes = set()
        self.by_category = Counter()

    def stop_id_for(self, ssp: str):
        if ssp in self.ssp_stop:
            return self.ssp_stop[ssp]
        place = self.n.stop_places.get(self.n.ssp_to_place.get(ssp, ""))
        if place is None:
            place = self.n.ssp_fallback.get(ssp)
            self.stats["stops_without_stop_place"] += 1
        if place is None:
            self.ssp_stop[ssp] = None
            return None
        stop_id = place["code"] or ssp.rpartition(":")[2]
        if stop_id not in self.stops:
            self.stops[stop_id] = {
                "stop_id": stop_id, "stop_code": uic7(place["code"]), "stop_name": clean_name(place["name"]),
                "stop_lat": f"{place['lat']:.6f}" if place["lat"] is not None else "",
                "stop_lon": f"{place['lon']:.6f}" if place["lon"] is not None else "",
            }
        self.ssp_stop[ssp] = stop_id
        return stop_id

    def service_for(self, day_type: str):
        period = self.n.periods.get(self.n.day_type_period.get(day_type, ""))
        if period is None:
            return None
        start, bits = period
        days = frozenset((start + timedelta(days=i)).strftime("%Y%m%d") for i, b in enumerate(bits) if b == "1")
        if not days:
            return None
        if days not in self.services:
            self.services[days] = f"S{len(self.services) + 1}"
        return self.services[days]

    def build(self):
        seen_trip_ids = set()
        for sj_id, name, pattern_ref, day_type, passing in self.n.journeys:
            pattern = self.n.patterns.get(pattern_ref)
            if pattern is None:
                self.stats["journeys_unknown_pattern"] += 1
                continue
            line_ref, points = pattern
            line = self.n.lines.get(line_ref)
            if line is None:
                self.stats["journeys_unknown_line"] += 1
                continue
            service_id = self.service_for(day_type)
            if service_id is None:
                self.stats["journeys_without_days"] += 1
                continue

            rows = []
            for point_ref, arr, arr_off, dep, dep_off in passing:
                point = points.get(point_ref)
                if point is None:
                    continue
                stop_id = self.stop_id_for(point[0])
                if stop_id is None:
                    continue
                arr_t, dep_t = gtfs_time(arr or dep, arr_off if arr else dep_off), gtfs_time(dep or arr, dep_off if dep else arr_off)
                rows.append([stop_id, arr_t, dep_t, point[1], point[2]])
            if len(rows) < 2:
                self.stats["journeys_too_few_stops"] += 1
                continue

            trip_id = sj_id.rpartition(":")[2]
            if trip_id in seen_trip_ids:
                trip_id = sj_id
            seen_trip_ids.add(trip_id)

            self.used_routes.add(line_ref)
            self.by_category[line["name"]] += 1
            headsign = self.stops[rows[-1][0]]["stop_name"]
            self.trips.append([line["code"], service_id, trip_id, name, headsign])
            for seq, (stop_id, arr_t, dep_t, boarding, alighting) in enumerate(rows, 1):
                pickup = "0" if boarding and seq < len(rows) else "1"
                drop_off = "0" if alighting and seq > 1 else "1"
                self.stop_times.append([trip_id, arr_t, dep_t, stop_id, seq, pickup, drop_off])

        self.stats.update({
            "stop_places": len(self.n.stop_places),
            "stops": len(self.stops),
            "stops_with_uic": sum(1 for s in self.stops.values() if s["stop_code"]),
            "routes": len(self.used_routes),
            "journeys": len(self.n.journeys),
            "trips": len(self.trips),
            "stop_times": len(self.stop_times),
            "services": len(self.services),
            "calendar_dates": sum(len(d) for d in self.services),
        })

    def write(self, out_path: str, agency_id: str, agency_name: str, agency_url: str):
        def csv_bytes(header, rows):
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
            return buf.getvalue().encode("utf-8")

        all_dates = sorted(d for days in self.services for d in days)
        tmp = out_path + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("agency.txt", csv_bytes(
                ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
                [[agency_id, agency_name, agency_url, TIMEZONE, "it"]]))
            z.writestr("stops.txt", csv_bytes(
                ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type"],
                [[s["stop_id"], s["stop_code"], s["stop_name"], s["stop_lat"], s["stop_lon"], "0"]
                 for s in sorted(self.stops.values(), key=lambda s: s["stop_id"])]))
            z.writestr("routes.txt", csv_bytes(
                ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
                [[l["code"], agency_id, l["short"], l["name"], GTFS_BUS if l["mode"] == "bus" else GTFS_RAIL]
                 for ref_id, l in sorted(self.n.lines.items()) if ref_id in self.used_routes]))
            z.writestr("trips.txt", csv_bytes(
                ["route_id", "service_id", "trip_id", "trip_short_name", "trip_headsign"], self.trips))
            z.writestr("stop_times.txt", csv_bytes(
                ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"],
                self.stop_times))
            z.writestr("calendar_dates.txt", csv_bytes(
                ["service_id", "date", "exception_type"],
                [[sid, d, "1"] for days, sid in self.services.items() for d in sorted(days)]))
            z.writestr("feed_info.txt", csv_bytes(
                ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version"],
                [[f"TrainNomad (NeTEx {agency_name} via CCISS)", "https://www.cciss.it", "it",
                  all_dates[0] if all_dates else "", all_dates[-1] if all_dates else "", self.n.publication]]))
        os.replace(tmp, out_path)


def process_source(agency_id: str, source: dict, input_file: str = None):
    """Télécharge et convertit un flux NeTEx en GTFS."""
    logging.info(f"\n{'='*60}\n🚄 Traitement de {source['name']}\n{'='*60}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        src = input_file
        if not src:
            src = os.path.join(tmp_dir, f"{agency_id.lower()}_netex.xml.gz")
            download_netex(src, source["url"], source["name"])

        netex = NetexReader()
        with open_xml(src) as stream:
            netex.parse(stream)

    builder = GtfsBuilder(netex)
    builder.build()

    if not builder.trips:
        logging.error(f"❌ Aucun train extrait du NeTEx {source['name']}, GTFS non écrit")
        return None

    out_path = source["out_zip"]
    builder.write(out_path, agency_id, source["name"], source["website"])

    report = {
        "agency": agency_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source["url"],
        "publication": netex.publication,
        "valid_between": list(netex.valid_between),
        "zip_mb": round(os.path.getsize(out_path) / 1e6, 2),
        "stats": dict(builder.stats),
        "trips_by_category": dict(builder.by_category.most_common()),
    }

    logging.info(f"📊 {json.dumps(report['stats'], ensure_ascii=False)}")
    logging.info(f"✅ GTFS écrit : {out_path} ({report['zip_mb']} Mo)")

    return report


def main():
    parser = argparse.ArgumentParser(description="Convertit les flux NeTEx italiens (Trenitalia, Italo) en GTFS")
    parser.add_argument("--input", help="fichier NeTEx déjà téléchargé (pour un seul opérateur)")
    parser.add_argument("--operator", choices=["TRENITALIA", "ITALO", "all"], default="all",
                        help="Opérateur à traiter (défaut: all)")
    args = parser.parse_args()

    reports = {}
    operators_to_process = [args.operator] if args.operator != "all" else list(NETEX_SOURCES.keys())

    for op_id in operators_to_process:
        source = NETEX_SOURCES[op_id]
        input_file = args.input if len(operators_to_process) == 1 else None
        report = process_source(op_id, source, input_file)
        if report:
            reports[op_id] = report

    # Rapport combiné
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(reports, f, indent=2, ensure_ascii=False)

    logging.info(f"\n✅ Traitement terminé : {len(reports)} opérateur(s)")


if __name__ == "__main__":
    main()
