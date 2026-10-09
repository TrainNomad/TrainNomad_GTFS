"""
Fusion des opérateurs en une seule base des gares TrainNomad.

Entrées : referentiel/operateurs/<OP>/gares.csv, villes.csv, codes.csv (un dossier par opérateur,
produits par build_gares_<op>.py). Sorties (dossier referentiel/) :

  gares.csv    une ligne par gare physique, toutes compagnies confondues
               + operateurs (SNCF|SNCB...), ville_nom (lecture), nom_force (saisie manuelle, conservée)
  villes.csv   une ligne par ville ; ville_parent (saisie manuelle, conservée) : rattache une commune
               à sa grande ville (Saint-Gilles -> Bruxelles)
  codes.csv    tous les identifiants de chaque gare, toutes compagnies
  rapports/fusion.csv  gares fournies par plusieurs opérateurs dont les données divergent

Une même gare vue par deux opérateurs (Bruxelles-Midi : SNCF et SNCB) a le même id (TN + UIC) :
ses lignes sont fusionnées ; les données viennent de l'opérateur du pays de la gare (SNCB pour
Bruxelles-Midi, SNCF pour Lille), les catégories de trains et les codes de tous les opérateurs.
Une gare sans UIC chez un opérateur (id TNOSM...) rejoint la gare de même objet OSM. Une gare qui a
deux UIC (Londres St Pancras : 7015400 chez Eurostar, 7015550 au Royaume-Uni) est une seule gare, sous
l'id de l'opérateur du pays, les deux UIC dans codes.csv : les lignes sont réunies quand elles ont le
même objet OSM, ou quand l'objet OSM de l'une porte l'UIC de l'autre à moins de 1 km.

Usage :
    python referentiel/fusion.py
"""
import glob
import logging
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import BASE_DIR, GARES_COLUMNS, OPERATEURS_DIR, VILLES_COLUMNS, fold, km, merge_cities  # noqa: E402

GARES_CSV = os.path.join(BASE_DIR, "gares.csv")
VILLES_CSV = os.path.join(BASE_DIR, "villes.csv")
CODES_CSV = os.path.join(BASE_DIR, "codes.csv")
RAPPORT_CSV = os.path.join(BASE_DIR, "rapports", "fusion.csv")

# pays de chaque opérateur : ses données font foi pour les gares de son pays
OPERATOR_COUNTRY = {"SNCF": "FR", "SNCB": "BE", "DB": "DE", "DB_REGIO": "DE", "SWISS": "CH", "RENFE": "ES",
                    "CP": "PT", "TRENITALIA": "IT", "ITALO": "IT", "NATIONAL_RAIL": "GB", "CFL": "LU", "NS": "NL",
                    "OBB": "AT"}
MANUAL_GARES = ["nom_force"]
MANUAL_VILLES = ["ville_parent"]


def read(path):
    return pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)


def previous_manual(path, cols):
    """Colonnes saisies à la main dans la base précédente : {id: {col: valeur}}."""
    if not os.path.exists(path):
        return {}
    df = read(path)
    cols = [c for c in cols if c in df.columns]
    return {r["id"]: {c: r[c] for c in cols} for r in df.to_dict("records") if any(r[c] for c in cols)}


def union(values):
    out = []
    for v in values:
        for x in (v or "").split("|"):
            if x and x not in out:
                out.append(x)
    return "|".join(out)


