# Enrichissement des gares (stations.csv)

Scripts pour enrichir le fichier `stations.csv` avec les codes UIC et données des différents opérateurs.

## Structure

```
stations_enrichment/
├── enrich_cp_stations.py       # Enrichissement gares CP (Portugal)
├── find_orphan_stations.py     # Détection orphelins sur tout le fichier
├── cp_orphan_stations.geojson  # Gares CP non matchées (pour review)
├── orphan_stations.geojson     # Toutes les gares problématiques
├── mappings/
│   └── cp_uic.csv              # Mapping CP stop_id → UIC
└── README.md
```

## Usage

### CP (Portugal)

```bash
cd Backend/gtfs

# Preview des modifications (sans appliquer)
python stations_enrichment/enrich_cp_stations.py

# Appliquer les modifications à stations.csv
# Ajoute les codes UIC + la colonne cp_id
python stations_enrichment/enrich_cp_stations.py --apply
```

### Détection des orphelins (tout le fichier)

```bash
# Analyser tout le fichier
python stations_enrichment/find_orphan_stations.py

# Filtrer par pays
python stations_enrichment/find_orphan_stations.py --country PT

# Vérifier contre un GTFS spécifique
python stations_enrichment/find_orphan_stations.py --gtfs CP
```

### Visualiser les gares orphelines

1. Ouvrir https://geojson.io
2. Charger le fichier `orphan_stations.geojson` ou `cp_orphan_stations.geojson`
3. Visualiser sur la carte et corriger manuellement si besoin

## Colonnes ajoutées à stations.csv

| Colonne | Description |
|---------|-------------|
| `cp_id` | Identifiant CP (format: 94_31039) |

## Format des codes UIC

| Pays | Préfixe | Exemple |
|------|---------|---------|
| Portugal | 94 | 9431039 |
| Espagne | 71 | 7100402 |
| Italie | 83 | 8300070 |
| France | 87 | 8775814 |

## Ajouter un nouvel opérateur

1. Copier `enrich_cp_stations.py` comme template
2. Adapter `extract_uic_from_stop_id()` selon le format de l'opérateur
3. Ajuster les règles de matching si nécessaire
4. Mettre à jour `build_network.py` : ajouter la colonne et le matching
