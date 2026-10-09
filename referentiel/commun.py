"""
Partie commune du référentiel des gares TrainNomad : chaque opérateur a son script
(build_gares_<op>.py) qui lit son GTFS et fournit une liste de gares ; ce module les enrichit
(OpenStreetMap, Nominatim, Wikidata) et écrit les fichiers de l'opérateur :

  operateurs/<OP>/gares.csv    une ligne par gare de l'opérateur
  operateurs/<OP>/villes.csv   villes de ces gares
  operateurs/<OP>/codes.csv    identifiants (codes de l'opérateur, UIC, OSM, Wikidata...)
  operateurs/<OP>/rapport.csv  gares à relire

fusion.py réunit ensuite tous les opérateurs en une seule base (gares.csv, villes.csv, codes.csv) :
on peut ainsi ajouter ou refaire un pays sans relancer les autres.

Gare fournie par un opérateur (dict) :
  cle        identifiant unique dans le flux (ex. UIC8 pour la SNCF)
  uic        UIC à 7 chiffres, "" si inconnu : la gare prend alors l'uic_ref de son objet OSM s'il en a un
  code_osm   facultatif : (étiquette OSM, valeur) qui confirme la gare trouvée par son nom, même si le nom
             diffère (Royaume-Uni : ("ref:crs", "EDB") pour Edinburgh) -> statut "code"
  uic_pays   facultatif : pays où cet UIC est valable quand il est déduit d'une formule
             (Renfe : "71" + code interne, vrai seulement en Espagne). Si la gare est dans un autre
             pays (Lyon Part-Dieu, Porto Campanhã), l'UIC est écarté : la gare prend l'id de son objet
             OSM et fusion.py la réunit avec celle de l'opérateur du pays
  uic8       UIC à 8 chiffres (SNCF), facultatif
  nom_gtfs   nom dans le GTFS
  lat, lon   coordonnées GTFS
  type       "gare" (au moins un train), "arret_car" ou "port" (ferry seulement)
  trains     catégories de trains, séparées par |
  cars       catégories de cars, séparées par |
  codes      [(source, code), ...] identifiants de l'opérateur (ex. ("SNCF", "StopArea:OCE87686006"))
"""
import logging
import math
import os
import re
import sys
import unicodedata

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
OPERATEURS_DIR = os.path.join(BASE_DIR, "operateurs")
sys.path.insert(0, BASE_DIR)
import sources  # noqa: E402
from sources import Nominatim, OpenRailwayMap, Overpass, Wikidata  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GARES_COLUMNS = ["id", "type", "nom_fr", "nom_en", "nom_local", "uic", "uic8", "lat", "lon", "pays", "fuseau",
                 "ville_id", "trains", "cars", "statut", "osm_id", "wikidata", "ifopt", "code_db", "cle", "nom_gtfs"]
VILLES_COLUMNS = ["id", "nom_fr", "nom_en", "pays", "lat", "lon"]
RAPPORT_COLUMNS = ["id", "cle", "nom_gtfs", "nom_fr", "statut", "km_osm", "nom_osm", "ville", "ville_source",
                   "a_faire"]

RAILWAY_KINDS = {"station", "halt"}
UIC_MAX_KM = 5.0     # un uic_ref mal saisi dans OSM ne doit pas déplacer la gare
NAME_MAX_KM = 2.0
NAME_CONTAINS_MAX_KM = 1.0  # nom OSM seulement contenu dans celui du GTFS (ou l'inverse)

UIC_COUNTRY = {"87": "FR", "80": "DE", "85": "CH", "88": "BE", "82": "LU", "83": "IT", "71": "ES",
               "84": "NL", "81": "AT", "70": "GB", "94": "PT", "51": "PL", "54": "CZ", "55": "HU",
               "56": "SK", "79": "SI", "78": "HR", "86": "DK", "74": "SE", "76": "NO"}
WIKIDATA_COUNTRY = {"Q142": "FR", "Q183": "DE", "Q39": "CH", "Q31": "BE", "Q32": "LU", "Q38": "IT",
                    "Q29": "ES", "Q55": "NL", "Q40": "AT", "Q145": "GB", "Q45": "PT", "Q235": "MC",
                    "Q347": "LI", "Q228": "AD", "Q36": "PL", "Q213": "CZ", "Q28": "HU", "Q214": "SK",
                    "Q215": "SI", "Q224": "HR", "Q35": "DK", "Q34": "SE", "Q20": "NO", "Q27": "IE"}
