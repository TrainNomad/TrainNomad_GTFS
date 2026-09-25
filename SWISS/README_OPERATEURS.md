# Swiss GTFS - Version Operateurs de Voyageurs

## Vue d'Ensemble

Cette version du GTFS Suisse contient **UNIQUEMENT** les opérateurs de transports de voyageurs majeurs :
- Grands réseaux nationaux/régionaux
- Trains panoramiques et de montagne touristiques
- Compagnies à voie étroite intégrables

**Opérateurs supprimés:**
- Musées et chemins de fer historiques
- Transports urbains mineurs
- Autres pays (DB, SNCF, ÖBB, etc.)
- Infrastructure/information

---

## Statistiques

```
Operateurs: 30 (filtre de 65)
Routes: 75,648 (89%)
Stops: 3,576 (48%)
Trips: 178,694 (73%)
Horaires: 1,989,643
Taille: 16.6 MB

Reduction: -52% stops, -27% trips
```

---

## Operateurs Conserves

### Grands Acteurs Nationaux & Regionaux

| ID | Nom | Type |
|----|-----|------|
| 000011 | SBB (Schweizerische Bundesbahnen) | National |
| L7____ | SBB GmbH | National |
| 000351 | SBB (Grenzverkehr) | International |
| 000033 | BLS AG | Regional |
| 000082 | SOB (Südostbahn) | Regional |
| 000065 | THURBO | Regional |
| 000074 | Regionalps (RA) | Regional |
| 000086 | ZB (Zentralbahn) | Regional |

### Compagnies à Voie Étroite & Panoramiques

| ID | Nom | Notable |
|----|-----|---------|
| 000072 | RhB (Rhaetische Bahn) | Glacier Express, Bernina Express |
| 000093 | MGB (Matterhorn Gotthard Bahn) | Cervin Express |
| 000048 | MGB (variant) | Cervin Express |
| 000064 | MOB (Montreux-Oberland Bernois) | Panoramique des Alpes |
| 000043 | CJ (Chemins de fer du Jura) | Regional |
| 000053 | TPF (Transports publics fribourgeois) | Regional |
| 000023 | TPC (Transports Publics du Chablais) | Regional |
| 000097 | TRAVYS (Vallée de Joux) | Regional |
| 000029 | MBC (Morges-Bière-Cossonay) | Regional |
| 000055 | LEB (Lausanne-Echallens-Bercher) | Regional |
| 000049 | FART (Ferrovie Ticinesi) | Regional |
| 000047 | FLP (Lugano-Ponte Tresa) | Regional |

### Trains de Montagne & Funiculaires

| ID | Nom | Altitude |
|----|-----|----------|
| 000035 | BOB (Berner Oberland-Bahnen) | Mont |
| 000140 | BOB (variant) | Mont |
| 000124 | JB (Jungfraubahn) | Jungfrau (3454m) |
| 000157 | WAB (Wengernalpbahn) | Wengen |
| 000121 | GGB (Gornergratbahn) | Matterhorn (4084m) |
| 000104 | BRB (Brienz Rothorn Bahn) | Brienz Rothorn |
| 000136 | PB (Pilatusbahnen) | Mont Pilate (2132m) |
| 000022 | AB (Appenzeller Bahnen) | Regional |
| 000088 | RBS (Regionalverkehr Bern-Solothurn) | Regional |
| 000078 | SZU (Sihltal-Zürich-Uetliberg-Bahn) | Uetliberg |

---

## Couverture Geographique

### Regions Couvertes

- **Alpes Suisses**: RhB, MGB, WAB, GGB, BRB, PB
- **Region Bern**: BOB, BRB, SZU, RBS
- **Romande**: MOB, CJ, TPF, TPC, MBC, LEB, TRAVYS
- **Tessin**: FART, FLP
- **Zurich & Nordoest**: THURBO, ZB, AB, SZU
- **Suisse entiere**: SBB, BLS, SOB

### Itineraires Touristiques Majeurs

