"""
Compile les GTFS (SNCF, Eurostar, Renfe, European Sleeper, SNCB, ...) en un fichier binaire unique `network.bin`
chargé tel quel en RAM par le moteur de routage Go (dossier Europe/).

Pipeline :
  1. Téléchargement des GTFS (cache local dans feeds/)
  2. Dédoublonnage des gares + regroupement par ville : base des gares TrainNomad (referentiel/gares.csv,
     villes.csv, codes.csv) ; stations.csv (UIC, uic8_sncf, renfe_id) pour les opérateurs qui n'y sont pas encore
  3. Horaires : calendriers -> bitsets de jours, heures en minutes locales de l'agence
     (le moteur convertit en UTC avec le fuseau de l'agence -> Eurostar/Londres corrects)
  4. Regroupement des trajets en "routes" RAPTOR (même suite d'arrêts, sans dépassement)
  5. Correspondances : même gare (temps de changement), à pied (< 1 km), même ville (métro/RER)
  6. Écriture de network.bin + network.bin.gz

Usage : python build_network.py [--refresh]
"""
import argparse
import gzip
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import time
import unicodedata
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_DIR = os.path.join(BASE_DIR, "feeds")
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
STATIONS_CSV = os.path.join(BASE_DIR, "stations.csv")
# gares absentes de stations.csv, ajoutées depuis OpenStreetMap (GERMANY/osm_lookup.py)
STATIONS_EXTRA_CSV = os.path.join(BASE_DIR, "stations_extra.csv")
# base des gares TrainNomad : gares.csv, villes.csv, codes.csv (referentiel/fusion.py)
REFERENTIEL_DIR = os.path.join(BASE_DIR, "referentiel")
OUT_BIN = os.path.join(BASE_DIR, "network.bin")
OUT_GZ = OUT_BIN + ".gz"
REPORT_PATH = os.path.join(BASE_DIR, "harmonization_report.json")

FORMAT_VERSION = 1
NDAYS = 128                 # jours couverts par les bitsets (2 mots de 64 bits)
DAYS_BEFORE = 1             # la fenêtre commence la veille (trains de nuit en cours)
FEED_MAX_AGE_H = 6          # réutilise un GTFS téléchargé depuis moins de 6 h

MIN_CHANGE = 10             # minutes de correspondance dans la même gare
HUB_CHANGE = 15             # ... dans les grandes gares
HUB_DAILY_DEPARTURES = 150  # seuil "grande gare" (départs moyens par jour)
WALK_MAX_KM = 1.0           # correspondance à pied entre deux gares proches
CITY_MAX_KM = 20.0          # correspondance en transport urbain dans la même ville
CITY_TRANSFER_MAX = 90      # plafond (minutes) d'une traversée de ville

NO_PICKUP = 1
NO_DROPOFF = 2

# Opérateurs dont les trajets déjà fournis par un flux chargé avant (même numéro, mêmes horaires
# sur tous leurs arrêts, mêmes jours) sont écartés : trains transfrontaliers SNCB présents dans le
# GTFS SNCF (TER Lille - Courtrai, Maubeuge - Charleroi...).
DEDUP_AGAINST_EARLIER = {"SNCB"}
# Flux sans numéro de train (Allemagne) : même dédoublonnage, mais en comparant gares et horaires
# (à TIME_TOLERANCE minutes près). Le numéro du trajet déjà connu est repris.
DEDUP_BY_TIMES = {"DB", "DB_REGIO", "FLIXTRAIN"}
TIME_TOLERANCE = 2

SNCF_TYPES = {
    "Train TER": "SNCF TER",
    "Car TER": "SNCF Car TER",
    "TGV INOUI": "SNCF TGV INOUI",
    "OUIGO": "SNCF OUIGO",
    "INTERCITES": "SNCF Intercités",
    "INTERCITES de nuit": "SNCF Intercités de nuit",
    # Trains franco-allemands 95xx (Paris - Francfort / Stuttgart / Munich, Francfort - Marseille) :
    # le GTFS SNCF les classe tous "ICE", qu'ils soient assurés en TGV INOUI ou en ICE
    "ICE": "DB SNCF en coopération",
    "Lyria": "SNCF TGV Lyria",
    "TramTrain": "SNCF Tram-train",
    "Car à réservation": "SNCF Car",
    "Navette": "SNCF Navette",
    "Train": "SNCF Train",
}

RENFE_TYPES = {
    "AVE": "Renfe AVE",
    "AVE INT": "Renfe AVE International",
    "AVLO": "Renfe Avlo",
    "ALVIA": "Renfe Alvia",
    "INTERCITY": "Renfe Intercity",
    "EUROMED": "Renfe Euromed",
    "MD": "Renfe Media Distancia",
    "REGIONAL": "Renfe Regional",
    "REG.EXP.": "Renfe Regional Exprés",
    "PROXIMDAD": "Renfe Proximidad",
    "AVANT": "Renfe Avant",
    "AVANT EXP": "Renfe Avant Exprés",
    "TRENCELTA": "Renfe Tren Celta",
}

# route_short_name du GTFS Trenitalia (PublicCode des Line NeTEx) -> type affiché
TRENITALIA_TYPES = {
    "FR": "Trenitalia Frecciarossa",
    "FA": "Trenitalia Frecciargento",
    "FB": "Trenitalia Frecciabianca",
    "FL": "Trenitalia FrecciaLink",
    "IC": "Trenitalia Intercity",
    "ICN": "Trenitalia Intercity Notte",
    "EC": "Trenitalia EuroCity",
    "EN": "Trenitalia EuroNight",
    "EXP": "Trenitalia Espresso",
    "RV": "Trenitalia Regionale Veloce",
    "REG": "Trenitalia Regionale",
    "MET": "Trenitalia Metropolitano",
    "SFM": "Trenitalia SFM",
    "BUS": "Trenitalia Bus",
}

# route_short_name du GTFS Italo -> type affiché
ITALO_TYPES = {}

# route_short_name du GTFS CP (Portugal) -> type affiché
CP_TYPES = {
    "AP": "CP Alfa Pendular",
    "IC": "CP Intercidades",
    "IR": "CP InterRegional",
    "R": "CP Regional",
    "U": "CP Urbano",
    "S": "CP Suburbano",
}

# route_short_name du GTFS Ouigo España -> type affiché
OUIGO_ES_TYPES = {
    "OUIGO": "Ouigo España",
}

