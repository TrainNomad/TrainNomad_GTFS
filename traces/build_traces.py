"""
Tracés des trains sur les voies ferrées d'OpenStreetMap, pour un opérateur.

Pour chaque paire de gares consécutives des trains de l'opérateur (Rennes -> Laval), calcule le chemin
sur les rails et garde jusqu'à deux variantes :
  rapide     chemin le plus rapide à la vitesse maximale des voies (emprunte les lignes à grande vitesse)
  classique  le même calcul sans les lignes à grande vitesse
Une seule variante ("unique") quand les deux chemins sont identiques.

Le tracé d'un train entre deux arrêts est ensuite la variante la plus concordante avec son horaire
(fonction variante_concordante) : parmi les variantes réalisables dans la durée prévue, celle dont le
temps théorique s'en approche le plus. Un TGV Rennes -> Laval en 25 min ne peut être que sur la LGV
(28,8 min au mieux par la ligne classique) ; un OUIGO Train Classique en 37 min est sur la ligne classique.

Entrées :
  GTFS de l'opérateur (operators.json : feeds/<OP>.zip ou gtfs_path)
  referentiel/gares.csv, codes.csv (coordonnées des gares, stop_id -> gare)
  voies railway=rail d'OpenStreetMap (API Overpass, par carreaux de 1°, cache dans traces/cache/)

Sorties (traces/sortie/<OP>/, hors de git) :
  paires.csv            a;b;variante;km;km_lgv;min_theorique;km_droit   (a < b, une ligne par variante)
  sans_trace.csv        paires sans tracé (gare loin des voies, voies non raccordées) : trait droit
  gares/<id>.json       tracés au départ de la gare <id> vers les gares d'id supérieur :
                        {"<b>": {"<variante>": {"km":…, "min":…, "pts":"<polyline encodée, précision 5>"}}}
                        Le trajet b -> a se lit dans le fichier de a, tracé à inverser.

Les tracés déjà calculés sont conservés d'un lancement à l'autre (--refaire pour tout recalculer).
Les cars ne sont pas tracés (route_type 3, arrêts « Car » de la SNCF).

Usage :
    python traces/build_traces.py                           (toutes les compagnies activées, puis publier.py --release et globe.py)
    python traces/build_traces.py --op SNCB --pays BE      (gares belges uniquement)
    python traces/build_traces.py --op SNCB                 (tout l'opérateur, trains internationaux compris)
    python traces/build_traces.py --op SNCF --offline      (voies déjà en cache uniquement)
    python traces/build_traces.py --op SNCF --max-paires 200
"""
import argparse
import csv
import gzip
import heapq
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, GTFS_DIR)
from build_network import FEEDS_DIR, OPERATORS_FILE, REFERENTIEL_DIR, Referentiel, parse_minutes, read_gtfs  # noqa: E402

CACHE_DIR = os.path.join(BASE_DIR, "cache")
OUT_DIR = os.path.join(BASE_DIR, "sortie")

OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "TrainNomad/1.0 (+https://trainnomad.eu) traces"
OVERPASS_DELAY_S = 2.0      # politesse envers les serveurs publics
CELL_MARGIN_DEG = 0.2       # voies cherchées autour de chaque paire de gares

