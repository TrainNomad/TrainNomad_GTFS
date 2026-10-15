"""
Référentiel des gares TrainNomad — opérateur Trenord (Lombardie : trains régionaux R, RegioExpress RE,
Malpensa Express ; sans la banlieue S).

Source : TRENORD/trenord_gtfs.zip, le GTFS de la Région Lombardie préparé par
TRENORD/ingest_trenord_gtfs.py. Le stop_id est le code de la gare en Italie ("S01700" = Milano
Centrale) ; l'ingestion en tire l'UIC ("8301700"), le même que Trenitalia : fusion.py réunit les
deux opérateurs sur la même gare. Cet UIC n'est juste que pour les gares du réseau national : les
gares du réseau Ferrovienord (Milano Cadorna, Malpensa, Saronno) ont un code interne. Il n'est donc
gardé que si OpenStreetMap porte le même UIC sur la gare (uic_a_confirmer) ; sinon la gare prend
celui de son objet OpenStreetMap, ou un identifiant propre.
Mêmes règles que Trenitalia (build_gares_trenitalia.py) : nom d'OpenStreetMap d'abord, et un UIC
italien porté par une gare suisse (Chiasso, Lugano, Locarno) est écarté.

Sortie : referentiel/operateurs/TRENORD/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_trenord.py         (python TRENORD/ingest_trenord_gtfs.py d'abord si besoin)
    python referentiel/build_gares_trenord.py --offline
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_trenitalia import italian_names, load_italian_stations, search_names  # noqa: E402
from commun import BASE_DIR, GTFS_DIR, km, run  # noqa: E402

TRENORD_ZIP = os.path.join(GTFS_DIR, "TRENORD", "trenord_gtfs.zip")
GARES_CSV = os.path.join(BASE_DIR, "gares.csv")
CONFIRME_MAX_KM = 1.0
# Gares que la recherche OSM rattache à un mauvais objet : code Trenord -> UIC
# (Lugano Paradiso tombait sur la station du funiculaire du San Salvatore, à 180 m)
UIC_IMPOSES = {"S05301": "8505301"}


def uic_des_autres() -> dict:
    """{UIC: (lat, lon)} des gares déjà en base chez une autre compagnie que Trenord (Trenitalia, Swiss...)."""
    if not os.path.exists(GARES_CSV):
        return {}
    with open(GARES_CSV, encoding="utf-8") as f:
        return {r["uic"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f, delimiter=";")
                if r["uic"] and r["lat"] and set(r["operateurs"].split("|")) - {"TRENORD"}}


def stations():
    """L'UIC déduit du code est confirmé par OpenStreetMap, ou par une autre compagnie qui a déjà la même
    gare au même endroit (Rho Fiera, Sesto S.Giovanni : absentes d'OpenRailwayMap, elles faisaient doublon)."""
    autres = uic_des_autres()
    out = []
    for s in load_italian_stations(TRENORD_ZIP, "TRENORD", "Trenord"):
        if s["cle"] in UIC_IMPOSES:
            out.append({**s, "uic": UIC_IMPOSES[s["cle"]], "uic_pays": ""})
            continue
        pos = autres.get(s["uic"])
        confirme = pos is not None and km(s["lat"], s["lon"], *pos) <= CONFIRME_MAX_KM
        out.append({**s, "uic_a_confirmer": not confirme})
    return out


if __name__ == "__main__":
    run("TRENORD", "Trenord (Lombardie)", stations, op_country="IT", names=italian_names, search_name=search_names)