# route_short_name du GTFS Suisse (SBB/CFF/FFS) -> type affiché
SWISS_TYPES = {
    # Grands acteurs - SBB S-Bahn
    "S": "SBB S-Bahn",
    "S1": "SBB S-Bahn",
    "S2": "SBB S-Bahn",
    "S3": "SBB S-Bahn",
    "S4": "SBB S-Bahn",
    "S5": "SBB S-Bahn",
    "S6": "SBB S-Bahn",
    "S7": "SBB S-Bahn",
    "S8": "SBB S-Bahn",
    "S9": "SBB S-Bahn",
    "S10": "SBB S-Bahn",
    "S11": "SBB S-Bahn",
    "S12": "SBB S-Bahn",
    "S13": "SBB S-Bahn",
    "S14": "SBB S-Bahn",
    "S15": "SBB S-Bahn",
    "S16": "SBB S-Bahn",
    "S17": "SBB S-Bahn",
    "S19": "SBB S-Bahn",
    "S20": "SBB S-Bahn",
    "S21": "SBB S-Bahn",
    "S22": "SBB S-Bahn",
    "S23": "SBB S-Bahn",
    "S24": "SBB S-Bahn",
    "S25": "SBB S-Bahn",
    "S26": "SBB S-Bahn",
    "S27": "SBB S-Bahn",
    "S28": "SBB S-Bahn",
    "S29": "SBB S-Bahn",
    "S30": "SBB S-Bahn",
    "S31": "SBB S-Bahn",
    "S32": "SBB S-Bahn",
    "S33": "SBB S-Bahn",
    "S35": "SBB S-Bahn",
    "S36": "SBB S-Bahn",
    "S37": "SBB S-Bahn",
    "S40": "SBB S-Bahn",
    "S41": "SBB S-Bahn",
    "R": "Swiss Regional",
    "RE": "Swiss RegioExpress",
    "IR": "Swiss RegioExpress",
    "IR13": "Swiss RegioExpress",
    "IR15": "Swiss RegioExpress",
    "IR16": "Swiss RegioExpress",
    "IR17": "Swiss RegioExpress",
    "IR26": "Swiss RegioExpress",
    "IR27": "Swiss RegioExpress",
    "IR35": "Swiss RegioExpress",
    "IR36": "Swiss RegioExpress",
    "IR37": "Swiss RegioExpress",
    "IR38": "Swiss RegioExpress",
    "IR46": "Swiss RegioExpress",
    "IR55": "Swiss RegioExpress",
    "IR56": "Swiss RegioExpress",
    "IR57": "Swiss RegioExpress",
    "IR65": "Swiss RegioExpress",
    "IR66": "Swiss RegioExpress",
    "IR70": "Swiss RegioExpress",
    "IR75": "Swiss RegioExpress",
    "IR90": "Swiss RegioExpress",
    "IR95": "Swiss RegioExpress",
    "TER": "Swiss TER",
    "IC": "SBB InterCity",
    "IC1": "SBB InterCity",
    "IC2": "SBB InterCity",
    "IC3": "SBB InterCity",
    "IC5": "SBB InterCity",
    "IC6": "SBB InterCity",
    "IC8": "SBB InterCity",
    "IC9": "SBB InterCity",
    "IC21": "SBB InterCity",
    "IC24": "SBB InterCity",
    "IC51": "SBB InterCity",
    "IC55": "SBB InterCity",
    "IC61": "SBB InterCity",
    "IC81": "SBB InterCity",
    "EC": "SBB EuroCity",
    "ICE": "SBB ICE",
    # Trains touristiques/montagne
    "CC": "Swiss Chemins de fer",
    "PE": "Swiss Chemin de fer",
    "RB": "Swiss Chemin de fer",
    "RJX": "SBB RailJet",
    "NJ": "SBB Night Jet",
    # Autres
    "TGV": "SBB TGV Thalys",
    "SN": "Swiss Chemin de fer",
    "EXT": "Swiss Train",
}

# GTFS grandes lignes allemand (gtfs.de) : type = marque de l'opérateur (agency_name) + catégorie
# (1er mot de route_short_name, ex. "ICE 82" -> ICE). Ex. "DB ICE", "ÖBB Railjet", "PKP EuroCity".
DB_BRANDS = {
    "DB Fernverkehr AG": "DB", "DB Fernverkehr (Codesharing)": "DB", "ÖBB": "ÖBB", "SBB": "SBB",
    "Nederlandse Spoorwegen": "NS", "PKP Intercity": "PKP", "Ceske Drahy": "ČD", "MAV": "MÁV",
    "HZZP": "HŽPP", "ZSSK": "ZSSK", "Dänische Staatsbahnen": "DSB", "SNCF": "SNCF",
    "BahnTouristikExpress": "BahnTouristikExpress",
}
DB_CATEGORIES = {
    "ICE": "ICE", "IC": "Intercity", "EC": "EuroCity", "ECE": "EuroCity-Express", "EN": "EuroNight",
    "RJ": "Railjet", "RJX": "Railjet Xpress", "NJ": "Nightjet", "TGV": "TGV",
}

# GTFS régional allemand (gtfs.de) : catégorie = lettres de la ligne ("S5" -> S, "RE1" -> RE)
DB_REGIO_TYPES = {
    "RE": "Regional-Express", "RB": "Regionalbahn", "S": "S-Bahn", "IRE": "Interregio-Express",
    "MEX": "Metropolexpress", "RS": "Regio-S-Bahn", "FEX": "Flughafen-Express", "TER": "TER",
}

