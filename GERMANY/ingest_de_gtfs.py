"""
Télécharge et prépare le GTFS grandes lignes allemand (gtfs.de, données DELFI) :
ICE, IC, EC, ECE, EN, RJ de la DB et des opérateurs voisins.

Source : https://download.gtfs.de/germany/fv_free/latest.zip (CC BY 4.0, DELFI e.V. / gtfs.de)

Particularités du flux :
  - stop_id = identifiants internes gtfs.de (ex. 256012 = Berlin Hbf, ses voies sont des arrêts
    enfants) : aucun UIC ni numéro EVA. Les gares sont donc rapprochées de stations.csv
  - pas de numéro de train (trips.txt n'a ni trip_short_name ni trip_headsign) ;
    route_short_name = catégorie + ligne ("ICE 82", "IC 55", "EC")
  - un train international est coupé en un trajet par opérateur (ex. ČD Břeclav -> Bohumín puis
    PKP Bohumín -> Varsovie) : ces morceaux sont recollés

Rapprochement des gares (colonne stop_code = id Trainline de stations.csv) :
  1. GERMANY/stations_overrides.csv (choix manuels, prioritaires)
  2. candidats = vraies gares ferroviaires de stations.csv (UIC à 7 chiffres ou numéro EVA 800xxxx /
     801xxxx) : les arrêts de bus et stations de taxi voisins sont exclus
  3. gare retenue = à moins de 500 m (1,5 km si le nom est quasi identique) ET de nom proche ;
     si deux gares différentes sont aussi plausibles, la correspondance est signalée "à vérifier"
  4. tout est écrit dans GERMANY/stations_report.csv (une ligne par gare du flux)
Le nom affiché est toujours celui de stations.csv (build_network.py reprend la gare Trainline).

Usage :
    python GERMANY/ingest_de_gtfs.py
"""
import difflib
import io
import logging
import os
import re
import shutil
import sys
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, GTFS_DIR)
from build_network import StationRef, STATIONS_CSV  # noqa: E402
from osm_lookup import OsmLookup, extra_station_row, write_extra  # noqa: E402

OUTPUT_ZIP = os.path.join(BASE_DIR, "de_gtfs.zip")
REPORT_CSV = os.path.join(BASE_DIR, "stations_report.csv")
REGIO_ZIP = os.path.join(BASE_DIR, "de_regio_gtfs.zip")
REGIO_REPORT_CSV = os.path.join(BASE_DIR, "stations_report_regio.csv")
OVERRIDES_CSV = os.path.join(BASE_DIR, "stations_overrides.csv")
DE_GTFS_URL = "https://download.gtfs.de/germany/fv_free/latest.zip"
DE_REGIO_URL = "https://download.gtfs.de/germany/rv_free/latest.zip"

# Deux flux gtfs.de, deux opérateurs dans build_network.py :
#   fv (Fernverkehr) -> "DB"        : ICE, IC, EC, EN, RJ
#   rv (Regionalverkehr) -> "DB_REGIO" : RE, RB, S-Bahn et compagnies régionales
FEEDS = {
    "fv": {"url": DE_GTFS_URL, "zip": OUTPUT_ZIP, "report": REPORT_CSV,
           "exclude_agencies": set(), "same_line_only": False},
    # FlixTrain vient de son propre GTFS (FLIXTRAIN/), plus complet ; dans le régional, les morceaux
    # ne sont recollés que sur la même ligne (S1 -> S1) : 139 compagnies se croisent dans les gares
    "rv": {"url": DE_REGIO_URL, "zip": REGIO_ZIP, "report": REGIO_REPORT_CSV,
           "exclude_agencies": {"Flixtrain"}, "same_line_only": True},
}

MAX_KM = 0.5           # distance maximale gare GTFS <-> gare stations.csv
MAX_KM_SAME_NAME = 1.5  # ... si les noms sont quasi identiques (coordonnées étrangères approximatives)
MIN_NAME = 0.6         # similarité de nom minimale
BARE_KM = 0.3            # gares de stations.csv sans numéro
PROXIMITY_KM = 0.25      # sans nom concordant : une seule gare à moins de 250 m...
PROXIMITY_ALONE_KM = 0.6  # ... et aucune autre à moins de 600 m
AMBIGUOUS_GAP = 0.1    # écart de score sous lequel deux gares différentes sont jugées ambiguës

