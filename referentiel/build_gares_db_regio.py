"""
Référentiel des gares TrainNomad — Allemagne, trains régionaux (RE, RB et compagnies régionales,
sans la banlieue : ni S-Bahn ni Regio-S-Bahn).

Source : GERMANY/de_regio_gtfs.zip, le GTFS gtfs.de préparé par GERMANY/ingest_de_gtfs.py. Même
flux et mêmes règles que les grandes lignes : voir build_gares_db.py. Une gare desservie par les
deux porte le même stop_id dans les deux flux, fusion.py les réunit.

Plusieurs milliers de gares : le premier passage dure quelques heures (une requête par gare et par
source), il peut être interrompu et relancé, il reprend où il s'est arrêté.

Sortie : referentiel/operateurs/DB_REGIO/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_db_regio.py        (python GERMANY/ingest_de_gtfs.py d'abord si besoin)
    python referentiel/build_gares_db_regio.py --offline
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_db import load_german_stations, names, search_names  # noqa: E402
from commun import GTFS_DIR, run  # noqa: E402

REGIO_ZIP = os.path.join(GTFS_DIR, "GERMANY", "de_regio_gtfs.zip")

if __name__ == "__main__":
    run("DB_REGIO", "Allemagne, trains régionaux", lambda: load_german_stations(REGIO_ZIP, "DB_REGIO", regio=True),
        op_country="DE", search_name=search_names, names=names)