TIMEZONES = {"FR": "Europe/Paris", "DE": "Europe/Berlin", "CH": "Europe/Zurich", "BE": "Europe/Brussels",
             "LU": "Europe/Luxembourg", "IT": "Europe/Rome", "ES": "Europe/Madrid", "NL": "Europe/Amsterdam",
             "AT": "Europe/Vienna", "GB": "Europe/London", "PT": "Europe/Lisbon", "MC": "Europe/Monaco",
             "LI": "Europe/Vaduz", "AD": "Europe/Andorra", "PL": "Europe/Warsaw", "CZ": "Europe/Prague",
             "HU": "Europe/Budapest", "SK": "Europe/Bratislava", "SI": "Europe/Ljubljana",
             "HR": "Europe/Zagreb", "DK": "Europe/Copenhagen", "SE": "Europe/Stockholm",
             "NO": "Europe/Oslo", "IE": "Europe/Dublin"}


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * 111.2, (lon1 - lon2) * 111.2 * math.cos(math.radians(lat1)))


def fold_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def fold(s: str) -> str:
    """Comparaison de noms : sans accents, casse, espaces ni ponctuation ; les saints s'écrivent tous
    de la même façon ("Marseille St Charles" chez Renfe = "Marseille-Saint-Charles", "Venezia S.Lucia"
    chez Trenitalia = "Venezia Santa Lucia", "S.Antonino" = "Sant'Antonino")."""
    s = re.sub(r"\b(sainte|saint|santa|santo|sant|san|ste|st)\b\.?|\bs\.", "st", fold_name(s))
    s = s.replace("hauptbahnhof", "hbf")  # "Essen Hauptbahnhof" = "Essen Hbf"
    return re.sub(r"[^a-z0-9]", "", s)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold_name(s)).strip("-")


# ---------------------------------------------------------------------------
# OpenStreetMap
# ---------------------------------------------------------------------------

def best_facility(results, lat, lon, max_km):
    best = None
    for f in results or []:
        if f.get("railway") not in RAILWAY_KINDS or f.get("latitude") is None:
            continue
        d = km(lat, lon, f["latitude"], f["longitude"])
        if d <= max_km and (best is None or d < best[0]):
            best = (d, f)
    return best


def same_osm_code(f: dict, st: dict) -> bool:
    """La gare OSM porte le code que l'opérateur donne à cet arrêt (code_osm)."""
    tag, value = st.get("code_osm") or ("", "")
    return bool(value) and str(f.get(tag) or "").strip().upper() == value.upper()


def match_around(results, st: dict, lat: float, lon: float):
    """Gare OSM autour du point : même UIC d'abord, sinon même nom (ou nom contenu l'un dans l'autre)."""
    cands = [(km(lat, lon, f["latitude"], f["longitude"]), f) for f in results or []]
    same_uic = [c for c in cands if st["uic"] and str(c[1].get("uic_ref", "")).strip() == st["uic"]]
    if same_uic:
        d, f = min(same_uic, key=lambda c: c[0])
        return f, "uic", d
    same_code = [c for c in cands if same_osm_code(c[1], st)]
    if same_code:
        d, f = min(same_code, key=lambda c: c[0])
        return f, "code", d
    g = fold(st["nom_gtfs"])
    same_name = []
    for d, f in cands:
        o = fold(f.get("name", ""))
        if (o == g and d <= NAME_MAX_KM) or (o and (o in g or g in o) and d <= NAME_CONTAINS_MAX_KM):
            same_name.append((d, f))
    if same_name:
        d, f = min(same_name, key=lambda c: c[0])
        return f, "nom", d
    return None, "gtfs", None


def find_osm(orm: OpenRailwayMap, st: dict, search_name: str, overpass: Overpass | None = None,
             chercher_uic: bool = True):
    """(étiquettes OSM, statut, distance) : par UIC (exact), sinon par nom à moins de 2 km ; pour une
    gare, si OpenRailwayMap ne trouve rien, recherche directe dans OSM autour du point (Overpass).
    statut : "uic", "code" (code de l'opérateur), "nom", "gtfs" (absente d'OSM) ou "en_attente"
    (hors ligne, pas encore cherchée)."""
    f, statut, d = _find_orm(orm, st, search_name, chercher_uic)
    if statut == "gtfs" and overpass is not None and st["type"] == "gare":
        res = overpass.stations_around(st["lat"], st["lon"])
        if res is not None:
            return match_around(res, st, st["lat"], st["lon"])
    return f, statut, d