JUNCTION_MAX_GAP = 5  # minutes entre l'arrivée d'un morceau et le départ du suivant au point de jonction

FILES_KEPT = ["agency.txt", "feed_info.txt", "routes.txt", "stops.txt", "trips.txt",
              "calendar.txt", "calendar_dates.txt", "stop_times.txt"]

# mots sans valeur pour comparer les noms ("Aalen, Hauptbahnhof" ~ "Aalen")
NOISE = {"bahnhof", "bf", "bhf", "hbf", "zob", "busbf", "bushaltestelle", "s", "u", "gr", "grenze", "st", "station",
         "gare", "fernbf", "fernbahnhof", "hl", "n", "glowny"}


# ---------------------------------------------------------------------------
# Lecture / écriture
# ---------------------------------------------------------------------------

def download(url: str, dest: str) -> None:
    logging.info(f"📥 Téléchargement {url}")
    with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f)
    zipfile.ZipFile(dest).testzip()
    logging.info(f"   ✅ {os.path.getsize(dest) / 1e6:.1f} Mo")


def read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def to_min(hms: str) -> int:
    h, m, *_ = hms.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------

def fold(s: str) -> str:
    s = (s or "").replace("ß", "ss").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = s.replace("hauptbahnhof", " hbf ").replace("koebenhavn", "kobenhavn")
    s = s.replace("central station", " hbf ").replace("(flixtrain)", " ")  # noms du GTFS FlixTrain
    return " ".join(re.sub(r"[^a-z0-9]", " ", s).split())


def core_tokens(s: str) -> set:
    return {t for t in fold(s).split() if t not in NOISE}


# arrêts annexes d'une gare (gare routière, S-Bahn séparée, taxis, navette...) : moins prioritaires
SECONDARY = {"bus", "busbahnhof", "busbf", "zob", "taxi", "taxistande", "kd", "tief", "autozug",
             "shuttle", "syltshuttle", "ort", "neg", "schillerplatz", "karlsplatz", "albtalbf"}


def name_score(gtfs_name: str, ref_name: str) -> float:
    a, b = fold(gtfs_name), fold(ref_name)
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = core_tokens(gtfs_name), core_tokens(ref_name)
    tok = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return max(seq, tok)


def osm_name_ok(gtfs_name: str, osm_name: str) -> bool:
    """Le nom OSM reprend le mot distinctif du nom GTFS (le dernier ou le plus long) :
    "Potsdam, Golm Bhf" ~ "Golm", mais "Kassel Lutherplatz" (arrêt de tram) != "Kassel Hauptbahnhof"."""
    g = [t for t in fold(gtfs_name).split() if t not in NOISE]
    o = set(fold(osm_name).split())
    if not g:
        return True
    return g[-1] in o or max(g, key=len) in o


def secondary_penalty(gtfs_name: str, ref_name: str) -> float:
    extra = (set(fold(ref_name).split()) - set(fold(gtfs_name).split())) & SECONDARY
    s_bahn = re.search(r"\(S\)|S-Bahn", ref_name)  # quais S-Bahn séparés : la gare principale d'abord
    return 0.15 if extra or s_bahn else 0.0


# ---------------------------------------------------------------------------
# Rapprochement des gares
# ---------------------------------------------------------------------------