- Glacier Express (RhB)
- Bernina Express (RhB)
- Cervin Express (MGB)
- Panoramique des Alpes (MOB)
- Jungfrau Railway (JB)
- Gornergrat Railway (GGB)
- Pilatusbahn (PB)

---

## Details du Filtrage

### Strategie

1. **Lire agency.txt** (65 operateurs)
2. **Filtrer par 30 operateurs specifiques** (voyages, montagne, panoramiques)
3. **Filtrer routes** (garder uniquement celles de ces operateurs)
4. **Filtrer trips** (garder uniquement ceux de ces routes)
5. **Filtrer stop_times** (garder uniquement les horaires de ces trips)
6. **Filtrer stops** (garder uniquement ceux utilises)

### Resultat

```
Agents:     65 → 30 operateurs (-54%)
Routes:     87,367 → 75,648 (-13%)
Stops:      7,395 → 3,576 (-52%)
Trips:      243,501 → 178,694 (-27%)
Schedules:  2,677,355 → 1,989,643 (-26%)
Taille:     20.1 MB → 16.6 MB (-17%)
```

---

## Types de Trains

Tous les types preserves :

**National**
- SBB: S-Bahn, IC, EC, TGV, etc.
- BLS/SOB: Regionaux, S-Bahn

**Montagne & Touristiques**
- RhB: Glacier Express, Bernina Express
- MGB: Cervin Express
- MOB: Panoramique des Alpes
- Others: Local mountain trains

**Regional**
- Variantes S-Bahn locales
- Trains regionaux
- Express regionaux

---

## Utilisation

### Configuration (operators.json)

```json
{
  "id": "SWISS",
  "name": "SBB/CFF/FFS (Switzerland) - Operateurs Majeurs",
  "enabled": true,
  "gtfs_dir": "./SWISS",
  "gtfs_path": "./SWISS/swiss_gtfs.zip",
  "script_path": "./SWISS/ingest_swiss_gtfs.py",
  "gtfs_url": "https://gtfs.geops.ch/dl/gtfs_train.zip",
  "transport_types": [
    "S-Bahn",
    "Regional",
    "RegioExpress",
    "InterCity",
    "EuroCity",
    "Glacier Express",
    "Bernina Express",
    "Cervin Express",
    "TGV Thalys",
    "Montagne"
  ]
}
```

### Integration

Le script `ingest_swiss_gtfs.py` automatiquement:
1. Telecharge le GTFS complet de gtfs.geops.ch
2. Filtre pour les 30 operateurs specifiques
3. Cree swiss_gtfs.zip optimise
4. Prêt pour build_network.py

---

## Avantages de la Version Filtree

✅ **Precision**: Uniquement operateurs de voyageurs majeurs  
✅ **Taille reduite**: 16.6 MB vs 20.1 MB  
✅ **Moins de bruit**: 52% moins de stops inutiles  
✅ **Focus voyage**: Tourisme + transport majeur  
✅ **Couverture complete**: Toute la Suisse couverte  
✅ **Trains celebres**: Glacier Express, Bernina, Cervin inclus  

---

## Comparaison des Versions

| Critere | Version Complete | Version Operateurs |
|---------|-----------------|-------------------|
| Stops | 5,728 | 3,576 (-38%) |
| Trips | 243,501 | 178,694 (-27%) |
| Operateurs | 65 | 30 |
| Taille | 20.1 MB | 16.6 MB |
| Focus | Tous les trains | Voyageurs majeurs |
| Musees | Inclus | Excluded |
| Urbain | Tous | Majeurs seulement |

**Choix**: La version operateurs est recommandee pour le routage voyageurs.

---

## Prochaines Etapes

1. Verifier integration avec build_network.py
2. Mapper UIC codes → stations.csv
3. Ajouter types de trains
4. Compiler network.bin avec donnees suisses
5. Tester routage complet

---

## Fichiers

- `swiss_gtfs.zip` (16.6 MB) - GTFS filtre
- `ingest_swiss_gtfs.py` - Script de traitement
- `README.md` - Documentation complete
- `README_OPERATEURS.md` - Ce fichier
- `DEMARCHE_DETAILLEE.md` - Details techniques
