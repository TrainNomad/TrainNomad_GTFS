"""
Horaires Irish Ferries Dublin <-> Cherbourg : PDF de la compagnie -> FERRIES/irish_ferries_traversees.csv.

Irish Ferries ne publie ni GTFS ni NeTEx. Ses horaires sont des PDF (export Excel) liés depuis la page de la
ligne (ROUTE_PAGE) : un tableau par mois et par sens, une colonne par jour, avec trois lignes (navire, heure de
départ, heure d'arrivée le lendemain), toutes les heures en heure locale du port. Les jours sans traversée
sont des cases vides : le texte brut du PDF les saute et décale tout ; on lit donc la position de chaque mot
et on rattache chaque heure à la colonne du jour la plus proche.

Contrôles : jour de la semaine imprimé = jour calculé, navire + départ + arrivée présents pour chaque jour
servi, durée plausible. En cas de doute : erreur, le CSV n'est pas réécrit.

Le CSV est versionné : FERRIES/ingest_ferries.py en fait le GTFS (flux « irish »), sans lire de PDF. À relancer
quand Irish Ferries publie un nouveau PDF (nouvelle saison, horaires modifiés).

Dépendance locale (pas utilisée par la CI) : pip install pymupdf

Usage :
    python FERRIES/irish_ferries_pdf.py --telecharger          PDF trouvés sur la page de la ligne
    python FERRIES/irish_ferries_pdf.py a.pdf b.pdf            PDF déjà téléchargés
"""
import argparse
import csv
import os
import re
import sys
import tempfile
import urllib.request
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(BASE_DIR, "irish_ferries_traversees.csv")
SITE = "https://www.irishferries.com"
ROUTE_PAGE = SITE + "/ie-en/routes-and-times/rosslare-france/"  # page Dublin - Cherbourg malgré son adresse
UA = {"User-Agent": "Mozilla/5.0 (TrainNomad GTFS)"}

MONTHS = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                                      "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
SHIPS = {"WBY": "W.B. Yeats", "IIS": "Isle of Inisheer", "JJ": "James Joyce"}
DIRECTIONS = {"Dublin": "DUB-CER", "Cherbourg": "CER-DUB"}
LABEL_X = 260   # libellés (mois, sens) à gauche de cette abscisse, cases des jours à droite
COL_TOL = 12    # écart maximal (pt) entre une case et le centre de la colonne de son jour
ROW_TOL = 3     # mots d'une même ligne : ordonnées à moins de ROW_TOL pt