class StationMatcher:
    def __init__(self, ref: StationRef, osm=None):
        """osm : OsmLookup facultatif, consulté pour les gares que stations.csv ne permet pas de reconnaître."""
        self.ref = ref
        self.osm = osm
        self.extra = {}  # gares ajoutées depuis OpenStreetMap (-> stations_extra.csv)
        df = pd.read_csv(STATIONS_CSV, sep=";", dtype=str, keep_default_na=False,
                         usecols=["id", "name", "uic", "db_id", "latitude", "longitude", "is_city", "country",
                                  "time_zone"])
        # gares ferroviaires : UIC ou numéro HAFAS/EVA à 7 chiffres ; les entrées "ville" sans UIC sont exclues
        rail = df["uic"].str.fullmatch(r"\d{7}") | df["db_id"].str.fullmatch(r"\d{7}")
        # (Trainline marque "ville" beaucoup de petites gares : seules les entrées ville DB 809xxxx sont exclues)
        city_only = (df["is_city"] == "t") & (df["uic"] == "") & ((df["db_id"] == "") | df["db_id"].str.startswith("809"))
        df = df[rail & ~city_only & (df["latitude"] != "") & (df["longitude"] != "")]
        self.has_uic = (df["uic"] != "").to_numpy()
        self.ids = df["id"].to_numpy()
        self.names = df["name"].to_numpy()
        self.lat = df["latitude"].astype(float).to_numpy()
        self.lon = df["longitude"].astype(float).to_numpy()
        self.country = df["country"].str[:2].str.upper().to_numpy()
        self.tz = df["time_zone"].to_numpy()
        # gares de stations.csv sans aucun numéro : seulement si nom quasi identique et tout près
        full = pd.read_csv(STATIONS_CSV, sep=";", dtype=str, keep_default_na=False,
                           usecols=["id", "name", "uic", "db_id", "latitude", "longitude", "is_city"])
        bare = full[(full["uic"] == "") & (full["db_id"] == "")
                    & (full["latitude"] != "") & (full["longitude"] != "")]
        self.bare_ids, self.bare_names = bare["id"].to_numpy(), bare["name"].to_numpy()
        self.bare_lat = bare["latitude"].astype(float).to_numpy()
        self.bare_lon = bare["longitude"].astype(float).to_numpy()
        self.overrides = {}
        if os.path.exists(OVERRIDES_CSV):
            ov = pd.read_csv(OVERRIDES_CSV, sep=";", dtype=str, keep_default_na=False, comment="#")
            for r in ov.itertuples():
                if r.trainline_id:
                    self.overrides.setdefault(r.gtfs_name, []).append(
                        (float(r.lat) if r.lat else None, float(r.lon) if r.lon else None, r.trainline_id))
        logging.info(f"   {len(self.ids)} gares ferroviaires candidates, {len(self.overrides)} choix manuels")

    def match(self, name: str, lat: float, lon: float) -> dict:
        m = self.match_local(name, lat, lon)
        if self.osm is None or m["status"] not in ("non trouvée", "à vérifier (nom différent)"):
            return m
        f = self.osm.find(name, lat, lon)
        if not f or not osm_name_ok(name, f["osm_name"]):
            return m
        # 1. le numéro UIC/EVA d'OSM retrouve la gare dans stations.csv (db_id, puis uic), à moins de 2 km
        rid = self.ref.by_db.get(f["uic_ref"]) or self.ref.by_uic.get(f["uic_ref"]) if f["uic_ref"] else None
        if rid:
            row = self.ref.rows[self.ref.canonical(rid)]
            if row["lat"] is None or np.hypot((row["lat"] - lat) * 111.2,
                                              (row["lon"] - lon) * 111.2 * np.cos(np.radians(lat))) > 2:
                rid = None  # numéro attribué à une autre gare dans stations.csv : on ne s'y fie pas
        if rid:
            rid = self.ref.canonical(rid)
            status = "ok (confirmé OSM)" if rid == m["trainline_id"] else "OSM (UIC)"
            return {"trainline_id": rid, "status": status, "score": None, "km": float(f["km"]),
                    "alternative": f"OSM {f['osm_name']} uic {f['uic_ref']}"}
        if m["status"] != "non trouvée":
            return m  # gare proche proposée, OSM sans UIC exploitable : on garde la proposition
        # 2. gare absente de stations.csv : ajoutée depuis OSM
        sid = f"OSM{f['osm_id']}"
        if sid not in self.ref.rows:
            row = extra_station_row(f)
            if not row["country"]:
                near = int(np.argmin(np.hypot(self.lat - lat, (self.lon - lon) * np.cos(np.radians(lat)))))
                row["country"], row["time_zone"] = self.country[near], self.tz[near]
            self.extra[sid] = row
            self.ref.rows[sid] = {"id": sid, "name": row["name"], "lat": float(row["latitude"]),
                                  "lon": float(row["longitude"]), "parent": "", "country": row["country"],
                                  "tz": row["time_zone"], "is_city": False, "same_as": "", "uic": row["uic"]}
        return {"trainline_id": sid, "status": "ajoutée (OSM)", "score": None, "km": float(f["km"]),
                "alternative": f"OSM {f['osm_name']} uic {f['uic_ref'] or '-'}"}

    def match_local(self, name: str, lat: float, lon: float) -> dict:
        rid = None
        for olat, olon, oid in self.overrides.get(name, ()):
            # lat/lon facultatifs : départagent deux gares GTFS de même nom (à moins de 2 km)
            if olat is None or abs(olat - lat) * 111.2 + abs(olon - lon) * 75 < 2:
                rid = oid
                break
        if rid:
            if rid not in self.ref.rows:
                raise ValueError(f"stations_overrides.csv : id Trainline inconnu {rid} ({name})")
            rid = self.ref.canonical(rid)
            return {"trainline_id": rid, "status": "manuel", "score": 1.0, "km": None, "alternative": ""}

        km = np.hypot((self.lat - lat) * 111.2, (self.lon - lon) * 111.2 * np.cos(np.radians(lat)))
        best_by_station = {}
        for i in np.flatnonzero(km <= MAX_KM_SAME_NAME):
            sim = name_score(name, self.names[i])
            if sim < MIN_NAME or (km[i] > MAX_KM and sim < 0.85):
                continue
            score = sim - km[i] * 0.2 - secondary_penalty(name, self.names[i]) + 0.02 * self.has_uic[i]
            rid = self.ref.canonical(self.ids[i])
            if rid not in best_by_station or score > best_by_station[rid][0]:
                best_by_station[rid] = (score, sim, km[i])
        if not best_by_station:
            # gare de stations.csv sans numéro (UIC/EVA) : nom quasi identique, à moins de BARE_KM
            kb = np.hypot((self.bare_lat - lat) * 111.2, (self.bare_lon - lon) * 111.2 * np.cos(np.radians(lat)))
            near = [(name_score(name, self.bare_names[i]), i) for i in np.flatnonzero(kb <= BARE_KM)]
            near = [(sc, i) for sc, i in near if sc >= 0.85]
            if near:
                sc, i = max(near)
                return {"trainline_id": self.ref.canonical(self.bare_ids[i]), "status": "ok (sans UIC)",
                        "score": round(sc, 2), "km": round(float(kb[i]), 3), "alternative": ""}
            # nom trop différent ("Ostbahnhof", "GD, Bahnhof") : une seule gare tout près -> proposée, à vérifier
            near = {self.ref.canonical(self.ids[i]) for i in np.flatnonzero(km <= PROXIMITY_KM)}
            around = {self.ref.canonical(self.ids[i]) for i in np.flatnonzero(km <= PROXIMITY_ALONE_KM)}
            if len(near) == 1 and around == near:
                rid = near.pop()
                return {"trainline_id": rid, "status": "à vérifier (nom différent)", "score": None,
                        "km": round(float(km[[self.ref.canonical(x) == rid for x in self.ids]].min()), 3),
                        "alternative": ""}
            return {"trainline_id": "", "status": "non trouvée", "score": None, "km": None, "alternative": ""}
        ranked = sorted(best_by_station.items(), key=lambda kv: -kv[1][0])
        rid, (score, sim, d) = ranked[0]
        status, alt = "ok", ""
        # deux entrées stations.csv de même nom au même endroit ("Brig" / "Brig Bahnhof") = même gare
        rivals = [(r, v) for r, v in ranked[1:]
                  if not (core_tokens(self.ref.rows[r]["name"]) == core_tokens(self.ref.rows[rid]["name"])
                          and abs(v[2] - d) < 0.3)]
        if rivals and score - rivals[0][1][0] < AMBIGUOUS_GAP:
            ranked = [ranked[0]] + rivals
            status = "à vérifier"
            alt = f"{ranked[1][0]} {self.ref.rows[ranked[1][0]]['name']}"
        return {"trainline_id": rid, "status": status, "score": round(sim, 2), "km": round(float(d), 3),
                "alternative": alt}


