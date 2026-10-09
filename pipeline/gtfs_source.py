"""
Pipeline GTFS TrainNomad : chaque compagnie est préparée à part, puis le réseau global est assemblé.

Une « source » = un script d'ingestion de operators.json (script_path) et les opérateurs qu'il produit
(ex. Trenitalia/ingest_trenitalia.py -> TRENITALIA + ITALO), ou un opérateur sans script, simplement
téléchargé depuis son gtfs_url (SNCF, Renfe, Eurostar, European Sleeper). Nom de la source : dossier du
script en minuscules (uk, trenitalia, germany...) ou id de l'opérateur (sncf, renfe...).

Les GTFS préparés sont publiés dans la Release « gtfs-latest » (un <OP>.zip + <OP>.json par opérateur,
remplacés à chaque mise à jour) : jamais dans l'historique git, et seule la dernière version est gardée.

Commandes :
  python pipeline/gtfs_source.py liste [--json]
      sources connues et opérateurs de chacune (--json : sources ayant un opérateur activé, pour les workflows)
  python pipeline/gtfs_source.py preparer <source> [--forcer]
      lance le script (ou télécharge), vérifie chaque GTFS, écrit pipeline/sortie/<OP>.zip + <OP>.json
  python pipeline/gtfs_source.py placer [--dossier D] [--depuis-release]
      met les GTFS publiés là où build_network.py les attend (gtfs_path ou feeds/<OP>.zip)
  python pipeline/gtfs_source.py verifier-reseau <ancien_rapport.json> <nouveau_rapport.json> [--forcer]
      refuse un réseau qui a perdu un opérateur ou trop de trajets

En local, pour compiler avec les derniers GTFS publiés sans tout relancer :
  python pipeline/gtfs_source.py placer --depuis-release && python build_network.py
"""
import argparse
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPERATORS_FILE = os.path.join(BASE_DIR, "operators.json")
FEEDS_DIR = os.path.join(BASE_DIR, "feeds")
SORTIE_DIR = os.path.join(BASE_DIR, "pipeline", "sortie")
RECU_DIR = os.path.join(BASE_DIR, "pipeline", "recu")

REPO = "TrainNomad/TrainNomad_GTFS"
RELEASE_TAG = "gtfs-latest"
RELEASE_URL = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}"

REQUIRED_FILES = ["agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"]
BAISSE_MAX_SOURCE = 0.5   # un GTFS qui perd plus de 50 % de ses trajets n'est pas publié (sauf --forcer)
BAISSE_MAX_RESEAU = 0.3   # un réseau qui perd plus de 30 % de ses trajets n'est pas publié (sauf --forcer)
AGE_MAX_JOURS = 8         # au-delà, l'assemblage signale un GTFS non mis à jour (mise à jour hebdomadaire)

logging.basicConfig(level=logging.INFO, format="%(message)s")
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def warn(msg: str):
    print(f"::warning::{msg}" if IN_CI else f"⚠️ {msg}", flush=True)


def error(msg: str):
    print(f"::error::{msg}" if IN_CI else f"❌ {msg}", flush=True)


def summary(lines: list[str]):
    """Résumé affiché sur la page du run GitHub Actions (et dans la console)."""
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def load_operators() -> list[dict]:
    with open(OPERATORS_FILE, encoding="utf-8") as f:
        return json.load(f)


def sources(ops: list[dict]) -> dict[str, dict]:
    out = {}
    for op in ops:
        script = op.get("script_path")
        key = os.path.normpath(script).split(os.sep)[0].lower() if script else op["id"].lower()
        src = out.setdefault(key, {"script": script, "ops": []})
        src["ops"].append(op)
    return out


def http_get(url: str, timeout: int = 120) -> bytes | None:
    """Contenu de l'URL, None si absente (404)."""
    headers = {"User-Agent": "TrainNomad-pipeline"}
    if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fmt(n) -> str:
    """12345 -> « 12 345 » (nombres des résumés)."""
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else "?"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Vérification d'un GTFS
# ---------------------------------------------------------------------------

def count_rows(z: zipfile.ZipFile, name: str) -> int:
    n = 0
    with z.open(name) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return max(n - 1, 0)  # sans l'en-tête (une dernière ligne sans \n compense à peu près)


