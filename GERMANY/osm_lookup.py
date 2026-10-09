"""
Complète le rapprochement des gares avec OpenStreetMap, via l'API OpenRailwayMap :
https://api.openrailwaymap.org/v2/facility?name=...  (données © contributeurs OpenStreetMap, ODbL)

Utilisé seulement pour les gares que stations.csv ne permet pas de reconnaître (nom trop différent ou
gare absente). Pour chacune :
  1. recherche par nom (puis par nom nettoyé : "Bahnhof", abréviations tchèques...) ;
  2. on ne retient que le résultat le plus proche, à moins de MAX_KM des coordonnées du GTFS ;
  3. son uic_ref (numéro EVA/UIC) retrouve la gare dans stations.csv (colonnes db_id puis uic) ;
  4. sinon la gare est ajoutée à stations_extra.csv (id "OSM<osm_id>", nom OSM, UIC, pays, fuseau),
     que build_network.py charge avec stations.csv.

Les réponses sont gardées dans GERMANY/osm_stations.csv (à versionner) : l'API n'est interrogée que
pour les nouvelles gares (et les échecs de plus de RETRY_DAYS jours).
"""
import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
CACHE_CSV = os.path.join(BASE_DIR, "osm_stations.csv")
EXTRA_CSV = os.path.join(GTFS_DIR, "stations_extra.csv")

API_URL = "https://api.openrailwaymap.org/v2/facility"
USER_AGENT = "TrainNomad/1.0 (+https://trainnomad.eu) gtfs-station-matching"
MAX_KM = 2.0
DELAY_S = 1.0          # politesse envers l'API publique
RETRY_DAYS = 30
RAILWAY_KINDS = {"station", "halt", "tram_stop", "stop"}

CACHE_COLUMNS = ["gtfs_name", "gtfs_lat", "gtfs_lon", "checked", "osm_id", "osm_name", "uic_ref",
                 "lat", "lon", "railway", "km"]

# préfixe UIC -> pays, fuseau
UIC_COUNTRIES = {
    "51": ("PL", "Europe/Warsaw"), "54": ("CZ", "Europe/Prague"), "55": ("HU", "Europe/Budapest"),
    "56": ("SK", "Europe/Bratislava"), "78": ("HR", "Europe/Zagreb"), "79": ("SI", "Europe/Ljubljana"),
    "80": ("DE", "Europe/Berlin"), "81": ("AT", "Europe/Vienna"), "82": ("LU", "Europe/Luxembourg"),
    "83": ("IT", "Europe/Rome"), "84": ("NL", "Europe/Amsterdam"), "85": ("CH", "Europe/Zurich"),
    "86": ("DK", "Europe/Copenhagen"), "87": ("FR", "Europe/Paris"), "88": ("BE", "Europe/Brussels"),
    "74": ("SE", "Europe/Stockholm"), "76": ("NO", "Europe/Oslo"),
}

# abréviations des noms du flux allemand pour les gares étrangères
ABBREVIATIONS = [
    (r"\bhl\.\s*n\.", "hlavní nádraží"), (r"\bdol\.\s*nadr\.", "dolní nádraží"),
    (r"\bhor\.\s*nadr\.", "horní nádraží"), (r"\bzast\.", "zastávka"), (r"\bnadr\.", "nádraží"),
    (r"\bn\.\s*", "nad "),
]
NOISE = r"\b(Bahnhof|Bhf\.?|Bf\.?|ZOB|Busbf|Bushaltestelle)\b|\(Gr\)|\[Grenze\]|\(Stadtbahn\)|\(FlixTrain\)"


def km_between(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * 111.2, (lon1 - lon2) * 111.2 * math.cos(math.radians(lat1)))


LOOSE_MAX_KM = 0.6     # recherche sur un seul mot du nom : seulement tout près


def name_variants(name: str) -> list[tuple[str, float]]:
    """(texte cherché, distance maximale) : nom exact, nom nettoyé, puis morceaux du nom."""
    out = [(name, MAX_KM)]
    clean = re.sub(NOISE, " ", name)
    clean = " ".join(clean.replace(",", " ").split())
    for pat, rep in ABBREVIATIONS:
        clean = re.sub(pat, rep, clean)
    clean = " ".join(clean.split())
    if clean and clean != name:
        out.append((clean, MAX_KM))
    # "KA Tullastraße/Alter Schlachthof" : morceaux séparés par / ou ,
    for part in re.split(r"[/,]", clean):
        part = part.strip()
        if len(part) >= 5 and part not in (v for v, _ in out):
            out.append((part, LOOSE_MAX_KM))
    return out