SNAP_MAX_KM = 1.0           # une gare à plus de 1 km de toute voie n'est pas tracée
SNAP_RADIUS_KM = 0.3        # une gare est accrochée à toutes les voies proches (lignes à 4 voies : le train
SNAP_MAX_NODES = 60         # peut être sur n'importe laquelle, sans aller chercher un aiguillage au loin)
DETOUR_MAX = 2.5            # tracé refusé s'il dépasse 2,5 fois la distance à vol d'oiseau (+ 5 km)
HS_MIN_SPEED = 220          # au-delà : ligne à grande vitesse, même sans étiquette highspeed=yes
DEFAULT_SPEED = {"highspeed": 300.0, "main": 140.0, "branch": 90.0, "other": 60.0, "service": 30.0}
SKIPPED_SERVICES = {"yard", "spur"}   # voies de dépôt et embranchements privés
HEURISTIC_SPEED = 320.0     # vitesse d'un minorant sûr du temps restant (A*)
MAX_SETTLED = 400_000       # garde-fou : nœuds explorés par recherche
SIMPLIFY_M = 15.0           # tolérance de simplification des tracés
SCHEDULE_TOLERANCE = 1.02   # un horaire arrondi à la minute peut être 2 % sous le temps théorique
# Un train ne tient jamais le temps théorique à vitesse maximale (accélérations, freinages, marges) :
# une variante n'est réalisable que si l'horaire lui laisse au moins 15 % de plus. Sans cela, un IC
# Louvain -> Liège en 32 min (ligne à grande vitesse) passait pour un train de la ligne classique (30 min).
MIN_OVERHEAD = 1.15


# ---------------------------------------------------------------------------
# Paires de gares et durées, d'après le GTFS
# ---------------------------------------------------------------------------

def feed_path(op: dict) -> str:
    return os.path.join(GTFS_DIR, op["gtfs_path"]) if op.get("gtfs_path") else os.path.join(FEEDS_DIR, f"{op['id']}.zip")


def load_pairs(op: dict, ref: Referentiel) -> dict:
    """{(gare a, gare b): durées en minutes (triées) des trains allant de a à b sans arrêt intermédiaire}."""
    path = feed_path(op)
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : compiler ou télécharger d'abord le GTFS de {op['id']}")
    z = zipfile.ZipFile(path)
    routes = read_gtfs(z, "routes.txt")
    trips = read_gtfs(z, "trips.txt")
    bus_routes = set(routes.loc[routes.get("route_type", "") == "3", "route_id"]) if "route_type" in routes else set()
    bus_trips = set(trips.loc[trips["route_id"].isin(bus_routes), "trip_id"])

    st = read_gtfs(z, "stop_times.txt")
    st = st[~st["trip_id"].isin(bus_trips)]
    st = st[~st["stop_id"].str.contains("OCECar", regex=False)]   # cars SNCF (TER, à réservation)
    st["seq"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "seq"], kind="stable")
    gare = {s: ref.match(op["id"], s) for s in st["stop_id"].unique()}
    unknown = sum(1 for g in gare.values() if not g)
    if unknown:
        logging.info(f"   {unknown} arrêts absents de codes.csv : ignorés")
    # arrêt inconnu : None (et non NaN, que pandas met à la place et qui passerait pour une gare)
    gid = np.array([gare[s] or None for s in st["stop_id"]], dtype=object)
    arr = parse_minutes(st["arrival_time"].where(st["arrival_time"] != "", st["departure_time"]))
    dep = parse_minutes(st["departure_time"].where(st["departure_time"] != "", st["arrival_time"]))
    trip = st["trip_id"].to_numpy()

    pairs = defaultdict(set)
    for i in range(len(trip) - 1):
        if trip[i] != trip[i + 1] or not gid[i] or not gid[i + 1] or gid[i] == gid[i + 1]:
            continue
        d = int(arr[i + 1] - dep[i])
        if d > 0:
            pairs[(gid[i], gid[i + 1])].add(d)
    return {k: sorted(v) for k, v in pairs.items()}


# ---------------------------------------------------------------------------
# Voies d'OpenStreetMap
# ---------------------------------------------------------------------------

def cells_for(pairs, gares) -> list:
    """Carreaux de 1° couvrant chaque paire de gares (rectangle des deux gares + marge)."""
    cells = set()
    for a, b in pairs:
        ga, gb = gares[a], gares[b]
        s, n = min(ga["lat"], gb["lat"]) - CELL_MARGIN_DEG, max(ga["lat"], gb["lat"]) + CELL_MARGIN_DEG
        w, e = min(ga["lon"], gb["lon"]) - CELL_MARGIN_DEG, max(ga["lon"], gb["lon"]) + CELL_MARGIN_DEG
        for la in range(math.floor(s), math.floor(n) + 1):
            for lo in range(math.floor(w), math.floor(e) + 1):
                cells.add((la, lo))
    return sorted(cells)