def _find_orm(orm: OpenRailwayMap, st: dict, search_name: str, chercher_uic: bool = True):
    if st["uic"] and chercher_uic:
        res = orm.facility(uic_ref=st["uic"])
        if res is None:
            return None, "en_attente", None
        hit = best_facility(res, st["lat"], st["lon"], UIC_MAX_KM)
        if hit:
            return hit[1], "uic", hit[0]
    if st["type"] != "gare":
        return None, "gtfs", None  # arrêt de car : jamais rattaché à une gare par son nom
    # search_name peut être une liste : le nom du flux, puis des écritures plus simples essayées tant
    # que rien n'est trouvé (la recherche exige tous les mots : "S.Antonino Vaie" ne trouve rien,
    # "Antonino Vaie" trouve "Sant'Antonino-Vaie")
    # Un élément peut aussi être une autre recherche de l'API, par référence : {"ref": "KGX"}.
    for name in ([search_name] if isinstance(search_name, (str, dict)) else search_name):
        res = orm.facility(**(name if isinstance(name, dict) else {"name": name}))
        if res is None:
            return None, "en_attente", None
        found = _match_by_name(res, st)
        if found[0]:
            return found
    return None, "gtfs", None


def _match_by_name(res: list, st: dict):
    hit = best_facility([f for f in res if same_osm_code(f, st)], st["lat"], st["lon"], UIC_MAX_KM)
    if hit:
        return hit[1], "code", hit[0]
    # la recherche par nom renvoie aussi des noms approchants ("Limoux" -> "Limoux-Flassian", une autre
    # halte) : nom identique jusqu'à 2 km, nom contenu dans l'autre jusqu'à 1 km seulement
    g = fold(st["nom_gtfs"])
    # Le nom identique passe avant le nom contenu, même plus proche : "Stuttgart Hbf" est la gare
    # "Stuttgart Hauptbahnhof", pas le point "Stuttgart Hbf tief" de sa partie souterraine.
    same = [f for f in res if fold(f.get("name", "")) == g]
    contains = [f for f in res if fold(f.get("name", "")) and fold(f["name"]) != g
                and (fold(f["name"]) in g or g in fold(f["name"]))]
    for group, max_km in ((same, NAME_MAX_KM), (contains, NAME_CONTAINS_MAX_KM)):
        hit = best_facility(group, st["lat"], st["lon"], max_km)
        if hit:
            return hit[1], "nom", hit[0]
    return None, "gtfs", None


# ---------------------------------------------------------------------------
# Noms et ville
# ---------------------------------------------------------------------------

def default_names(gtfs_name: str, f: dict | None, country: str, op_country: str) -> tuple[str, str, str]:
    """(nom_fr, nom_en, nom_local).
    nom_fr : gares du pays de l'opérateur -> nom du GTFS (l'opérateur écrit en français : SNCF, SNCB),
             remplacé par le nom OSM si c'est le même nom mieux écrit ("Marssac-sur-tarn") ou s'il est abrégé ;
             gares étrangères -> nom officiel local d'OSM.
    nom_en : name:en d'OSM, sinon le nom local officiel ("Antwerpen-Centraal" plutôt que "Anvers-Central") :
             pas de traduction inventée."""
    local = (f or {}).get("name") or gtfs_name
    if country == op_country or not f:
        nom_fr = local if f and (fold(local) == fold(gtfs_name) or "." in gtfs_name) else gtfs_name
    else:
        nom_fr = f.get("official_name") or f.get("uic_name") or local
    nom_en = (f or {}).get("name:en") or local or nom_fr
    return nom_fr, nom_en, local


# Natures Wikidata (P31) d'une commune, et des divisions plus petites qu'une commune.
# Le géocodage rend parfois une paroisse portugaise (freguesia) ou une section de commune belge au lieu
# de la commune : soit OSM n'indique pas la commune à cet endroit (Pinhão, Poulseur), soit la recherche
# par nom tombe sur la paroisse qui porte le nom de la commune (Castelo Branco, Águeda).
COMMUNE_TYPES = {
    "Q13217644",   # municipalité du Portugal
    "Q493522",     # commune de Belgique
    "Q15273785",   # commune belge avec le titre de ville
    "Q484170",     # commune française
    "Q70208",      # commune suisse
    "Q54935504",   # commune suisse (ville)
}
SUB_COMMUNE_TYPES = {
    "Q1131296",    # freguesia (paroisse portugaise)
    "Q56046844",   # ancienne freguesia portugaise
    "Q2785216",    # section de commune (Belgique)
}


