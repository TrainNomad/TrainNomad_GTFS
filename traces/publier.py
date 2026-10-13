#!/usr/bin/env python3
"""
Assemble les tracés de tous les opérateurs (traces/sortie/<OP>/gares/) pour le front.

Une même gare peut avoir un fichier chez plusieurs opérateurs (Paris Nord : SNCF et Eurostar) : les
fichiers sont fusionnés, pas écrasés.

    python traces/publier.py              copie dans le front local (trainnomad-react/public/traces/)
    python traces/publier.py --release    prépare aussi traces/release/ pour la Release traces-latest :
                                            traces.tar.gz   tous les tracés fusionnés, téléchargé par le
                                                            front à chaque build (scripts/fetch-traces.mjs)
                                            <OP>.zip        le dossier sortie/<OP>, repris par le workflow
                                                            « Tracés » pour ne calculer que les nouveautés
    python traces/publier.py --release --sans-front        (workflow : pas de front à côté)
"""
import argparse
import glob
import io
import json
import os
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SORTIE_DIR = os.path.join(ROOT, "sortie")
RELEASE_DIR = os.path.join(ROOT, "release")
FRONT_DIR = os.path.normpath(os.path.join(ROOT, "..", "..", "..", "trainnomad-react"))


def operators() -> list:
    if not os.path.isdir(SORTIE_DIR):
        return []
    return sorted(op for op in os.listdir(SORTIE_DIR) if os.path.isdir(os.path.join(SORTIE_DIR, op, "gares")))


def merge() -> dict:
    """{gare: {gare d'arrivée: {variante: tracé}}}, tous opérateurs confondus."""
    merged = {}
    for op in operators():
        files = glob.glob(os.path.join(SORTIE_DIR, op, "gares", "*.json"))
        for path in files:
            gid = os.path.splitext(os.path.basename(path))[0]
            with open(path, encoding="utf-8") as f:
                station = merged.setdefault(gid, {})
                for dest, variants in json.load(f).items():
                    station.setdefault(dest, {}).update(variants)
        print(f"   {op} : {len(files)} fichiers")
    return merged


def dump(station: dict) -> bytes:
    return json.dumps(station, separators=(",", ":")).encode("utf-8")


def write_front(merged: dict, front: str):
    out_dir = os.path.join(front, "public", "traces")
    os.makedirs(out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(out_dir, "*.json")):
        os.remove(old)
    size = 0
    for gid, station in merged.items():
        data = dump(station)
        with open(os.path.join(out_dir, f"{gid}.json"), "wb") as f:
            f.write(data)
        size += len(data)
    print(f"✅ {len(merged)} fichiers, {size / 1e6:.1f} Mo -> {out_dir}")


def write_release(merged: dict):
    os.makedirs(RELEASE_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(RELEASE_DIR, "*")):
        os.remove(old)
    # tracés fusionnés : les fichiers à la racine de l'archive
    path = os.path.join(RELEASE_DIR, "traces.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        for gid in sorted(merged):
            data = dump(merged[gid])
            info = tarfile.TarInfo(f"{gid}.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    print(f"✅ {path} ({os.path.getsize(path) / 1e6:.1f} Mo)")
    # état de chaque opérateur
    for op in operators():
        path = os.path.join(RELEASE_DIR, f"{op}.zip")
        base = os.path.join(SORTIE_DIR, op)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for folder, _, files in os.walk(base):
                for name in files:
                    full = os.path.join(folder, name)
                    z.write(full, os.path.relpath(full, base).replace(os.sep, "/"))
        print(f"   {path} ({os.path.getsize(path) / 1e6:.1f} Mo)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--front", default=FRONT_DIR, help="dossier du front (trainnomad-react)")
    parser.add_argument("--release", action="store_true", help="prépare traces/release/ (Release traces-latest)")
    parser.add_argument("--sans-front", action="store_true", help="ne copie pas dans le front")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    merged = merge()
    if not merged:
        sys.exit(f"❌ aucun tracé dans {SORTIE_DIR}")
    if not args.sans_front:
        if not os.path.isdir(os.path.join(args.front, "public")):
            sys.exit(f"❌ dossier introuvable : {os.path.join(args.front, 'public')} (ou --sans-front)")
        write_front(merged, args.front)
    if args.release:
        write_release(merged)


if __name__ == "__main__":
    main()
