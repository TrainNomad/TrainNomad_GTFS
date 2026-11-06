#!/usr/bin/env python3
"""
Cartes des trains au départ des villes du site, sur les vraies voies, façon plan de réseau.

Les villes : toutes celles des liaisons du front (src/lib/seoPagesData.json). Pour chacune, toutes les autres
atteignables en direct ou avec une correspondance (/explorer de l'API Europe, puis /search pour les trains et tous
leurs arrêts ; la journée type des pages et la même une semaine plus tard, pour ne pas tomber sur des travaux), et
ses trains de nuit (/explorer sur une semaine). Trait plein : le train direct (une ligne par compagnie) ;
pointillés : la suite après une correspondance. Les lignes qui partagent les mêmes voies sont écartées côte à côte.
Fond : les terres en points (comme le globe de l'accueil) et les frontières en trait fin.

Écrit, pour la carte interactive de la page /train/ :
  public/cartes/europe.svg           le fond (terres, frontières), dans la projection commune à toutes les villes
  public/cartes/reseau/<ville>.json  les lignes et les villes desservies, pour chaque filtre (direct et une
                                     correspondance, directs, moins de 3 h, trains de nuit)
  src/lib/cartes.json                l'index : position et étiquette de chaque ville, filtres disponibles
et, pour les moteurs d'images, une carte cadrée sur chaque ville : public/cartes/depuis-<ville>[-…].webp (rendue
par Edge ou Chrome sans fenêtre ; par défaut « direct et une correspondance » et « trains de nuit »).
Réponses de l'API gardées 3 jours dans traces/cache/cartes/ : une relance ne refait que le rendu.

    python traces/cartes_villes.py --toutes
    python traces/cartes_villes.py --toutes --manquantes     (reprise d'un calcul interrompu)
    python traces/cartes_villes.py --ville Paris --ville Rennes
    python traces/cartes_villes.py --toutes --images toutes      (images des 4 filtres, plus long)
    python traces/cartes_villes.py --ville Paris --sortie C:/tmp/cartes
"""
import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import hashlib
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from collections import Counter, defaultdict

import csv
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_traces import REFERENTIEL_DIR, simplify  # noqa: E402
from globe import DETOUR_MAX, FRONT_DIR, decode_polyline, graphe, km, route  # noqa: E402

W, H = 1600, 1200
PAD = 70                 # marge autour des lignes (px)
GAP = 4.6                # écart entre deux lignes côte à côte (px)
LINE_W = 3.4
NEAR_PX = 4.5            # deux lignes à moins de 4,5 px l'une de l'autre partagent les voies
STEP_PX = 4.0            # pas de rééchantillonnage des lignes pour l'écartement
STATION_KM = 3           # gare introuvable par son id : gares tracées à moins de 3 km
API = os.environ.get("VITE_API_URL", "https://api.trainnomad.eu")
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "cartes")
CACHE_DAYS = 3
FERRY_OPERATORS = {"BRITTANY_FERRIES", "DFDS", "TRANSMANCHE", "IRISH_FERRIES", "STENA_LINE"}

BG = "#0e1a2b"
MINT = "#34d399"
OPERATORS = {            # couleur, nom affiché
    "SNCF": ("#f4a259", "SNCF"),
    "EUROSTAR": ("#f7d046", "Eurostar"),
    "TRENITALIA": ("#3cc9b4", "Trenitalia"),
    "ITALO": ("#e0567a", "Italo"),
    "RENFE": ("#b38cff", "Renfe"),
    "OUIGO_ES": ("#ff7ad9", "Ouigo España"),
    "DB": ("#ff5d6c", "DB"),
    "DB_REGIO": ("#ff9aa8", "DB Regio"),
    "EUROPEAN_SLEEPER": ("#7fa8ff", "European Sleeper"),
    "NATIONAL_RAIL": ("#9fe26a", "National Rail"),
    "CP": ("#ff8a4c", "CP"),
    "SWISS": ("#ff4545", "CFF"),
    "SNCB": ("#5cc8ff", "SNCB"),
    "NS": ("#ffd98a", "NS"),
    "FLIXTRAIN": ("#a6e83a", "FlixTrain"),
    "OBB": ("#e8505b", "ÖBB"),
    "CD": ("#4cc9f0", "České dráhy"),
    "TRENORD": ("#57d68d", "Trenord"),
    "FERRY": ("#9fd8ff", "Bateau"),
}
FALLBACK = ["#f4a259", "#3cc9b4", "#b38cff", "#ff5d6c", "#7fa8ff", "#9fe26a", "#5cc8ff"]


NIGHT = re.compile(r"nuit|night|notte|sleeper", re.I)   # Intercités de nuit, Nightjet, EuroNight, Caledonian Sleeper…
FERRY = re.compile(r"ferry|bateau|ship|fähre|traghetto", re.I)   # traversées (Holyhead – Dublin…)
COACH = re.compile(r"\b(car|bus|autocar)\b", re.I)   # cars de remplacement pendant des travaux
MAX_3H = 180

# carte -> (suffixe du fichier, surtitre, phrase sous le nom : n1 = destinations directes, n2 = les autres)
MODES = {
    "correspondance": ("", "TRAINS AU DÉPART DE", lambda n1, n2: f"{n1} destinations en direct, {n2} avec une correspondance"),
    "direct": ("-trains-directs", "TRAINS DIRECTS AU DÉPART DE", lambda n1, n2: f"{n1} destinations sans changer de train"),
    "3h": ("-moins-de-3h", "À MOINS DE 3 HEURES DE", lambda n1, n2: f"{n1 + n2} destinations en train en 3 heures au plus"),
    "nuit": ("-trains-de-nuit", "TRAINS DE NUIT AU DÉPART DE", lambda n1, n2: f"{n1 + n2} destinations en dormant à bord"),
}


# ---------------------------------------------------------------- données (API Europe)

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slugify(s: str) -> str:
    return norm(s).replace(" ", "-")


