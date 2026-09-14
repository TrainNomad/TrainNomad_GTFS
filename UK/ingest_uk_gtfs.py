"""
Télécharge le flux d'horaires National Rail (CIF, fichiers RJTTF) et le convertit en GTFS.

Sortie : UK/uk_gtfs.zip, lu par build_network.py (opérateur NATIONAL_RAIL de operators.json)
et fusionné avec les autres réseaux (SNCF, Eurostar, Renfe...).

Pipeline :
  1. Authentification NRDP (variables d'environnement NR_EMAIL / NR_PASSWORD), puis
     téléchargement du zip RJTTF (.MCA horaires, .ZTR services manuels, .MSN gares)
  2. Gares : codes CRS du .MSN et des enregistrements TI ; coordonnées de stations.csv
     (colonne atoc_id = code CRS), à défaut celles du .MSN (grille OSGB36 -> WGS84)
  3. Horaires : BS/BX/LO/LI/LT, arrêts voyageurs uniquement (codes d'activité), heures publiques
  4. Surcharges STP par train, jour par jour : C (annulation) < N < O < P, la plus petite lettre gagne
  5. Dédoublonnage des trajets identiques, calendriers compacts (calendar + calendar_dates)
  6. Écriture du GTFS + rapport UK/uk_gtfs_report.json

Usage :
  python UK/ingest_uk_gtfs.py                        # télécharge avec NR_EMAIL / NR_PASSWORD
  python UK/ingest_uk_gtfs.py --input RJTTF123.zip   # convertit un fichier déjà téléchargé
"""
import argparse
import csv
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
STATIONS_CSV = os.path.join(ROOT_DIR, "stations.csv")
OUT_ZIP = os.path.join(BASE_DIR, "uk_gtfs.zip")
REPORT_PATH = os.path.join(BASE_DIR, "uk_gtfs_report.json")

NRDP_AUTH_URL = "https://opendata.nationalrail.co.uk/authenticate"
NRDP_TIMETABLE_URL = "https://opendata.nationalrail.co.uk/api/staticfeeds/3.0/timetable"

TIMEZONE = "Europe/London"
DAYS_BEFORE = 2      # trains de nuit partis la veille
DAYS_AHEAD = 140     # build_network.py couvre 128 jours ; marge pour une mise à jour hebdomadaire
MAX_ZIP_MB = 95      # limite GitHub : 100 Mo par fichier

# Statut du train (BS) -> route_type GTFS. F, T, 2, 3 = fret.
PASSENGER_STATUS = {"P": 2, "1": 2, "B": 3, "5": 3, "S": 4, "4": 4}
# Catégories voyageurs ; les autres (EE/ES rames vides, P* colis, J*/H* fret, D* service) sont ignorées.
PASSENGER_CATEGORIES = {"OL", "OO", "OU", "OW", "XC", "XD", "XI", "XR", "XU", "XX", "XZ", "BR", "BS", "SS"}
BUS_CATEGORIES = {"BR", "BS"}
METRO_CATEGORIES = {"OL"}
# Eurostar a son propre GTFS (opérateur EUROSTAR) : on évite les doublons.
EXCLUDED_ATOC = {"ES"}

PICKUP_CODES = {"T", "TB", "U", "R"}
DROPOFF_CODES = {"T", "TF", "D", "R"}
STP_PRIORITY = "CNOP"  # ordre alphabétique = priorité (C annule, N/O remplacent P)

