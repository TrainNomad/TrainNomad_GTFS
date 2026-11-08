"""Temps réel SNCF (GTFS-RT) : retards, suppressions et messages de perturbation.

Sources officielles et gratuites (transport.data.gouv.fr), mises à jour en continu :
  - trip-updates   : pour chaque train du jour, heure réelle et retard à chaque arrêt, trains supprimés
  - service-alerts : messages voyageurs (travaux, placement dans le train, incidents...)

Un train est désigné par son **numéro + date de circulation** : c'est ce que l'API Europe renvoie déjà
pour chaque trajet (operator = "SNCF", train_number, departure). Le flux donne aussi l'heure prévue
(heure réelle - retard), il n'a donc pas besoin du GTFS théorique.

Utilisation :
    python temps_reel/sncf.py                    # résumé : trains en retard / supprimés en ce moment
    python temps_reel/sncf.py 6193               # état du train 6193 aujourd'hui
    python temps_reel/sncf.py 6193 2026-11-06    # ... à une date donnée
    python temps_reel/sncf.py 6193 --de "Paris Gare de Lyon" --a Marseille   # trajet d'un voyageur

Dépendances : requests, gtfs-realtime-bindings
"""

from __future__ import annotations

import csv
import html
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date as Date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2 as rt

URL_RETARDS = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
URL_MESSAGES = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"

PARIS = ZoneInfo("Europe/Paris")
STATIONS_CSV = Path(__file__).resolve().parent.parent / "stations.csv"

# "OCESN6193F1187_F:OUI:..." -> train 6193 ; suffixe R = car de substitution ("OCESN468256R...")
RE_NUMERO = re.compile(r"^OCESN(\d+)([A-Z])")
# "StopPoint:OCETGV INOUI-87686006" ou "StopArea:OCE87686006" -> code UIC 87686006
RE_UIC = re.compile(r"(\d{8})$")

_RS = rt.TripDescriptor.ScheduleRelationship
_RS_ARRET = rt.TripUpdate.StopTimeUpdate.ScheduleRelationship


@dataclass
class Arret:
    uic: str
    gare: str
    arrivee_prevue: str | None = None   # "HH:MM" heure de Paris
    arrivee_reelle: str | None = None
    retard_arrivee_min: int = 0
    depart_prevu: str | None = None
    depart_reel: str | None = None
    retard_depart_min: int = 0
    supprime: bool = False              # arrêt non desservi


@dataclass
class EtatTrain:
    numero: str
    date: str                           # "AAAA-MM-JJ" (date de départ du train)
    etat: str                           # "a_l_heure" | "retard" | "supprime" | "ajoute"
    retard_max_min: int = 0
    car_de_substitution: bool = False
    arrets: list[Arret] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    maj: str | None = None              # heure de la dernière information sur ce train


@dataclass
class EtatTrajet:
    """Ce qui intéresse un voyageur : sa montée et sa descente."""
    numero: str
    date: str
    etat: str                           # "inconnu" | "a_l_heure" | "retard" | "supprime" | "arret_supprime"
    depart_prevu: str | None = None
    depart_reel: str | None = None
    retard_depart_min: int = 0
    arrivee_prevue: str | None = None
    arrivee_reelle: str | None = None
    retard_arrivee_min: int = 0
    messages: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------
# Référentiel des gares (nom à partir du code UIC)

_noms: dict[str, str] | None = None
_noms7: dict[str, str] = {}


def nom_gare(uic: str) -> str:
    """Nom de la gare. Le flux met un 0 à la place de la clé de contrôle pour les gares étrangères
    (Luxembourg 82001000 au lieu de 82001008) : on compare alors les 7 premiers chiffres."""
    global _noms
    if _noms is None:
        _noms = {}
        if STATIONS_CSV.exists():
            with open(STATIONS_CSV, encoding="utf-8") as f:
                for r in csv.DictReader(f, delimiter=";"):
                    principale = r.get("is_main_station") == "t"
                    # en cas de doublon, la gare principale l'emporte
                    for index, code in ((_noms, r.get("uic8_sncf") or ""), (_noms7, (r.get("uic") or "")[:7])):
                        if code and (code not in index or principale):
                            index[code] = r["name"]
    return _noms.get(uic) or _noms7.get(uic[:7]) or uic


# --------------------------------------------------------------------------------------------------
# Lecture des flux

def _telecharger(url: str) -> rt.FeedMessage:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        contenu = r.content
    except requests.exceptions.SSLError:
        # Comme build_network.py : si le certificat intermédiaire manque côté Python, curl sait le récupérer
        contenu = subprocess.run(["curl", "-sSfL", "--max-time", "30", url],
                                 check=True, capture_output=True).stdout
    flux = rt.FeedMessage()
    flux.ParseFromString(contenu)
    return flux