def service_dates(z: zipfile.ZipFile, files: dict) -> tuple[str, str]:
    """Première et dernière date de circulation (calendar.txt et calendar_dates.txt), AAAAMMJJ."""
    first, last = None, None

    def seen(d: str):
        nonlocal first, last
        if len(d) == 8 and d.isdigit():
            first = d if first is None or d < first else first
            last = d if last is None or d > last else last

    if "calendar.txt" in files:
        with z.open(files["calendar.txt"]) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                seen((row.get("start_date") or "").strip())
                seen((row.get("end_date") or "").strip())
    if "calendar_dates.txt" in files:
        with z.open(files["calendar_dates.txt"]) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                if (row.get("exception_type") or "").strip() == "1":
                    seen((row.get("date") or "").strip())
    return first or "", last or ""


def check_gtfs(path: str) -> dict:
    """Statistiques du GTFS ; lève ValueError s'il est inutilisable."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("fichier ZIP invalide")
    with z:
        bad = z.testzip()
        if bad:
            raise ValueError(f"ZIP corrompu ({bad})")
        files = {os.path.basename(n): n for n in z.namelist() if n.endswith(".txt")}
        missing = [f for f in REQUIRED_FILES if f not in files]
        if missing:
            raise ValueError(f"fichiers manquants : {', '.join(missing)}")
        if "calendar.txt" not in files and "calendar_dates.txt" not in files:
            raise ValueError("ni calendar.txt ni calendar_dates.txt")
        stats = {name[:-4]: count_rows(z, files[name]) for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")}
        if stats["trips"] == 0 or stats["stop_times"] == 0:
            raise ValueError("aucun trajet")
        stats["date_debut"], stats["date_fin"] = service_dates(z, files)
    if stats["date_fin"] and stats["date_fin"] < date.today().strftime("%Y%m%d"):
        raise ValueError(f"horaires expirés (dernier jour de circulation : {stats['date_fin']})")
    return stats


# ---------------------------------------------------------------------------
# preparer : une source -> pipeline/sortie/<OP>.zip + <OP>.json
# ---------------------------------------------------------------------------

def produce(src_name: str, src: dict) -> dict[str, str]:
    """Lance le script de la source (ou télécharge) ; renvoie {id opérateur: chemin du GTFS}."""
    ops = [o for o in src["ops"] if o.get("enabled", True)]
    if src["script"]:
        script = os.path.join(BASE_DIR, src["script"])
        logging.info(f"▶️ {src['script']}")
        r = subprocess.run([sys.executable, script], cwd=BASE_DIR)
        if r.returncode != 0:
            raise RuntimeError(f"{src['script']} a échoué (code {r.returncode})")
        return {o["id"]: os.path.join(BASE_DIR, o["gtfs_path"]) for o in ops}
    sys.path.insert(0, BASE_DIR)
    import build_network  # téléchargement avec les mêmes règles que le build (clé API, repli curl...)
    paths = {}
    for o in ops:
        p = build_network.fetch_feed(o, refresh=True)
        if not p:
            raise RuntimeError(f"téléchargement de {o['id']} impossible ({o.get('gtfs_url')})")
        paths[o["id"]] = p
    return paths


def cmd_preparer(name: str, forcer: bool) -> int:
    all_sources = sources(load_operators())
    if name not in all_sources:
        error(f"source inconnue : {name} (connues : {', '.join(sorted(all_sources))})")
        return 2
    src = all_sources[name]
    if not any(o.get("enabled", True) for o in src["ops"]):
        warn(f"{name} : aucun opérateur activé dans operators.json, rien à publier")
        return 0
    paths = produce(name, src)

    os.makedirs(SORTIE_DIR, exist_ok=True)
    rows = ["| Opérateur | Trajets | Horaires | Circulation | Statut |", "|---|---|---|---|---|"]
    failed = 0
    for op_id, path in paths.items():
        try:
            if not os.path.exists(path):
                raise ValueError(f"{os.path.relpath(path, BASE_DIR)} absent après le script")
            stats = check_gtfs(path)
            prev_raw = http_get(f"{RELEASE_URL}/{op_id}.json")
            prev = json.loads(prev_raw) if prev_raw else None
            if prev and prev.get("trips") and stats["trips"] < prev["trips"] * (1 - BAISSE_MAX_SOURCE):
                msg = f"{stats['trips']} trajets contre {prev['trips']} publiés le {prev.get('built_at', '?')[:10]}"
                if not forcer:
                    raise ValueError(f"baisse anormale : {msg} (relancer avec « forcer » si c'est voulu)")
                warn(f"{op_id} : {msg}, publié quand même (forcer)")
            meta = {
                "operator": op_id,
                "source": name,
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run": os.environ.get("GITHUB_RUN_ID", "local"),
                "size": os.path.getsize(path),
                "sha256": sha256(path),
                **stats,
            }
            shutil.copyfile(path, os.path.join(SORTIE_DIR, f"{op_id}.zip"))
            with open(os.path.join(SORTIE_DIR, f"{op_id}.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            status = "✅ prêt à publier"
            soon = date.fromordinal(date.today().toordinal() + 7).strftime("%Y%m%d")
            if stats["date_fin"] and stats["date_fin"] < soon:
                warn(f"{op_id} : horaires valables seulement jusqu'au {stats['date_fin']}")
                status = "⚠️ expire bientôt"
            rows.append(f"| {op_id} | {fmt(stats['trips'])} | {fmt(stats['stop_times'])} | "
                        f"{stats['date_debut']} → {stats['date_fin']} | {status} |")
        except ValueError as e:
            failed += 1
            error(f"{op_id} : {e} — la version déjà publiée est conservée")
            rows.append(f"| {op_id} | | | | ❌ {e} |")
    summary([f"### GTFS {name}", "", *rows])
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# placer : GTFS publiés -> emplacements lus par build_network.py
# ---------------------------------------------------------------------------

def download_release(dest: str):
    """Télécharge tous les fichiers de la Release gtfs-latest dans dest."""
    os.makedirs(dest, exist_ok=True)
    raw = http_get(f"https://api.github.com/repos/{REPO}/releases/tags/{RELEASE_TAG}", timeout=60)
    if raw is None:
        warn(f"Release {RELEASE_TAG} introuvable : aucun GTFS publié pour l'instant")
        return
    for asset in json.loads(raw).get("assets", []):
        data = http_get(asset["browser_download_url"], timeout=600)
        if data is None:
            warn(f"{asset['name']} : téléchargement impossible")
            continue
        with open(os.path.join(dest, asset["name"]), "wb") as f:
            f.write(data)
        logging.info(f"   ⬇️ {asset['name']} ({len(data) / 1e6:.1f} Mo)")


def cmd_placer(dossier: str, depuis_release: bool) -> int:
    if depuis_release:
        download_release(dossier)
    now = datetime.now(timezone.utc)
    rows = ["| Opérateur | Source | Publié le | Âge | Trajets | Statut |", "|---|---|---|---|---|---|"]
    for name, src in sorted(sources(load_operators()).items()):
        for op in src["ops"]:
            if not op.get("enabled", True):
                continue
            op_id = op["id"]
            zip_path = os.path.join(dossier, f"{op_id}.zip")
            meta_path = os.path.join(dossier, f"{op_id}.json")
            if not os.path.exists(zip_path):
                if op.get("gtfs_path"):
                    warn(f"{op_id} : aucun GTFS publié (lancer le workflow GTFS de la source « {name} ») — absent du réseau")
                    rows.append(f"| {op_id} | {name} | | | | ❌ absent |")
                else:
                    warn(f"{op_id} : aucun GTFS publié, build_network.py le téléchargera directement")
                    rows.append(f"| {op_id} | {name} | | | | ⚠️ téléchargé pendant l'assemblage |")
                continue
            meta = {}
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            if meta.get("sha256") and meta["sha256"] != sha256(zip_path):
                warn(f"{op_id} : empreinte différente de {op_id}.json (publication en cours ?) — fichier utilisé quand même")
            dest = os.path.join(BASE_DIR, op["gtfs_path"]) if op.get("gtfs_path") else os.path.join(FEEDS_DIR, f"{op_id}.zip")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(zip_path, dest)  # date de modification = maintenant : build_network.py le prend en cache
            built = meta.get("built_at", "")
            age = ""
            status = "✅"
            if built:
                days = (now - datetime.fromisoformat(built)).total_seconds() / 86400
                age = f"{days:.1f} j"
                if days > AGE_MAX_JOURS:
                    warn(f"{op_id} : GTFS publié il y a {days:.0f} jours (workflow GTFS de la source « {name} » en échec ?)")
                    status = "⚠️ ancien"
            rows.append(f"| {op_id} | {name} | {built[:16].replace('T', ' ')} | {age} | {fmt(meta.get('trips'))} | {status} |")
    summary(["### GTFS utilisés pour l'assemblage", "", *rows])
    return 0


# ---------------------------------------------------------------------------
# verifier-reseau : garde-fou avant de publier network.bin.gz
# ---------------------------------------------------------------------------

def cmd_verifier(ancien: str, nouveau: str, forcer: bool) -> int:
    with open(nouveau, encoding="utf-8") as f:
        new = json.load(f)
    old = {}
    if os.path.exists(ancien) and os.path.getsize(ancien) > 0:
        with open(ancien, encoding="utf-8") as f:
            old = json.load(f)
    problems = []
    if not new.get("trips"):
        problems.append("le réseau ne contient aucun trajet")
    if old.get("trips") and new.get("trips", 0) < old["trips"] * (1 - BAISSE_MAX_RESEAU):
        problems.append(f"{new.get('trips', 0)} trajets contre {old['trips']} dans le réseau publié")
    rows = ["| Opérateur | Réseau publié | Nouveau réseau | Évolution |", "|---|---|---|---|"]
    ops = sorted({k[6:] for k in list(old) + list(new)
                  if k.startswith("trips_") and k not in ("trips_merged", "trips_out_of_window")
                  and not k.endswith(("_replaced", "_duplicates"))})
    for op in ops:
        a, b = old.get(f"trips_{op}", 0), new.get(f"trips_{op}", 0)
        evo = f"{(b - a) / a:+.0%}" if a else "nouveau"
        if a and not b:
            problems.append(f"{op} a disparu du réseau ({a} trajets auparavant)")
            evo = "❌ disparu"
        elif a and b < a * (1 - BAISSE_MAX_SOURCE):
            warn(f"{op} : {b} trajets contre {a} ({evo})")
            evo = f"⚠️ {evo}"
        rows.append(f"| {op} | {fmt(a)} | {fmt(b)} | {evo} |")
    rows.append(f"| **Total** | {fmt(old.get('trips', 0))} | {fmt(new.get('trips', 0))} | |")
    summary(["### Contrôle du réseau avant publication", "", *rows])
    if problems:
        for p in problems:
            (warn if forcer else error)(p)
        if not forcer:
            error("réseau non publié : l'API garde la version actuelle (relancer avec « forcer » si c'est voulu)")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("liste")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("preparer")
    p.add_argument("source")
    p.add_argument("--forcer", action="store_true")
    p = sub.add_parser("placer")
    p.add_argument("--dossier", default=RECU_DIR)
    p.add_argument("--depuis-release", action="store_true")
    p = sub.add_parser("verifier-reseau")
    p.add_argument("ancien")
    p.add_argument("nouveau")
    p.add_argument("--forcer", action="store_true")
    args = parser.parse_args()

    if args.cmd == "liste":
        if args.json:
            print(json.dumps(sorted(n for n, s in sources(load_operators()).items()
                                    if any(o.get("enabled", True) for o in s["ops"]))))
            return 0
        for name, src in sorted(sources(load_operators()).items()):
            ops = ", ".join(o["id"] + ("" if o.get("enabled", True) else " (désactivé)") for o in src["ops"])
            print(f"{name:18s} {src['script'] or 'téléchargement direct':42s} {ops}")
        return 0
    if args.cmd == "preparer":
        return cmd_preparer(args.source, args.forcer)
    if args.cmd == "placer":
        return cmd_placer(args.dossier, args.depuis_release)
    return cmd_verifier(args.ancien, args.nouveau, args.forcer)


if __name__ == "__main__":
    sys.exit(main())