def find_pdf_links() -> list[str]:
    req = urllib.request.Request(ROUTE_PAGE, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    links = sorted(set(re.findall(r'["\'](/globalassets/[^"\']*/schedule/[^"\']+\.pdf)["\']', html, re.I)))
    if not links:
        sys.exit(f"❌ aucun PDF d'horaires trouvé sur {ROUTE_PAGE}")
    return [SITE + link for link in links]


def download(url: str, dest_dir: str) -> str:
    path = os.path.join(dest_dir, os.path.basename(url))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    print(f"   ⬇️  {url}")
    return path


def rows_of(page) -> list[tuple[float, list[tuple[float, str]]]]:
    """Mots de la page groupés en lignes : [(y, [(x du centre, mot), ...])], de haut en bas."""
    rows: list[tuple[float, list]] = []
    for x0, y0, x1, _y1, w, *_ in sorted(page.get_text("words"), key=lambda t: t[1]):
        if rows and abs(rows[-1][0] - y0) <= ROW_TOL:
            rows[-1][1].append(((x0 + x1) / 2, w))
        else:
            rows.append((y0, [((x0 + x1) / 2, w)]))
    return [(y, sorted(ws)) for y, ws in rows]


def cells(words) -> list[tuple[float, str]]:
    return [(x, w) for x, w in words if x > LABEL_X]


def label(words) -> str:
    return " ".join(w for x, w in words if x <= LABEL_X)


def is_day_row(words) -> bool:
    c = [w for _, w in cells(words)]
    return len(c) >= 28 and c == [str(i) for i in range(1, len(c) + 1)]


def parse_pdf(path: str) -> list[dict]:
    import fitz  # pymupdf

    doc = fitz.open(path)
    title = " ".join(t[4] for t in doc[0].get_text("words"))
    m = re.search(r"Schedule\s+(20\d\d)", title)
    if not m:
        raise ValueError(f"{path} : année du tableau introuvable (« Sailing Schedule AAAA »)")
    base_year = int(m.group(1))
    out = []
    for page in doc:
        rows = rows_of(page)
        day_idx = [i for i, (_, ws) in enumerate(rows) if is_day_row(ws)]
        year, prev_month = base_year, 0
        for k, i in enumerate(day_idx):
            y_day, day_words = rows[i]
            cols = {x: int(w) for x, w in cells(day_words)}
            # mois : libellé à gauche, quelques points au-dessus de la ligne des jours
            near = [(abs(y - y_day), ws) for y, ws in rows if abs(y - y_day) < 20 and label(ws)]
            month_words = [w for _, ws in sorted(near, key=lambda t: t[0]) for x, w in ws if x <= LABEL_X]
            month = next((MONTHS[w] for w in month_words if w in MONTHS), None)
            if month is None:
                raise ValueError(f"{path} p{page.number + 1} : mois introuvable près de y={y_day:.0f}")
            explicit = next((int(w) for w in month_words if re.fullmatch(r"20\d\d", w)), None)
            if explicit:
                year = explicit
            elif month < prev_month:
                year += 1
            prev_month = month
            # jours de la semaine : ligne au-dessus (le nom du mois peut s'intercaler), contrôle année et mois
            wd_row = next((ws for _, ws in reversed(rows[max(0, i - 4):i])
                           if cells(ws) and all(w in WEEKDAYS for _, w in cells(ws))), [])
            if not wd_row:
                raise ValueError(f"{path} : {month:02d}/{year} ligne des jours de la semaine introuvable")
            for x, w in cells(wd_row):
                d = cols.get(min(cols, key=lambda c: abs(c - x)))
                if w in WEEKDAYS and WEEKDAYS[date(year, month, d).weekday()] != w:
                    raise ValueError(f"{path} : {d:02d}/{month:02d}/{year} imprimé {w}, calculé "
                                     f"{WEEKDAYS[date(year, month, d).weekday()]} (année ou mois mal lu)")
            # lignes du bloc, jusqu'à la ligne des jours suivante ; première occurrence de chaque type
            end = day_idx[k + 1] if k + 1 < len(day_idx) else len(rows)
            ship_row = dep_row = arr_row = None
            direction = None
            for _, ws in rows[i + 1:end]:
                lab, c = label(ws), [w for _, w in cells(ws)]
                if lab.startswith("Arrival"):
                    arr_row = arr_row or ws
                elif lab.split(" ")[0] in DIRECTIONS and dep_row is None:
                    direction, dep_row = DIRECTIONS[lab.split(" ")[0]], ws
                elif c and all(w in SHIPS for w in c):
                    ship_row = ship_row or ws
            if dep_row is None:
                continue  # mois vide
            if ship_row is None or arr_row is None:
                raise ValueError(f"{path} : {month:02d}/{year} bloc incomplet (navire ou arrivée manquant)")

            def by_day(ws, what):
                res = {}
                for x, w in cells(ws):
                    cx = min(cols, key=lambda c: abs(c - x))
                    if abs(cx - x) > COL_TOL:
                        raise ValueError(f"{path} : {month:02d}/{year} {what} « {w} » hors colonne (x={x:.0f})")
                    res[cols[cx]] = w
                return res

            ships, deps, arrs = by_day(ship_row, "navire"), by_day(dep_row, "départ"), by_day(arr_row, "arrivée")
            if not (set(ships) == set(deps) == set(arrs)):
                raise ValueError(f"{path} : {month:02d}/{year} jours incohérents entre navire / départ / arrivée")
            for d in sorted(deps):
                dep, arr = deps[d], arrs[d]
                if not (re.fullmatch(r"\d{4}", dep) and re.fullmatch(r"\d{4}", arr)):
                    raise ValueError(f"{path} : {d:02d}/{month:02d}/{year} heure illisible {dep} / {arr}")
                # durée en heures locales (arrivée le lendemain) : 16 h à 23 h selon le sens et le navire
                dur = (24 * 60 + int(arr[:2]) * 60 + int(arr[2:])) - (int(dep[:2]) * 60 + int(dep[2:]))
                if not 16 * 60 <= dur <= 23 * 60:
                    raise ValueError(f"{path} : {d:02d}/{month:02d}/{year} durée invraisemblable {dep} -> {arr}")
                out.append({"date": date(year, month, d).isoformat(), "sens": direction, "navire": ships[d],
                            "depart": f"{dep[:2]}:{dep[2:]}", "arrivee": f"{arr[:2]}:{arr[2:]}"})
    if not out:
        raise ValueError(f"{path} : aucune traversée lue")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*", help="PDF d'horaires Irish Ferries (France)")
    ap.add_argument("--telecharger", action="store_true", help="prendre les PDF liés depuis la page de la ligne")
    args = ap.parse_args()
    if not args.pdfs and not args.telecharger:
        ap.error("donner des PDF ou --telecharger")
    with tempfile.TemporaryDirectory() as tmp:
        pdfs = list(args.pdfs) + ([download(u, tmp) for u in find_pdf_links()] if args.telecharger else [])
        sailings: dict[tuple, dict] = {}
        for p in pdfs:
            rows = parse_pdf(p)
            first, last = min(r["date"] for r in rows), max(r["date"] for r in rows)
            print(f"   📄 {os.path.basename(p)} : {len(rows)} traversées du {first} au {last}")
            for r in rows:
                key = (r["date"], r["sens"])
                if key in sailings and sailings[key] != r:
                    print(f"   ⚠️ {key} différent d'un PDF à l'autre : {sailings[key]} / {r} (dernier gardé)")
                sailings[key] = r
    rows = sorted(sailings.values(), key=lambda r: (r["date"], r["sens"]))
    tmp_out = OUT_CSV + ".part"
    with open(tmp_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "sens", "navire", "depart", "arrivee"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp_out, OUT_CSV)
    n = {s: sum(r["sens"] == s for r in rows) for s in DIRECTIONS.values()}
    print(f"✅ {OUT_CSV} : {len(rows)} traversées ({n['DUB-CER']} Dublin -> Cherbourg, "
          f"{n['CER-DUB']} Cherbourg -> Dublin), du {rows[0]['date']} au {rows[-1]['date']}")
    lendemain = date.fromisoformat(rows[-1]["date"]) + timedelta(days=1)
    print(f"   ℹ️  horaires connus jusqu'au {lendemain} : relancer quand un nouveau PDF paraît")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