def _hhmm(ts: int) -> str | None:
    return datetime.fromtimestamp(ts, PARIS).strftime("%H:%M") if ts else None


def _texte_fr(traduction: rt.TranslatedString) -> str:
    textes = {t.language or "": t.text for t in traduction.translation}
    texte = textes.get("fr") or next(iter(textes.values()), "")
    return html.unescape(re.sub(r"<[^>]+>", " ", texte)).replace("\xa0", " ").strip()


def _numero(trip_id: str) -> tuple[str, bool] | None:
    m = RE_NUMERO.match(trip_id)
    return (m.group(1), m.group(2) == "R") if m else None


def _date_iso(start_date: str) -> str:
    return f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}" if len(start_date) == 8 else ""


def lire_messages(flux: rt.FeedMessage, maintenant: int) -> dict[str, list[str]]:
    """Messages actifs par numéro de train."""
    par_train: dict[str, list[str]] = {}
    for e in flux.entity:
        a = e.alert
        if a.active_period and not any(
            (not p.start or p.start <= maintenant) and (not p.end or maintenant <= p.end) for p in a.active_period
        ):
            continue
        titre = _texte_fr(a.header_text)
        detail = _texte_fr(a.description_text)
        texte = titre if not detail or detail.startswith(titre) else f"{titre} — {detail}"
        if not texte:
            continue
        for ie in a.informed_entity:
            n = _numero(ie.trip.trip_id) if ie.trip.trip_id else None
            if n and texte not in par_train.setdefault(n[0], []):
                par_train[n[0]].append(texte)
    return par_train


def lire_retards(flux: rt.FeedMessage, messages: dict[str, list[str]]) -> dict[tuple[str, str], EtatTrain]:
    """État de chaque train, indexé par (numéro, date "AAAA-MM-JJ")."""
    trains: dict[tuple[str, str], EtatTrain] = {}
    for e in flux.entity:
        tu = e.trip_update
        n = _numero(tu.trip.trip_id)
        if not n:
            continue
        numero, car = n
        jour = _date_iso(tu.trip.start_date)
        rel = tu.trip.schedule_relationship

        arrets = []
        for s in tu.stop_time_update:
            m = RE_UIC.search(s.stop_id)
            uic = m.group(1) if m else s.stop_id
            ra = s.arrival.delay if s.HasField("arrival") else 0
            rd = s.departure.delay if s.HasField("departure") else 0
            ta = s.arrival.time if s.HasField("arrival") else 0
            td = s.departure.time if s.HasField("departure") else 0
            arrets.append(Arret(
                uic=uic, gare=nom_gare(uic),
                arrivee_prevue=_hhmm(ta - ra) if ta else None, arrivee_reelle=_hhmm(ta),
                retard_arrivee_min=round(ra / 60),
                depart_prevu=_hhmm(td - rd) if td else None, depart_reel=_hhmm(td),
                retard_depart_min=round(rd / 60),
                supprime=s.schedule_relationship == _RS_ARRET.SKIPPED,
            ))

        retard_max = max([max(a.retard_arrivee_min, a.retard_depart_min) for a in arrets] or [0])
        if rel == _RS.CANCELED:
            etat = "supprime"
        elif rel == _RS.ADDED and not car:
            etat = "ajoute"
        else:
            etat = "retard" if retard_max >= 5 else "a_l_heure"

        train = EtatTrain(
            numero=numero, date=jour, etat=etat, retard_max_min=retard_max, car_de_substitution=car,
            arrets=arrets, messages=list(messages.get(numero, [])),
            maj=_hhmm(tu.timestamp) if tu.timestamp else None,
        )
        # Un même numéro peut apparaître deux fois (train + car de substitution) : le train l'emporte
        cle = (numero, jour)
        if cle not in trains or trains[cle].car_de_substitution:
            trains[cle] = train
    return trains


