"""
Test du réseau compilé avec le vrai moteur Europe (dépôt TrainNomad_SQL), avant publication.

Le workflow lance l'API sur network.bin.gz, puis ce script vérifie qu'elle répond et qu'une relation
par pays trouve des trajets (J+3, 8 h). Une relation sans résultat = un pays cassé : réseau non publié.

Usage :
    python pipeline/tester_reseau.py [http://localhost:8000] [--forcer]
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

RELATIONS = [
    ("Paris", "Lyon"),            # SNCF
    ("Paris", "London"),          # Eurostar
    ("Madrid", "Barcelona"),      # Renfe
    ("Lisboa", "Porto"),          # CP
    ("London", "Edinburgh"),      # National Rail
    ("Milano", "Roma"),           # Trenitalia / Italo
    ("Zürich", "Genève"),         # Suisse
    ("Bruxelles", "Liège"),       # SNCB
    ("Berlin", "München"),        # DB
]
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def get(url: str, timeout: int = 120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    forcer = "--forcer" in sys.argv
    base = (args[0] if args else "http://localhost:8000").rstrip("/")

    for _ in range(180):  # le chargement du réseau prend quelques secondes à une minute
        try:
            health = get(f"{base}/health", timeout=5)
            break
        except OSError:
            time.sleep(1)
    else:
        print("::error::l'API ne répond pas sur /health" if IN_CI else "❌ l'API ne répond pas")
        return 1

    day = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    rows = ["| Relation | Trajets |", "|---|---|"]
    failures = []
    for a, b in RELATIONS:
        q = urllib.parse.urlencode({"from": a, "to": b, "date": day, "time": "08:00"})
        try:
            n = len(get(f"{base}/search?{q}").get("journeys") or [])
        except OSError as e:
            n, err = 0, str(e)
        else:
            err = ""
        rows.append(f"| {a} → {b} | {n if n else '❌ ' + (err or 'aucun')} |")
        if not n:
            failures.append(f"{a} → {b}")

    lines = [f"### Test du moteur ({health.get('trips')} trajets, horaires du {health.get('valid_from')} "
             f"au {health.get('valid_to')}, chargé en {health.get('load_ms')} ms)", "", *rows]
    text = "\n".join(lines) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)

    if failures:
        level = "warning" if forcer else "error"
        msg = f"aucun trajet trouvé pour : {', '.join(failures)}"
        print(f"::{level}::{msg}" if IN_CI else msg)
        return 0 if forcer else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