def api(path, **params):
    """GET sur l'API Europe, réponse gardée CACHE_DAYS jours dans traces/cache/cartes/ (relances rapides).
    Cache illisible (calcul coupé pendant une écriture) : refait. Trois essais ; à défaut {} et un avertissement,
    la ville ou la liaison concernée manque sur la carte au lieu d'arrêter tout le calcul."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    cache = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_DAYS * 86400:
        try:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    req = urllib.request.Request(url, headers={"User-Agent": "TrainNomad-cartes"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as err:
            if err.code < 500:   # 4xx : ville inconnue, date hors période… inutile de recommencer
                print(f"   ⚠️ {path} {params.get('from')} → {params.get('to', '')} : HTTP {err.code}")
                return {}
            last = err
        except (OSError, ValueError) as err:
            last = err
        time.sleep(5 * (attempt + 1))
    else:
        print(f"   ⚠️ {path} {params.get('from')} → {params.get('to', '')} : {last}, ignoré")
        return {}
    # cache écrit d'un coup (jamais de fichier à moitié écrit) ; Windows refuse parfois le remplacement d'un
    # fichier qu'un antivirus ou l'indexation tient ouvert : quelques essais, puis la réponse sert sans cache
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for attempt in range(5):
            try:
                os.replace(tmp, cache)
                break
            except PermissionError:
                time.sleep(0.5 * (attempt + 1))
        else:
            os.remove(tmp)
    except OSError:
        pass
    return data


def compact(j):
    """Trajet de l'API -> {duration_min, transfers, trains: [{type, operator, number, dep, arr, stops}]}."""
    trains = [{
        "type": l.get("train_type") or "",
        # une traversée en bateau a sa propre couleur, quelle que soit la compagnie qui la vend
        "operator": "FERRY" if FERRY.search(l.get("train_type") or "") or l["operator"] in FERRY_OPERATORS
        else l["operator"],
        "number": l.get("train_number") or "",
        "dep": l["departure"], "arr": l["arrival"],
        "stops": [(s["id"], s.get("city") or s["name"], s["lat"], s["lon"]) for s in l["stops"]
                  if s.get("lat") or s.get("lon")],
    } for l in j["legs"] if l["type"] == "train"]
    return {"duration_min": j["duration_min"], "transfers": j["transfers"], "trains": trains}


def is_night(train):
    if COACH.search(train["type"]):
        return False   # car de remplacement (travaux) : pas un train de nuit
    if NIGHT.search(train["type"]):
        return True
    # pas de nom explicite : départ le soir, arrivée le lendemain après au moins 5 h de route
    return train["dep"][11:13] >= "19" and train["arr"][:10] > train["dep"][:10] and \
        (datetime.fromisoformat(train["arr"]) - datetime.fromisoformat(train["dep"])).total_seconds() >= 5 * 3600


def one_per_operator(js):
    """Le plus rapide de chaque compagnie (celle du premier train) : une ligne par compagnie sur la carte."""
    by_op = {}
    for j in sorted(js, key=lambda j: j["duration_min"]):
        by_op.setdefault(j["trains"][0]["operator"], j)
    return list(by_op.values())


def pick_journeys(js, mode):
    """Voyages dessinés pour une liaison selon la carte ; [] : la destination n'y figure pas.
    Avec des directs : un par compagnie ; sinon le voyage avec le moins de correspondances (le plus rapide à égalité)."""
    if mode == "3h":
        js = [j for j in js if j["duration_min"] <= MAX_3H]
    elif mode == "direct":
        js = [j for j in js if j["transfers"] == 0]
    else:
        js = [j for j in js if j["transfers"] <= 1]
    if not js:
        return []
    direct = [j for j in js if j["transfers"] == 0]
    if direct:
        return one_per_operator(direct)
    return sorted(js, key=lambda j: (j["transfers"], j["duration_min"]))[:1]


def night_services(origin_id, days, log):
    """Trains de nuit au départ de la ville sur une semaine : un par service, avec tous ses arrêts jusqu'au terminus.
    Candidats : les destinations directes de /explorer (départ dès 16 h, puis dès 19 h 30) atteintes le lendemain ;
    /search vers chacune donne le train et ses arrêts."""
    cands = {}
    # deux recherches par jour : dès 16 h (European Sleeper Paris–Berlin part à 17 h 45) et dès 19 h 30, où le
    # train de nuit n'est plus caché par un TGV de jour plus rapide (Toulouse → Paris)
    for d in days:
        for start in ("16:00", "19:30"):
            for x in api("/explorer", **{"from": origin_id, "date": d, "time": start, "max_transfers": 0,
                                         "limit": 3000}).get("destinations", []):
                dr = x.get("direct")
                if dr and dr["arrival"][:10] > d:
                    cands.setdefault(x["place"]["id"], (d, start, x["place"]))
    log(f"   {len(cands)} destinations candidates pour les trains de nuit")
    places = {p["id"]: p for _, _, p in cands.values()}
    found = {}
    for pid, (d, start, place) in cands.items():
        data = api("/search", **{"from": origin_id, "to": pid, "date": d, "time": start, "limit": 20})
        for j in data.get("journeys", []):
            if j["transfers"]:
                continue
            t = compact(j)["trains"][0]
            if is_night(t) and len(t["stops"]) >= 2:
                key = (t["operator"], t["number"])
                if key not in found or len(t["stops"]) > len(found[key]["stops"]):
                    found[key] = t
    # un même train cherché vers plusieurs de ses arrêts : on garde le parcours le plus long
    services = []
    for t in sorted(found.values(), key=lambda t: -len(t["stops"])):
        ids = {s[0] for s in t["stops"]}
        if not any(ids <= {s[0] for s in o["stops"]} for o in services):
            services.append(t)
    return services, places


# ---------------------------------------------------------------- géométrie

class Proj:
    """Projection azimutale équivalente de Lambert centrée sur l'Europe, puis mise à l'échelle de l'image."""

    def __init__(self, lon0=10.0, lat0=50.0):
        self.l0, self.p0 = math.radians(lon0), math.radians(lat0)
        self.k, self.tx, self.ty = 1.0, 0.0, 0.0

    def raw(self, lat, lon):
        p, l = np.radians(lat), np.radians(lon) - self.l0
        k = np.sqrt(2 / (1 + math.sin(self.p0) * np.sin(p) + math.cos(self.p0) * np.cos(p) * np.cos(l)))
        x = k * np.cos(p) * np.sin(l)
        y = k * (math.cos(self.p0) * np.sin(p) - math.sin(self.p0) * np.cos(p) * np.cos(l))
        return x, -y

    def fit(self, pts_latlon, top_reserve=150, bottom_reserve=120):
        x, y = self.raw(pts_latlon[:, 0], pts_latlon[:, 1])
        bw, bh = x.max() - x.min(), y.max() - y.min()
        aw, ah = W - 2 * PAD, H - 2 * PAD - top_reserve - bottom_reserve
        self.k = min(aw / bw, ah / bh)
        self.tx = PAD + (aw - bw * self.k) / 2 - x.min() * self.k
        self.ty = PAD + top_reserve + (ah - bh * self.k) / 2 - y.min() * self.k

    def __call__(self, lat, lon):
        x, y = self.raw(np.asarray(lat, float), np.asarray(lon, float))
        return np.stack([x * self.k + self.tx, y * self.k + self.ty], axis=-1)


