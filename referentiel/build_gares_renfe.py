"""
Référentiel des gares TrainNomad — opérateur Renfe (Espagne : AVE, Avlo, Alvia, Avant, Euromed,
Intercity, Media Distancia, Regional, Proximidad).

Source : feeds/RENFE.zip (GTFS Renfe grandes lignes et moyenne distance, téléchargé si absent).
Le stop_id Renfe est un code à 5 chiffres ("17000" = Madrid-Chamartín) : UIC = "71" + ce code
(7117000), en Espagne seulement. Les arrêts étrangers du flux (France, Portugal) ont aussi un code
Renfe à 5 chiffres, qui n'est pas un UIC : ils sont rattachés par leur objet OSM à la gare de la
SNCF ou de CP lors de la fusion. La gare est cherchée dans OpenStreetMap par UIC, sinon par nom.

Noms : le GTFS Renfe donne le nom officiel espagnol ("Madrid-Puerta de Atocha"), gardé comme nom
de la gare ; la ville est traduite par Wikidata (Sevilla -> Séville / Seville).

Sortie : referentiel/operateurs/RENFE/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_renfe.py
    python referentiel/build_gares_renfe.py --offline
"""
import logging
import os
import re
import subprocess
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

RENFE_ZIP = os.path.join(GTFS_DIR, "feeds", "RENFE.zip")
RENFE_URL = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"


def read(z, name, **kw):
    df = pd.read_csv(z.open(name), dtype=str, keep_default_na=False, **kw)
    df.columns = [c.strip() for c in df.columns]  # en-têtes Renfe suivis d'espaces
    return df.apply(lambda c: c.str.strip())


def uic_of(stop_id: str) -> str:
    """Code Renfe à 5 chiffres -> UIC espagnol 71xxxxx ; UIC étranger (87..., 94...) gardé tel quel."""
    digits = re.sub(r"\D", "", stop_id)
    if len(digits) <= 5:
        return "71" + digits.zfill(5)
    return digits[:7]


def load_renfe_stations(path: str = RENFE_ZIP) -> list[dict]:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        logging.info(f"📥 Téléchargement {RENFE_URL}")
        # le serveur Renfe n'envoie pas son certificat intermédiaire : curl sait le récupérer
        subprocess.run(["curl", "-sSfL", "-o", path, RENFE_URL], check=True)
    z = zipfile.ZipFile(path)
    stops = read(z, "stops.txt")
    st = read(z, "stop_times.txt", usecols=lambda c: c.strip() in ("trip_id", "stop_id"))
    trips = read(z, "trips.txt")
    routes = read(z, "routes.txt")

    # catégories de trains (AVE, ALVIA, MD...) qui desservent chaque arrêt
    cat = trips.set_index("trip_id")["route_id"].map(routes.set_index("route_id")["route_short_name"])
    st["cat"] = st["trip_id"].map(cat).fillna("").str.upper()
    served = st.groupby("stop_id")["cat"].agg(lambda s: sorted(set(s) - {""}))

    out = []
    for r in stops[stops["stop_id"].isin(served.index)].itertuples(index=False):
        # uic_pays : "71" + code n'est un vrai UIC qu'en Espagne ; Renfe numérote aussi ses arrêts
        # étrangers (87303 Lyon Part-Dieu, 94346 Porto Campanhã), qui ne doivent pas recevoir ce faux UIC
        out.append({"cle": r.stop_id, "uic": uic_of(r.stop_id), "uic_pays": "ES",
                    "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "|".join(served[r.stop_id]), "cars": "",
                    "codes": [("RENFE", r.stop_id)]})
    logging.info(f"   {len(out)} gares Renfe")
    return out


if __name__ == "__main__":
    run("RENFE", "Renfe (Espagne)", load_renfe_stations, op_country="ES")
