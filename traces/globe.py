#!/usr/bin/env python3
"""
Lignes du globe de la page d'accueil : le vrai parcours sur les voies entre deux villes.

Lit la liste des villes et des liaisons du front (src/lib/globeNetwork.json), cherche pour chacune le
chemin le plus rapide dans les tracés déjà calculés (traces/sortie/, tous opérateurs confondus, en
enchaînant les tronçons de gare en gare) et écrit src/lib/globeTraces.json : une ligne très simplifiée
(le globe est petit) par liaison. Une liaison sans tracé complet (pays pas encore calculé) est absente
du fichier : le globe garde alors sa courbe.

    python traces/globe.py
    python traces/globe.py --front "C:/chemin/vers/trainnomad-react"

À relancer après avoir calculé les tracés d'un nouveau pays, puis commiter le fichier dans le front.
"""
import argparse
import csv
import glob
import heapq
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_traces import REFERENTIEL_DIR, encode_polyline, simplify  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
SORTIE_DIR = os.path.join(ROOT, "sortie")
FRONT_DIR = os.path.normpath(os.path.join(ROOT, "..", "..", "..", "trainnomad-react"))

CITY_RADIUS_KM = 8      # gares d'une ville : à moins de 8 km de son centre
DETOUR_MAX = 2.2        # au-delà de 2,2 fois la ligne droite, ce n'est pas la liaison annoncée
SIMPLIFY_M = 1500       # un pixel du globe vaut plusieurs kilomètres


def km(a, b):
    dx = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(b[0] - a[0], dx) * 111.2


def decode_polyline(s: str) -> list:
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for axis in (0, 1):
            result = shift = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        pts.append((lat / 1e5, lon / 1e5))
    return pts


def load_graph():
    """{gare: {voisine: (minutes, gare d'où part le tracé, tracé encodé)}}, dans les deux sens."""
    adj = {}
    for path in glob.glob(os.path.join(SORTIE_DIR, "*", "gares", "*.json")):
        a = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            for b, variantes in json.load(f).items():
                best = min(variantes.values(), key=lambda v: v["min"])   # la plus rapide
                for x, y in ((a, b), (b, a)):
                    old = adj.setdefault(x, {}).get(y)
                    if old is None or best["min"] < old[0]:
                        adj[x][y] = (best["min"], a, best["pts"])
    return adj


def route(adj, sources, targets):
    """Chemin le plus rapide d'une des gares de départ à une des gares d'arrivée : liste de gares."""
    dist = {s: 0.0 for s in sources}
    prev = {}
    heap = [(0.0, s) for s in sources]
    targets = set(targets)
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if u in targets:
            out = [u]
            while out[-1] in prev:
                out.append(prev[out[-1]])
            return out[::-1]
        for v, (minutes, _, _) in adj.get(u, {}).items():
            nd = d + minutes
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--front", default=FRONT_DIR, help="dossier du front (trainnomad-react)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    lib = os.path.join(args.front, "src", "lib")
    with open(os.path.join(lib, "globeNetwork.json"), encoding="utf-8") as f:
        network = json.load(f)

    adj = load_graph()
    coords = {}
    with open(os.path.join(REFERENTIEL_DIR, "gares.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if r["id"] in adj and r["lat"]:
                coords[r["id"]] = (float(r["lat"]), float(r["lon"]))

    def stations(city):
        c = (city["lat"], city["lng"])
        return [g for g, p in coords.items() if km(c, p) <= CITY_RADIUS_KM]

    out = {}
    for a, b in network["routes"]:
        ca, cb = network["cities"][a], network["cities"][b]
        pa, pb = (ca["lat"], ca["lng"]), (cb["lat"], cb["lng"])
        name = f"{ca['label']} – {cb['label']}"
        chain = route(adj, stations(ca), stations(cb))
        if not chain:
            print(f"   ⚪ {name} : pas de tracé complet")
            continue
        pts = [pa]
        for u, v in zip(chain, chain[1:]):
            _, first, encoded = adj[u][v]
            seg = decode_polyline(encoded)
            pts += [coords[u]] + (seg if first == u else seg[::-1])
        pts += [coords[chain[-1]], pb]
        length = sum(km(p, q) for p, q in zip(pts, pts[1:]))
        if length > DETOUR_MAX * km(pa, pb):
            print(f"   ⚪ {name} : {length:.0f} km par les voies pour {km(pa, pb):.0f} km à vol d'oiseau, écarté")
            continue
        line = simplify(np.array(pts), SIMPLIFY_M)
        out[f"{a}-{b}"] = encode_polyline(line)
        print(f"   🟢 {name} : {length:.0f} km, {len(chain)} gares, {len(line)} points")

    path = os.path.join(lib, "globeTraces.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"✅ {len(out)}/{len(network['routes'])} liaisons sur les voies -> {path} ({os.path.getsize(path) / 1e3:.1f} ko)")


if __name__ == "__main__":
    main()