class Router:
    """Parcours sur les voies d'arrêt en arrêt (graphe des tracés de globe.py), mis en cache par paire d'arrêts."""

    def __init__(self):
        self.adj, self.coords = graphe()
        self.ids = list(self.coords)
        self.xy = np.array([self.coords[i] for i in self.ids])
        self.by_uic = {}
        with open(os.path.join(REFERENTIEL_DIR, "gares.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                for col in ("uic", "uic8"):
                    if r[col]:
                        self.by_uic.setdefault(r[col], r["id"])
        self.cache = {}
        self.approx = 0

    def nodes(self, stop):
        sid, _, lat, lon = stop
        code = sid.split(":")[-1]
        tn = self.by_uic.get(code) or ("TN" + code)
        if tn in self.adj:
            return [tn]
        d = np.hypot(self.xy[:, 0] - lat, (self.xy[:, 1] - lon) * math.cos(math.radians(lat))) * 111.2
        return [self.ids[i] for i in np.nonzero(d <= STATION_KM)[0]]

    def hop(self, a, b):
        key = (a[0], b[0])
        if key not in self.cache:
            pa, pb = (a[2], a[3]), (b[2], b[3])
            chain = route(self.adj, self.nodes(a), self.nodes(b))
            pts = None
            if chain:
                pts = [pa]
                for u, v in zip(chain, chain[1:]):
                    _, first, encoded = self.adj[u][v]
                    seg = decode_polyline(encoded)
                    pts += [self.coords[u]] + (seg if first == u else seg[::-1])
                pts += [self.coords[chain[-1]], pb]
                if sum(km(p, q) for p, q in zip(pts, pts[1:])) > DETOUR_MAX * max(km(pa, pb), 2):
                    pts = None
            if pts is None:
                self.approx += 1
                pts = [pa, pb]
            self.cache[key] = pts
        return self.cache[key]

    def path(self, stops):
        pts = []
        for a, b in zip(stops, stops[1:]):
            pts += self.hop(a, b)
        return simplify(np.array(pts), 400) if len(pts) > 2 else np.array(pts)


def resample(xy, step):
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if d[-1] < step:
        return xy
    t = np.linspace(0, d[-1], int(d[-1] / step) + 1)
    return np.stack([np.interp(t, d, xy[:, 0]), np.interp(t, d, xy[:, 1])], axis=1)


def smooth(v, win):
    if len(v) < 3:
        return v
    win = min(win, len(v) | 1)
    k = np.ones(win) / win
    return np.convolve(np.pad(v, win // 2, mode="edge"), k, mode="valid")


def bundle(lines, hubs):
    """Écarte côte à côte les compagnies qui empruntent les mêmes voies ; les trains d'une même compagnie (et du même
    style, direct ou après une correspondance) restent sur un seul trait : Paris–Rennes et Paris–Nantes en SNCF ne
    font qu'une ligne jusqu'au Mans, puis se séparent. Ordre des compagnies : direction moyenne de leurs lignes.
    Près d'une ville (hubs : [(x, y, rayon)]) l'écart retombe à zéro : les lignes s'y rejoignent comme à une gare."""
    def key(l):
        return (l["op"], l["after"])
    angles = defaultdict(list)
    for l in lines:
        angles[key(l)].append(l["rank"][0])
    order = {k: (sum(v) / len(v), k) for k, v in angles.items()}
    xs = [l["xy"] for l in lines]
    boxes = [(x[:, 0].min(), x[:, 1].min(), x[:, 0].max(), x[:, 1].max()) for x in xs]
    near = [[{i} for _ in range(len(x))] for i, x in enumerate(xs)]
    for i, a in enumerate(xs):
        for j, b in enumerate(xs):
            if j <= i:
                continue
            ba, bb = boxes[i], boxes[j]
            if ba[2] < bb[0] - NEAR_PX or bb[2] < ba[0] - NEAR_PX or ba[3] < bb[1] - NEAR_PX or bb[3] < ba[1] - NEAR_PX:
                continue
            d = np.hypot(a[:, None, 0] - b[None, :, 0], a[:, None, 1] - b[None, :, 1])
            for k in np.nonzero(d.min(axis=1) < NEAR_PX)[0]:
                near[i][k].add(j)
            for k in np.nonzero(d.min(axis=0) < NEAR_PX)[0]:
                near[j][k].add(i)
    for i, l in enumerate(lines):
        x = xs[i]
        def offset(group):
            keys = sorted({key(lines[m]) for m in group}, key=order.get)
            return (keys.index(key(l)) - (len(keys) - 1) / 2) * GAP
        off = np.array([offset(g) for g in near[i]])
        off = smooth(off, 31)
        for hx, hy, r in hubs:
            d = np.hypot(x[:, 0] - hx, x[:, 1] - hy)
            off *= np.clip((d - 8) / r, 0, 1)
        t = np.gradient(x, axis=0)
        t = np.stack([smooth(t[:, 0], 9), smooth(t[:, 1], 9)], axis=1)
        t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        normal = np.stack([-t[:, 1], t[:, 0]], axis=1)
        l["xy"] = x + normal * off[:, None]


# ---------------------------------------------------------------- fond de carte (world-atlas, TopoJSON)

def topo_arcs(topo):
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    out = []
    for arc in topo["arcs"]:
        a = np.cumsum(np.array(arc, float), axis=0)
        out.append(np.stack([a[:, 1] * sy + ty, a[:, 0] * sx + tx], axis=1))  # (lat, lon)
    return out


def rings(geom):
    polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]] if geom["type"] == "Polygon" else []
    for poly in polys:
        for ring in poly:
            yield ring


def path_d(xy, closed):
    """Tracé SVG au pixel près ; les points hors de l'image sont ramenés juste au bord (le contour de l'Eurasie
    ne pèse plus rien), les points à moins de 2 px du précédent sont sautés."""
    xy = np.round(np.stack([np.clip(xy[:, 0], -4, W + 4), np.clip(xy[:, 1], -4, H + 4)], axis=1)).astype(int)
    keep = [xy[0]]
    for p in xy[1:]:
        if abs(p[0] - keep[-1][0]) + abs(p[1] - keep[-1][1]) >= 2:
            keep.append(p)
    if len(keep) < 2:
        return ""
    return "M" + "L".join(f"{p[0]},{p[1]}" for p in keep) + ("Z" if closed else "")


def visible(xy, margin=200):
    return not (xy[:, 0].max() < -margin or xy[:, 0].min() > W + margin
                or xy[:, 1].max() < -margin or xy[:, 1].min() > H + margin)


def basemap(front, proj):
    atlas = os.path.join(front, "node_modules", "world-atlas")
    with open(os.path.join(atlas, "land-50m.json"), encoding="utf-8") as f:
        land = json.load(f)
    arcs = topo_arcs(land)
    land_d = []
    for geom in land["objects"]["land"]["geometries"]:
        for ring in rings(geom):
            pts = np.concatenate([arcs[i] if i >= 0 else arcs[~i][::-1] for i in ring])
            if pts[:, 1].min() > 60 or pts[:, 1].max() < -40 or pts[:, 0].max() < 20:
                continue  # hors d'Europe : inutile (et loin du centre de la projection)
            xy = proj(pts[:, 0], pts[:, 1])
            if visible(xy):
                land_d.append(path_d(xy, True))

    with open(os.path.join(atlas, "countries-50m.json"), encoding="utf-8") as f:
        countries = json.load(f)
    arcs = topo_arcs(countries)
    used = Counter()
    for geom in countries["objects"]["countries"]["geometries"]:
        for i in {a if a >= 0 else ~a for ring in rings(geom) for a in ring}:
            used[i] += 1
    border_d = []
    for i, n in used.items():
        pts = arcs[i]
        if n < 2 or pts[:, 1].min() > 60 or pts[:, 1].max() < -40 or pts[:, 0].max() < 20:
            continue
        xy = proj(pts[:, 0], pts[:, 1])
        if visible(xy, 0):
            border_d.append(path_d(xy, False))
    return "".join(land_d), "".join(border_d)


# ---------------------------------------------------------------- étiquettes

def text_w(s, size, weight=700):
    return len(s) * size * (0.62 if weight >= 700 else 0.56)


class Labels:
    def __init__(self):
        self.boxes = []

    def overlap(self, b):
        x0, y0, x1, y1 = b
        if x0 < 10 or x1 > W - 10 or y0 < 10 or y1 > H - 10:
            return math.inf
        return sum(max(0, min(x1, c[2]) - max(x0, c[0])) * max(0, min(y1, c[3]) - max(y0, c[1])) for c in self.boxes)

    def block(self, b):
        self.boxes.append(b)

    def place(self, x, y, r, text, size, weight, strict=False):
        """Position libre la plus proche autour du point ; à défaut celle qui recouvre le moins (None si strict)."""
        w, h = text_w(text, size, weight), size
        best = None
        for far in (1, 1.9, 2.8):
            g = (r + 5) * far
            for anchor, tx, ty in (
                ("start", x + g, y + h * 0.35), ("end", x - g, y + h * 0.35),
                ("middle", x, y - g - 2), ("middle", x, y + g + h * 0.8),
                ("start", x + g * 0.7, y - g * 0.7), ("end", x - g * 0.7, y - g * 0.7),
                ("start", x + g * 0.7, y + g * 0.7 + h * 0.6), ("end", x - g * 0.7, y + g * 0.7 + h * 0.6),
            ):
                x0 = tx if anchor == "start" else tx - w if anchor == "end" else tx - w / 2
                b = (x0 - 3, ty - h * 0.85, x0 + w + 3, ty + h * 0.25)
                cost = self.overlap(b)
                if cost == 0:
                    self.block(b)
                    return anchor, tx, ty
                if best is None or cost < best[0]:
                    best = (cost, b, (anchor, tx, ty))
        if strict:
            return None
        self.block(best[1])
        return best[2]


# ---------------------------------------------------------------- rendu

def font_face(front):
    files = os.path.join(front, "node_modules", "@fontsource", "plus-jakarta-sans", "files")
    css = []
    for weight in (500, 700, 800):
        p = os.path.join(files, f"plus-jakarta-sans-latin-{weight}-normal.woff2")
        if os.path.exists(p):
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            css.append(f"@font-face{{font-family:'Plus Jakarta Sans';font-weight:{weight};"
                       f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return "".join(css)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def fmt_min(m):
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def site_cities(data):
    """Villes du site : départs et arrivées des liaisons, {nom: {name, id, lat, lon}}."""
    out = {}
    for l in data["liaisons"]:
        for k in ("from", "to"):
            out.setdefault(l[k]["name"], {kk: l[k][kk] for kk in ("name", "id", "lat", "lon")})
    return out


def settings_cities(front, known, log):
    """Villes nommées dans les réglages des pages (src/lib/seoPages.json) mais sans liaison générée (Dublin : pas
    encore de page, mais une ligne sur la carte), retrouvées par /stations : la ville, sinon sa gare principale."""
    path = os.path.join(front, "src", "lib", "seoPages.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    names = list(cfg.get("liaisonsDepuis", []))
    for group in cfg.get("liaisonsEntre", {}).values():
        names += group
    for pair in cfg.get("liaisons", []):
        names += pair
    out = {}
    for name in dict.fromkeys(names):
        if name in known:
            continue
        res = api("/stations", q=name, limit=10).get("results", [])
        hit = next((r for r in res if r["type"] == "city" and norm(r["name"]) == norm(name)), None) \
            or next((r for r in res if norm(r.get("city") or "") == norm(name)), None)
        if hit:
            out[name] = {"name": name, "id": hit["id"], "lat": hit["lat"], "lon": hit["lon"]}
        else:
            log(f"   ⚠️ {name} (réglages des pages) : introuvable dans l'API")
    return out


def reachable(city, ctx):
    """Villes du site atteignables depuis `city` en direct ou avec une correspondance : {nom: journée à relever}
    (la journée où le trajet est direct, sinon le plus court)."""
    by_id = {c["id"]: n for n, c in ctx["cities"].items()}
    best = {}
    for d in ctx["days"]:
        res = api("/explorer", **{"from": ctx["cities"][city]["id"], "date": d, "time": "05:00", "max_transfers": 1,
                                  "limit": 5000})
        for x in res.get("destinations", []):
            name = by_id.get(x["place"]["id"])
            if name and name != city:
                score = (0 if x.get("direct") else 1, x["duration_min"])
                if name not in best or score < best[name][0]:
                    best[name] = (score, d)
    return {n: d for n, (_, d) in best.items()}


def pair_journeys(city, dest, day, ctx):
    """Trajets de la journée entre deux villes du site (une page de /search depuis 5 h)."""
    key = (city, dest)
    if key not in ctx["pairs"]:
        a, b = ctx["cities"][city], ctx["cities"][dest]
        res = api("/search", **{"from": a["id"], "to": b["id"], "date": day, "time": "05:00", "limit": 50})
        ctx["pairs"][key] = [compact(j) for j in res.get("journeys", [])
                             if j["departure"][:10] == day and not j.get("long_wait")]
    return ctx["pairs"][key]


def collect_city(city, mode, ctx):
    """Lignes vers les autres villes du site : un tronçon par train, de ville en ville."""
    if city not in ctx["reach"]:
        ctx["reach"][city] = reachable(city, ctx)
    src, dests, vias = {}, {}, {}
    for dest, day in sorted(ctx["reach"][city].items()):
        chosen = pick_journeys(pair_journeys(city, dest, day, ctx), mode)
        if not chosen:
            continue
        b = ctx["cities"][dest]
        slug = ctx["slug_of"].get((city, dest))
        direct = any(j["transfers"] == 0 for j in chosen)
        tip = f"{city} → {dest} · {fmt_min(min(j['duration_min'] for j in chosen))}" + \
            (" · direct" if direct else " · 1 correspondance")
        dests[dest] = {"lat": b["lat"], "lon": b["lon"], "direct": direct, "slug": slug, "id": b["id"],
                       "min": min(j["duration_min"] for j in chosen)}
        for j in chosen:
            # deux gares d'une même ville (Paris-Nord -> Paris-Est) : pas un tronçon
            trains = [t for t in j["trains"] if len(t["stops"]) >= 2 and t["stops"][0][1] != t["stops"][-1][1]]
            for i, t in enumerate(trains):
                ca = city if i == 0 else t["stops"][0][1]
                cb = dest if i == len(trains) - 1 else t["stops"][-1][1]
                if i < len(trains) - 1:
                    vias.setdefault(cb, (t["stops"][-1][2], t["stops"][-1][3]))
                src.setdefault((ca, cb, t["operator"]), {"stops": t["stops"], "op": t["operator"], "after": i > 0,
                                                         "dest": dest, "dest_id": b["id"], "slug": slug, "tip": tip})
    return list(src.values()), dests, vias


def collect_night(city, ctx, log):
    """Lignes des trains de nuit : chaque train jusqu'à son terminus, grandes villes desservies en chemin."""
    start = date.fromisoformat(ctx["days"][0])
    services, places = night_services(ctx["cities"][city]["id"],
                                      [(start + timedelta(days=i)).isoformat() for i in range(7)], log)
    # villes nommées en chemin : celles du site ou d'au moins 3 gares (pas Pamiers ni Les Cabannes)
    big = {p["name"] for p in places.values() if p.get("type") == "city" and p.get("stations", 0) >= 3}
    big |= set(ctx["cities"])
    lines, dests, vias = [], {}, {}
    for t in services:
        last = t["stops"][-1]
        log(f"   🌙 {t['type']} {t['number']} : {t['stops'][0][1]} → {last[1]} ({len(t['stops'])} arrêts)")
        slug = ctx["slug_of"].get((city, last[1]))
        lines.append({"stops": t["stops"], "op": t["operator"], "after": False, "dest": last[1], "dest_id": last[0],
                      "slug": slug, "tip": f"{city} → {last[1]} · {t['type']} {t['number']} · départ {t['dep'][11:16]}"})
        dests[last[1]] = {"lat": last[2], "lon": last[3], "direct": True, "slug": slug, "id": last[0]}
        for st in t["stops"][1:-1]:
            if st[1] in big and st[1] != city:
                vias.setdefault(st[1], (st[2], st[3]))
    for name in dests:
        vias.pop(name, None)
    return lines, dests, vias


def collect(city, mode, ctx, log):
    """(lignes avec leur parcours en (lat, lon), destinations, villes de correspondance ou desservies en chemin)."""
    if mode == "nuit":
        lines, dests, vias = collect_night(city, ctx, log)
    else:
        lines, dests, vias = collect_city(city, mode, ctx)
    before = ctx["router"].approx
    for l in lines:
        l["ll"] = ctx["router"].path(l["stops"])
    if ctx["router"].approx > before:
        log(f"   ⚪ {ctx['router'].approx - before} tronçons entre deux arrêts sans tracé : trait droit")
    return lines, dests, vias


def layout(lines, origin, vias, proj):
    """Copies des lignes projetées, rééchantillonnées et écartées côte à côte ; position du départ."""
    ox, oy = proj(origin["lat"], origin["lon"])
    out = [dict(l) for l in lines]
    # rang = direction de la fin du tronçon vue du départ, coupée là où il n'y a pas de ligne
    for l in out:
        l["xy"] = resample(proj(l["ll"][:, 0], l["ll"][:, 1]), STEP_PX)
        ex, ey = l["xy"][-1]
        l["angle"] = math.atan2(ey - oy, ex - ox) % (2 * math.pi)
    angles = sorted(l["angle"] for l in out)
    gaps = [(angles[(i + 1) % len(angles)] - a) % (2 * math.pi) for i, a in enumerate(angles)]
    cut = angles[(gaps.index(max(gaps)) + 1) % len(angles)] if angles else 0
    for l in out:
        l["rank"] = ((l["angle"] - cut) % (2 * math.pi), l["after"], l["op"])
    hubs = [(ox, oy, 90)] + [tuple(proj(*vias[v])) + (35,) for v in vias]
    bundle(out, hubs)
    return out, (ox, oy)


def operators_of(lines):
    ops = []
    for l in sorted(lines, key=lambda l: l["after"]):
        if l["op"] not in ops:
            ops.append(l["op"])
    return ops


def op_color(op, ops):
    return OPERATORS.get(op, (FALLBACK[ops.index(op) % len(FALLBACK)], op.title()))[0]


def op_name(op):
    return OPERATORS.get(op, ("", op.title()))[1]


def render_layer(city, collected, ctx, gproj):
    """Calque de la carte interactive pour une ville et un filtre, dans la projection commune."""
    lines, dests, vias = collected
    if not lines:
        return None
    origin = ctx["cities"][city]
    placed, (ox, oy) = layout(lines, origin, vias, gproj)
    ops = operators_of(placed)
    labels = Labels()
    nodes = [("direct" if d["direct"] else "corresp", n, *gproj(d["lat"], d["lon"]), d) for n, d in dests.items()]
    nodes += [("via", n, *gproj(lat, lon), None) for n, (lat, lon) in vias.items() if n not in dests and n != city]
    for kind, _, x, y, _ in nodes:
        r = 6 if kind != "via" else 4.5
        labels.block((x - r, y - r, x + r, y + r))
    labels.block((ox - 14, oy - 14, ox + 14, oy + 14))
    o_label = labels.place(ox, oy, 14, city, 26, 800)
    out_nodes = []
    for kind, name, x, y, d in sorted(nodes, key=lambda n: ("direct", "corresp", "via").index(n[0])):
        size, weight = (18, 700) if kind != "via" else (13, 500)
        a, tx, ty = labels.place(x, y, 7, name, size, weight)
        out_nodes.append({"name": name, "kind": kind, "x": round(float(x), 1), "y": round(float(y), 1),
                          "label": [a, round(float(tx), 1), round(float(ty), 1)],
                          **({"slug": d["slug"]} if d and d.get("slug") else {}),
                          **({"id": d["id"]} if d else {})})
    n_direct = sum(d["direct"] for d in dests.values())
    order = sorted(placed, key=lambda l: (not l["after"], l["rank"]))
    return {
        "direct": n_direct, "other": len(dests) - n_direct,
        "origin": {"x": round(float(ox), 1), "y": round(float(oy), 1),
                   "label": [o_label[0], round(float(o_label[1]), 1), round(float(o_label[2]), 1)]},
        "lines": [{
            "points": " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in l["xy"]),
            "color": op_color(l["op"], ops), "after": l["after"],
            "tip": f"{l['tip']} · {op_name(l['op'])}", "dest": l["dest"], "id": l["dest_id"],
            **({"slug": l["slug"]} if l.get("slug") else {}),
        } for l in order],
        "nodes": out_nodes,
        "legend": [[op_name(op), op_color(op, ops)] for op in ops],
    }


def render(city, mode, ctx, collected):
    """Image cadrée sur la ville (moteurs d'images) : SVG avec un emplacement /*FONTS*/ pour la police."""
    lines, dests, vias = collected
    front = ctx["front"]
    if not lines:
        return None, None
    origin = ctx["cities"][city]
    proj = Proj()
    # au moins un pays autour de la ville : une carte avec deux destinations proches ne zoome pas sur une rue
    around = np.array([[origin["lat"] + dy, origin["lon"] + dx] for dy in (-3.5, 3.5) for dx in (-5.5, 5.5)])
    allpts = np.concatenate([l["ll"] for l in lines] + [around])
    proj.fit(allpts)
    lines, (ox, oy) = layout(lines, origin, vias, proj)

    land_d, border_d = basemap(front, proj)

    ops_used = operators_of(lines)

    def color(op):
        return op_color(op, ops_used)

    out = []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    suffix, kicker, sentence = MODES[mode]
    out.append(f"<title>{esc(kicker.capitalize())} {esc(city)} – carte TrainNomad</title>")
    out.append("<style>/*FONTS*/text{font-family:'Plus Jakarta Sans',system-ui,sans-serif;fill:#fff}</style>")
    out.append('<defs><pattern id="dots" width="7" height="7" patternUnits="userSpaceOnUse">'
               '<circle cx="3.5" cy="3.5" r="1.15" fill="#c9d6ea" fill-opacity=".5"/></pattern>'
               f'<radialGradient id="glow"><stop offset="0" stop-color="{MINT}" stop-opacity=".45"/>'
               f'<stop offset="1" stop-color="{MINT}" stop-opacity="0"/></radialGradient></defs>')
    out.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')
    out.append(f'<path d="{land_d}" fill="url(#dots)" stroke="#9fb3cf" stroke-opacity=".16" stroke-width=".8" fill-rule="nonzero"/>')
    out.append(f'<path d="{border_d}" fill="none" stroke="#9fb3cf" stroke-opacity=".14" stroke-width=".8"/>')
    out.append(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="90" fill="url(#glow)"/>')

    def pl(xy):
        return " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in xy)

    # sous-couche sombre puis lignes : les pointillés d'abord, les directs par-dessus
    order = sorted(lines, key=lambda l: (not l["after"], l["rank"]))
    def data(slug, tip=None):
        """Attributs lus par la carte interactive du site (survol, lien vers la page de la liaison)."""
        return (f' data-slug="{esc(slug)}"' if slug else "") + (f' data-tip="{esc(tip)}"' if tip else "")

    for l in order:
        out.append(f'<polyline points="{pl(l["xy"])}" fill="none" stroke="{BG}" stroke-width="{LINE_W + 3}" '
                   f'stroke-linejoin="round" stroke-linecap="round" stroke-opacity="{.55 if l["after"] else .9}"'
                   f'{data(l["slug"])}/>')
    for l in order:
        dash = ' stroke-dasharray="0.1 7"' if l["after"] else ""
        w = LINE_W + (0.6 if l["after"] else 0)
        tip = f'{l["tip"]} · {OPERATORS.get(l["op"], ("", l["op"]))[1]}'
        out.append(f'<polyline points="{pl(l["xy"])}" fill="none" stroke="{color(l["op"])}" stroke-width="{w}" '
                   f'stroke-linejoin="round" stroke-linecap="round"{dash}{data(l["slug"], tip)}/>')

    # points et étiquettes : départ, destinations directes, destinations avec correspondance, correspondances
    labels = Labels()
    for (x0, y0, x1, y1) in [(0, 0, 720, 190), (0, H - 150, W, H)]:   # titre et légende
        labels.block((x0, y0, x1, y1))
    nodes = []
    for name, d in dests.items():
        x, y = proj(d["lat"], d["lon"])
        nodes.append(("direct" if d["direct"] else "corresp", name, x, y, d.get("slug")))
    for name, (lat, lon) in vias.items():
        if name not in dests and name != city:
            x, y = proj(lat, lon)
            nodes.append(("via", name, x, y, None))
    for kind, name, x, y, _ in nodes:
        r = 6 if kind != "via" else 4.5
        labels.block((x - r, y - r, x + r, y + r))
    labels.block((ox - 16, oy - 16, ox + 16, oy + 16))

    marks, texts = [], []
    marks.append(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="13" fill="{MINT}" stroke="#fff" stroke-width="3"/>')
    a, tx, ty = labels.place(ox, oy, 16, city, 34, 800)
    texts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{a}" font-size="34" font-weight="800" '
                 f'paint-order="stroke" stroke="{BG}" stroke-width="6" stroke-linejoin="round">{esc(city)}</text>')
    for kind, name, x, y, slug in sorted(nodes, key=lambda n: ("direct", "corresp", "via").index(n[0])):
        if kind == "direct":
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="#fff" stroke="{BG}" stroke-width="2.5"'
                         f'{data(slug)}/>')
            size, weight, op = 21, 700, 1
        elif kind == "corresp":
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="{BG}" stroke="#fff" stroke-width="3"'
                         f'{data(slug)}/>')
            size, weight, op = 20, 700, .92
        else:
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{BG}" stroke="#fff" stroke-width="2" stroke-opacity=".8"/>')
            size, weight, op = 15, 500, .7
        a, tx, ty = labels.place(x, y, 7, name, size, weight)
        texts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{a}" font-size="{size}" font-weight="{weight}" '
                     f'fill-opacity="{op}" paint-order="stroke" stroke="{BG}" stroke-width="5" '
                     f'stroke-linejoin="round"{data(slug)}>{esc(name)}</text>')
    out += marks + texts

    # titre
    n_direct = sum(d["direct"] for d in dests.values())
    out.append(f'<text x="{PAD}" y="{PAD + 40}" font-size="22" font-weight="800" fill="{MINT}" '
               f'letter-spacing="3">{esc(kicker)}</text>')
    out.append(f'<text x="{PAD}" y="{PAD + 104}" font-size="68" font-weight="800">{esc(city)}</text>')
    out.append(f'<text x="{PAD}" y="{PAD + 142}" font-size="20" font-weight="500" fill-opacity=".75">'
               f'{esc(sentence(n_direct, len(dests) - n_direct))}</text>')

    # légende
    ly = H - 70
    out.append(f'<line x1="{PAD}" y1="{ly}" x2="{PAD + 46}" y2="{ly}" stroke="#fff" stroke-width="{LINE_W}" stroke-linecap="round"/>')
    out.append(f'<text x="{PAD + 58}" y="{ly + 6}" font-size="17" font-weight="500">Train direct</text>')
    if any(l["after"] for l in lines):
        out.append(f'<line x1="{PAD + 200}" y1="{ly}" x2="{PAD + 246}" y2="{ly}" stroke="#fff" stroke-width="{LINE_W + .6}" '
               f'stroke-linecap="round" stroke-dasharray="0.1 7"/>')
        out.append(f'<text x="{PAD + 258}" y="{ly + 6}" font-size="17" font-weight="500">Après une correspondance</text>')
    x = PAD
    for op in ops_used:
        name = OPERATORS.get(op, ("", op.title()))[1]
        out.append(f'<rect x="{x}" y="{ly + 26}" width="22" height="6" rx="3" fill="{color(op)}"/>')
        out.append(f'<text x="{x + 30}" y="{ly + 35}" font-size="15" font-weight="500" fill-opacity=".8">{esc(name)}</text>')
        x += 30 + text_w(name, 15, 500) + 26
    out.append(f'<text x="{W - PAD}" y="{H - 44}" text-anchor="end" font-size="28" font-weight="800">'
               f'Train<tspan fill="{MINT}">Nomad</tspan>.eu</text>')
    logo = os.path.join(front, "public", "assets", "Logo", "SVG", "Trainnomad_blanc_svg.svg")
    if os.path.exists(logo):
        with open(logo, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        lx = W - PAD - text_w("TrainNomad.eu", 28, 800) - 74
        out.append(f'<image href="data:image/svg+xml;base64,{b64}" x="{lx:.0f}" y="{H - 98}" width="64" height="64"/>')
    out.append("</svg>")
    names = [n for n, d in sorted(dests.items(), key=lambda kv: not kv[1]["direct"])]
    meta = {"mode": mode, "direct": n_direct, "other": len(dests) - n_direct, "lines": len(lines),
            "title": f"{kicker.capitalize()} {city}",
            "alt": f"Carte des {kicker.lower()} {city} : {', '.join(names)}"}
    return "\n".join(out), meta


def browser():
    for p in (shutil.which("msedge"), shutil.which("chrome"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if p and os.path.exists(p):
            return p
    return None


def rasterize(svg_path, webp_path):
    exe = browser()
    if not exe:
        print("   ⚠️ ni Edge ni Chrome : pas d'image WebP")
        return False
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "carte.png")
        html = os.path.join(tmp, "carte.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write(f'<html><body style="margin:0"><img src="file:///{svg_path.replace(os.sep, "/")}" '
                    f'width="{W}" height="{H}"></body></html>')
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={tmp}",
                        f"--window-size={W},{H}", "--default-background-color=00000000", "--virtual-time-budget=3000",
                        f"--screenshot={png}", f"file:///{html.replace(os.sep, '/')}"],
                       check=True, capture_output=True, timeout=120)
        Image.open(png).convert("RGB").save(webp_path, "WEBP", quality=86, method=6)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--front", default=FRONT_DIR, help="dossier du front (trainnomad-react)")
    parser.add_argument("--ville", action="append", help="ville de départ (plusieurs fois possible)")
    parser.add_argument("--toutes", action="store_true", help="toutes les villes du site")
    parser.add_argument("--manquantes", action="store_true",
                        help="avec --toutes : seulement les villes pas encore faites ou faites avant cette journée type "
                             "(reprise d'un calcul interrompu)")
    parser.add_argument("--images", default="correspondance,nuit",
                        help="filtres dont on fait l'image : liste (correspondance,direct,3h,nuit), toutes ou aucune")
    parser.add_argument("--sortie", help="dossier de sortie (défaut : public/cartes du front)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    image_modes = list(MODES) if args.images == "toutes" else [] if args.images == "aucune" else args.images.split(",")

    with open(os.path.join(args.front, "src", "lib", "seoPagesData.json"), encoding="utf-8") as f:
        data = json.load(f)
    cities = site_cities(data)
    cities.update(settings_cities(args.front, cities, print))
    todo = list(cities) if args.toutes else (args.ville or [])
    if not todo:
        parser.error("--ville ou --toutes")
    unknown = [c for c in todo if c not in cities]
    if unknown:
        parser.error(f"villes absentes des liaisons du site : {', '.join(unknown)}")
    out_dir = args.sortie or os.path.join(args.front, "public", "cartes")
    os.makedirs(os.path.join(out_dir, "reseau"), exist_ok=True)
    index_path = os.path.join(args.front, "src", "lib", "cartes.json") if not args.sortie \
        else os.path.join(out_dir, "index.json")

    first = date.fromisoformat(data["date"])
    print("Graphe des tracés…")
    ctx = {"front": args.front, "router": Router(), "cities": cities, "reach": {}, "pairs": {},
           "slug_of": {(l["from"]["name"], l["to"]["name"]): l["slug"] for l in data["liaisons"]},
           # la journée type des pages et la même une semaine plus tard (travaux)
           "days": [data["date"], (first + timedelta(days=7)).isoformat()]}

    # projection commune : toutes les villes du site, un peu de marge autour
    gproj = Proj()
    pts = np.array([[c["lat"], c["lon"]] for c in cities.values()])
    pad = np.array([[pts[:, 0].min() - 1.5, pts[:, 1].min() - 2], [pts[:, 0].max() + 1.5, pts[:, 1].max() + 2]])
    gproj.fit(np.concatenate([pts, pad]), top_reserve=0, bottom_reserve=0)
    # signature du cadrage : une ville ajoutée au site recadre la carte ; les lignes calculées avant ne tombent plus
    # sur le fond, la page ne les affiche pas tant qu'elles n'ont pas la même signature que l'index
    proj_sig = f"{gproj.k:.3f}/{gproj.tx:.1f}/{gproj.ty:.1f}"
    land_d, border_d = basemap(args.front, gproj)
    with open(os.path.join(out_dir, "europe.svg"), "w", encoding="utf-8") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
                '<defs><pattern id="dots" width="7" height="7" patternUnits="userSpaceOnUse">'
                '<circle cx="3.5" cy="3.5" r="1.15" fill="#c9d6ea" fill-opacity=".5"/></pattern></defs>'
                f'<rect width="{W}" height="{H}" fill="{BG}"/>'
                f'<path d="{land_d}" fill="url(#dots)" stroke="#9fb3cf" stroke-opacity=".16" stroke-width=".8"/>'
                f'<path d="{border_d}" fill="none" stroke="#9fb3cf" stroke-opacity=".14" stroke-width=".8"/></svg>')

    # villes sur la carte de départ : un point chacune, le nom là où il tient (les autres au survol)
    index = {"cities": []}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    old_maps = {c["name"]: c.get("maps", []) for c in index.get("cities", [])}
    old_dests = {c["name"]: c.get("dests", []) for c in index.get("cities", [])}
    old_dates = {c["name"]: c.get("date") for c in index.get("cities", [])}
    labels = Labels()
    xy = {n: gproj(c["lat"], c["lon"]) for n, c in cities.items()}
    for x, y in xy.values():
        labels.block((x - 5, y - 5, x + 5, y + 5))
    weight = Counter(l["from"]["name"] for l in data["liaisons"]) + Counter(l["to"]["name"] for l in data["liaisons"])
    base = {}
    for n in sorted(cities, key=lambda n: -weight[n]):   # les villes les plus reliées gardent leur nom
        x, y = xy[n]
        lab = labels.place(x, y, 5, n, 15, 700, strict=True)
        base[n] = {"name": n, "slug": slugify(n), "id": cities[n]["id"], "x": round(float(x), 1),
                   "y": round(float(y), 1),
                   "label": [lab[0], round(float(lab[1]), 1), round(float(lab[2]), 1)] if lab else None,
                   "maps": old_maps.get(n, []), "dests": old_dests.get(n, []), "date": old_dates.get(n)}

    def save_index():
        index.update({"built_at": datetime.now().isoformat(timespec="seconds"), "width": W, "height": H,
                      "proj": proj_sig,
                      "cities": sorted(base.values(), key=lambda c: -weight[c["name"]])})
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)

    fonts = font_face(args.front)
    if args.manquantes:
        done = {c["name"] for c in index.get("cities", []) if c.get("dests") and c.get("date") == ctx["days"][0]}
        todo = [c for c in todo if c not in done]
        print(f"Reprise : {len(todo)} villes à faire ({', '.join(todo)})")
    for city in todo:
        layers, maps = {}, []
        for mode in MODES:
            print(f"Depuis {city} · {mode}")
            collected = collect(city, mode, ctx, print)
            layer = render_layer(city, collected, ctx, gproj)
            if not layer:
                print("   ⚪ aucune destination")
                continue
            layers[mode] = layer
            _, kicker, _ = MODES[mode]
            names = [n for n, d in sorted(collected[1].items(), key=lambda kv: not kv[1]["direct"])]
            meta = {"mode": mode, "direct": layer["direct"], "other": layer["other"], "lines": len(layer["lines"]),
                    "title": f"{kicker.capitalize()} {city}",
                    "alt": f"Carte des {kicker.lower()} {city} : {', '.join(names)}", "image": None}
            name = f"depuis-{slugify(city)}{MODES[mode][0]}"
            if os.path.exists(os.path.join(out_dir, name + ".webp")):
                meta["image"] = name   # image d'un calcul précédent, gardée si on ne la refait pas
            if mode in image_modes:
                svg, _ = render(city, mode, ctx, collected)
                with tempfile.TemporaryDirectory() as tmp:   # police intégrée pour le rendu
                    full = os.path.join(tmp, "carte.svg")
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(svg.replace("/*FONTS*/", fonts))
                    if rasterize(full, os.path.join(out_dir, name + ".webp")):
                        meta["image"] = name
            maps.append(meta)
            if mode == "correspondance":   # liste sous la carte de /train/ : les destinations de la ville
                base[city]["dests"] = sorted(
                    ({"name": n, "min": d["min"], "direct": d["direct"], "id": d["id"],
                      **({"slug": d["slug"]} if d.get("slug") else {})} for n, d in collected[1].items()),
                    key=lambda d: d["min"])
            print(f"   ✅ {layer['direct']} en direct, {layer['other']} autres, {len(layer['lines'])} lignes"
                  + (f", image {meta['image']}.webp" if meta["image"] else ""))
        with open(os.path.join(out_dir, "reseau", f"{slugify(city)}.json"), "w", encoding="utf-8") as f:
            json.dump({"proj": proj_sig, **layers}, f, ensure_ascii=False, separators=(",", ":"))
        base[city]["maps"] = maps
        base[city]["date"] = ctx["days"][0]   # journée type du calcul (reprise avec --manquantes)
        save_index()   # après chaque ville : un arrêt en cours de route garde les villes déjà faites
    save_index()
    print(f"Index des cartes -> {index_path}")


if __name__ == "__main__":
    main()
