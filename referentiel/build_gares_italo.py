"""
Référentiel des gares TrainNomad — opérateur Italo (NTV, trains à grande vitesse en Italie).

Source : Trenitalia/italo_gtfs.zip, le GTFS produit par Trenitalia/ingest_trenitalia.py.
Le stop_id est le code Italo ("MC_" = Milano Centrale), le stop_code l'UIC à 7 chiffres. Quelques
arrêts n'ont pas d'UIC dans le flux (Longarone-Zoldo, Villa San Giovanni Marittima) : ils prennent
celui de leur gare OpenStreetMap quand elle en a un. Les arrêts "... BUS" (Cortina d'Ampezzo, Tai di
Cadore) sont les cars Italo : arrêts de car, jamais rattachés à une gare par leur nom.

Noms : le flux ajoute une traduction ("Milano Centrale (Milan)", "Padua") : c'est le nom
d'OpenStreetMap qui est gardé.

Sortie : referentiel/operateurs/ITALO/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_italo.py           (python Trenitalia/ingest_trenitalia.py d'abord si besoin)
    python referentiel/build_gares_italo.py --offline
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gares_trenitalia import italian_names, load_italian_stations, search_names  # noqa: E402
from commun import GTFS_DIR, run  # noqa: E402

ITALO_ZIP = os.path.join(GTFS_DIR, "Trenitalia", "italo_gtfs.zip")


def load_italo_stations(path: str = ITALO_ZIP) -> list[dict]:
    out = load_italian_stations(path, "ITALO", "Italo")
    for s in out:
        if re.search(r"\bBUS$", s["nom_gtfs"]):  # car Italo : le flux le classe en train
            s.update({"type": "arret_car", "cars": "BUS", "trains": ""})
        else:
            s["trains"] = "ITALO"  # le flux nomme ses lignes par numéro ("1021_8907")
        # "Milano Centrale (Milan)" -> "Milano Centrale" ; "Cortina D'Ampezzo BUS" -> "Cortina D'Ampezzo"
        s["nom_gtfs"] = re.sub(r"\s*\([^)]*\)\s*$|\s+BUS$", "", s["nom_gtfs"]).strip()
    return out


if __name__ == "__main__":
    run("ITALO", "Italo (Italie)", load_italo_stations, op_country="IT", names=italian_names,
        search_name=search_names)
