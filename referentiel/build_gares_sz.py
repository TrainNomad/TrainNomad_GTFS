"""
Référentiel des gares TrainNomad — Slovénie (SŽ).

Source : SLOVENIA/si_gtfs.zip, le GTFS slovène préparé par SLOVENIA/ingest_si_gtfs.py (agence SŽ du GTFS
national DUJPP).

Une gare = un arrêt du flux ("1122804" = Ljubljana). Ce numéro à 7 chiffres est l'identifiant du registre
national des arrêts, PAS un UIC (les UIC slovènes commencent par 79) : la gare est cherchée dans
OpenStreetMap par son nom ; les gares déjà en base par les flux ÖBB et MÁV (Ljubljana, Maribor, Jesenice...)
sont retrouvées à la fusion. Un arrêt desservi seulement par des bus de remplacement est un arrêt de car.

Sortie : referentiel/operateurs/SZ/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_sz.py      (python SLOVENIA/ingest_si_gtfs.py d'abord si besoin)
    python referentiel/build_gares_sz.py --offline
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_hzpp import load_stations  # noqa: E402
from commun import GTFS_DIR, run  # noqa: E402

SI_ZIP = os.path.join(GTFS_DIR, "SLOVENIA", "si_gtfs.zip")


def search_names(st) -> list:
    """Recherches à essayer dans OpenRailwayMap : le nom du flux, puis sans précision entre parenthèses
    ou après une virgule."""
    name = st["nom_gtfs"]
    short = re.sub(r"\s*\(.*?\)$", "", name.split(",")[0]).strip()
    return list(dict.fromkeys(n for n in (name, short, short.replace("-", " ")) if len(n) > 2))


if __name__ == "__main__":
    run("SZ", "Slovénie (SŽ)", lambda: load_stations(SI_ZIP, "SZ"), op_country="SI",
        search_name=search_names, chercher_uic=False)