# route_short_name du GTFS SNCB/NMBS (Belgique) -> type affiché
# (catégories normalisées par SNCB/ingest_sncb_gtfs.py ; S1..S64 sont regroupés sous "S")
SNCB_TYPES = {
    "IC": "SNCB InterCity",
    "L": "SNCB Local",
    "P": "SNCB Heure de pointe",
    "S": "SNCB S-Train",
    "T": "SNCB Touristique",
    "EXT": "SNCB Extra",
    "EC": "SNCB EuroCity",
    "ICE": "SNCB ICE",
    "NJ": "SNCB Nightjet",
    "TRN": "SNCB Train",
}


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return " ".join(re.sub(r"[^a-z0-9]", " ", s).split())


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def to_float(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def read_gtfs(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return None
    df = pd.read_csv(z.open(name), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip().replace("﻿", "").lower() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].str.strip()
    return df


def parse_minutes(series: pd.Series) -> np.ndarray:
    """'8:30:00' / '25:10:00' -> minutes depuis le début du jour de service."""
    # Remplacer les strings vides par "0:00:00" pour éviter les erreurs de parsing
    series = series.fillna("0:00:00").replace("", "0:00:00")
    parts = series.str.split(":", n=2, expand=True)
    return (parts[0].astype(int) * 60 + parts[1].astype(int)).to_numpy(np.int32)


def covers(stops, arr, dep, sub_stops, sub_arr, sub_dep, tol=0, missing=0) -> bool:
    """Le trajet (stops, arr, dep) dessert les arrêts de sub_* aux mêmes horaires (à tol minutes près).
    missing : nombre d'arrêts intermédiaires de sub_* que l'autre flux peut ne pas publier
    (ex. Darmstadt, desserte intérieure allemande absente du GTFS SNCF)."""
    pos = {s: i for i, s in enumerate(stops)}
    if sub_stops[0] not in pos or sub_stops[-1] not in pos:
        return False
    absent = sum(1 for s in sub_stops[1:-1] if s not in pos)
    if absent > missing or len(sub_stops) - absent < 3 and absent:
        return False
    return (all(abs(dep[pos[s]] - sub_dep[i]) <= tol for i, s in enumerate(sub_stops[:-1]) if s in pos)
            and abs(arr[pos[sub_stops[-1]]] - sub_arr[-1]) <= tol)


# ---------------------------------------------------------------------------
# Téléchargement
# ---------------------------------------------------------------------------

def fetch_feed(op: dict, refresh: bool) -> str | None:
    if op.get("gtfs_path"):
        # GTFS produit dans le dépôt par un autre workflow (ex. UK/ingest_uk_gtfs.py)
        path = os.path.join(BASE_DIR, op["gtfs_path"])
        if not os.path.exists(path):
            logging.warning(f"  [{op['id']}] {path} introuvable, opérateur ignoré")
            return None
        logging.info(f"  [{op['id']}] GTFS local {op['gtfs_path']} ({os.path.getsize(path) / 1e6:.1f} Mo)")
        return path

    os.makedirs(FEEDS_DIR, exist_ok=True)
    path = os.path.join(FEEDS_DIR, f"{op['id']}.zip")
    if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < FEED_MAX_AGE_H * 3600:
        logging.info(f"  [{op['id']}] GTFS en cache ({os.path.getsize(path) / 1e6:.1f} Mo)")
        return path

    logging.info(f"  [{op['id']}] Téléchargement {op['gtfs_url']}")
    tmp = path + ".part"
    headers = {}
    api_key = op.get("api_key") or os.environ.get(f"{op['id']}_API_KEY")
    if api_key:
        if op.get("nap_api_key_header"):
            # Format NAP (Punto de Acceso Nacional) : use ApiKey header
            headers = {
                "ApiKey": api_key,
                "accept": "application/octet-stream",
            }
        else:
            # Format standard : use Bearer token
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-API-KEY": api_key,
            }
    try:
        with requests.get(op["gtfs_url"], headers=headers, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except requests.exceptions.SSLError:
        # Certains serveurs (Eurostar) n'envoient pas leur certificat intermédiaire :
        # curl sait le récupérer, requests non.
        logging.warning(f"  [{op['id']}] Erreur SSL avec requests, nouvel essai avec curl")
        curl_cmd = ["curl", "-sSfL", "-o", tmp, op["gtfs_url"]]
        if op.get("api_key"):
            if op.get("nap_api_key_header"):
                # Format NAP
                curl_cmd.extend(["-H", f"ApiKey: {op['api_key']}", "-H", "accept: application/octet-stream"])
            else:
                # Format standard
                curl_cmd.extend(["-H", f"Authorization: Bearer {op['api_key']}", "-H", f"X-API-KEY: {op['api_key']}"])
        try:
            subprocess.run(curl_cmd, check=True)
        except subprocess.CalledProcessError as e:
            logging.warning(f"  [{op['id']}] Échec du téléchargement curl : {e}, opérateur ignoré")
            return None
    except requests.exceptions.HTTPError as e:
        logging.warning(f"  [{op['id']}] Échec HTTP {e.response.status_code} : {e}, opérateur ignoré")
        return None
    except requests.exceptions.RequestException as e:
        logging.warning(f"  [{op['id']}] Échec du téléchargement : {e}, opérateur ignoré")
        return None
    try:
        zipfile.ZipFile(tmp).testzip()
    except zipfile.BadZipFile:
        logging.warning(f"  [{op['id']}] Fichier ZIP invalide, opérateur ignoré")
        return None
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Référentiel des gares (stations.csv, format Trainline)
# ---------------------------------------------------------------------------

class StationRef:
    def __init__(self, path: str):
        logging.info("📖 Chargement de stations.csv")
        cols = ["id", "name", "uic", "uic8_sncf", "latitude", "longitude", "parent_station_id",
                "country", "time_zone", "is_city", "renfe_id", "atoc_id", "trenitalia_id", "cp_id", "cff_id", "same_as",
                "db_id"]
        # Charger uniquement les colonnes existantes
        all_cols = pd.read_csv(path, sep=";", nrows=0, encoding="utf-8").columns.tolist()
        cols = [c for c in cols if c in all_cols]
        df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, usecols=cols, encoding="utf-8")
        extra_path = os.path.join(os.path.dirname(path), "stations_extra.csv")
        if os.path.exists(extra_path):
            extra = pd.read_csv(extra_path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8")
            extra = extra[~extra["id"].isin(set(df["id"]))]
            df = pd.concat([df, extra.reindex(columns=cols, fill_value="")], ignore_index=True)
            logging.info(f"   + {len(extra)} gares de stations_extra.csv (OpenStreetMap)")
        self.rows = {}
        self.by_uic, self.by_uic8, self.by_renfe, self.by_atoc, self.by_trenitalia, self.by_cp, self.by_cff = {}, {}, {}, {}, {}, {}, {}
        self.by_db = {}  # numéro EVA DB (colonne db_id)
        for r in df.itertuples(index=False):
            self.rows[r.id] = {
                "id": r.id, "name": r.name, "lat": to_float(r.latitude), "lon": to_float(r.longitude),
                "parent": r.parent_station_id.removesuffix(".0"), "country": r.country[:2].upper(),
                "tz": r.time_zone, "is_city": r.is_city == "t", "same_as": r.same_as.removesuffix(".0"),
                "uic": r.uic,
            }
            if r.uic:
                self.by_uic[r.uic] = r.id
            if r.uic8_sncf:
                self.by_uic8[r.uic8_sncf] = r.id
            if r.renfe_id:
                self.by_renfe[r.renfe_id] = r.id
            if r.atoc_id and r.atoc_id not in self.by_atoc:
                self.by_atoc[r.atoc_id] = r.id
            if r.trenitalia_id and r.trenitalia_id not in self.by_trenitalia:
                self.by_trenitalia[r.trenitalia_id] = r.id
            if hasattr(r, "cp_id") and r.cp_id and r.cp_id not in self.by_cp:
                self.by_cp[r.cp_id] = r.id
            if hasattr(r, "cff_id") and r.cff_id and r.cff_id not in self.by_cff:
                self.by_cff[r.cff_id] = r.id
            if getattr(r, "db_id", "") and r.db_id not in self.by_db:
                self.by_db[r.db_id] = r.id
        logging.info(f"   {len(self.rows)} lignes, {len(self.by_uic)} UIC, {len(self.by_cff)} CFF")

    def canonical(self, rid: str) -> str:
        """Suit same_as puis remonte les parents non-ville : un même complexe = une gare."""
        for _ in range(6):
            row = self.rows[rid]
            if row["same_as"] and row["same_as"] in self.rows and row["same_as"] != rid:
                rid = row["same_as"]
                continue
            p = row["parent"]
            if p and p in self.rows and not self.rows[p]["is_city"] and p != rid:
                rid = p
                continue
            break
        return rid

    def city_of(self, rid: str) -> str:
        """Ancêtre le plus haut ; si ce n'est pas une ville, la gare est sa propre ville."""
        seen = set()
        while rid not in seen:
            seen.add(rid)
            p = self.rows[rid]["parent"]
            if not p or p not in self.rows:
                break
            rid = p
        return rid

    def match(self, op_id: str, stop_id: str, stop_code: str):
        op = op_id.upper()
        rid = None
        if op == "SNCF":
            digits = re.sub(r"\D", "", stop_id.rsplit("-", 1)[-1]) or re.sub(r"\D", "", stop_id)
            if len(digits) >= 8:
                rid = self.by_uic8.get(digits[-8:]) or self.by_uic.get(digits[-8:-1])
            elif len(digits) == 7:
                rid = self.by_uic.get(digits)
        elif op == "RENFE":
            rid = self.by_renfe.get(stop_id) or self.by_uic.get("71" + stop_id.zfill(5))
        elif op == "NATIONAL_RAIL":
            # arrêts = codes CRS britanniques, colonne atoc_id de stations.csv
            rid = self.by_atoc.get(stop_id) or self.by_atoc.get(stop_code)
        elif op == "TRENITALIA":
            # stop_code = UIC à 7 chiffres (830000070 -> 8300070), colonne trenitalia_id de stations.csv
            rid = self.by_trenitalia.get(stop_code) or self.by_uic.get(stop_code)
        elif op == "ITALO":
            # Même logique que Trenitalia : UIC italiens
            rid = self.by_trenitalia.get(stop_code) or self.by_uic.get(stop_code)
            if not rid and stop_id:
                # Fallback : chercher UIC dans stop_id
                m = re.search(r"\d{7}", stop_id)
                if m:
                    rid = self.by_uic.get(m.group(0))
        elif op == "CP":
            # CP Portugal : d'abord cp_id, puis UIC (94xxxxx pour Portugal, 71xxxxx pour Espagne via Elvas-Badajoz)
            rid = self.by_cp.get(stop_id)
            if not rid:
                for cand in (stop_code, stop_id):
                    if cand:
                        # Format CP : 94_31039 → 9431039, 71_37606 → 7137606
                        clean = cand.replace("_", "").replace("-", "")
                        # D'abord Portugal (94), puis Espagne (71) pour la liaison transfrontalière
                        m = re.search(r"(94|71)\d{5}", clean)
                        if m:
                            rid = self.by_uic.get(m.group(0))
                            if rid:
                                break
        elif op == "OUIGO_ES":
            # Ouigo Espagne : mêmes gares que Renfe
            rid = self.by_renfe.get(stop_id) or self.by_uic.get("71" + stop_id.zfill(5))
        elif op == "SWISS":
            # Suisse : stop_id = UIC codes (85xxxxx)
            # Extraire UIC et chercher dans stations.csv
            clean_id = stop_id.split(":")[0] if stop_id and ":" in stop_id else stop_id
            if clean_id and clean_id.startswith("85"):
                rid = self.by_uic.get(clean_id)
            if not rid:
                # Fallback 1 : rechercher UIC dans stop_id
                m = re.search(r"85\d{5}", stop_id or "")
                if m:
                    rid = self.by_uic.get(m.group(0))
            if not rid:
                # Fallback 2 : rechercher via cff_id (colonne stations.csv)
                rid = self.by_cff.get(clean_id or stop_id)
        elif op in ("DB", "DB_REGIO", "FLIXTRAIN"):
            # Allemagne / FlixTrain : pas d'UIC dans les flux ; GERMANY/ingest_de_gtfs.py et
            # FLIXTRAIN/ingest_flixtrain_gtfs.py ont rapproché chaque gare de stations.csv et écrit
            # son id Trainline dans stop_code
            rid = stop_code if stop_code in self.rows else None
        elif op == "SNCB":
            # Belgique : UIC à 7 chiffres dans le stop_id (88xxxxx en Belgique, 80/84/87/82... à l'étranger)
            #   gare "gs:nmbssncb:S8814001", quai "gs:nmbssncb:8814001_12"
            m = re.search(r"nmbssncb:S?(\d{7})(?:_|$)", stop_id or "")
            if m:
                rid = self.by_uic.get(m.group(1))
        else:
            for cand in (stop_code, stop_id):
                m = re.search(r"\d{7,8}", cand or "")
                if m:
                    d = m.group(0)
                    rid = self.by_uic.get(d[:7]) or self.by_uic8.get(d)
                    if rid:
                        break
        return self.canonical(rid) if rid else None


# ---------------------------------------------------------------------------
# Base des gares TrainNomad (referentiel/gares.csv, villes.csv, codes.csv, écrits par referentiel/fusion.py)
# ---------------------------------------------------------------------------

class Referentiel:
    """Une gare = une ligne de gares.csv, rattachée à une ville de villes.csv ; codes.csv donne tous
    les identifiants d'une gare, dont les stop_id de chaque opérateur (source = id de l'opérateur).
    Un opérateur absent de la base passe encore par stations.csv (StationRef)."""

    def __init__(self, folder: str):
        self.gares, self.villes = {}, {}
        self.by_code = {}       # (opérateur, stop_id) -> id de gare
        self.by_uic = {}        # UIC 7 chiffres -> id de gare
        self.city_by_name = defaultdict(list)  # (nom normalisé, pays) -> villes
        paths = [os.path.join(folder, f) for f in ("gares.csv", "villes.csv", "codes.csv")]
        if not all(os.path.exists(p) for p in paths):
            logging.warning(f"⚠️ {folder} incomplet : gares rapprochées avec stations.csv uniquement")
            return
        logging.info("📖 Chargement du référentiel (gares.csv, villes.csv, codes.csv)")
        gares, villes, codes = (pd.read_csv(p, sep=";", dtype=str, keep_default_na=False, encoding="utf-8")
                                for p in paths)
        for v in villes.to_dict("records"):
            self.villes[v["id"]] = {"id": v["id"], "name": v["nom_fr"], "country": v["pays"],
                                    "lat": to_float(v["lat"]), "lon": to_float(v["lon"]),
                                    "parent": v.get("ville_parent", "")}
        for v in self.villes.values():
            self.city_by_name[(normalize_name(v["name"]), v["country"])].append(v)
        for g in gares.to_dict("records"):
            self.gares[g["id"]] = {
                "id": g["id"], "name": g.get("nom_force") or g["nom_fr"], "uic": g["uic"],
                "lat": to_float(g["lat"]), "lon": to_float(g["lon"]), "country": g["pays"], "tz": g["fuseau"],
                "city": self.top_city(g["ville_id"], g["pays"]),
            }
            if g["uic"]:
                self.by_uic[g["uic"]] = g["id"]
        for gid, source, code in zip(codes["gare_id"], codes["source"], codes["code"]):
            if gid in self.gares:
                self.by_code[(source.upper(), code)] = gid
        logging.info(f"   {len(self.gares)} gares, {len(self.villes)} villes, {len(self.by_code)} codes")

    def top_city(self, vid: str, country: str) -> str:
        """Suit ville_parent (id de ville, ou nom d'une ville du même pays) : Saint-Gilles -> Bruxelles."""
        for _ in range(5):
            parent = self.villes.get(vid, {}).get("parent", "")
            if parent and parent not in self.villes:
                named = self.city_by_name.get((normalize_name(parent), country), [])
                parent = named[0]["id"] if named else ""
            if not parent or parent == vid:
                break
            vid = parent
        return vid

    def match(self, op_id: str, stop_id: str):
        return self.by_code.get((op_id.upper(), stop_id))

    def city_named(self, name: str, country: str, lat, lon, max_km: float = 30.0):
        """Ville de la base portant ce nom, dans ce pays et à proximité (gare venue de stations.csv)."""
        for v in self.city_by_name.get((normalize_name(name), country), []):
            if lat is None or v["lat"] is None or haversine_km(lat, lon, v["lat"], v["lon"]) <= max_km:
                return v
        return None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class NetworkBuilder:
    def __init__(self, operators, ref: StationRef, gares: Referentiel):
        self.operators = operators
        self.ref = ref
        self.gares = gares
        today = datetime.now(timezone.utc).date()
        self.base_date = today - timedelta(days=DAYS_BEFORE)
        self.day_dates = [self.base_date + timedelta(days=i) for i in range(NDAYS)]
        self.day_index = {d.strftime("%Y%m%d"): i for i, d in enumerate(self.day_dates)}

        self.stops = []          # canonical stops (dict)
        self.stop_key_index = {} # key -> index
        self.timezones = []
        self.types = []
        self.trips = {}          # dédoublonnage : clé -> trip dict (bitset OR)
        self.stats = defaultdict(int)

    # -- gares -------------------------------------------------------------

    def intern(self, lst, value):
        try:
            return lst.index(value)
        except ValueError:
            lst.append(value)
            return len(lst) - 1

    def stop_for(self, op_id, raw, agency_tz):
        """Retourne l'index canonique d'un arrêt GTFS (création au besoin)."""
        # 1. code de l'opérateur dans la base des gares ; 2. stations.csv, puis la base par l'UIC ;
        # 3. stations.csv seul (opérateur pas encore dans la base)
        gid = self.gares.match(op_id, raw["stop_id"])
        rid = None if gid else self.ref.match(op_id, raw["stop_id"], raw.get("stop_code", ""))
        if rid:
            gid = self.gares.by_uic.get(self.ref.rows[rid]["uic"])
        if gid:
            key = "tn:" + gid
            self.stats["stops_matched_referentiel"] += 1
        elif rid:
            key = "ref:" + rid
            self.stats["stops_matched_ref"] += 1
        else:
            key = self.geo_match(raw)
            if key:
                self.stats["stops_matched_geo"] += 1
            else:
                key = f"{op_id}:{raw['stop_id']}"
                self.stats["stops_unmatched"] += 1
        if key in self.stop_key_index:
            return self.stop_key_index[key]

        if gid:
            g = self.gares.gares[gid]
            lat = g["lat"] if g["lat"] is not None else to_float(raw.get("stop_lat"))
            lon = g["lon"] if g["lon"] is not None else to_float(raw.get("stop_lon"))
            s = {
                "id": g["uic"] or gid, "name": g["name"], "lat": lat, "lon": lon,
                "tz": g["tz"] or raw.get("stop_timezone") or agency_tz, "country": g["country"],
            }
            city = self.gares.villes.get(g["city"])
            if city:
                s.update({"city_key": "tn:" + city["id"], "city_name": city["name"],
                          "city_country": city["country"], "city_lat": city["lat"], "city_lon": city["lon"]})
            else:
                s.update({"city_key": key, "city_name": g["name"], "city_country": g["country"],
                          "city_lat": None, "city_lon": None})
        elif rid:
            row = self.ref.rows[rid]
            city_rid = self.ref.city_of(rid)
            city = self.ref.rows[city_rid]
            s = {
                "id": row["uic"] or f"TL{rid}", "name": row["name"],
                "lat": row["lat"] if row["lat"] is not None else to_float(raw.get("stop_lat")),
                "lon": row["lon"] if row["lon"] is not None else to_float(raw.get("stop_lon")),
                "tz": row["tz"] or raw.get("stop_timezone") or agency_tz,
                "country": row["country"],
                "city_key": "ref:" + city_rid, "city_name": city["name"], "city_country": city["country"],
                "city_lat": city["lat"], "city_lon": city["lon"],
            }
            # même ville que les gares de la base (pas de « Rennes » en double)
            known = self.gares.city_named(city["name"], city["country"], s["lat"], s["lon"])
            if known:
                s.update({"city_key": "tn:" + known["id"], "city_name": known["name"],
                          "city_lat": known["lat"], "city_lon": known["lon"]})
        else:
            name = raw.get("stop_name", "") or raw["stop_id"]
            if name.isupper():
                name = name.title()
            s = {
                "id": key, "name": name,
                "lat": to_float(raw.get("stop_lat")), "lon": to_float(raw.get("stop_lon")),
                "tz": raw.get("stop_timezone") or agency_tz, "country": "",
                "city_key": key, "city_name": name, "city_country": "",
                "city_lat": None, "city_lon": None,
            }
        s["norm"] = normalize_name(s["name"])
        idx = len(self.stops)
        self.stops.append(s)
        self.stop_key_index[key] = idx
        return idx

    def geo_match(self, raw):
        """Arrêt inconnu du référentiel : fusion avec une gare existante proche et de nom proche."""
        lat, lon = to_float(raw.get("stop_lat")), to_float(raw.get("stop_lon"))
        if lat is None or lon is None:
            return None
        norm = normalize_name(raw.get("stop_name", ""))
        tokens = set(norm.split())
        for key, idx in self.stop_key_index.items():
            s = self.stops[idx]
            if s["lat"] is None or abs(s["lat"] - lat) > 0.003 or abs(s["lon"] - lon) > 0.005:
                continue
            if haversine_km(lat, lon, s["lat"], s["lon"]) <= 0.25 and tokens & set(s["norm"].split()):
                return key
        return None

    # -- calendriers ---------------------------------------------------------

    def service_bits(self, z):
        bits = defaultdict(int)
        cal = read_gtfs(z, "calendar.txt")
        if cal is not None and len(cal):
            wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            day_weekday = [d.weekday() for d in self.day_dates]
            day_str = [d.strftime("%Y%m%d") for d in self.day_dates]
            for r in cal.itertuples(index=False):
                r = r._asdict()
                flags = [r.get(w, "0") == "1" for w in wd]
                b = 0
                for i in range(NDAYS):
                    if r["start_date"] <= day_str[i] <= r["end_date"] and flags[day_weekday[i]]:
                        b |= 1 << i
                bits[r["service_id"]] |= b
        cd = read_gtfs(z, "calendar_dates.txt")
        if cd is not None and len(cd):
            if "exception_type" not in cd.columns:
                cd["exception_type"] = "1"
            for sid, d, ex in zip(cd["service_id"], cd["date"], cd["exception_type"]):
                i = self.day_index.get(d)
                if i is None:
                    continue
                if ex == "1":
                    bits[sid] |= 1 << i
                elif ex == "2":
                    bits[sid] &= ~(1 << i)
        return bits

    # -- horaires ------------------------------------------------------------

    def load_feed(self, op, path):
        op_id = op["id"].upper()
        op_idx = [o["id"].upper() for o in self.operators].index(op_id)
        z = zipfile.ZipFile(path)

        agency = read_gtfs(z, "agency.txt")
        agency_tz = agency["agency_timezone"].iloc[0]
        self.agency_names = dict(zip(agency["agency_id"], agency["agency_name"])) if "agency_id" in agency.columns else {}
        tz_idx = self.intern(self.timezones, agency_tz)
        logging.info(f"  [{op_id}] fuseau agence : {agency_tz}")

        stops_df = read_gtfs(z, "stops.txt")
        raw_stops = {r["stop_id"]: r for r in stops_df.to_dict("records")}

        routes = read_gtfs(z, "routes.txt")
        route_info = {r["route_id"]: r for r in routes.to_dict("records")}
        trips = read_gtfs(z, "trips.txt")
        bits = self.service_bits(z)

        st = read_gtfs(z, "stop_times.txt")
        for c in ("pickup_type", "drop_off_type"):
            if c not in st.columns:
                st[c] = ""
        st["seq"] = st["stop_sequence"].astype(int)
        st = st.sort_values(["trip_id", "seq"], kind="stable")
        arr_all = parse_minutes(st["arrival_time"].where(st["arrival_time"] != "", st["departure_time"]))
        dep_all = parse_minutes(st["departure_time"].where(st["departure_time"] != "", st["arrival_time"]))
        no_pick = (st["pickup_type"] == "1").to_numpy()
        no_drop = (st["drop_off_type"] == "1").to_numpy()
        stop_ids = st["stop_id"].to_numpy()
        trip_ids = st["trip_id"].to_numpy()

        # index canonique de chaque stop_id rencontré
        canon = {}
        for sid in pd.unique(stop_ids):
            raw = raw_stops.get(sid, {"stop_id": sid})
            canon[sid] = self.stop_for(op_id, raw, agency_tz)

        trip_meta = {r["trip_id"]: r for r in trips.to_dict("records")}
        bounds = np.flatnonzero(trip_ids[1:] != trip_ids[:-1]) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [len(trip_ids)]])

        earlier = defaultdict(list)    # numéro -> trajets des flux précédents
        at_stop = defaultdict(list)    # gare -> (trajet, rang) des flux précédents (flux sans numéro)
        if op_id in DEDUP_AGAINST_EARLIER | DEDUP_BY_TIMES:
            same_clock = {i for i, tz in enumerate(self.timezones) if self.same_offsets(tz, agency_tz)}
            here = set(canon.values())
            for t in self.trips.values():
                if t["op"] == op_idx or t["tz"] not in same_clock:
                    continue
                if op_id in DEDUP_AGAINST_EARLIER and t["number"]:
                    earlier[t["number"]].append(t)
                if op_id in DEDUP_BY_TIMES:
                    for i, s in enumerate(t["stops"][:-1]):
                        if s in here:
                            at_stop[s].append((t, i))

        kept = 0
        for a, b in zip(starts, ends):
            tid = trip_ids[a]
            meta = trip_meta.get(tid)
            if meta is None:
                continue
            days = bits.get(meta["service_id"], 0)
            if not days:
                self.stats["trips_out_of_window"] += 1
                continue

            # suite d'arrêts canoniques (fusion des arrêts consécutifs identiques)
            seq_stops, seq_arr, seq_dep, seq_flags = [], [], [], []
            for k in range(a, b):
                s = canon[stop_ids[k]]
                f = (NO_PICKUP if no_pick[k] else 0) | (NO_DROPOFF if no_drop[k] else 0)
                if seq_stops and seq_stops[-1] == s:
                    seq_dep[-1] = int(dep_all[k])
                    seq_flags[-1] &= f
                    continue
                seq_stops.append(s)
                seq_arr.append(int(arr_all[k]))
                seq_dep.append(int(dep_all[k]))
                seq_flags.append(f)
            if len(seq_stops) < 2:
                continue
            # horaires monotones
            for i in range(len(seq_stops)):
                if i and seq_arr[i] < seq_dep[i - 1]:
                    seq_arr[i] = seq_dep[i - 1]
                if seq_dep[i] < seq_arr[i]:
                    seq_dep[i] = seq_arr[i]
            seq_flags[0] |= NO_DROPOFF
            seq_flags[-1] |= NO_PICKUP
            if all(f & NO_PICKUP for f in seq_flags[:-1]):
                continue

            route = route_info.get(meta["route_id"], {})
            number, ttype, checkin = self.trip_labels(op_id, meta, route, stop_ids[a])
            tol, miss = 0, 0
            others = earlier.get(number, ())
            if at_stop:
                tol, miss, seen, others = TIME_TOLERANCE, 1, set(), []
                for i, s in enumerate(seq_stops[:-1]):
                    for o, j in at_stop.get(s, ()):
                        if abs(o["dep"][j] - seq_dep[i]) <= tol and id(o) not in seen:
                            seen.add(id(o))
                            others.append(o)
            for o in others:
                if not o["days"] & days:
                    continue
                if covers(o["stops"], o["arr"], o["dep"], seq_stops, seq_arr, seq_dep, tol, miss):
                    days &= ~o["days"]  # déjà fourni par l'autre flux
                    number = number or o["number"]
                elif covers(seq_stops, seq_arr, seq_dep, o["stops"], o["arr"], o["dep"], tol, miss):
                    o["days"] &= ~days  # l'autre flux n'en publie qu'une partie : on garde le trajet complet
                    number = number or o["number"]
                    self.stats[f"trips_{op_id}_replaced"] += 1
            if not days:
                self.stats[f"trips_{op_id}_duplicates"] += 1
                continue
            type_idx = self.intern(self.types, ttype)
            key = (op_idx, tuple(seq_stops), tuple(seq_flags), tuple(seq_arr), tuple(seq_dep), number, type_idx, checkin)
            t = self.trips.get(key)
            if t is None:
                self.trips[key] = {
                    "op": op_idx, "tz": tz_idx, "stops": seq_stops, "flags": seq_flags,
                    "arr": seq_arr, "dep": seq_dep, "number": number, "type": type_idx,
                    "checkin": checkin, "days": days,
                }
            else:
                t["days"] |= days
                self.stats["trips_merged"] += 1
            kept += 1
        if earlier or at_stop:
            self.trips = {k: t for k, t in self.trips.items() if t["days"]}
        self.stats[f"trips_{op_id}"] = kept
        logging.info(f"  [{op_id}] {kept} trajets retenus")

    def same_offsets(self, tz_a, tz_b):
        """Deux fuseaux à la même heure locale sur toute la fenêtre (ex. Europe/Paris et Europe/Brussels)."""
        a, b = ZoneInfo(tz_a), ZoneInfo(tz_b)
        days = (datetime(d.year, d.month, d.day, 12) for d in self.day_dates)
        return all(a.utcoffset(d) == b.utcoffset(d) for d in days)

    def trip_labels(self, op_id, meta, route, first_stop_id):
        checkin = 0
        if op_id == "SNCF":
            number = meta.get("trip_headsign", "")
            m = re.match(r"StopPoint:OCE(.*)-\d+$", first_stop_id)
            ttype = SNCF_TYPES.get(m.group(1), f"SNCF {m.group(1)}") if m else "SNCF Train"
            # OUIGO Train Classique (trains 40xx, lignes classiques vers Paris Austerlitz / Bercy) :
            # type à part, que le moteur ne compte pas comme grande vitesse
            if m and m.group(1) == "OUIGO" and re.fullmatch(r"40\d\d", number):
                ttype = "SNCF OUIGO Train Classique"
        elif op_id == "RENFE":
            number = meta.get("trip_short_name", "")
            number = str(int(number)) if number.isdigit() else number
            rs = route.get("route_short_name", "")
            ttype = RENFE_TYPES.get(rs.upper(), f"Renfe {rs}" if rs else "Renfe Train")
        elif op_id == "EUROSTAR":
            number = meta.get("trip_short_name", "") or meta["trip_id"].split("-")[0]
            ttype = "Eurostar"
            try:
                checkin = int(route.get("checkin_duration", "0") or 0) // 60
            except ValueError:
                checkin = 0
        elif op_id == "NATIONAL_RAIL":
            # un seul flux pour toutes les compagnies britanniques : le type est la compagnie
            number = meta.get("trip_short_name", "")
            route_type = route.get("route_type", "2")
            if route_type == "3":
                ttype = "Bus"
            elif route_type == "4":
                ttype = "Ferry"
            else:
                ttype = self.agency_names.get(route.get("agency_id", ""), "UK National Rail")
        elif op_id == "EUROPEAN_SLEEPER":
            # trip_id "ES-400-2026-09-13" (un trajet par date) -> train "400"
            m = re.match(r"ES-(\d+)", meta["trip_id"]) or re.match(r"ES-(\d+)", meta["route_id"])
            number = m.group(1) if m else meta["trip_id"]
            ttype = "European Sleeper"
        elif op_id == "TRENITALIA":
            # trip_short_name = numéro commercial (ServiceJourney.Name), catégorie = PublicCode de la Line
            number = meta.get("trip_short_name", "")
            rs = route.get("route_short_name", "")
            ttype = TRENITALIA_TYPES.get(rs.upper(), f"Trenitalia {route.get('route_long_name', 'Train')}" if route.get('route_long_name') else "Trenitalia Train")
        elif op_id == "ITALO":
            number = meta.get("trip_short_name", "")
            rs = route.get("route_short_name", "")
            long_name = route.get("route_long_name", "") or "Italo Train"
            # Si le nom commence par un chiffre (code NeTEx comme "8993_#3"), utiliser "Italo"
            ttype = "Italo Train" if long_name and long_name[0].isdigit() else f"Italo {long_name}"
        elif op_id == "CP":
            number = meta.get("trip_short_name", "")
            rs = route.get("route_short_name", "")
            ttype = CP_TYPES.get(rs.upper(), f"CP {route.get('route_long_name', 'Train')}" if route.get('route_long_name') else "CP Train")
        elif op_id == "OUIGO_ES":
            number = meta.get("trip_short_name", "")
            ttype = "Ouigo España"
        elif op_id == "SWISS":
            # Include route type (IC, RE, S-Bahn) in the train number
            rs = route.get("route_short_name", "")
            trip_num = meta.get("trip_short_name", "")
            number = f"{rs} {trip_num}".strip() if rs and trip_num else (trip_num or rs)
            ttype = "SBB"  # Generic SBB logo for all Swiss trains
        elif op_id == "DB":
            # pas de numéro de train dans le flux (repris d'un autre flux si le trajet y figure)
            number = meta.get("trip_short_name", "")
            cat = (route.get("route_short_name", "").split() or ["Train"])[0].upper()
            brand = DB_BRANDS.get(self.agency_names.get(route.get("agency_id", ""), ""), "DB")
            if brand == "SNCF" and cat in ("ICE", "TGV"):
                ttype = SNCF_TYPES["ICE"]  # trains franco-allemands : même libellé que le flux SNCF
            else:
                ttype = f"{brand} {DB_CATEGORIES.get(cat, cat.title())}"
        elif op_id == "DB_REGIO":
            # pas de numéro de train : la ligne ("RE 1", "S5") est l'identifiant affiché par la DB
            line = route.get("route_short_name", "")
            m = re.match(r"[A-Za-z]+", line)
            cat = m.group(0).upper() if m else ""
            number = line
            ttype = DB_REGIO_TYPES.get(cat, f"Train régional {cat}".strip())
        elif op_id == "FLIXTRAIN":
            number = meta.get("trip_short_name", "") or route.get("route_id", "")  # ligne FLX10...
            ttype = "FlixTrain"
        elif op_id == "SNCB":
            # trip_short_name = numéro commercial, route_short_name = catégorie (IC, L, P, S1..., EC...)
            number = meta.get("trip_short_name", "")
            rs = route.get("route_short_name", "").upper()
            cat = "S" if re.fullmatch(r"S\d+", rs) else rs
            ttype = SNCB_TYPES.get(cat, f"SNCB {rs}" if rs else "SNCB Train")
        else:
            number = meta.get("trip_short_name", "") or meta.get("trip_headsign", "")
            ttype = route.get("route_short_name", "") or route.get("route_long_name", "") or op_id
        return number, ttype, checkin

    # -- routes RAPTOR -------------------------------------------------------

    def build_routes(self):
        """Regroupe les trajets par (suite d'arrêts, règles montée/descente, fuseau, enregistrement)
        puis découpe chaque groupe pour qu'aucun trajet n'en dépasse un autre (condition RAPTOR)."""
        groups = defaultdict(list)
        for t in self.trips.values():
            groups[(t["tz"], t["checkin"], tuple(t["stops"]), tuple(t["flags"]))].append(t)

        routes = []
        for (tz, checkin, stops, flags), trips in groups.items():
            trips.sort(key=lambda t: (t["dep"][0], t["arr"][-1]))
            subs = []  # chaque sous-route : liste de trajets FIFO
            for t in trips:
                for sub in subs:
                    last = sub[-1]
                    if all(t["dep"][i] >= last["dep"][i] and t["arr"][i] >= last["arr"][i] for i in range(len(stops))):
                        sub.append(t)
                        break
                else:
                    subs.append([t])
            for sub in subs:
                routes.append({"tz": tz, "checkin": checkin, "stops": list(stops), "flags": list(flags), "trips": sub})
        self.stats["routes"] = len(routes)
        self.stats["trips"] = len(self.trips)
        return routes

    # -- correspondances -----------------------------------------------------

    def build_footpaths(self, used):
        n = len(self.stops)
        paths = defaultdict(dict)

        def add(a, b, minutes):
            if a == b:
                return
            if minutes < paths[a].get(b, 10**9):
                paths[a][b] = minutes
                paths[b][a] = minutes

        # grille de 0.02° pour trouver les gares proches
        grid = defaultdict(list)
        for i in range(n):
            s = self.stops[i]
            if used[i] and s["lat"] is not None:
                grid[(int(s["lat"] / 0.02), int(s["lon"] / 0.02))].append(i)
        for i in range(n):
            s = self.stops[i]
            if not used[i] or s["lat"] is None:
                continue
            gy, gx = int(s["lat"] / 0.02), int(s["lon"] / 0.02)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for j in grid.get((gy + dy, gx + dx), ()):
                        if j <= i:
                            continue
                        o = self.stops[j]
                        d = haversine_km(s["lat"], s["lon"], o["lat"], o["lon"])
                        if d <= WALK_MAX_KM:
                            add(i, j, math.ceil(5 + d * 1.25 / 4.5 * 60))

        by_city = defaultdict(list)
        for i in range(n):
            if used[i]:
                by_city[self.stops[i]["city_key"]].append(i)
        for members in by_city.values():
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    a, b = self.stops[members[x]], self.stops[members[y]]
                    if a["lat"] is None or b["lat"] is None:
                        continue
                    d = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
                    if d <= CITY_MAX_KM:
                        add(members[x], members[y], min(CITY_TRANSFER_MAX, math.ceil(25 + 5 * d)))
        return paths

    # -- export --------------------------------------------------------------

    def build(self):
        routes = self.build_routes()

        # seules les gares desservies sont exportées
        used = [False] * len(self.stops)
        weight = [0.0] * len(self.stops)
        for r in routes:
            for t in r["trips"]:
                active = bin(t["days"]).count("1") / NDAYS
                for s, f in zip(r["stops"], r["flags"]):
                    used[s] = True
                    if not f & NO_PICKUP:
                        weight[s] += active
        remap = {}
        for i, u in enumerate(used):
            if u:
                remap[i] = len(remap)
        old = list(remap.keys())
        n = len(old)

        footpaths = self.build_footpaths(used)

        # villes
        city_keys = []
        city_index = {}
        stop_city = np.zeros(n, np.int32)
        for new, oi in enumerate(old):
            ck = self.stops[oi]["city_key"]
            if ck not in city_index:
                city_index[ck] = len(city_keys)
                city_keys.append(oi)
            stop_city[new] = city_index[ck]
        city_members = defaultdict(list)
        for new in range(n):
            city_members[stop_city[new]].append(new)

        def city_field(ci, f, fallback):
            v = self.stops[city_keys[ci]][f]
            return v if v not in (None, "") else fallback

        cities = []
        for ci in range(len(city_keys)):
            members = city_members[ci]
            s0 = self.stops[city_keys[ci]]
            lat = city_field(ci, "city_lat", None)
            lon = city_field(ci, "city_lon", None)
            if lat is None:
                pts = [(self.stops[old[m]]["lat"], self.stops[old[m]]["lon"]) for m in members if self.stops[old[m]]["lat"] is not None]
                lat = sum(p[0] for p in pts) / len(pts) if pts else 0.0
                lon = sum(p[1] for p in pts) / len(pts) if pts else 0.0
            ck = s0["city_key"]
            cities.append({
                # base des gares : id de la ville (Wikidata, "Q647") ; stations.csv : "TL" + id Trainline
                "id": ck[3:] if ck.startswith("tn:") else ("TL" + ck[4:]) if ck.startswith("ref:") else ck,
                "name": s0["city_name"], "country": s0["city_country"] or s0["country"],
                "lat": lat, "lon": lon,
            })

        # bitsets de jours dédoublonnés
        words = (NDAYS + 63) // 64
        day_sets, day_set_index = [], {}
        for t in self.trips.values():
            if t["days"] not in day_set_index:
                day_set_index[t["days"]] = len(day_sets)
                day_sets.append(t["days"])
        day_bits = np.zeros(len(day_sets) * words, np.uint64)
        for i, v in enumerate(day_sets):
            for w in range(words):
                day_bits[i * words + w] = (v >> (64 * w)) & 0xFFFFFFFFFFFFFFFF

        # tableaux plats des routes
        route_stop_off = [0]
        route_trip_off = [0]
        route_time_off = [0]
        route_stops, route_flags, route_tz, route_checkin = [], [], [], []
        t_arr, t_dep, trip_days, trip_number, trip_type, trip_op = [], [], [], [], [], []
        for r in routes:
            route_stops.extend(remap[s] for s in r["stops"])
            route_flags.extend(r["flags"])
            route_stop_off.append(len(route_stops))
            route_tz.append(r["tz"])
            route_checkin.append(r["checkin"])
            for t in r["trips"]:
                t_arr.extend(t["arr"])
                t_dep.extend(t["dep"])
                trip_days.append(day_set_index[t["days"]])
                trip_number.append(t["number"])
                trip_type.append(t["type"])
                trip_op.append(t["op"])
            route_trip_off.append(len(trip_days))
            route_time_off.append(len(t_arr))
        if max(t_dep + t_arr) >= 65535:
            raise ValueError("horaire hors limite uint16")

        fp_off, fp_to, fp_min = [0], [], []
        for oi in old:
            for j, m in sorted(footpaths.get(oi, {}).items()):
                fp_to.append(remap[j])
                fp_min.append(m)
            fp_off.append(len(fp_to))

        change = []
        for oi in old:
            change.append(HUB_CHANGE if weight[oi] >= HUB_DAILY_DEPARTURES else MIN_CHANGE)

        self.stats.update({
            "stops": n, "cities": len(cities), "footpaths": len(fp_to), "stop_times": len(t_arr),
            "day_sets": len(day_sets),
        })
        meta = {
            "version": FORMAT_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_date": self.base_date.isoformat(),
            "ndays": NDAYS, "words": words,
            "timezones": self.timezones, "types": self.types,
            "operators": [{"id": o["id"].upper(), "name": o.get("name", o["id"])} for o in self.operators],
            "stats": dict(self.stats),
        }

        w = BinWriter()
        w.add_json("meta", meta)
        w.add_strings("stop.id", [self.stops[o]["id"] for o in old])
        w.add_strings("stop.name", [self.stops[o]["name"] for o in old])
        w.add_strings("stop.country", [self.stops[o]["country"] for o in old])
        w.add("stop.lat", np.array([self.stops[o]["lat"] or 0.0 for o in old], np.float32))
        w.add("stop.lon", np.array([self.stops[o]["lon"] or 0.0 for o in old], np.float32))
        w.add("stop.tz", np.array([self.intern(self.timezones, self.stops[o]["tz"]) for o in old], np.uint8))
        w.add("stop.city", stop_city)
        w.add("stop.change", np.array(change, np.uint16))
        w.add("stop.weight", np.array([weight[o] for o in old], np.float32))
        meta["timezones"] = self.timezones  # stop.tz a pu en ajouter
        w.replace_json("meta", meta)

        w.add_strings("city.id", [c["id"] for c in cities])
        w.add_strings("city.name", [c["name"] for c in cities])
        w.add_strings("city.country", [c["country"] for c in cities])
        w.add("city.lat", np.array([c["lat"] for c in cities], np.float32))
        w.add("city.lon", np.array([c["lon"] for c in cities], np.float32))

        w.add("fp.off", np.array(fp_off, np.uint32))
        w.add("fp.to", np.array(fp_to, np.int32))
        w.add("fp.min", np.array(fp_min, np.uint16))

        w.add("route.stopoff", np.array(route_stop_off, np.uint32))
        w.add("route.stops", np.array(route_stops, np.int32))
        w.add("route.flags", np.array(route_flags, np.uint8))
        w.add("route.tripoff", np.array(route_trip_off, np.uint32))
        w.add("route.timeoff", np.array(route_time_off, np.uint32))
        w.add("route.tz", np.array(route_tz, np.uint8))
        w.add("route.checkin", np.array(route_checkin, np.uint16))

        w.add("time.arr", np.array(t_arr, np.uint16))
        w.add("time.dep", np.array(t_dep, np.uint16))

        w.add("trip.days", np.array(trip_days, np.uint32))
        w.add("trip.type", np.array(trip_type, np.uint16))
        w.add("trip.op", np.array(trip_op, np.uint8))
        w.add_strings("trip.number", trip_number)
        w.add("days.bits", day_bits)
        return w