def fetch_cell(lat: int, lon: int, offline: bool):
    """Voies railway=rail du carreau (identifiants des nœuds, étiquettes, géométrie) ; None si indisponible."""
    path = os.path.join(CACHE_DIR, "osm", f"{lat}_{lon}.json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    if offline:
        return None
    query = f'[out:json][timeout:170];way["railway"="rail"]({lat},{lon},{lat + 1},{lon + 1});out body geom;'
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(6):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=200) as r:
                raw = r.read()
            elements = json.loads(raw)["elements"]   # une page d'erreur HTML lève ValueError
        except (OSError, ValueError, KeyError) as e:
            logging.warning(f"   carreau {lat},{lon} : {type(e).__name__} sur {url.split('/')[2]}, nouvel essai")
            time.sleep(10 * (attempt + 1))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path + ".part", "wt", encoding="utf-8") as f:
            json.dump(elements, f)
        os.replace(path + ".part", path)
        time.sleep(OVERPASS_DELAY_S)
        return elements
    return None


def way_speed(tags: dict):
    """(vitesse maximale en km/h, ligne à grande vitesse)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph)?", tags.get("maxspeed", ""))
    if m:
        v = float(m.group(1)) * (1.609344 if m.group(2) else 1.0)
        if v >= 10:
            return v, v > HS_MIN_SPEED or tags.get("highspeed") == "yes"
    if tags.get("highspeed") == "yes":
        return DEFAULT_SPEED["highspeed"], True
    if tags.get("service"):
        return DEFAULT_SPEED["service"], False
    return DEFAULT_SPEED.get(tags.get("usage"), DEFAULT_SPEED["other"]), False


def seg_km(xy: np.ndarray) -> np.ndarray:
    """Longueur de chaque segment d'une ligne de points (lat, lon), en km."""
    dlat = np.diff(xy[:, 0]) * 111.2
    dlon = np.diff(xy[:, 1]) * 111.2 * np.cos(np.radians(xy[:-1, 0]))
    return np.hypot(dlat, dlon)