def commune_of(wd: Wikidata, qid: str) -> str:
    """Remonte d'une paroisse ou d'une section à sa commune par Wikidata ("situé dans", P131) :
    Freguesia de Águeda -> Águeda, Ermesinde -> Valongo, Poulseur -> Comblain-au-Pont.
    Ne change rien si l'élément est déjà une commune ou si aucune commune connue ne le contient."""
    for _ in range(2):  # paroisse -> (union de paroisses) -> commune
        e = wd.get_many([qid]).get(qid) or {}
        if set(e.get("p31", [])) & COMMUNE_TYPES:
            return qid
        parents = e.get("p131", [])[:4]
        if not parents:
            return qid
        items = wd.get_many(parents)
        up = next((q for q in parents if set((items.get(q) or {}).get("p31", [])) & COMMUNE_TYPES), None)
        if up:
            return up
        sub = next((q for q in parents if set((items.get(q) or {}).get("p31", [])) & SUB_COMMUNE_TYPES), None)
        if not sub:
            return qid
        qid = sub
    return qid


# Natures Wikidata (P31) d'une vraie ville, par opposition à un district administratif (High Peak,
# Tameside) : ville, grande ville, métropole, capitale, district ayant le statut de cité (Birmingham,
# Manchester), chef-lieu de comté, ville de marché.
CITY_LIKE_TYPES = {"Q515", "Q1549591", "Q3957", "Q200250", "Q5119", "Q21503295", "Q1357964", "Q18511725"}
DISTRICT_PREFIX = re.compile(r"^(district métropolitain|borough londonien|borough|cité|metropolitan borough|"
                             r"london borough|municipal borough|city|royal borough)\s+(de|d'|of)\s*", re.I)


def plain_city_name(name: str) -> str:
    """ "district métropolitain de Liverpool" -> "Liverpool", "Cité de Wakefield" -> "Wakefield" """
    return DISTRICT_PREFIX.sub("", name or "") or name


def city_of(nominatim: Nominatim, wd: Wikidata, lat: float, lon: float,
            country_hint: str = "") -> tuple[str, dict]:
    """(QID, {fr, en, coord, p17, country}) de la ville dont la limite contient le point."""
    c = nominatim.city(lat, lon, country_hint)
    obj = c.get("objet_wikidata")
    if obj and obj != c.get("wikidata") and set((wd.get_many([obj]).get(obj) or {}).get("p31", [])) & COMMUNE_TYPES:
        c = {**c, "wikidata": obj}  # l'objet du géocodage est la commune : Wünnewil-Flamatt, Haut-Intyamon
    if c.get("alt_city"):
        # village dans le périmètre d'une ville : la ville, si c'en est vraiment une (pas un district)
        alt = nominatim.city_wikidata(c["alt_city"], c.get("country", ""))
        if alt and set((wd.get_many([alt]).get(alt) or {}).get("p31", [])) & CITY_LIKE_TYPES:
            c = {**c, "name": c["alt_city"], "name_fr": c["alt_city"], "wikidata": alt}
    qid = c.get("wikidata", "")
    if qid:
        qid = commune_of(wd, qid)
        e = wd.get_many([qid]).get(qid, {})
        if e:
            fr = e.get("fr") or e.get("en") or c.get("name_fr") or c.get("name")
            en = e.get("en") or e.get("fr") or c.get("name_en") or c.get("name")
            if c.get("country") in Nominatim.ZOOM_BY_COUNTRY:
                fr, en = plain_city_name(fr), plain_city_name(en)
            return qid, {"fr": fr, "en": en,
                         "coord": e.get("coord"), "p17": e.get("p17", []), "country": c.get("country", "")}
    if c.get("name"):
        return "", {"fr": c.get("name_fr") or c["name"], "en": c.get("name_en") or c["name"],
                    "coord": None, "p17": [], "country": c.get("country", "")}
    return "", {}


def merge_cities(gares: list, villes: dict, max_km: float = 30.0) -> int:
    """Une ville = un identifiant : une ville sans Wikidata est rattachée à la ville Wikidata de même nom
    et même pays à moins de max_km ; les villes devenues inutiles sont retirées."""
    by_name = {}
    for v in villes.values():
        if v["id"].startswith("Q") and v["lat"] not in (None, ""):
            by_name.setdefault((fold(v["nom_fr"]), v["pays"]), []).append(v)
    remap = {}
    for v in villes.values():
        if v["id"].startswith("Q") or v["lat"] in (None, ""):
            continue
        for cand in by_name.get((fold(v["nom_fr"]), v["pays"]), []):
            if km(float(v["lat"]), float(v["lon"]), float(cand["lat"]), float(cand["lon"])) <= max_km:
                remap[v["id"]] = cand["id"]
                break
    for g in gares:
        g["ville_id"] = remap.get(g["ville_id"], g["ville_id"])
    used = {g["ville_id"] for g in gares}
    for vid in list(villes):
        if vid not in used:
            del villes[vid]
    return len(remap)