ATOC_OPERATORS = {
    "AW": ("Transport for Wales", "https://tfw.wales"),
    "CC": ("c2c", "https://www.c2c-online.co.uk"),
    "CH": ("Chiltern Railways", "https://www.chilternrailways.co.uk"),
    "CS": ("Caledonian Sleeper", "https://www.sleeper.scot"),
    "EM": ("East Midlands Railway", "https://www.eastmidlandsrailway.co.uk"),
    "GC": ("Grand Central", "https://www.grandcentralrail.com"),
    "GN": ("Great Northern", "https://www.greatnorthernrail.com"),
    "GR": ("LNER", "https://www.lner.co.uk"),
    "GW": ("Great Western Railway", "https://www.gwr.com"),
    "GX": ("Gatwick Express", "https://www.gatwickexpress.com"),
    "HT": ("Hull Trains", "https://www.hulltrains.co.uk"),
    "HX": ("Heathrow Express", "https://www.heathrowexpress.com"),
    "IL": ("Island Line", "https://www.southwesternrailway.com"),
    "LD": ("Lumo", "https://www.lumo.co.uk"),
    "LE": ("Greater Anglia", "https://www.greateranglia.co.uk"),
    "LM": ("West Midlands Trains", "https://www.westmidlandsrailway.co.uk"),
    "LO": ("London Overground", "https://tfl.gov.uk"),
    "LT": ("London Underground", "https://tfl.gov.uk"),
    "ME": ("Merseyrail", "https://www.merseyrail.org"),
    "NT": ("Northern", "https://www.northernrailway.co.uk"),
    "NY": ("North Yorkshire Moors Railway", "https://www.nymr.co.uk"),
    "SE": ("Southeastern", "https://www.southeasternrailway.co.uk"),
    "SJ": ("Sheffield Supertram", "https://www.supertram.com"),
    "SN": ("Southern", "https://www.southernrailway.com"),
    "SR": ("ScotRail", "https://www.scotrail.co.uk"),
    "SW": ("South Western Railway", "https://www.southwesternrailway.com"),
    "SX": ("Stansted Express", "https://www.stanstedexpress.com"),
    "TL": ("Thameslink", "https://www.thameslinkrailway.com"),
    "TP": ("TransPennine Express", "https://www.tpexpress.co.uk"),
    "TW": ("Tyne and Wear Metro", "https://www.nexus.org.uk"),
    "VT": ("Avanti West Coast", "https://www.avantiwestcoast.co.uk"),
    "WR": ("West Coast Railways", "https://westcoastrailways.co.uk"),
    "XC": ("CrossCountry", "https://www.crosscountrytrains.co.uk"),
    "XR": ("Elizabeth line", "https://tfl.gov.uk"),
}
DEFAULT_AGENCY_URL = "https://www.nationalrail.co.uk"


# ---------------------------------------------------------------------------
# Téléchargement
# ---------------------------------------------------------------------------

def download_timetable(dest: str):
    email = os.environ.get("NR_EMAIL", "").strip()
    password = os.environ.get("NR_PASSWORD", "")
    if not email or not password:
        sys.exit("❌ Variables NR_EMAIL / NR_PASSWORD absentes (secrets GitHub du dépôt).")

    logging.info("🔐 Authentification sur le National Rail Data Portal")
    r = requests.post(NRDP_AUTH_URL, data={"username": email, "password": password},
                      headers={"Accept": "application/json"}, timeout=60)
    if r.status_code in (401, 403):
        sys.exit(f"❌ Authentification refusée (HTTP {r.status_code}). Vérifiez NR_EMAIL / NR_PASSWORD. "
                 "Le portail NRDP a été remplacé par le Rail Data Marketplace (raildata.org.uk) : "
                 "un ancien compte peut ne plus être accepté.")
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        sys.exit("❌ Réponse d'authentification sans jeton.")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{token}")  # le jeton n'apparaîtra jamais dans les logs

    logging.info("📥 Téléchargement du flux d'horaires (RJTTF)")
    with requests.get(NRDP_TIMETABLE_URL, headers={"X-Auth-Token": token}, stream=True, timeout=900) as r:
        if r.status_code in (401, 403):
            sys.exit(f"❌ Accès au flux refusé (HTTP {r.status_code}) : l'abonnement au flux "
                     "« Timetable » est-il actif sur ce compte ?")
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f, 1 << 20)
    if not zipfile.is_zipfile(dest):
        sys.exit("❌ Le fichier reçu n'est pas un zip.")
    logging.info(f"   {os.path.getsize(dest) / 1e6:.1f} Mo")


