"""
Accès aux sources ouvertes du référentiel des gares, avec cache disque (reprise après interruption,
et aucune requête pour ce qui est déjà connu) :

  - OpenRailwayMap (données OpenStreetMap, ODbL) : https://api.openrailwaymap.org/v2/facility
      recherche par uic_ref (exacte) ou par nom ; renvoie toutes les étiquettes OSM de la gare
      (name, name:fr, name:en, uic_ref, ref:FR:uic8, ref:IFOPT, railway:ref, wikidata, addr:city...)
  - Wikidata (CC0) : API wbgetentities, 50 éléments par appel
      libellés fr/en, commune (P131), coordonnées (P625), pays (P17), nature (P31)
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
USER_AGENT = "TrainNomad/1.0 (+https://trainnomad.eu) referentiel-gares"

ORM_URL = "https://api.openrailwaymap.org/v2/facility"
ORM_DELAY_S = 1.0       # politesse envers l'API publique
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"

# mode hors ligne (--offline) : uniquement le cache, aucune requête ; ce qui manque est rendu comme
# "pas encore cherché" (None pour OpenRailwayMap, {} pour Nominatim / Wikidata)
OFFLINE = False


def http_json(url: str, tries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.warning(f"   {url[:90]}... : {e}")
            time.sleep(5 * (attempt + 1))
    return None


class JsonCache:
    def __init__(self, name: str):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, name)
        self.data = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        self.dirty = 0

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        self.dirty += 1
        if self.dirty >= 50:
            self.save()

    def save(self):
        if not self.dirty:
            return
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = 0


class OpenRailwayMap:
    """Recherche de gares OSM. Les réponses (même vides) sont gardées dans cache/orm.json."""

    def __init__(self):
        self.cache = JsonCache("orm.json")
        self.requests = 0

    def facility(self, **params) -> list:
        key = urllib.parse.urlencode(sorted(params.items()))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if OFFLINE:
            return None
        time.sleep(ORM_DELAY_S)
        self.requests += 1
        res = http_json(ORM_URL + "?" + urllib.parse.urlencode({**params, "limit": 10}))
        if res is None:
            return []  # échec réseau : pas mis en cache, sera réessayé
        self.cache.set(key, res)
        return res

    def save(self):
        self.cache.save()


def _claim_ids(entity, prop):
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            out.append(v["id"])
    return out


def _claim_texts(entity, prop):
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, str) and c.get("rank") != "deprecated":
            out.append(v.strip())
    return out


def _coord(entity):
    for c in entity.get("claims", {}).get("P625", []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "latitude" in v:
            return [v["latitude"], v["longitude"]]
    return None


class Wikidata:
    """Éléments Wikidata réduits à l'utile : {fr, en, p31, p131, p17, p279, uic, ibnr, coord}.
    Cache : cache/wikidata.json."""

    def __init__(self):
        self.cache = JsonCache("wikidata.json")
        self.requests = 0

    def get_many(self, qids, station: bool = False) -> dict:
        """station=True : il faut aussi les codes de gare (uic = P722, ibnr = P954, le numéro EVA de la
        Deutsche Bahn) ; les entrées mises en cache avant leur ajout sont relues une fois."""
        qids = [q for q in dict.fromkeys(qids) if q and q.startswith("Q")]
        missing = [] if OFFLINE else [q for q in qids if self.cache.get(q) is None
                                      or (station and self.cache.get(q) and "uic" not in self.cache.get(q))]
        for i in range(0, len(missing), 50):
            batch = missing[i:i + 50]
            url = WIKIDATA_URL + "?" + urllib.parse.urlencode({
                "action": "wbgetentities", "ids": "|".join(batch), "props": "labels|claims",
                "languages": "fr|en", "format": "json"})
            self.requests += 1
            res = http_json(url) or {}
            for q, e in res.get("entities", {}).items():
                if "missing" in e:
                    self.cache.set(q, {})
                    continue
                labels = e.get("labels", {})
                self.cache.set(q, {
                    "fr": labels.get("fr", {}).get("value", ""), "en": labels.get("en", {}).get("value", ""),
                    "p31": _claim_ids(e, "P31"), "p131": _claim_ids(e, "P131"), "p17": _claim_ids(e, "P17"),
                    "p279": _claim_ids(e, "P279"),
                    "uic": _claim_texts(e, "P722"), "ibnr": _claim_texts(e, "P954"),
                    "coord": _coord(e),
                })
            time.sleep(1.0)  # Wikidata limite le débit (HTTP 429)
        return {q: self.cache.get(q) or {} for q in qids}

    def save(self):
        self.cache.save()


class Nominatim:
    """Géocodage OpenStreetMap : commune qui contient un point.
    https://nominatim.org/release-docs/latest/api/Reverse/ — 1 requête/s maximum (règle d'usage).
    Cache : cache/nominatim.json ("lat,lon" arrondis à 4 décimales ; "ville:<nom>|<pays>" pour l'identifiant
    Wikidata d'une commune).

    La commune est lue dans l'adresse (city, sinon town, village, municipality) et non dans l'objet
    renvoyé, qui peut être une section de commune : la gare de Bruges est dans la section Sint-Michiels
    de la commune de Bruges. Les 19 communes de la Région de Bruxelles-Capitale donnent "Bruxelles"."""

    URL = "https://nominatim.openstreetmap.org/reverse"
    SEARCH_URL = "https://nominatim.openstreetmap.org/search"
    DELAY_S = 1.1
    CITY_KEYS = ("city", "town", "village", "municipality")
    # selon le pays, "municipality" n'a pas le même sens : commune en Belgique (Sy -> Ferrières),
    # intercommunalité en France (Les Lacs -> "Fougères-Vitré") : jamais utilisé en France
    CITY_KEYS_BY_COUNTRY = {"BE": ("municipality", "city", "town", "village"),
                            # Portugal : municipality = commune (concelho) ; city/town = souvent la paroisse
                            # (freguesia) : Alcantarilha -> Silves, Aguas Santas -> Maia
                            "PT": ("municipality", "city", "town", "village"),
                            "FR": ("city", "town", "village"),
                            # Royaume-Uni : city = la grande ville (Manchester, Londres) mais aussi le district
                            # (Tameside, High Peak) : la ville (town) ou le village d'abord
                            "GB": ("town", "village", "city", "municipality")}
    # Royaume-Uni : les villes n'ont pas de limite dans OSM, seulement les districts. À l'échelle de la
    # commune (zoom 10) Crewe donne "Cheshire East" ; à l'échelle de la rue (zoom 18) l'adresse contient
    # la ville : town = Crewe. Une requête à cette échelle pour ces pays.
    ZOOM_BY_COUNTRY = {"GB": 18}
    # anciennes entrées (sans adresse) : relues seulement si elles peuvent désigner une section de commune
    DISTRICT_TYPES = {"city_district", "suburb", "borough", "quarter", "neighbourhood", "hamlet"}
    REGIONS_AS_CITY = {"Bruxelles-Capitale": "Bruxelles", "Brussel-Hoofdstad": "Bruxelles",
                       "Région de Bruxelles-Capitale": "Bruxelles"}

    def __init__(self):
        self.cache = JsonCache("nominatim.json")
        self.requests = 0

    def _get(self, url, params):
        time.sleep(self.DELAY_S)
        self.requests += 1
        return http_json(url + "?" + urllib.parse.urlencode(params))

    def city(self, lat: float, lon: float, country_hint: str = "") -> dict:
        """{name, name_fr, name_en, wikidata, type, country} de la commune contenant le point ({} si inconnue).
        country_hint : pays probable de la gare (préfixe UIC, pays de l'opérateur), pour interroger
        directement à la bonne échelle."""
        if country_hint in self.ZOOM_BY_COUNTRY:
            fine = self._city_from_street(lat, lon, country_hint)
            if fine:
                return fine
        key = f"{lat:.4f},{lon:.4f}"
        hit = self.cache.get(key)
        stale = hit is not None and "address" not in hit and (
            hit.get("country") != "FR" or hit.get("type") in self.DISTRICT_TYPES)
        if hit is None or stale:  # entrées anciennes douteuses (sections de commune) : relues une fois
            if OFFLINE:
                return self._resolve(hit) if hit else {}
            res = self._get(self.URL, {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "extratags": 1,
                                       "namedetails": 1, "addressdetails": 1, "accept-language": "fr"})
            if res is None:
                return self._resolve(hit) if hit else {}  # échec réseau : pas mis en cache
            nd = res.get("namedetails") or {}
            hit = {"name": nd.get("name") or res.get("name", ""), "name_fr": res.get("name", ""),
                   "name_en": nd.get("name:en", ""), "wikidata": (res.get("extratags") or {}).get("wikidata", ""),
                   "type": res.get("addresstype", ""), "address": res.get("address") or {},
                   "country": (res.get("address") or {}).get("country_code", "").upper()}
            self.cache.set(key, hit)
        if not country_hint and hit.get("country") in self.ZOOM_BY_COUNTRY:
            fine = self._city_from_street(lat, lon, hit["country"])
            if fine:
                return fine
        return self._resolve(hit)

    def _city_from_street(self, lat: float, lon: float, country: str) -> dict | None:
        """Ville lue dans l'adresse à l'échelle de la rue (pays de ZOOM_BY_COUNTRY) ; None si le point
        n'est pas dans ce pays ou si l'adresse ne nomme aucune ville (on revient à l'échelle de la commune)."""
        zoom = self.ZOOM_BY_COUNTRY[country]
        key = f"{lat:.4f},{lon:.4f},z{zoom}"
        hit = self.cache.get(key)
        if hit is None:
            if OFFLINE:
                return None
            res = self._get(self.URL, {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": zoom,
                                       "addressdetails": 1, "accept-language": "fr"})
            if res is None:
                return None  # échec réseau : pas mis en cache
            addr = res.get("address") or {}
            hit = {"address": addr, "country": addr.get("country_code", "").upper()}
            self.cache.set(key, hit)
        if hit.get("country") != country:
            return None
        keys = self.CITY_KEYS_BY_COUNTRY.get(country, self.CITY_KEYS)
        used = next((k for k in keys if hit["address"].get(k)), "")
        if not used:
            return None
        commune = hit["address"][used]
        out = {"name": commune, "name_fr": commune, "name_en": "", "type": "commune", "country": country,
               "wikidata": self.city_wikidata(commune, country)}
        # un "village" peut être un quartier d'une grande ville (Selly Oak : village Metchley, city
        # Birmingham) : la ville est proposée, commun.city_of la retient si c'en est vraiment une
        if used == "village" and hit["address"].get("city"):
            out["alt_city"] = hit["address"]["city"]
        return out

    def _resolve(self, hit: dict) -> dict:
        addr = hit.get("address")
        if addr is None:  # entrée ancienne : objet renvoyé tel quel (commune en France)
            return hit
        keys = self.CITY_KEYS_BY_COUNTRY.get(hit.get("country", ""), self.CITY_KEYS)
        commune = next((addr[k] for k in keys if addr.get(k)), "")
        commune = self.REGIONS_AS_CITY.get(addr.get("county", ""), commune) or self.REGIONS_AS_CITY.get(
            addr.get("state", ""), commune)
        if not commune or (commune == hit.get("name_fr") and hit.get("wikidata")):
            return hit  # l'objet renvoyé est déjà la commune (Paris, Édimbourg...)
        qid = self.city_wikidata(commune, hit.get("country", ""))
        out = {"name": commune, "name_fr": commune, "name_en": "", "wikidata": qid, "type": "commune",
               "country": hit.get("country", "")}
        # l'objet renvoyé est parfois la commune elle-même, classée "quartier" alors que l'adresse nomme un
        # village voisin (Flamatt : objet Wünnewil-Flamatt, village Niedermettlen) : commun.city_of tranche
        if hit.get("wikidata") and hit.get("type") in self.DISTRICT_TYPES:
            out["objet_wikidata"] = hit["wikidata"]
        return out

    def city_wikidata(self, name: str, country: str) -> str:
        """Identifiant Wikidata d'une commune, par recherche Nominatim (nom + pays)."""
        key = f"ville:{name}|{country}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if OFFLINE:
            return ""
        res = self._get(self.SEARCH_URL, {"city": name, "countrycodes": country.lower(), "format": "jsonv2",
                                          "extratags": 1, "limit": 3, "accept-language": "fr"})
        if res is None:
            return ""
        qid = next(((r.get("extratags") or {}).get("wikidata", "") for r in res
                    if (r.get("extratags") or {}).get("wikidata")), "")
        self.cache.set(key, qid)
        return qid

    def save(self):
        self.cache.save()


class Overpass:
    """Recherche directe dans OpenStreetMap (Overpass) des gares autour d'un point : points, surfaces
    et relations. Utilisée quand OpenRailwayMap ne trouve rien : son API ne connaît que les gares
    dessinées comme un point, alors qu'Épinal ou Charleville-Mézières sont dessinées comme une surface.
    Cache : cache/overpass.json. Les étiquettes sont rendues au format OpenRailwayMap
    (name, uic_ref, wikidata..., latitude, longitude, osm_id)."""

    SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
               "https://overpass.private.coffee/api/interpreter"]
    DELAY_S = 5.0     # le serveur public limite le débit : une requête toutes les 5 s
    WAIT_429_S = 45   # "trop de requêtes" : on attend avant de réessayer
    MAX_FAILURES = 8  # gares sans réponse sur un passage : serveurs saturés, Overpass abandonné pour ce passage
    DISABLED = True  # activé par --avec-overpass

    def __init__(self):
        self.cache = JsonCache("overpass.json")
        self.requests = 0
        self.failures = 0

    def stations_around(self, lat: float, lon: float, radius: int = 1500):
        key = f"{lat:.4f},{lon:.4f},{radius}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if OFFLINE or self.DISABLED or self.failures >= self.MAX_FAILURES:
            return None
        q = (f'[out:json][timeout:60];nwr["railway"~"^(station|halt)$"](around:{radius},{lat},{lon});'
             f'out center tags;')
        for attempt in range(3):
            url = self.SERVERS[0]  # les miroirs répondent en erreur 500 : serveur principal seulement
            time.sleep(self.DELAY_S)
            self.requests += 1
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=90) as r:
                    res = json.load(r)
            except urllib.error.HTTPError as e:
                logging.warning(f"   Overpass ({url.split('/')[2]}) : {e}")
                if e.code == 429:  # "trop de requêtes" : on laisse le serveur souffler puis on réessaie
                    time.sleep(self.WAIT_429_S)
                continue
            except (urllib.error.URLError, OSError, ValueError) as e:
                logging.warning(f"   Overpass ({url.split('/')[2]}) : {e}")
                continue
            out = []
            for e in res.get("elements", []):
                c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
                if c.get("lat") is None:
                    continue
                tags = dict(e.get("tags", {}))
                tags.update({"latitude": c["lat"], "longitude": c["lon"],
                             "osm_id": str(e["id"]) if e["type"] == "node" else f"{e['type']}/{e['id']}"})
                out.append(tags)
            self.cache.set(key, out)
            return out
        self.failures += 1
        if self.failures == self.MAX_FAILURES:
            logging.warning("   Overpass saturé : abandonné pour ce passage (les gares concernées seront "
                            "retentées au prochain lancement)")
        return None  # serveurs indisponibles : pas mis en cache, réessayé au prochain passage

    def save(self):
        self.cache.save()