class OsmLookup:
    def __init__(self, offline: bool = False):
        self.offline = offline or os.environ.get("OSM_LOOKUP_OFFLINE") == "1"
        self.cache = {}
        if os.path.exists(CACHE_CSV):
            df = pd.read_csv(CACHE_CSV, sep=";", dtype=str, keep_default_na=False)
            self.cache = {(r["gtfs_name"], r["gtfs_lat"], r["gtfs_lon"]): r for r in df.to_dict("records")}
        self.queries = 0

    @staticmethod
    def key(name, lat, lon):
        return (name, f"{lat:.3f}", f"{lon:.3f}")

    def _query(self, name: str) -> list:
        url = API_URL + "?" + urllib.parse.urlencode({"name": name, "limit": 20})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(3):
            try:
                time.sleep(DELAY_S)
                self.queries += 1
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except (OSError, ValueError) as e:
                logging.warning(f"   OpenRailwayMap {name!r} : {e}")
                time.sleep(5 * (attempt + 1))
        return []

    def find(self, name: str, lat: float, lon: float) -> dict | None:
        """Gare OSM la plus proche portant ce nom, ou None. Résultat mis en cache."""
        k = self.key(name, lat, lon)
        hit = self.cache.get(k)
        if hit is not None:
            fresh = (date.today() - date.fromisoformat(hit["checked"])).days < RETRY_DAYS
            if hit["osm_id"] or fresh or self.offline:
                return hit if hit["osm_id"] else None
        if self.offline:
            return None
        best = None
        for variant, max_km in name_variants(name):
            for f in self._query(variant):
                if f.get("railway") not in RAILWAY_KINDS or f.get("latitude") is None:
                    continue
                d = km_between(lat, lon, f["latitude"], f["longitude"])
                if d <= max_km and (best is None or d < best[0]):
                    best = (d, f)
            if best:
                break
        row = {"gtfs_name": name, "gtfs_lat": k[1], "gtfs_lon": k[2], "checked": date.today().isoformat(),
               "osm_id": "", "osm_name": "", "uic_ref": "", "lat": "", "lon": "", "railway": "", "km": ""}
        if best:
            d, f = best
            uic = re.sub(r"\D", "", str(f.get("uic_ref") or ""))
            row.update({"osm_id": str(f["osm_id"]), "osm_name": f.get("name") or name,
                        "uic_ref": uic if len(uic) == 7 else "", "lat": f"{f['latitude']:.6f}",
                        "lon": f"{f['longitude']:.6f}", "railway": f.get("railway", ""), "km": f"{d:.3f}"})
        self.cache[k] = row
        if len(self.cache) % 50 == 0:
            self.save()  # sauvegarde régulière : le premier passage est long (1 requête/s)
        return row if row["osm_id"] else None

    def save(self):
        if not self.cache:
            return
        df = pd.DataFrame(list(self.cache.values()), columns=CACHE_COLUMNS).sort_values(["gtfs_name", "gtfs_lat"])
        df.to_csv(CACHE_CSV, sep=";", index=False, encoding="utf-8")


def extra_station_row(f: dict) -> dict:
    """Ligne stations_extra.csv (format stations.csv) pour une gare OSM absente de stations.csv."""
    country, tz = UIC_COUNTRIES.get(f["uic_ref"][:2], ("", ""))
    return {"id": f"OSM{f['osm_id']}", "name": f["osm_name"], "uic": f["uic_ref"], "latitude": f["lat"],
            "longitude": f["lon"], "country": country, "time_zone": tz, "is_city": "f",
            "source": "openstreetmap"}


def write_extra(rows: dict):
    """Fusionne les gares ajoutées avec celles déjà présentes dans stations_extra.csv."""
    cols = ["id", "name", "uic", "latitude", "longitude", "country", "time_zone", "is_city", "source"]
    old = pd.read_csv(EXTRA_CSV, sep=";", dtype=str, keep_default_na=False) if os.path.exists(EXTRA_CSV) else \
        pd.DataFrame(columns=cols)
    new = pd.DataFrame(list(rows.values()), columns=cols)
    df = pd.concat([old, new]).drop_duplicates("id", keep="last").sort_values("id")
    df.to_csv(EXTRA_CSV, sep=";", index=False, encoding="utf-8")