def map_stations(stops: pd.DataFrame, used: set, matcher: StationMatcher) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Ajoute stop_code = id Trainline (gare et voies) ; renvoie aussi le rapport."""
    stops = stops.copy()
    stops["station"] = np.where(stops["parent_station"] != "", stops["parent_station"], stops["stop_id"])
    by_id = stops.set_index("stop_id")
    stations = sorted(set(stops.loc[stops["stop_id"].isin(used), "station"]))
    report, code = [], {}
    for sid in stations:
        r = by_id.loc[sid]
        m = matcher.match(r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
        code[sid] = m["trainline_id"]
        row = matcher.ref.rows.get(m["trainline_id"], {})
        report.append({"gtfs_stop_id": sid, "gtfs_name": r["stop_name"], "status": m["status"],
                       "trainline_id": m["trainline_id"], "trainline_name": row.get("name", ""),
                       "uic": row.get("uic", ""), "country": row.get("country", ""),
                       "name_score": m["score"], "km": m["km"], "alternative": m["alternative"]})
    stops["stop_code"] = stops["station"].map(code).fillna("")
    return stops.drop(columns="station"), pd.DataFrame(report)


# ---------------------------------------------------------------------------
# Calendriers
# ---------------------------------------------------------------------------

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def service_dates(cal: pd.DataFrame, cd: pd.DataFrame) -> dict:
    days = defaultdict(set)
    for r in cal.itertuples(index=False):
        d = datetime.strptime(r.start_date, "%Y%m%d").date()
        end = datetime.strptime(r.end_date, "%Y%m%d").date()
        while d <= end:
            if getattr(r, WEEKDAYS[d.weekday()]) == "1":
                days[r.service_id].add(d)
            d += timedelta(days=1)
    for r in cd.itertuples(index=False):
        d = datetime.strptime(r.date, "%Y%m%d").date()
        if r.exception_type == "1":
            days[r.service_id].add(d)
        else:
            days[r.service_id].discard(d)
    return days


# ---------------------------------------------------------------------------
# Trajets coupés à la frontière
# ---------------------------------------------------------------------------

def merge_border_splits(trips, routes, st, stops, days, same_line_only=False):
    """Recolle A puis B quand : A finit là où B commence, à la même heure, avec changement
    d'opérateur, sans demi-tour, et sans autre candidat. Le trajet recollé circule les jours
    communs ; A et B restent seuls les autres jours."""
    parent = dict(zip(stops["stop_id"], stops["parent_station"]))
    st = st.assign(station=st["stop_id"].map(lambda s: parent.get(s) or s), seq=st["stop_sequence"].astype(int))
    st = st.sort_values(["trip_id", "seq"])
    seqs = st.groupby("trip_id")["station"].apply(list)
    first = st.groupby("trip_id").first()
    last = st.groupby("trip_id").last()
    agency = trips["route_id"].map(routes.set_index("route_id")["agency_id"])
    agency.index = trips["trip_id"]
    service = trips.set_index("trip_id")["service_id"]

    L = last.reset_index()[["trip_id", "station", "arrival_time"]].rename(columns={"trip_id": "A"})
    F = first.reset_index()[["trip_id", "station", "arrival_time"]].rename(columns={"trip_id": "B"})
    pairs = L.merge(F, on="station", suffixes=("_a", "_b"))
    # même train : le morceau suivant est noté à la jonction 0 à JUNCTION_MAX_GAP min après l'arrivée
    gap = pairs["arrival_time_b"].map(to_min) - pairs["arrival_time_a"].map(to_min)
    pairs = pairs[(gap >= 0) & (gap <= JUNCTION_MAX_GAP) & (pairs["A"] != pairs["B"])]
    pairs = pairs[np.array([agency[a] != agency[b] for a, b in zip(pairs["A"], pairs["B"])], dtype=bool)]
    if same_line_only:
        line = trips["route_id"].map(routes.set_index("route_id")["route_short_name"])
        line.index = trips["trip_id"]
        pairs = pairs[np.array([line[a] == line[b] for a, b in zip(pairs["A"], pairs["B"])], dtype=bool)]
    pairs = pairs[np.array([not (len(seqs[a]) > 1 and len(seqs[b]) > 1 and seqs[a][-2] == seqs[b][1])
                            for a, b in zip(pairs["A"], pairs["B"])], dtype=bool)]
    pairs["common"] = [days[service[a]] & days[service[b]] for a, b in zip(pairs["A"], pairs["B"])]
    pairs = pairs[pairs["common"].map(len) > 0]
    pairs = pairs[~pairs["A"].duplicated(keep=False) & ~pairs["B"].duplicated(keep=False)]
    if pairs.empty:
        return trips, st.drop(columns=["station", "seq"]), {}, 0

    # chaînes A -> B -> C...
    nxt = dict(zip(pairs["A"], pairs["B"]))
    common = {(a, b): c for a, b, c in zip(pairs["A"], pairs["B"], pairs["common"])}
    heads = [a for a in nxt if a not in set(nxt.values())]
    by_trip = {tid: g for tid, g in st.groupby("trip_id")}
    trip_rows = trips.set_index("trip_id")
    new_trips, new_st, new_services, removed_days = [], [], {}, defaultdict(set)
    for head in heads:
        chain, d = [head], days[service[head]]
        while chain[-1] in nxt:
            b = nxt[chain[-1]]
            d = d & common[(chain[-1], b)] & days[service[b]]
            chain.append(b)
        if not d:
            continue
        tid = "+".join(chain)
        sid = "merged:" + tid
        new_services[sid] = d
        for t in chain:
            removed_days[service[t] + "|" + t] |= d
        parts = []
        for k, t in enumerate(chain):
            g = by_trip[t].copy()
            if k:
                # arrêt de jonction : arrivée du morceau précédent, départ de celui-ci
                prev = parts[-1]
                parts[-1] = prev.iloc[:-1]
                g.iloc[0, g.columns.get_loc("arrival_time")] = prev.iloc[-1]["arrival_time"]
                g.iloc[0, g.columns.get_loc("drop_off_type")] = prev.iloc[-1]["drop_off_type"]
            parts.append(g)
        g = pd.concat(parts)
        g = g.assign(trip_id=tid, stop_sequence=[str(i) for i in range(len(g))])
        new_st.append(g)
        row = trip_rows.loc[chain[0]].to_dict()
        # catégorie de la partie la plus longue (ex. le tronçon DB d'un EC Prague - Hambourg)
        longest = max(chain, key=lambda t: len(by_trip[t]))
        row.update({"trip_id": tid, "service_id": sid, "route_id": trip_rows.loc[longest, "route_id"]})
        new_trips.append(row)

    # les morceaux ne circulent plus seuls les jours où ils sont recollés
    per_trip_days = {}
    for key, d in removed_days.items():
        svc, t = key.split("|", 1)
        rest = days[svc] - d
        new_sid = f"rest:{t}"
        per_trip_days[t] = (new_sid, rest)
    trips = trips.copy()
    for t, (sid, rest) in per_trip_days.items():
        trips.loc[trips["trip_id"] == t, "service_id"] = sid
        new_services[sid] = rest
    trips = pd.concat([trips, pd.DataFrame(new_trips)], ignore_index=True)
    st = pd.concat([st] + new_st, ignore_index=True).drop(columns=["station", "seq"])
    return trips, st, new_services, len(new_trips)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build(src: str, dst: str, matcher: StationMatcher, report_csv: str = REPORT_CSV,
          exclude_agencies=(), same_line_only=False) -> dict:
    stats = {}
    with zipfile.ZipFile(src) as z:
        agency, feed_info = read(z, "agency.txt"), read(z, "feed_info.txt")
        routes, trips = read(z, "routes.txt"), read(z, "trips.txt")
        cal, cd = read(z, "calendar.txt"), read(z, "calendar_dates.txt")
        st, stops = read(z, "stop_times.txt"), read(z, "stops.txt")
    stats["trips_total"] = len(trips)

    # 1. trains uniquement (route_type 2 = rail ; 100-117 = types étendus ferroviaires)
    rt = pd.to_numeric(routes["route_type"], errors="coerce").fillna(-1).astype(int)
    routes = routes[(rt == 2) | rt.between(100, 117)]
    excluded = set(agency.loc[agency["agency_name"].isin(exclude_agencies), "agency_id"])
    routes = routes[~routes["agency_id"].isin(excluded)]
    trips = trips[trips["route_id"].isin(routes["route_id"])]
    st = st[st["trip_id"].isin(set(trips["trip_id"]))]

    # 2. trains internationaux coupés par opérateur
    days = service_dates(cal, cd)
    trips, st, new_services, merged = merge_border_splits(trips, routes, st, stops, days, same_line_only)
    stats["trips_merged_at_border"] = merged
    if new_services:
        extra = [{"service_id": sid, "date": d.strftime("%Y%m%d"), "exception_type": "1"}
                 for sid, ds in new_services.items() for d in sorted(ds)]
        cd = pd.concat([cd, pd.DataFrame(extra)], ignore_index=True)
    used_services = set(trips["service_id"])
    cal = cal[cal["service_id"].isin(used_services)]
    cd = cd[cd["service_id"].isin(used_services)]

    # 3. points de simple passage (frontières) retirés
    passing = (st["pickup_type"] == "1") & (st["drop_off_type"] == "1")
    st = st[~passing]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]
    stats["stop_times_passing_removed"] = int(passing.sum())

    # 4. gares -> stations.csv
    used = set(st["stop_id"])
    keep = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops = stops[stops["stop_id"].isin(used | keep)]
    stops, report = map_stations(stops, used, matcher)
    # points frontière techniques ("Horka(Gr)") inconnus de stations.csv : retirés des horaires
    border = (stops["stop_code"] == "") & stops["stop_name"].str.contains(r"\(Gr\)|\[Grenze\]", regex=True)
    st = st[~st["stop_id"].isin(set(stops.loc[border, "stop_id"]))]
    counts = st.groupby("trip_id").size()
    st = st[st["trip_id"].isin(counts[counts >= 2].index)]
    trips = trips[trips["trip_id"].isin(set(st["trip_id"]))]
    report.loc[(report["status"] == "non trouvée")
               & report["gtfs_name"].str.contains(r"\(Gr\)|\[Grenze\]", regex=True), "status"] = "frontière retirée"
    report.sort_values(["status", "gtfs_name"]).to_csv(report_csv, sep=";", index=False, encoding="utf-8")
    stats.update({f"stations_{k}": int(v) for k, v in report["status"].value_counts().items()})

    routes = routes[routes["route_id"].isin(trips["route_id"])]
    agency = agency[agency["agency_id"].isin(routes["agency_id"])]
    tables = {"agency.txt": agency, "feed_info.txt": feed_info, "routes.txt": routes, "stops.txt": stops,
              "trips.txt": trips, "calendar.txt": cal, "calendar_dates.txt": cd, "stop_times.txt": st}
    tmp = dst + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for name in FILES_KEPT:
            buf = io.StringIO()
            tables[name].to_csv(buf, index=False)
            out.writestr(name, buf.getvalue())
    os.replace(tmp, dst)
    stats.update({"trips_kept": len(trips), "stop_times": len(st)})
    return stats


def main():
    """python GERMANY/ingest_de_gtfs.py [fv] [rv]   (les deux par défaut)"""
    names = [a for a in sys.argv[1:] if a in FEEDS] or list(FEEDS)
    osm = OsmLookup()
    matcher = StationMatcher(StationRef(STATIONS_CSV), osm)
    for name in names:
        feed = FEEDS[name]
        print("=" * 60)
        print(f"🚄 Ingestion GTFS Allemagne {name} (gtfs.de / DELFI)")
        print("=" * 60)
        raw = feed["zip"] + ".raw"
        try:
            download(feed["url"], raw)
            stats = build(raw, feed["zip"], matcher, feed["report"], feed["exclude_agencies"], feed["same_line_only"])
        finally:
            if os.path.exists(raw):
                os.remove(raw)
        for k, v in stats.items():
            logging.info(f"   {k} : {v}")
        print(f"\n✅ {feed['zip']} ({os.path.getsize(feed['zip']) / 1e6:.1f} Mo)")
        print(f"📋 Rapport des gares : {feed['report']} (lignes « à vérifier » / « non trouvée » à trancher "
              f"dans {os.path.basename(OVERRIDES_CSV)})")
        osm.save()
    write_extra(matcher.extra)
    logging.info(f"   OpenStreetMap : {osm.queries} requêtes, {len(matcher.extra)} gares ajoutées à stations_extra.csv")


if __name__ == "__main__":
    main()
