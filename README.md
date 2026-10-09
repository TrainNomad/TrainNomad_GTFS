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
| GTFS · Allemagne (DB) | `germany` | DB, DB_REGIO (désactivé) |
| GTFS · Italie (Trenitalia + Italo) | `trenitalia` | TRENITALIA, ITALO |
| GTFS · FlixTrain | `flixtrain` | FLIXTRAIN |

## Contrôles (fiabilité)

Rien de cassé n'est publié : en cas de refus, la version précédente reste en place et le run est en échec
(notification GitHub par e-mail).

- **GTFS d'une compagnie** : ZIP lisible, fichiers obligatoires présents, au moins un trajet, horaires non
  expirés, et pas plus de 50 % de trajets en moins que la version publiée. Avertissement si les horaires
  expirent dans moins de 7 jours.
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

## Référentiel des gares

`referentiel/` : un script par compagnie (`build_gares_<op>.py`), la fusion (`fusion.py`) et la base
(`gares.csv`, `villes.csv`, `codes.csv`), lue par `build_network.py`. Les caches des API et les sources
téléchargées (`referentiel/cache/`, `referentiel/sources_officielles/`) restent en local. `stations.csv`
(Trainline) complète le référentiel pour les compagnies qui n'y sont pas encore.