# ---------------------------------------------------------------------------
# Construction des fichiers d'un opérateur
# ---------------------------------------------------------------------------

def to_do(type_: str, statut: str, nom_osm: str, nom_gtfs: str, ville_id: str) -> str:
    """Ce qu'il reste à relire pour une gare ("" si rien) : colonne a_faire du rapport."""
    a_faire = []
    if type_ == "gare" and statut == "gtfs":
        a_faire.append("gare absente d'OpenRailwayMap : relancer avec --avec-overpass")
    elif statut == "nom" and fold(nom_osm) != fold(nom_gtfs):
        a_faire.append("vérifier que la gare OSM est la bonne (nom différent)")
    elif statut == "en_attente":
        a_faire.append("pas encore cherchée (hors ligne) : relancer sans --offline")
    if not ville_id.startswith("Q"):
        a_faire.append("ville sans Wikidata : vérifier ou renseigner ville_parent")
    return " ; ".join(a_faire)


def read_previous(out_dir: str):
    """Fichiers déjà produits pour l'opérateur (mode incrémental) : gares par id, id par clé du flux,
    villes par id, codes par gare, rapport par id."""
    def rd(name):
        path = os.path.join(out_dir, name)
        return pd.read_csv(path, sep=";", dtype=str, keep_default_na=False) if os.path.exists(path) else None
    g, v, c, r = rd("gares.csv"), rd("villes.csv"), rd("codes.csv"), rd("rapport.csv")
    gares = {row["id"]: row for row in g.to_dict("records")} if g is not None else {}
    by_cle = {row["cle"]: row["id"] for row in gares.values() if row.get("cle")}
    villes = {row["id"]: row for row in v.to_dict("records")} if v is not None else {}
    codes = {}
    if c is not None:
        for row in c.to_dict("records"):
            codes.setdefault(row["gare_id"], []).append(row)
    rapport = {row["id"]: row for row in r.to_dict("records")} if r is not None else {}
    return gares, by_cle, villes, codes, rapport


def needs_work(row: dict) -> bool:
    """Gare déjà en base mais incomplète : retentée au lieu d'être reprise telle quelle."""
    if row.get("statut") == "en_attente" or not row.get("ville_id", "").startswith("Q"):
        return True
    return row.get("type") == "gare" and row.get("statut") == "gtfs" and not Overpass.DISABLED


def station_id(st: dict, f: dict | None, op: str) -> str:
    if st["uic"]:
        return f"TN{st['uic']}"
    if f and f.get("osm_id"):
        return f"TNOSM{f['osm_id']}"
    return f"TN{op}-{slug(st['cle'])}"