def open_members(path: str):
    """Renvoie (zip, {extension: nom}) en descendant dans un éventuel zip imbriqué."""
    z = zipfile.ZipFile(path)
    names = {os.path.splitext(n)[1].upper(): n for n in z.namelist()}
    if ".MCA" not in names and ".ZIP" in names:
        inner = io.BytesIO(z.read(names[".ZIP"]))
        z = zipfile.ZipFile(inner)
        names = {os.path.splitext(n)[1].upper(): n for n in z.namelist()}
    if ".MCA" not in names or ".MSN" not in names:
        sys.exit(f"❌ Fichiers .MCA / .MSN introuvables dans le zip : {sorted(z.namelist())}")
    return z, names


def read_lines(z: zipfile.ZipFile, name: str):
    with z.open(name) as raw:
        for line in io.TextIOWrapper(raw, encoding="latin-1"):
            yield line.rstrip("\r\n").ljust(80)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def osgb36_to_wgs84(east: float, north: float):
    """Grille nationale britannique (EPSG:27700) -> WGS84, précision ~5 m (Helmert)."""
    a, b, f0 = 6377563.396, 6356256.909, 0.9996012717
    lat0, lon0, n0, e0 = math.radians(49), math.radians(-2), -100000, 400000
    e2 = 1 - (b * b) / (a * a)
    n = (a - b) / (a + b)

    lat, m = lat0, 0.0
    while True:
        lat = (north - n0 - m) / (a * f0) + lat
        dl, sl = lat - lat0, lat + lat0
        m = b * f0 * ((1 + n + 1.25 * n**2 + 1.25 * n**3) * dl
                      - (3 * n + 3 * n**2 + 21 / 8 * n**3) * math.sin(dl) * math.cos(sl)
                      + (15 / 8 * n**2 + 15 / 8 * n**3) * math.sin(2 * dl) * math.cos(2 * sl)
                      - 35 / 24 * n**3 * math.sin(3 * dl) * math.cos(3 * sl))
        if abs(north - n0 - m) < 1e-5:
            break

    s, c, t = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * f0 / math.sqrt(1 - e2 * s * s)
    rho = a * f0 * (1 - e2) / (1 - e2 * s * s) ** 1.5
    eta2 = nu / rho - 1
    t2, t4, t6 = t * t, t**4, t**6
    de = east - e0
    lat = (lat - t / (2 * rho * nu) * de**2
           + t / (24 * rho * nu**3) * (5 + 3 * t2 + eta2 - 9 * t2 * eta2) * de**4
           - t / (720 * rho * nu**5) * (61 + 90 * t2 + 45 * t4) * de**6)
    lon = (lon0 + de / (c * nu)
           - (nu / rho + 2 * t2) / (6 * c * nu**3) * de**3
           + (5 + 28 * t2 + 24 * t4) / (120 * c * nu**5) * de**5
           - (61 + 662 * t2 + 1320 * t4 + 720 * t6) / (5040 * c * nu**7) * de**7)

    # Airy 1830 -> cartésien -> Helmert -> GRS80/WGS84
    s = math.sin(lat)
    nu_c = a / math.sqrt(1 - e2 * s * s)
    x = nu_c * math.cos(lat) * math.cos(lon)
    y = nu_c * math.cos(lat) * math.sin(lon)
    z = (1 - e2) * nu_c * s
    tx, ty, tz, k = 446.448, -125.157, 542.060, -20.4894e-6
    rx, ry, rz = (math.radians(v / 3600) for v in (0.1502, 0.2470, 0.8421))
    x, y, z = (tx + (1 + k) * x - rz * y + ry * z,
               ty + rz * x + (1 + k) * y - rx * z,
               tz - ry * x + rx * y + (1 + k) * z)
    a2, b2 = 6378137.0, 6356752.3142
    e2w = 1 - (b2 * b2) / (a2 * a2)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2w))
    for _ in range(10):
        lat = math.atan2(z + e2w * a2 / math.sqrt(1 - e2w * math.sin(lat) ** 2) * math.sin(lat), p)
    return math.degrees(lat), math.degrees(math.atan2(y, x))


def parse_yymmdd(s: str):
    if s == "999999":
        return date.max
    try:
        return date(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]))
    except ValueError:
        return None


def hhmm(s: str):
    """'0930' / '0930H' (demi-minute ignorée) -> minutes ; None si vide."""
    s = s[:4]
    return int(s[:2]) * 60 + int(s[2:]) if s.isdigit() else None