class RailGraph:
    """Graphe des voies : sommets = nœuds OSM communs à plusieurs voies, extrémités et points
    d'accrochage des gares ; arêtes = portions de voie entre deux sommets (géométrie conservée)."""

    def __init__(self):
        self.way_nodes, self.way_xy, self.way_speed, self.way_hs = [], [], [], []
        self.seen = set()

    def add_cell(self, elements):
        for w in elements:
            if w["id"] in self.seen or len(w.get("nodes", ())) < 2:
                continue
            tags = w.get("tags", {})
            if tags.get("service") in SKIPPED_SERVICES:
                continue
            self.seen.add(w["id"])
            v, hs = way_speed(tags)
            self.way_nodes.append(np.asarray(w["nodes"], np.int64))
            self.way_xy.append(np.asarray([(p["lat"], p["lon"]) for p in w["geometry"]], np.float64))
            self.way_speed.append(v)
            self.way_hs.append(hs)

    def build(self, stations: dict):
        """stations : {id de gare: (lat, lon)}. Accroche chaque gare au nœud de voie le plus proche."""
        all_nodes = np.concatenate(self.way_nodes)
        all_xy = np.concatenate(self.way_xy)
        km_per_deg = np.array([111.2, 111.2 * math.cos(math.radians(float(all_xy[:, 0].mean())))])
        tree = cKDTree(all_xy * km_per_deg)
        self.snap = {}   # gare -> nœuds de voie proches
        for gid, (lat, lon) in stations.items():
            dist, idx = tree.query(np.array([lat, lon]) * km_per_deg, k=SNAP_MAX_NODES, distance_upper_bound=SNAP_MAX_KM)
            # Aucune voie à portée (carreau indisponible…) : distances infinies, indices hors tableau
            near = [int(all_nodes[i]) for d, i in zip(dist, idx)
                    if np.isfinite(d) and d <= max(SNAP_RADIUS_KM, dist[0] + 0.05)]
            if near:
                self.snap[gid] = sorted(set(near))
        del tree

        uniq, counts = np.unique(all_nodes, return_counts=True)
        ends = np.concatenate([[n[0], n[-1]] for n in self.way_nodes])
        snapped = np.array([n for nodes in self.snap.values() for n in nodes], np.int64)
        vertices = np.unique(np.concatenate([uniq[counts > 1], ends, snapped]))
        del all_nodes, all_xy, uniq, counts

        self.adj = defaultdict(list)   # sommet -> [(voisin, minutes, km, km LGV, voie, i0, i1)]
        self.pos = {}
        for wi, (nodes, xy) in enumerate(zip(self.way_nodes, self.way_xy)):
            at = np.searchsorted(vertices, nodes).clip(max=len(vertices) - 1)
            cut = np.flatnonzero(vertices[at] == nodes)
            cum = np.concatenate([[0.0], np.cumsum(seg_km(xy))])
            v, hs = self.way_speed[wi], self.way_hs[wi]
            for i0, i1 in zip(cut[:-1], cut[1:]):
                a, b = int(nodes[i0]), int(nodes[i1])
                length = float(cum[i1] - cum[i0])
                if a == b or length <= 0:
                    continue
                minutes, hs_km = length / v * 60, length if hs else 0.0
                self.adj[a].append((b, minutes, length, hs_km, wi, int(i0), int(i1)))
                self.adj[b].append((a, minutes, length, hs_km, wi, int(i1), int(i0)))
                self.pos[a], self.pos[b] = xy[i0], xy[i1]
        self.way_nodes = None
        self.snap = {g: [n for n in nodes if n in self.pos] for g, nodes in self.snap.items()}
        self.snap = {g: nodes for g, nodes in self.snap.items() if nodes}
        logging.info(f"   graphe : {len(self.pos)} sommets, {sum(len(e) for e in self.adj.values()) // 2} arêtes, "
                     f"{len(self.snap)}/{len(stations)} gares accrochées aux voies")

    def route(self, sources: list, targets: list, allow_hs: bool):
        """A* sur le temps de parcours à vitesse maximale, d'un des nœuds de départ à un des nœuds
        d'arrivée : (minutes, km, km LGV, points) ou None."""
        goal = set(targets) - set(sources)
        if not goal:
            return None
        tlat, tlon = np.mean([self.pos[n] for n in goal], axis=0)
        cos = math.cos(math.radians(tlat))
        slack = SNAP_RADIUS_KM + 0.1   # les nœuds d'arrivée sont autour de ce point : minorant gardé sûr

        def h(n):
            p = self.pos[n]
            return max(0.0, math.hypot((p[0] - tlat) * 111.2, (p[1] - tlon) * 111.2 * cos) - slack) / HEURISTIC_SPEED * 60

        best = {n: 0.0 for n in sources}
        prev = {}
        heap = [(h(n), 0.0, n) for n in sources]
        heapq.heapify(heap)
        settled = 0
        b = None
        while heap:
            _, t, n = heapq.heappop(heap)
            if n in goal:
                b = n
                break
            if t > best.get(n, math.inf):
                continue
            settled += 1
            if settled > MAX_SETTLED:
                return None
            for m, minutes, length, hs_km, wi, i0, i1 in self.adj[n]:
                if hs_km and not allow_hs:
                    continue
                nt = t + minutes
                if nt < best.get(m, math.inf):
                    best[m] = nt
                    prev[m] = (n, length, hs_km, wi, i0, i1)
                    heapq.heappush(heap, (nt + h(m), nt, m))
        if b is None:
            return None
        parts, km, hs_total, n = [], 0.0, 0.0, b
        while n in prev:
            n, length, hs_km, wi, i0, i1 = prev[n]
            xy = self.way_xy[wi]
            parts.append(xy[i0:i1 + 1] if i0 < i1 else xy[i1:i0 + 1][::-1])
            km += length
            hs_total += hs_km
        pts = np.concatenate([p if i == 0 else p[1:] for i, p in enumerate(parts[::-1])])
        return best[b], km, hs_total, pts


