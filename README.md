# TrainNomad — GTFS et réseau Europe

Ce dépôt construit le réseau ferroviaire lu par l'API Europe (`network.bin.gz`) à partir des GTFS de
chaque compagnie. Il ne contient **que du code et les référentiels de gares** : tout ce qu'un build
produit est publié dans les Releases GitHub, jamais dans l'historique git.

## Fonctionnement

```
Workflows « GTFS · <compagnie> »  (un par compagnie, le dimanche entre 00:05 et 01:30 UTC, ou à la main)
   script d'ingestion ou téléchargement → contrôles → Release gtfs-latest : <OP>.zip + <OP>.json
                                                       (remplacés à chaque fois, seule la dernière version existe)
Workflow « Réseau Europe (assemblage) »  (dimanche 04:00 UTC, ou à la main)
   GTFS de gtfs-latest → build_network.py → test avec le moteur Europe → contrôle → Release network-latest
   → POST /reload sur l'API
```

| Release | Contenu | Lu par |
|---|---|---|
| `gtfs-latest` | un GTFS vérifié par opérateur + sa fiche (date, empreinte, trajets, période de circulation) | l'assemblage, `placer --depuis-release` en local |
| `network-latest` | `network.bin.gz`, `harmonization_report.json` | l'API Europe |

## Compagnies (sources)

Une source = un script d'ingestion de `operators.json` (`script_path`) et les opérateurs qu'il produit, ou un
opérateur simplement téléchargé depuis son `gtfs_url`. `python pipeline/gtfs_source.py liste` les affiche.

| Workflow | Source | Opérateurs |
|---|---|---|
| GTFS · France (SNCF) | `sncf` | SNCF |
| GTFS · Espagne (Renfe) | `renfe` | RENFE |
| GTFS · Espagne (Ouigo España) | `ouigo_es` | OUIGO_ES (secret `OUIGO_ES_API_KEY`) |
| GTFS · Eurostar | `eurostar` | EUROSTAR |
| GTFS · European Sleeper | `european_sleeper` | EUROPEAN_SLEEPER |
| GTFS · Portugal (CP) | `cp` | CP (+ prolongement Elvas → Badajoz) |
| GTFS · Belgique (SNCB) | `sncb` | SNCB |
| GTFS · Suisse | `swiss` | SWISS |
| GTFS · Royaume-Uni (National Rail) | `uk` | NATIONAL_RAIL (secrets `NR_EMAIL`, `NR_PASSWORD`) |
| GTFS · Allemagne (DB) | `germany` | DB, DB_REGIO |
| GTFS · Italie (Trenitalia + Italo) | `trenitalia` | TRENITALIA, ITALO |
| GTFS · FlixTrain | `flixtrain` | FLIXTRAIN |
| GTFS · Pays-Bas (NS) | `netherlands` | NS |
| GTFS · Italie (Trenord) | `trenord` | TRENORD |
| GTFS · Pologne (PKP Intercity, PolRegio...) | `poland` | PKP (désactivé : référentiel des gares à construire) |
| GTFS · Slovaquie (ZSSK, RegioJet, Leo Express) | `slovakia` | ZSSK (désactivé : référentiel des gares à construire) |
| GTFS · Hongrie (MÁV, GYSEV) | `hungary` | MAV (désactivé : référentiel des gares à construire) |
| GTFS · Irlande (Irish Rail, Enterprise) | `ireland` | IRISH_RAIL (désactivé : référentiel des gares à construire) |
| GTFS · Irlande du Nord (NI Railways) | `northern_ireland` | NI_RAILWAYS (TransXChange converti ; désactivé : référentiel des gares à construire) |
| GTFS · Ferries (Brittany Ferries, Transmanche) | `ferries` | BRITTANY_FERRIES, TRANSMANCHE (désactivés : référentiel des ports à construire) |

## Contrôles (fiabilité)

Rien de cassé n'est publié : en cas de refus, la version précédente reste en place et le run est en échec
(notification GitHub par e-mail).

- **GTFS d'une compagnie** : ZIP lisible, fichiers obligatoires présents, au moins un trajet, horaires non
  expirés, et pas plus de 30 % de **trains par jour** en moins que la version publiée (médiane des 14 prochains
  jours ; avertissement au-delà de 15 %). On compte les trains qui circulent, pas les lignes du fichier : une
  compagnie qui réécrit son GTFS sans changer son offre (SNCB, novembre 2026) passe sans « forcer ». Avertissement
  si les horaires expirent dans moins de 7 jours.
- **Assemblage** : avertissement si le GTFS d'une compagnie a plus de 8 jours (son workflow échoue).
- **Réseau** : testé avec le vrai moteur Europe (dépôt TrainNomad_SQL) sur une relation par pays ; refusé si
  une relation ne trouve plus de trajet, si un opérateur disparaît ou si le réseau perd plus de 30 % de ses
  trajets.
- **Passer outre** (changement voulu : fin d'un service, nouvelle source) : relancer à la main avec « forcer ».

Le résumé de chaque run (page du run sur GitHub) affiche les tableaux de ces contrôles.

## Cas courants

- **Mettre à jour une compagnie** : Actions → « GTFS · … » → Run workflow (le réseau est reconstruit ensuite).
- **Ajouter un pays** : script d'ingestion dans `<PAYS>/`, opérateur dans `operators.json` (`gtfs_path`,
  `script_path`), puis un workflow `gtfs-<source>.yml` copié d'un autre (changer `name`, `source`, l'heure).
  Lancer ce workflow : seul ce pays est construit, l'assemblage reprend les autres déjà publiés.
- **Tout reconstruire** (première mise en service, panne générale) : Actions → « Tout reconstruire ».
- **Compiler en local avec les derniers GTFS publiés** :
  ```sh
  python pipeline/gtfs_source.py placer --depuis-release
  python build_network.py
  ```
- **Préparer une compagnie en local** : `python pipeline/gtfs_source.py preparer cp` (résultat dans `pipeline/sortie/`).

## Temps réel (retards, suppressions)

`temps_reel/` lit les flux temps réel des compagnies (GTFS-RT) et donne l'état de chaque train, désigné
par **numéro + date de circulation** : la même clé que `train_number` / `departure` des trajets de l'API Europe.

| Compagnie | Module | Source |
|---|---|---|
| SNCF | `temps_reel/sncf.py` | transport.data.gouv.fr : `sncf-gtfs-rt-trip-updates` (retards, trains supprimés) et `sncf-gtfs-rt-service-alerts` (messages), sans clé |

```sh
pip install requests gtfs-realtime-bindings
python temps_reel/sncf.py                                   # résumé : retards et suppressions en cours
python temps_reel/sncf.py 6193                              # un train, arrêt par arrêt
python temps_reel/sncf.py 6193 --de Paris --a Avignon       # le trajet d'un voyageur (montée / descente)
```

En Python : `TempsReelSNCF().actualiser()` (toutes les 30 à 60 s), puis `.train(numero, date)` ou
`.trajet(numero, date, depart, arrivee)`. Les gares de montée et de descente se donnent par code UIC, par
nom ou par heure prévue `"HH:MM"`. Le flux ne contient que les trains du jour : un train absent est `inconnu`.

## Référentiel des gares

`referentiel/` : un script par compagnie (`build_gares_<op>.py`), la fusion (`fusion.py`) et la base
(`gares.csv`, `villes.csv`, `codes.csv`), lue par `build_network.py`. Les caches des API et les sources
téléchargées (`referentiel/cache/`, `referentiel/sources_officielles/`) restent en local. `stations.csv`
(Trainline) complète le référentiel pour les compagnies qui n'y sont pas encore.
