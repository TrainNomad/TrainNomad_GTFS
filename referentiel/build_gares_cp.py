"""
Référentiel des gares TrainNomad — opérateur CP (Comboios de Portugal : Alfa Pendular, Intercidades,
InterRegional, Regional, Urbanos).

Source : CP/cp_gtfs.zip, le GTFS CP complété de la liaison Elvas - Badajoz par CP/ingest_cp.py.
Le stop_id contient l'UIC : "94_1008" -> 9401008 (préfixe pays + code sur 5 chiffres) ;
"71_37606" -> 7137606 (Badajoz, Espagne). La gare est cherchée dans OpenStreetMap par UIC, sinon par nom.

Noms : le GTFS CP les écrit sans accents ("Porto Sao Bento"). Quand OSM porte le même nom mieux
écrit ("Porto São Bento"), c'est celui d'OSM qui est gardé (règle commune) ; sinon celui de CP,
corrigeable à la main dans la colonne nom_force de gares.csv.

Sortie : referentiel/operateurs/CP/ (gares, villes, codes, rapport), puis fusion.py.

Usage :
    python referentiel/build_gares_cp.py              (python CP/ingest_cp.py d'abord si besoin)
    python referentiel/build_gares_cp.py --offline
"""
import logging
import os
import sys
import zipfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import GTFS_DIR, run  # noqa: E402

CP_ZIP = os.path.join(GTFS_DIR, "CP", "cp_gtfs.zip")


def uic_of(stop_id: str) -> str:
    """ "94_1008" -> "9401008" ; "71_37606" -> "7137606" """
    prefix, _, code = stop_id.partition("_")
    return prefix + code.zfill(5) if prefix.isdigit() and code.isdigit() else ""


def category(short_name: str) -> str:
    """AP, IC, IR, R, U ; les lignes urbaines de Lisbonne et Porto ("Linha de Sintra") comptent comme U."""
    s = (short_name or "").strip()
    return "U" if s.lower().startswith("linha") else s.upper()


def load_cp_stations(path: str = CP_ZIP) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"❌ {path} absent : lancer d'abord python CP/ingest_cp.py")
    z = zipfile.ZipFile(path)

    def read(name, **kw):
        return pd.read_csv(z.open(name), dtype=str, keep_default_na=False, **kw)

    stops = read("stops.txt")
    st = read("stop_times.txt", usecols=["trip_id", "stop_id"])
    trips = read("trips.txt", usecols=["trip_id", "route_id"])
    routes = read("routes.txt")

    cat = trips.set_index("trip_id")["route_id"].map(routes.set_index("route_id")["route_short_name"].map(category))
    st["cat"] = st["trip_id"].map(cat).fillna("")
    served = st.groupby("stop_id")["cat"].agg(lambda s: sorted(set(s) - {""}))

    out = []
    for r in stops[stops["stop_id"].isin(served.index)].itertuples(index=False):
        out.append({"cle": r.stop_id, "uic": uic_of(r.stop_id), "nom_gtfs": " ".join(r.stop_name.split()),
                    "lat": float(r.stop_lat), "lon": float(r.stop_lon), "type": "gare",
                    "trains": "|".join(served[r.stop_id]), "cars": "",
                    "codes": [("CP", r.stop_id)]})
    logging.info(f"   {len(out)} gares CP")
    return out


if __name__ == "__main__":
    run("CP", "CP (Portugal)", load_cp_stations, op_country="PT")