# ---------------------------------------------------------------------------
# Géométrie : simplification et encodage
# ---------------------------------------------------------------------------

def simplify(pts: np.ndarray, tol_m: float = SIMPLIFY_M) -> np.ndarray:
    """Douglas-Peucker (itératif) sur des points (lat, lon)."""
    if len(pts) < 3:
        return pts
    xy = np.column_stack([pts[:, 0] * 111_200.0, pts[:, 1] * 111_200.0 * math.cos(math.radians(float(pts[0, 0])))])
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = xy[i], xy[j]
        seg = b - a
        norm = math.hypot(seg[0], seg[1])
        mid = xy[i + 1:j] - a
        dist = np.abs(seg[0] * mid[:, 1] - seg[1] * mid[:, 0]) / norm if norm else np.hypot(mid[:, 0], mid[:, 1])
        k = int(dist.argmax())
        if dist[k] > tol_m:
            k += i + 1
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return pts[keep]


def encode_polyline(pts: np.ndarray) -> str:
    """Format « encoded polyline » (précision 5), lisible par les bibliothèques de cartes."""
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in np.round(pts * 1e5).astype(np.int64):
        for v in (int(lat) - prev_lat, int(lon) - prev_lon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = int(lat), int(lon)
    return "".join(out)


# ---------------------------------------------------------------------------
# Choix de la variante d'un train
# ---------------------------------------------------------------------------

def variante_concordante(variantes: dict, duree_min: float) -> str | None:
    """variantes : {nom: temps minimal théorique}. Parmi les variantes réalisables dans la durée de
    l'horaire (temps théorique + 15 %), celle dont le temps s'en approche le plus ; sinon la plus rapide."""
    if not variantes:
        return None
    ok = {k: t for k, t in variantes.items() if t * MIN_OVERHEAD <= duree_min * SCHEDULE_TOLERANCE}
    if ok:
        return max(ok, key=ok.get)
    return min(variantes, key=variantes.get)


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------

def load_existing(out_dir: str) -> dict:
    """Tracés déjà calculés : {(a, b): {variante: {...}}}."""
    done = {}
    folder = os.path.join(out_dir, "gares")
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                for b, variantes in json.load(f).items():
                    done[(name[:-5], b)] = variantes
    return done


def run_all(args):
    """Sans --op : chaque compagnie activée l'une après l'autre (un processus chacune : la mémoire est rendue
    entre deux, et une compagnie en échec n'arrête pas les suivantes), puis publier.py --release et globe.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(OPERATORS_FILE, encoding="utf-8") as f:
        ops = [o for o in json.load(f) if o.get("enabled")]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    SEP = "=" * 70

    def launch(script, *options):
        """Lance un script en affichant sa sortie au fil de l'eau : (code de retour, carreaux indisponibles)."""
        proc = subprocess.Popen([sys.executable, os.path.join(here, script), *options], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        incomplete = False
        for line in proc.stdout:
            print(line, end="", flush=True)
            incomplete = incomplete or "carreaux indisponibles" in line
        return proc.wait(), incomplete

    options = [o for o, on in (("--offline", args.offline), ("--refaire", args.refaire)) if on]
    if args.pays:
        options += ["--pays", args.pays]
    bilan = []
    for op in ops:
        print(f"\n{SEP}\n{op['id']}\n{SEP}", flush=True)
        if not os.path.exists(feed_path(op)):
            bilan.append((op["id"], "GTFS absent, ignorée"))
            continue
        code, incomplete = launch("build_traces.py", "--op", op["id"], *options)
        bilan.append((op["id"], "ÉCHEC" if code else "carreaux indisponibles : à relancer" if incomplete else "ok"))
    for script, opts in (("publier.py", ["--release"]), ("globe.py", [])):
        print(f"\n{SEP}\n{script}\n{SEP}", flush=True)
        code, _ = launch(script, *opts)
        bilan.append((script, "ÉCHEC" if code else "ok"))
    print(f"\n{SEP}\nBilan ({(time.time() - t0) / 60:.0f} min)")
    for name, state in bilan:
        print(f"   {'✅' if state == 'ok' else '⚠️'} {name} : {state}")
    if any("relancer" in state for _, state in bilan):
        print("   Relancer la même commande : seuls les carreaux et les trajets manquants seront repris.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--op", help="identifiant de l'opérateur dans operators.json (SNCF, SNCB...) ; "
                                     "sans lui : toutes les compagnies activées, puis publier.py et globe.py")
    parser.add_argument("--pays", help="ne trace que les paires dont les deux gares sont dans ces pays (FR, ou FR,BE pour inclure les transfrontaliers)")
    parser.add_argument("--offline", action="store_true", help="voies en cache uniquement, aucun téléchargement")
    parser.add_argument("--refaire", action="store_true", help="recalcule les tracés déjà présents")
    parser.add_argument("--max-paires", type=int, help="limite le nombre de paires (essai)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.op:
        return run_all(args)

    with open(OPERATORS_FILE, encoding="utf-8") as f:
        op = next((o for o in json.load(f) if o["id"].upper() == args.op.upper()), None)
    if op is None:
        sys.exit(f"❌ opérateur {args.op} inconnu dans operators.json")
    out_dir = os.path.join(OUT_DIR, op["id"].upper())
    t0 = time.time()

    ref = Referentiel(REFERENTIEL_DIR)
    logging.info(f"🚆 Paires de gares de {op['id']}")
    directed = load_pairs(op, ref)
    gares = {g: v for g, v in ref.gares.items() if v["lat"] is not None}
    # une paire = un tracé pour les deux sens (a < b)
    pairs = sorted({(min(a, b), max(a, b)) for a, b in directed if a in gares and b in gares})
    if args.pays:
        pays = set(args.pays.upper().split(","))
        pairs = [(a, b) for a, b in pairs if gares[a]["country"] in pays and gares[b]["country"] in pays]
        directed = {k: v for k, v in directed.items() if (min(k), max(k)) in set(pairs)}
    if args.max_paires:
        pairs = pairs[:args.max_paires]
    done = {} if args.refaire else load_existing(out_dir)
    # Paires calculées alors qu'il manquait des carreaux de voies : le tracé a pu contourner le trou
    # (Madrid -> Séville par Mérida). Elles sont recalculées au lancement suivant.
    doubt_path = os.path.join(out_dir, "a_refaire.json")
    doubt = set()
    if os.path.exists(doubt_path):
        with open(doubt_path, encoding="utf-8") as f:
            doubt = {tuple(p) for p in json.load(f)}
    done = {p: v for p, v in done.items() if p not in doubt}
    todo = [p for p in pairs if p not in done]
    logging.info(f"   {len(pairs)} paires, {len(todo)} à calculer")

    if todo:
        cells = cells_for(todo, gares)
        logging.info(f"🗺️  Voies OpenStreetMap : {len(cells)} carreaux")
        graph = RailGraph()
        missing = set()
        for i, (lat, lon) in enumerate(cells, 1):
            elements = fetch_cell(lat, lon, args.offline)
            if elements is None:
                missing.add((lat, lon))
                continue
            graph.add_cell(elements)
            logging.info(f"   {i}/{len(cells)} carreaux, {len(graph.seen)} voies")
        # seules les paires dont le rectangle touche un carreau manquant sont douteuses : un carreau en mer
        # (Fehmarn, 54_11) ne doit pas faire recalculer tout l'opérateur à chaque lancement
        touched = {p for p in todo if missing & set(cells_for([p], gares))}
        if missing:
            logging.warning(f"   {len(missing)} carreaux indisponibles ({', '.join(f'{a}_{o}' for a, o in sorted(missing))}) : "
                            f"{len(touched)} tracés provisoires (manquants ou détournés), recalculés au prochain lancement")
        if not graph.seen:
            sys.exit("❌ aucune voie chargée")
        doubt = (doubt - set(todo)) | touched
        stations = {g: (gares[g]["lat"], gares[g]["lon"]) for p in todo for g in p}
        graph.build(stations)

        logging.info("🧭 Calcul des tracés")
        for i, (a, b) in enumerate(todo, 1):
            variantes = {}
            na, nb = graph.snap.get(a), graph.snap.get(b)
            if na and nb:
                ga, gb = gares[a], gares[b]
                straight = float(seg_km(np.array([[ga["lat"], ga["lon"]], [gb["lat"], gb["lon"]]]))[0])
                fast = graph.route(na, nb, True)
                slow = graph.route(na, nb, False) if fast and fast[2] > 0.5 else None
                for name, r in (("rapide", fast), ("classique", slow)):
                    if r and r[1] <= DETOUR_MAX * straight + 5:
                        variantes[name] = {"km": round(r[1], 1), "km_lgv": round(r[2], 1), "min": round(r[0], 1),
                                           "pts": encode_polyline(simplify(r[3]))}
                if "rapide" in variantes and fast[2] <= 0.5:
                    variantes = {"unique": variantes["rapide"]}
            done[(a, b)] = variantes
            if i % 500 == 0:
                logging.info(f"   {i}/{len(todo)} paires ({time.time() - t0:.0f} s)")

    # écriture : un fichier par gare de départ, paires.csv, sans_trace.csv
    os.makedirs(os.path.join(out_dir, "gares"), exist_ok=True)
    by_station = defaultdict(dict)
    rows, empty = [], []
    for (a, b) in pairs:
        variantes = done.get((a, b), {})
        ga, gb = gares[a], gares[b]
        straight = float(seg_km(np.array([[ga["lat"], ga["lon"]], [gb["lat"], gb["lon"]]]))[0])
        if not variantes:
            empty.append([a, b, ga["name"], gb["name"], round(straight, 1)])
            continue
        by_station[a][b] = variantes
        for name, v in variantes.items():
            rows.append([a, b, name, v["km"], v["km_lgv"], v["min"], round(straight, 1)])
    size = 0
    for a, content in by_station.items():
        path = os.path.join(out_dir, "gares", f"{a}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f, separators=(",", ":"))
        size += os.path.getsize(path)
    for name, header, data in (("paires.csv", ["a", "b", "variante", "km", "km_lgv", "min_theorique", "km_droit"], rows),
                               ("sans_trace.csv", ["a", "b", "nom_a", "nom_b", "km_droit"], empty)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(data)

    if doubt:
        with open(doubt_path, "w", encoding="utf-8") as f:
            json.dump(sorted(doubt), f)
    elif os.path.exists(doubt_path):
        os.remove(doubt_path)

    # bilan : variante retenue pour chaque (paire, durée d'horaire)
    chosen = defaultdict(int)
    impossible = 0
    for (a, b), durations in directed.items():
        variantes = {k: v["min"] for k, v in done.get((min(a, b), max(a, b)), {}).items()}
        for d in durations:
            name = variante_concordante(variantes, d)
            chosen[name or "sans tracé"] += 1
            if name and variantes[name] > d * SCHEDULE_TOLERANCE:
                impossible += 1
    logging.info(f"📊 {len(pairs) - len(empty)}/{len(pairs)} paires tracées, {len(by_station)} fichiers, {size / 1e6:.1f} Mo")
    logging.info(f"   variantes retenues par (paire, durée) : {dict(chosen)} ; "
                 f"{impossible} horaires plus courts que le temps théorique du tracé")
    print(f"\n✅ {out_dir}  ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