def main():
    ops = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(OPERATEURS_DIR, "*", "gares.csv")))
    if not ops:
        sys.exit("❌ aucun opérateur dans referentiel/operateurs/ : lancer d'abord build_gares_<op>.py")
    logging.info(f"🔗 Fusion des opérateurs : {', '.join(ops)}")
    gares = pd.concat([read(os.path.join(OPERATEURS_DIR, o, "gares.csv")).assign(operateur=o) for o in ops],
                      ignore_index=True)
    villes = pd.concat([read(os.path.join(OPERATEURS_DIR, o, "villes.csv")) for o in ops], ignore_index=True)
    codes = pd.concat([read(os.path.join(OPERATEURS_DIR, o, "codes.csv")) for o in ops], ignore_index=True)

    # une gare = un id. Lignes à réunir : même objet OSM, ou second UIC (code "UIC" venu de l'objet OSM)
    # qui est l'id d'une autre gare à moins de 1 km
    group = {i: i for i in gares["id"]}

    def root(i):
        while group[i] != i:
            i = group[i]
        return i

    for _, g in gares[gares["osm_id"] != ""].groupby("osm_id"):
        for i in g["id"]:
            group[root(i)] = root(g["id"].iloc[0])
    pos = gares.drop_duplicates("id").set_index("id")
    for c in codes[codes["source"] == "UIC"].itertuples():
        other = "TN" + c.code
        if other != c.gare_id and other in pos.index and c.gare_id in pos.index:
            a, b = pos.loc[c.gare_id], pos.loc[other]
            if km(float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])) <= 1.0:
                group[root(other)] = root(c.gare_id)
    # gare jamais trouvée dans OSM (statut "gtfs") posée à moins de 150 m d'une gare trouvée : la même gare
    # sous un autre code (Basel Bad Bf : 8014431 côté allemand, 8500090 côté suisse ; Bâle Saint-Jean :
    # 8718793 à la SNCF, 8500016 en Suisse), si elle porte le même nom ou vient d'un autre opérateur
    ops = gares.groupby("id")["operateur"].agg(set)
    found_ids = set(gares.loc[gares["statut"] != "gtfs", "id"])
    grid = {}
    for r in pos[(pos["type"] == "gare") & pos.index.isin(found_ids)].itertuples():
        grid.setdefault((round(float(r.lat) * 100), round(float(r.lon) * 100)), []).append(r)
    for r in pos[(pos["type"] == "gare") & ~pos.index.isin(found_ids)].itertuples():
        la, lo = float(r.lat), float(r.lon)
        near = [(km(la, lo, float(x.lat), float(x.lon)), x.Index)
                for da in (-1, 0, 1) for do in (-1, 0, 1)
                for x in grid.get((round(la * 100) + da, round(lo * 100) + do), [])
                if fold(x.nom_fr) == fold(r.nom_fr) or fold(x.nom_gtfs) == fold(r.nom_gtfs)
                or not (ops[x.Index] & ops[r.Index])]
        near = [n for n in near if n[0] <= 0.15]
        if near:
            group[root(r.Index)] = root(min(near)[1])
    # id gardé : celui d'une ligne trouvée dans OSM, de l'opérateur du pays de la gare s'il a un UIC,
    # sinon la première ligne qui a un UIC
    remap = {}
    for _, g in gares.groupby(gares["id"].map(root)):
        if g["id"].nunique() < 2:
            continue
        rows = sorted(g.to_dict("records"), key=lambda r: r["statut"] == "gtfs")
        target = next((r for r in rows if r["uic"] and OPERATOR_COUNTRY.get(r["operateur"]) == r["pays"]), None) \
            or next((r for r in rows if r["uic"]), rows[0])
        remap.update({r["id"]: target["id"] for r in rows if r["id"] != target["id"]})
    gares["id_origine"] = gares["id"]
    gares["id"] = gares["id"].replace(remap)
    codes["gare_id"] = codes["gare_id"].replace(remap)

    merged, conflicts = [], []
    for gid, g in gares.groupby("id", sort=True):
        # la ligne dont l'id a été gardé d'abord, puis les lignes trouvées dans OSM
        rows = sorted(g.to_dict("records"), key=lambda r: (r["id_origine"] != gid, r["statut"] == "gtfs"))
        # données de l'opérateur du pays de la gare, sinon de la ligne trouvée par UIC, sinon la première
        main = next((r for r in rows if OPERATOR_COUNTRY.get(r["operateur"]) == r["pays"]), None) \
            or next((r for r in rows if r["statut"] == "uic"), rows[0])
        out = {c: main.get(c, "") for c in GARES_COLUMNS}
        out["type"] = "gare" if any(r["type"] == "gare" for r in rows) else main["type"]
        out["trains"] = union(r["trains"] for r in rows)
        out["cars"] = union(r["cars"] for r in rows)
        out["operateurs"] = union(r["operateur"] for r in rows)
        for c in ("uic", "uic8", "osm_id", "wikidata", "ifopt", "code_db"):
            out[c] = out[c] or next((r[c] for r in rows if r[c]), "")
        merged.append(out)
        if len(rows) > 1:
            for r in rows:
                if r is main:
                    continue
                d = km(float(main["lat"]), float(main["lon"]), float(r["lat"]), float(r["lon"]))
                if r["ville_id"] != main["ville_id"] or d > 1.0:
                    conflicts.append({"id": gid, "nom": main["nom_fr"], "operateur_retenu": main["operateur"],
                                      "ville_retenue": main["ville_id"], "autre_operateur": r["operateur"],
                                      "autre_nom": r["nom_fr"], "autre_ville": r["ville_id"], "km": round(d, 3)})

    villes_d = {}
    for v in villes.to_dict("records"):
        if v["id"] not in villes_d or (v["lat"] and not villes_d[v["id"]]["lat"]):
            villes_d[v["id"]] = v
    merge_cities(merged, villes_d)

    # colonnes saisies à la main : conservées d'une fusion à l'autre
    manual_g = previous_manual(GARES_CSV, MANUAL_GARES)
    manual_v = previous_manual(VILLES_CSV, MANUAL_VILLES)
    for g in merged:
        g["nom_force"] = manual_g.get(g["id"], {}).get("nom_force", "")
        g["ville_nom"] = villes_d.get(g["ville_id"], {}).get("nom_fr", "")
    for v in villes_d.values():
        v["ville_parent"] = manual_v.get(v["id"], {}).get("ville_parent", "")

    cols = ["id", "type", "nom_fr", "nom_en", "nom_local", "nom_force", "uic", "uic8", "lat", "lon", "pays",
            "fuseau", "ville_id", "ville_nom", "operateurs", "trains", "cars", "statut", "osm_id", "wikidata",
            "ifopt", "code_db"]
    pd.DataFrame(merged)[cols].to_csv(GARES_CSV, sep=";", index=False, encoding="utf-8")
    pd.DataFrame(list(villes_d.values()))[VILLES_COLUMNS + ["ville_parent"]].sort_values("id").to_csv(
        VILLES_CSV, sep=";", index=False, encoding="utf-8")
    codes.drop_duplicates().sort_values(["gare_id", "source", "code"]).to_csv(CODES_CSV, sep=";", index=False,
                                                                              encoding="utf-8")
    os.makedirs(os.path.dirname(RAPPORT_CSV), exist_ok=True)
    pd.DataFrame(conflicts, columns=["id", "nom", "operateur_retenu", "ville_retenue", "autre_operateur",
                                     "autre_nom", "autre_ville", "km"]).to_csv(RAPPORT_CSV, sep=";", index=False,
                                                                                encoding="utf-8")
    multi = sum(1 for g in merged if "|" in g["operateurs"])
    logging.info(f"   {len(merged)} gares ({multi} communes à plusieurs opérateurs), {len(villes_d)} villes, "
                 f"{len(codes.drop_duplicates())} codes, {len(conflicts)} divergences à relire")
    print(f"\n✅ {GARES_CSV}\n   {VILLES_CSV}\n   {CODES_CSV}\n📋 {RAPPORT_CSV}")


if __name__ == "__main__":
    main()