def build_operator(op: str, op_country: str, stations: list[dict], search_name=None, names=None,
                   complet: bool = False, chercher_uic: bool = True) -> dict:
    """Enrichit les gares d'un opérateur et écrit operateurs/<op>/*.csv.
    search_name(st) -> nom envoyé à OpenRailwayMap (par défaut nom_gtfs), ou liste de noms à essayer
    names(st, f, country) -> (nom_fr, nom_en, nom_local) (par défaut default_names)
    chercher_uic=False : pas de recherche par UIC (pays où OSM ne le porte presque jamais : une requête
    de moins par gare), la gare est cherchée par son nom et confirmée par code_osm

    Mode incrémental (par défaut) : une gare déjà en base et complète est reprise telle quelle (seuls ses
    types de trains et ses codes sont remis à jour depuis le GTFS) ; seules les gares nouvelles ou
    incomplètes sont cherchées. complet=True (--complet) recalcule toutes les gares (depuis le cache)."""
    search_name = search_name or (lambda st: st["nom_gtfs"])
    names = names or (lambda st, f, country: default_names(st["nom_gtfs"], f, country, op_country))
    out_dir = os.path.join(OPERATEURS_DIR, op)
    os.makedirs(out_dir, exist_ok=True)
    orm, wd, nominatim, overpass = OpenRailwayMap(), Wikidata(), Nominatim(), Overpass()

    gares, villes, codes, rapport = [], {}, [], []
    prev_g, prev_cle, prev_v, prev_c, prev_r = read_previous(out_dir)
    op_sources = {src for st in stations for src, _ in st["codes"]}
    work, reserved = [], set()
    for st in stations:
        pid = prev_cle.get(st["cle"]) or (f"TN{st['uic']}" if st["uic"] else None)
        row = prev_g.get(pid) if pid else None
        if complet or row is None or needs_work(row):
            work.append(st)
            continue
        # gare déjà en base et complète : reprise sans calcul ni appel
        row = dict(row)
        row.update({"type": st["type"], "trains": st["trains"], "cars": st["cars"], "uic8": st.get("uic8", ""),
                    "cle": st["cle"], "nom_gtfs": st["nom_gtfs"]})
        gares.append(row)
        if row["osm_id"]:
            reserved.add(str(row["osm_id"]))
        if row["ville_id"] in prev_v:
            villes[row["ville_id"]] = prev_v[row["ville_id"]]
        codes += [{"gare_id": pid, "source": c["source"], "code": c["code"]} for c in prev_c.get(pid, [])
                  if c["source"] not in op_sources]
        codes += [{"gare_id": pid, "source": src, "code": c} for src, c in st["codes"]]
        a_faire = to_do(row["type"], row["statut"], row["nom_local"], st["nom_gtfs"], row["ville_id"])
        if a_faire:
            old = prev_r.get(pid, {})
            rapport.append({"id": pid, "cle": st["cle"], "nom_gtfs": st["nom_gtfs"], "nom_fr": row["nom_fr"],
                            "statut": row["statut"], "km_osm": old.get("km_osm", ""), "nom_osm": row["nom_local"],
                            "ville": villes.get(row["ville_id"], {}).get("nom_fr", ""),
                            "ville_source": "wikidata", "a_faire": a_faire})
    reused = len(stations) - len(work)
    logging.info(f"   {reused} gares reprises telles quelles, {len(work)} à chercher"
                 + (" (--complet)" if complet else ""))
    stations = work
    try:
        # 1. gare OSM de chaque arrêt
        matches = []
        for n, st in enumerate(stations, 1):
            if n % 100 == 0:
                logging.info(f"   recherche OSM {n}/{len(stations)} ({orm.requests} requêtes OpenRailwayMap, "
                             f"{overpass.requests} Overpass)")
            matches.append(find_osm(orm, st, search_name(st), overpass, chercher_uic))
        # un objet OSM = un seul arrêt du flux : en cas de conflit (Redondela et Redondela-Picota), on garde
        # le meilleur rattachement (UIC, puis nom identique, puis le plus proche) ; les autres gardent le GTFS
        claims = {}
        for i, (f, statut, dist) in enumerate(matches):
            if f and f.get("osm_id"):
                exact = fold(f.get("name", "")) == fold(stations[i]["nom_gtfs"])
                rank = (statut not in ("uic", "code"), not exact, dist or 0)
                # clé en texte : OpenRailwayMap rend l'id en nombre, Overpass en chaîne
                claims.setdefault(str(f["osm_id"]), []).append((rank, i))
        for osm_id, lst in claims.items():
            keep = 0 if osm_id in reserved else 1  # objet déjà pris par une gare reprise : aucun
            for _, i in sorted(lst)[keep:]:
                matches[i] = (None, "gtfs", None)

        # 2. ville, noms, codes
        for n, (st, (f, statut, dist)) in enumerate(zip(stations, matches), 1):
            if n % 100 == 0:
                logging.info(f"   villes {n}/{len(stations)} ({nominatim.requests} requêtes Nominatim, "
                             f"{wd.requests} Wikidata)")
            qid = (f or {}).get("wikidata", "")
            wd_station = wd.get_many([qid]).get(qid, {}) if qid else {}

            glat = float((f or {}).get("latitude", st["lat"]))
            glon = float((f or {}).get("longitude", st["lon"]))
            city_qid, city = city_of(nominatim, wd, glat, glon, UIC_COUNTRY.get(st["uic"][:2], "") or op_country)
            # pays du géocodage d'abord : le P17 de Wikidata liste aussi les anciens pays (Florence et
            # Worms ont été françaises sous l'Empire, et sortaient en France)
            country = (city.get("country", "")
                       or next((WIKIDATA_COUNTRY[q] for q in (city.get("p17") or []) + (wd_station.get("p17") or [])
                                if q in WIKIDATA_COUNTRY), "")
                       or (f or {}).get("addr:country", "") or UIC_COUNTRY.get(st["uic"][:2], ""))
            if st["uic"] and st.get("uic_pays") and country and country != st["uic_pays"]:
                st = {**st, "uic": "", "uic8": ""}  # UIC de formule, faux hors de son pays
            ibnr = []
            if not st["uic"] and f:
                # pas d'UIC dans le flux : celui de la gare dans Wikidata (P722), sinon celui de l'objet OSM,
                # s'il est bien du pays de la gare. En Allemagne l'uic_ref d'OSM est tantôt l'UIC, tantôt
                # le numéro EVA de la DB (Hannover Hbf : UIC 8013552, EVA 8000152) : Wikidata donne les
                # deux séparément (P954 = EVA), l'EVA n'est jamais pris comme UIC
                wd_codes = wd.get_many([qid], station=True).get(qid, {}) if qid else {}
                ibnr = [c for c in wd_codes.get("ibnr", []) if re.fullmatch(r"\d{7}", c)]
                osm_uic = re.sub(r"\D", "", str(f.get("uic_ref") or ""))
                found = next((c for c in wd_codes.get("uic", []) if re.fullmatch(r"\d{7}", c)), "") \
                    or ("" if osm_uic in ibnr else osm_uic)
                if len(found) == 7 and UIC_COUNTRY.get(found[:2], country) == country:
                    st = {**st, "uic": found}
            gid = station_id(st, f, op)
            nom_fr, nom_en, local = names(st, f, country)
            if fold(nom_en) != fold(local) and fold(nom_en) in (fold(city.get("en") or ""), fold(city.get("fr") or "")):
                nom_en = local  # name:en d'OSM réduit à la ville ("Rome" pour Roma Termini) : nom local

            if city_qid:
                ville_source, ville_id = "wikidata", city_qid
                coord = city.get("coord") or [None, None]
                villes[ville_id] = {"id": ville_id, "nom_fr": city.get("fr") or city.get("en"),
                                    "nom_en": city.get("en") or city.get("fr"), "pays": country,
                                    "lat": coord[0] if coord[0] is not None else glat,
                                    "lon": coord[1] if coord[1] is not None else glon}
            elif city:
                ville_source, ville_id = "nominatim", f"V{country}-{slug(city['fr'])}"
                villes.setdefault(ville_id, {"id": ville_id, "nom_fr": city["fr"], "nom_en": city["en"],
                                             "pays": country, "lat": glat, "lon": glon})
            elif (f or {}).get("addr:city"):
                ville_source, ville_id = "osm", f"V{country}-{slug(f['addr:city'])}"
                villes.setdefault(ville_id, {"id": ville_id, "nom_fr": f["addr:city"], "nom_en": f["addr:city"],
                                             "pays": country, "lat": glat, "lon": glon})
            else:
                # hors ligne sans géocodage en cache : ville provisoire, complétée au prochain passage
                ville_source = "à compléter" if sources.OFFLINE else "gare"
                ville_id = f"V{gid}"
                villes[ville_id] = {"id": ville_id, "nom_fr": nom_fr, "nom_en": nom_en, "pays": country,
                                    "lat": glat, "lon": glon}

            gares.append({
                "id": gid, "type": st["type"], "nom_fr": nom_fr, "nom_en": nom_en, "nom_local": local,
                "uic": st["uic"], "uic8": st.get("uic8", ""), "lat": round(glat, 6), "lon": round(glon, 6),
                "pays": country, "fuseau": TIMEZONES.get(country, ""), "ville_id": ville_id,
                "trains": st["trains"], "cars": st["cars"], "statut": statut,
                "osm_id": (f or {}).get("osm_id", ""), "wikidata": qid, "ifopt": (f or {}).get("ref:IFOPT", ""),
                "code_db": (f or {}).get("railway:ref:DB") or ((f or {}).get("railway:ref", "") if country == "DE" else ""),
                "cle": st["cle"], "nom_gtfs": st["nom_gtfs"],
            })
            codes += [{"gare_id": gid, "source": s, "code": c} for s, c in st["codes"]]
            if st["uic"]:
                codes.append({"gare_id": gid, "source": "UIC", "code": st["uic"]})
            if st.get("uic8"):
                codes.append({"gare_id": gid, "source": "UIC8", "code": st["uic8"]})
            # second UIC de la gare, porté par son objet OSM (Londres St Pancras : 7015550 dans le fichier
            # britannique, 7015400 dans OSM et chez Eurostar) : fusion.py réunit les deux
            osm_uic = re.sub(r"\D", "", str((f or {}).get("uic_ref") or ""))
            if len(osm_uic) == 7 and osm_uic != st["uic"]:
                codes.append({"gare_id": gid, "source": "IBNR" if osm_uic in ibnr else "UIC", "code": osm_uic})
            codes += [{"gare_id": gid, "source": "IBNR", "code": c} for c in ibnr]
            for source, key in (("OSM", "osm_id"), ("WIKIDATA", "wikidata"), ("IFOPT", "ref:IFOPT"),
                                ("CRS", "ref:crs")):
                if (f or {}).get(key):
                    codes.append({"gare_id": gid, "source": source, "code": str(f[key])})
            # rapport : uniquement ce qui est à relire (un arrêt de car absent d'OSM est normal)
            a_faire = to_do(st["type"], statut, (f or {}).get("name", ""), st["nom_gtfs"],
                            ville_id if ville_source == "wikidata" else "")
            if a_faire:
                rapport.append({"id": gid, "cle": st["cle"], "nom_gtfs": st["nom_gtfs"], "nom_fr": nom_fr,
                                "statut": statut, "km_osm": round(dist, 3) if dist is not None else "",
                                "nom_osm": (f or {}).get("name", ""), "ville": villes[ville_id]["nom_fr"],
                                "ville_source": ville_source, "a_faire": a_faire})
    finally:
        orm.save()
        wd.save()
        nominatim.save()
        overpass.save()

    merge_cities(gares, villes)
    gares_df = pd.DataFrame(gares, columns=GARES_COLUMNS).drop_duplicates("id").sort_values("id")
    gares_df.to_csv(os.path.join(out_dir, "gares.csv"), sep=";", index=False, encoding="utf-8")
    pd.DataFrame(list(villes.values()), columns=VILLES_COLUMNS).sort_values("id").to_csv(
        os.path.join(out_dir, "villes.csv"), sep=";", index=False, encoding="utf-8")
    pd.DataFrame(codes, columns=["gare_id", "source", "code"]).drop_duplicates().sort_values(
        ["gare_id", "source", "code"]).to_csv(os.path.join(out_dir, "codes.csv"), sep=";", index=False, encoding="utf-8")
    pd.DataFrame(rapport, columns=RAPPORT_COLUMNS).to_csv(os.path.join(out_dir, "rapport.csv"), sep=";",
                                                          index=False, encoding="utf-8")
    stats = {"gares": len(gares_df), "reprises_telles_quelles": reused, "cherchees": len(stations),
             "villes": len(villes), "codes": len(codes),
             "requetes_openrailwaymap": orm.requests, "requetes_wikidata": wd.requests,
             "requetes_nominatim": nominatim.requests, "requetes_overpass": overpass.requests}
    stats.update({f"type_{k}": int(v) for k, v in gares_df["type"].value_counts().items()})
    stats.update({f"statut_{k}": int(v) for k, v in gares_df["statut"].value_counts().items()})
    stats["ville_wikidata"] = int(gares_df["ville_id"].str.startswith("Q").sum())
    return stats