def pick_time(public: str, scheduled: str):
    """Heure publique, sinon heure de service. '0000' public vaut « non renseigné » sauf vers minuit."""
    p, w = hhmm(public), hhmm(scheduled)
    if p is None or (p == 0 and w is not None and 60 <= w < 23 * 60):
        return w
    return p


def activities(field: str):
    return {field[i:i + 2].strip() for i in range(0, len(field), 2)} - {""}


def boarding(codes, allowed, default_open):
    """Type GTFS de montée / descente : 0 normal, 1 interdit, 3 sur demande."""
    if "N" in codes:  # arrêt non annoncé au public
        return 1
    if codes & allowed:
        return 3 if "R" in codes else 0
    return 0 if default_open and not codes else 1


def gtfs_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

class Schedule:
    __slots__ = ("uid", "stp", "duration", "mask", "atoc", "route_type", "identity", "stops")

    def __init__(self, uid, stp, duration, mask, route_type, identity):
        self.uid, self.stp, self.duration, self.mask = uid, stp, duration, mask
        self.route_type, self.identity = route_type, identity
        self.atoc = ""
        self.stops = [] if route_type is not None else None


class CifConverter:
    def __init__(self, days_ahead: int):
        today = datetime.now(timezone.utc).date()
        self.win_start = today - timedelta(days=DAYS_BEFORE)
        self.dates = [self.win_start + timedelta(days=i) for i in range(DAYS_BEFORE + days_ahead)]
        self.weekday_masks = [0] * 7
        for i, d in enumerate(self.dates):
            self.weekday_masks[d.weekday()] |= 1 << i

        self.tiploc_crs = {}       # TIPLOC -> CRS
        self.stations = {}         # CRS -> {name, lat, lon, change}
        self.schedules = defaultdict(list)
        self.stats = Counter()
        self.unknown_tiplocs = Counter()
        self.header = {}

    # -- calendrier ------------------------------------------------------------

    def days_mask(self, start, end, days_run):
        a = max((start - self.win_start).days, 0)
        b = min((end - self.win_start).days, len(self.dates) - 1) if end != date.max else len(self.dates) - 1
        if a > b:
            return 0
        weekdays = 0
        for k, ch in enumerate(days_run[:7]):
            if ch == "1":
                weekdays |= self.weekday_masks[k]
        return (((1 << (b + 1)) - 1) ^ ((1 << a) - 1)) & weekdays

    # -- gares -------------------------------------------------------------------

    def load_msn(self, z, name):
        for line in read_lines(z, name):
            if line[0] != "A" or "FILE-SPEC" in line:
                continue
            tiploc, crs = line[36:43].strip(), line[49:52].strip()
            if not re.fullmatch(r"[A-Z0-9]{3}", crs):
                continue
            self.tiploc_crs[tiploc] = crs
            st = self.stations.setdefault(crs, {"name": line[5:35].strip(), "lat": None, "lon": None, "change": 0})
            east, north = line[52:57], line[58:63]
            if st["lat"] is None and east.isdigit() and north.isdigit() and int(east) and int(north) not in (0, 69999):
                st["lat"], st["lon"] = osgb36_to_wgs84((int(east) - 10000) * 100, (int(north) - 60000) * 100)
            if line[63:65].strip().isdigit():
                st["change"] = max(st["change"], int(line[63:65]))
        logging.info(f"   {len(self.stations)} gares (CRS), {len(self.tiploc_crs)} TIPLOC")

    def apply_reference(self, path):
        """Nom et coordonnées de stations.csv (plus précis que le .MSN) via atoc_id = CRS."""
        if not os.path.exists(path):
            logging.warning(f"⚠️ {path} introuvable : coordonnées du .MSN uniquement")
            return
        matched = 0
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                crs = row["atoc_id"]
                st = self.stations.get(crs)
                if not st or st.get("ref") or not row["latitude"] or not row["longitude"]:
                    continue
                st.update(name=row["name"], lat=float(row["latitude"]), lon=float(row["longitude"]), ref=True)
                matched += 1
        self.stats["stations_matched_ref"] = matched
        logging.info(f"   {matched} gares enrichies par stations.csv")

    # -- horaires ----------------------------------------------------------------

    def load_cif(self, z, name):
        cur = None
        for line in read_lines(z, name):
            rt = line[:2]
            if rt == "BS":
                self.finish(cur)
                cur = self.start_schedule(line)
            elif cur is None:
                if rt == "TI":
                    crs = line[53:56].strip()
                    if crs:
                        self.tiploc_crs.setdefault(line[2:9].strip(), crs)
                elif rt == "HD" and not self.header:
                    self.header = {"file": line[32:39].strip(), "extract": line[22:28], "update": line[46]}
            elif rt == "BX":
                cur.atoc = line[11:13].strip()
            elif cur.stops is None:
                continue
            elif rt == "LO":
                t = pick_time(line[15:19], line[10:15])
                codes = activities(line[29:41])
                cur.stops.append((line[2:9].strip(), t, t, boarding(codes, PICKUP_CODES, True), 1))
            elif rt == "LI":
                if line[20:25].strip():  # point de passage sans arrêt
                    continue
                codes = activities(line[42:54])
                pickup = boarding(codes, PICKUP_CODES, False)
                drop = boarding(codes, DROPOFF_CODES, False)
                if pickup == 1 and drop == 1:
                    continue
                arr, dep = pick_time(line[25:29], line[10:15]), pick_time(line[29:33], line[15:20])
                cur.stops.append((line[2:9].strip(), arr if arr is not None else dep,
                                  dep if dep is not None else arr, pickup, drop))
            elif rt == "LT":
                t = pick_time(line[15:19], line[10:15])
                codes = activities(line[25:37])
                cur.stops.append((line[2:9].strip(), t, t, 1, boarding(codes, DROPOFF_CODES, True)))
                self.finish(cur)
                cur = None
        self.finish(cur)

    def start_schedule(self, line):
        self.stats["cif_schedules"] += 1
        if line[2] == "D":
            return None
        start, end = parse_yymmdd(line[9:15]), parse_yymmdd(line[15:21])
        if start is None or end is None:
            self.stats["schedules_bad_dates"] += 1
            return None
        mask = self.days_mask(start, end, line[21:28])
        if not mask:
            return None
        stp = line[79] if line[79] in STP_PRIORITY else "P"
        status, category = line[29], line[30:32].strip()
        route_type = PASSENGER_STATUS.get(status)
        if stp == "C" or (category and category not in PASSENGER_CATEGORIES):
            route_type = None
        elif route_type == 2 and category in BUS_CATEGORIES:
            route_type = 3
        elif route_type == 2 and category in METRO_CATEGORIES:
            route_type = 1
        duration = (min(end, self.dates[-1]) - start).days
        return Schedule(line[3:9], stp, duration, mask, route_type, line[32:36].strip())

    def finish(self, s):
        if s is None:
            return
        if s.stops is not None:
            s.stops = self.clean_stops(s.stops)
        self.schedules[s.uid].append(s)  # même non voyageur : une surcharge STP masque la version P

    def clean_stops(self, raw):
        """TIPLOC -> CRS, fusion des TIPLOC consécutifs d'une même gare, heures croissantes (> 24 h)."""
        out = []
        offset, prev = 0, None
        for tiploc, arr, dep, pickup, drop in raw:
            if arr is None:
                continue
            arr += offset
            if prev is not None and arr < prev:
                offset += 1440
                arr += 1440
            dep += offset
            if dep < arr:
                offset += 1440
                dep += 1440
            prev = dep
            crs = self.tiploc_crs.get(tiploc)
            st = self.stations.get(crs)
            if st is None or st["lat"] is None:
                self.unknown_tiplocs[tiploc] += 1
                continue
            if out and out[-1][0] == crs:
                c, a, _, p, d = out[-1]
                out[-1] = (c, a, dep, min(p, pickup), min(d, drop))
            else:
                out.append((crs, arr, dep, pickup, drop))
        return out

    def resolve(self):
        """Surcharges STP puis dédoublonnage : clé = contenu du trajet, valeur = jours de circulation."""
        trips = {}
        for uid, scheds in self.schedules.items():
            scheds.sort(key=lambda s: (STP_PRIORITY.index(s.stp), s.duration))
            claimed = 0
            for s in scheds:
                days = s.mask & ~claimed
                claimed |= s.mask
                if not days or not s.stops:
                    continue
                if s.atoc in EXCLUDED_ATOC:
                    self.stats["trips_excluded_operator"] += 1
                    continue
                if len(s.stops) < 2:
                    self.stats["trips_too_few_stops"] += 1
                    continue
                stops = list(s.stops)
                stops[0] = stops[0][:4] + (1,)
                stops[-1] = stops[-1][:3] + (1, stops[-1][4])
                key = (s.atoc or "ZZ", s.route_type, s.identity, tuple(stops))
                if key in trips:
                    trips[key][0] |= days
                    self.stats["trips_merged"] += 1
                else:
                    trips[key] = [days, uid]
        self.stats["trips"] = len(trips)
        return trips

    # -- écriture ----------------------------------------------------------------

    def service_rows(self, mask):
        """Calendrier compact : jours de semaine majoritaires + exceptions."""
        days = [i for i in range(len(self.dates)) if mask >> i & 1]
        first, last = days[0], days[-1]
        active, total = [0] * 7, [0] * 7
        for i in range(first, last + 1):
            wd = self.dates[i].weekday()
            total[wd] += 1
            active[wd] += mask >> i & 1
        pattern = [2 * active[w] > total[w] for w in range(7)]
        exceptions = []
        for i in range(first, last + 1):
            on, usual = bool(mask >> i & 1), pattern[self.dates[i].weekday()]
            if on != usual:
                exceptions.append((self.dates[i].strftime("%Y%m%d"), "1" if on else "2"))
        span = (self.dates[first].strftime("%Y%m%d"), self.dates[last].strftime("%Y%m%d"))
        return (pattern if any(pattern) else None), span, exceptions

    def write(self, trips, out_path):
        services, routes, agencies = {}, {}, {}
        used_stops = set()
        tmp = out_path + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            def writer(name, header):
                fh = io.TextIOWrapper(zf.open(name, "w"), encoding="utf-8", newline="")
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(header)
                return fh, w

            # un seul fichier ouvert à la fois dans le zip : stop_times en flux, trips gardés en mémoire
            trip_rows = []
            fh_st, w_st = writer("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"])
            for n, ((atoc, route_type, identity, stops), (mask, uid)) in enumerate(
                    sorted(trips.items(), key=lambda kv: (kv[0][0], kv[0][3][0][1], kv[1][1]))):
                service_id = services.setdefault(mask, f"S{len(services) + 1}")
                origin, dest = self.stations[stops[0][0]]["name"], self.stations[stops[-1][0]]["name"]
                route_key = (atoc, route_type, origin, dest)
                if route_key not in routes:
                    routes[route_key] = f"{atoc}-{len(routes) + 1}"
                agencies[atoc] = True
                trip_id = f"{uid}-{n + 1}"
                trip_rows.append([routes[route_key], service_id, trip_id, identity or uid, dest])
                for seq, (crs, arr, dep, pickup, drop) in enumerate(stops, 1):
                    used_stops.add(crs)
                    w_st.writerow([trip_id, gtfs_time(arr), gtfs_time(dep), crs, seq, pickup, drop])
            fh_st.close()

            fh, w = writer("trips.txt", ["route_id", "service_id", "trip_id", "trip_short_name", "trip_headsign"])
            w.writerows(trip_rows)
            fh.close()

            fh, w = writer("agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"])
            for atoc in sorted(agencies):
                name, url = ATOC_OPERATORS.get(atoc, (f"National Rail ({atoc})", DEFAULT_AGENCY_URL))
                if atoc not in ATOC_OPERATORS:
                    self.stats[f"unknown_atoc_{atoc}"] += 1
                w.writerow([atoc, name, url, TIMEZONE, "en"])
            fh.close()

            fh, w = writer("routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"])
            for (atoc, route_type, origin, dest), route_id in routes.items():
                w.writerow([route_id, atoc, "", f"{origin} – {dest}", route_type])
            fh.close()

            fh, w = writer("stops.txt", ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type", "stop_timezone"])
            for crs in sorted(used_stops):
                st = self.stations[crs]
                name = st["name"].title() if st["name"].isupper() else st["name"]
                w.writerow([crs, crs, name, f"{st['lat']:.6f}", f"{st['lon']:.6f}", 0, TIMEZONE])
            fh.close()

            calendar_rows, date_rows = [], []
            for mask, service_id in services.items():
                pattern, span, exceptions = self.service_rows(mask)
                if pattern:
                    calendar_rows.append([service_id, *("1" if p else "0" for p in pattern), *span])
                date_rows.extend([service_id, d, ex] for d, ex in exceptions)
            fh, w = writer("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"])
            w.writerows(calendar_rows)
            fh.close()
            fh, w = writer("calendar_dates.txt", ["service_id", "date", "exception_type"])
            w.writerows(date_rows)
            fh.close()

            fh, w = writer("transfers.txt", ["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time"])
            for crs in sorted(used_stops):
                if self.stations[crs]["change"]:
                    w.writerow([crs, crs, 2, self.stations[crs]["change"] * 60])
            fh.close()

            fh, w = writer("feed_info.txt", ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version"])
            w.writerow(["TrainNomad (données National Rail Enquiries)", DEFAULT_AGENCY_URL, "en",
                        self.dates[0].strftime("%Y%m%d"), self.dates[-1].strftime("%Y%m%d"),
                        self.header.get("file", "") or datetime.now(timezone.utc).strftime("%Y%m%d")])
            fh.close()
        os.replace(tmp, out_path)

        self.stats.update({"stops": len(used_stops), "routes": len(routes), "services": len(services), "agencies": len(agencies)})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", help="zip RJTTF déjà téléchargé (sinon téléchargement avec NR_EMAIL / NR_PASSWORD)")
    parser.add_argument("--output", default=OUT_ZIP)
    parser.add_argument("--days", type=int, default=int(os.environ.get("UK_GTFS_DAYS", DAYS_AHEAD)))
    parser.add_argument("--no-ztr", action="store_true", help="ignore les services manuels (.ZTR : bus, ferries)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        source = args.input
        if not source:
            source = os.path.join(tmpdir, "timetable.zip")
            download_timetable(source)

        conv = CifConverter(args.days)
        logging.info(f"📅 Fenêtre : {conv.dates[0]} → {conv.dates[-1]}")
        z, names = open_members(source)

        logging.info(f"🚉 Gares : {names['.MSN']}")
        conv.load_msn(z, names[".MSN"])
        conv.apply_reference(STATIONS_CSV)

        logging.info(f"🚆 Horaires : {names['.MCA']}")
        conv.load_cif(z, names[".MCA"])
        if conv.header.get("update") == "U":
            logging.warning("⚠️ Le .MCA est un fichier de mise à jour (U) et non un extrait complet (F)")
        if ".ZTR" in names and not args.no_ztr:
            logging.info(f"🚌 Services manuels : {names['.ZTR']}")
            conv.load_cif(z, names[".ZTR"])

        trips = conv.resolve()
        logging.info(f"💾 Écriture de {args.output}")
        conv.write(trips, args.output)

    size_mb = os.path.getsize(args.output) / 1e6
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": [conv.dates[0].isoformat(), conv.dates[-1].isoformat()],
        "cif_header": conv.header,
        "zip_mb": round(size_mb, 2),
        "stats": dict(conv.stats),
        "unmatched_tiplocs_top": conv.unknown_tiplocs.most_common(30),
    }
    report_path = REPORT_PATH if os.path.abspath(args.output) == OUT_ZIP else os.path.splitext(args.output)[0] + "_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logging.info(f"📊 {json.dumps(dict(conv.stats), ensure_ascii=False)}")
    logging.info(f"✅ {args.output} : {size_mb:.1f} Mo")
    if size_mb > MAX_ZIP_MB:
        sys.exit(f"❌ {size_mb:.0f} Mo dépasse la limite GitHub ({MAX_ZIP_MB} Mo) : réduire --days.")


if __name__ == "__main__":
    main()