# ---------------------------------------------------------------------------
# Format binaire
#   en-tête : "TNNET001" | u32 nb_sections | u32 réservé
#   section : nom (24 o, UTF-8 complété par \0) | u8 type | 7 o réservés | u64 nb éléments | u64 nb octets
#             puis les données, complétées à un multiple de 8 octets. Tout est little-endian.
#   types   : 1=u8 2=u16 3=u32 4=i32 5=u64 6=f32
#   chaînes : deux sections "<nom>.off" (u32, n+1 offsets) et "<nom>.dat" (u8, UTF-8 concaténé)
# ---------------------------------------------------------------------------

DTYPES = {np.dtype("uint8"): 1, np.dtype("uint16"): 2, np.dtype("uint32"): 3,
          np.dtype("int32"): 4, np.dtype("uint64"): 5, np.dtype("float32"): 6}


class BinWriter:
    def __init__(self):
        self.sections = []

    def add(self, name, arr: np.ndarray):
        assert len(name) <= 24, name
        arr = np.ascontiguousarray(arr)
        self.sections.append((name, DTYPES[arr.dtype], arr.astype(arr.dtype.newbyteorder("<"), copy=False)))

    def add_json(self, name, obj):
        self.add(name, np.frombuffer(json.dumps(obj, ensure_ascii=False).encode("utf-8"), np.uint8))

    def replace_json(self, name, obj):
        self.sections = [s for s in self.sections if s[0] != name]
        self.add_json(name, obj)

    def add_strings(self, name, values):
        data = [v.encode("utf-8") for v in values]
        off = np.zeros(len(data) + 1, np.uint32)
        off[1:] = np.cumsum([len(d) for d in data]) if data else []
        self.add(name + ".off", off)
        self.add(name + ".dat", np.frombuffer(b"".join(data), np.uint8))

    def write(self, path):
        with open(path, "wb") as f:
            f.write(b"TNNET001")
            f.write(struct.pack("<II", len(self.sections), 0))
            for name, code, arr in self.sections:
                raw = arr.tobytes()
                f.write(name.encode("utf-8").ljust(24, b"\0"))
                f.write(struct.pack("<B7xQQ", code, arr.size, len(raw)))
                f.write(raw)
                f.write(b"\0" * (-len(raw) % 8))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="force le re-téléchargement des GTFS")
    parser.add_argument("--only", help="opérateurs à compiler, séparés par des virgules (ex. SNCF,SNCB)")
    parser.add_argument("--out", default=OUT_BIN, help="fichier produit (défaut : network.bin)")
    args = parser.parse_args()

    with open(OPERATORS_FILE, encoding="utf-8") as f:
        operators = [o for o in json.load(f) if o.get("enabled", True) and (o.get("gtfs_url") or o.get("gtfs_path"))]
    if args.only:
        only = {x.strip().upper() for x in args.only.split(",")}
        operators = [o for o in operators if o["id"].upper() in only]

    ref = StationRef(STATIONS_CSV)
    builder = NetworkBuilder(operators, ref, Referentiel(REFERENTIEL_DIR))
    logging.info(f"📅 Fenêtre : {builder.day_dates[0]} → {builder.day_dates[-1]}")
    for op in operators:
        path = fetch_feed(op, args.refresh)
        if path:
            builder.load_feed(op, path)

    writer = builder.build()
    out_bin, out_gz = args.out, args.out + ".gz"
    writer.write(out_bin)
    with open(out_bin, "rb") as fi, gzip.open(out_gz, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    if out_bin == OUT_BIN:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(builder.stats), f, indent=2, ensure_ascii=False)

    logging.info(f"📊 {json.dumps(dict(builder.stats), ensure_ascii=False)}")
    logging.info(f"✅ {out_bin} : {os.path.getsize(out_bin) / 1e6:.2f} Mo | .gz : {os.path.getsize(out_gz) / 1e6:.2f} Mo")


if __name__ == "__main__":
    main()