class TempsReelSNCF:
    """Instantané du temps réel SNCF. `actualiser()` à appeler toutes les 30 à 60 s."""

    def __init__(self):
        self.trains: dict[tuple[str, str], EtatTrain] = {}
        self.horodatage: datetime | None = None

    def actualiser(self) -> "TempsReelSNCF":
        retards = _telecharger(URL_RETARDS)
        try:
            messages = lire_messages(_telecharger(URL_MESSAGES), retards.header.timestamp)
        except (requests.RequestException, subprocess.CalledProcessError):
            messages = {}   # les messages sont un plus : leur absence ne bloque pas les retards
        self.trains = lire_retards(retards, messages)
        self.horodatage = datetime.fromtimestamp(retards.header.timestamp, PARIS)
        return self

    def train(self, numero: str, jour: str | Date | None = None) -> EtatTrain | None:
        jour = (jour or datetime.now(PARIS).date())
        jour = jour.isoformat() if isinstance(jour, Date) else jour
        return self.trains.get((str(int(numero)) if str(numero).isdigit() else str(numero), jour))

    def trajet(self, numero: str, jour: str | Date | None = None,
               depart: str | None = None, arrivee: str | None = None) -> EtatTrajet:
        """État d'un trajet voyageur. `depart` / `arrivee` : code UIC, nom de gare (début du nom)
        ou heure prévue "HH:MM" (ce que l'API Europe renvoie pour chaque trajet)."""
        t = self.train(numero, jour)
        jour_txt = (jour.isoformat() if isinstance(jour, Date) else jour) or datetime.now(PARIS).date().isoformat()
        if t is None:
            # Pas dans le flux : la SNCF ne publie que les trains du jour qui ont une information
            return EtatTrajet(numero=str(numero), date=jour_txt, etat="inconnu")

        def trouver(cle: str | None, champ: str) -> Arret | None:
            if not cle:
                return None
            cle_n = cle.lower()
            for a in t.arrets:
                if cle in (a.uic, getattr(a, champ)) or a.gare.lower().startswith(cle_n):
                    return a
            return None

        a_dep = trouver(depart, "depart_prevu") or (t.arrets[0] if t.arrets else None)
        a_arr = trouver(arrivee, "arrivee_prevue") or (t.arrets[-1] if t.arrets else None)

        r = EtatTrajet(numero=t.numero, date=t.date, etat=t.etat if t.etat != "ajoute" else "a_l_heure",
                       messages=t.messages)
        if a_dep:
            r.depart_prevu, r.depart_reel, r.retard_depart_min = a_dep.depart_prevu, a_dep.depart_reel, a_dep.retard_depart_min
        if a_arr:
            r.arrivee_prevue, r.arrivee_reelle, r.retard_arrivee_min = a_arr.arrivee_prevue, a_arr.arrivee_reelle, a_arr.retard_arrivee_min
        if r.etat != "supprime":
            if (a_dep and a_dep.supprime) or (a_arr and a_arr.supprime):
                r.etat = "arret_supprime"
            else:
                r.etat = "retard" if max(r.retard_depart_min, r.retard_arrivee_min) >= 5 else "a_l_heure"
        return r


# --------------------------------------------------------------------------------------------------
# Ligne de commande (test)

def _afficher_train(t: EtatTrain):
    titre = {"a_l_heure": "à l'heure", "retard": f"retard jusqu'à {t.retard_max_min} min",
             "supprime": "SUPPRIMÉ", "ajoute": "train ajouté"}[t.etat]
    print(f"Train {t.numero} du {t.date}{' (car de substitution)' if t.car_de_substitution else ''} : {titre}"
          f"{f'  [info de {t.maj}]' if t.maj else ''}")
    for a in t.arrets:
        prevu = a.depart_prevu or a.arrivee_prevue or "--:--"
        reel = a.depart_reel or a.arrivee_reelle or "--:--"
        retard = max(a.retard_arrivee_min, a.retard_depart_min)
        note = " non desservi" if a.supprime else (f" +{retard} min" if retard else "")
        print(f"  {prevu} → {reel}  {a.gare}{note}")
    for m in t.messages:
        print(f"  ⚠ {m}")


def main(args: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tr = TempsReelSNCF().actualiser()
    print(f"Temps réel SNCF du {tr.horodatage:%d/%m/%Y %H:%M:%S} : {len(tr.trains)} trains\n")

    opts = {}
    pos = []
    i = 0
    while i < len(args):
        if args[i] in ("--de", "--a") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
        else:
            pos.append(args[i])
            i += 1

    if not pos:
        par_etat: dict[str, list[EtatTrain]] = {}
        for t in tr.trains.values():
            par_etat.setdefault(t.etat, []).append(t)
        for etat in ("a_l_heure", "retard", "supprime", "ajoute"):
            print(f"  {etat:10} {len(par_etat.get(etat, []))}")
        print("\nPlus gros retards :")
        for t in sorted(par_etat.get("retard", []), key=lambda t: -t.retard_max_min)[:10]:
            print(f"  {t.numero:>7}  {t.date}  +{t.retard_max_min} min  "
                  f"{t.arrets[0].gare if t.arrets else ''} → {t.arrets[-1].gare if t.arrets else ''}")
        return 0

    numero, jour = pos[0], (pos[1] if len(pos) > 1 else None)
    t = tr.train(numero, jour)
    if t is None:
        print(f"Train {numero} : aucune information temps réel (pas encore publié, ou pas un train SNCF du jour)")
        return 1
    _afficher_train(t)
    if opts:
        print()
        print(asdict(tr.trajet(numero, jour, opts.get("--de"), opts.get("--a"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
