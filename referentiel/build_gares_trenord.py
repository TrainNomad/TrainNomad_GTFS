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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_trenitalia import italian_names, load_italian_stations, search_names  # noqa: E402
from commun import GTFS_DIR, run  # noqa: E402

TRENORD_ZIP = os.path.join(GTFS_DIR, "TRENORD", "trenord_gtfs.zip")

if __name__ == "__main__":
    run("TRENORD", "Trenord (Lombardie)",
        lambda: [{**s, "uic_a_confirmer": True} for s in load_italian_stations(TRENORD_ZIP, "TRENORD", "Trenord")],
        op_country="IT", names=italian_names, search_name=search_names)