def run(op: str, title: str, load, op_country: str, **kwargs):
    """Point d'entrée commun : python referentiel/build_gares_<op>.py [--offline] [--limit N]"""
    print("=" * 60)
    print(f"🏛️  Référentiel des gares TrainNomad — {title}")
    print("=" * 60)
    sources.OFFLINE = "--offline" in sys.argv  # uniquement le cache, aucune requête
    # recherche OSM directe autour des gares introuvables (Overpass) : seulement sur demande, ses serveurs
    # étant souvent saturés ; par défaut, une gare introuvable dans OpenRailwayMap reste "gtfs"
    sources.Overpass.DISABLED = "--avec-overpass" not in sys.argv
    stations = load()
    if "--limit" in sys.argv:  # essai sur quelques gares
        k = int(sys.argv[sys.argv.index("--limit") + 1])
        stations = pd.DataFrame(stations).sample(min(k, len(stations)), random_state=1).to_dict("records")
    stats = build_operator(op, op_country, stations, complet="--complet" in sys.argv, **kwargs)
    for k, v in stats.items():
        logging.info(f"   {k} : {v}")
    print(f"\n✅ {os.path.join(OPERATEURS_DIR, op)}  (gares, villes, codes, rapport)")
    print("➡️  Puis : python referentiel/fusion.py  (base commune de tous les opérateurs)")
