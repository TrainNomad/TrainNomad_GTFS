# Swiss GTFS Integration (SBB/CFF/FFS)

## Overview

This folder contains the Swiss train GTFS (General Transit Feed Specification) data from SBB (Schweizerische Bundesbahnen), CFF (Chemins de fer fédéraux suisses), and FFS (Ferrovie federali svizzere) - the Swiss national railway operators.

**Data Source**: [gtfs.geops.ch](https://gtfs.geops.ch/)

## Architecture & Processing

### Data Ingestion Strategy

#### Problem
The original GTFS from gtfs.geops.ch contains **7,395 stops** including:
- Tunnel sections (Gotthard, Alptransit, etc.)
- Infrastructure-only entries
- Many stops without actual train services

#### Solution: Smart Filtering

**Step 1: Identify Active Stops**
```
Read stop_times.txt → Collect unique stop_ids
7,395 stops → Filter → 5,728 stops with actual trips
(27% reduction, removing infrastructure-only entries)
```

**Step 2: Extract UIC Codes**
```
Swiss UIC codes: 85XYZZZ format
3,734 UIC codes identified from active stops
Used for automatic station matching
```

**Step 3: Generate Filtered GTFS**
```
Original: 200+ MB
Filtered: 20.1 MB (89% space saved)
- Only stops with train services
- All trips and schedules preserved
- All transfers maintained
```

### Data Structure

**Stops**: 5,728 (filtered from 7,395)
- Only stops with actual train services
- UIC codes: 3,734 unique identifiers
- Includes S-Bahn, Regional, and long-distance stops

**Trips**: 243,501 total
- trip_short_name = Train number (1-40 for S-Bahn, 1-10 for IC, etc.)
- Multiple trips per route (different times/services)

**Routes**: 87,367 unique combinations
- Each train number × service pattern

**Stop Times**: 2,677,355 schedule entries
- Complete timetables for all services

## Train Types

### Urban Transit (S-Bahn)
- **S1-S41**: Local and regional S-Bahn networks
- Routes: ~15,000
- Example: S1 Zurich (urban rapid transit)

### Regional Services
- **R**: Regional trains (11,256 routes)
- **RE**: RegioExpress (7,225 routes)
- **TER**: Train Express Regional (5,596 routes)
- **IR/IR35/IR95**: Cantonal RegioExpress variants
- Total: ~30,000 routes

### InterCity Services
- **IC/IC1/IC5**: Swiss InterCity (3,200+ routes)
- **EC**: EuroCity international (948 routes)
- **TGV**: High-speed Thalys/TGV (997 routes)

### Other
- **NJ**: Night Jet (ÖBB night trains)
- **EXT**: External/other operators

## File Structure

```
SWISS/
├── ingest_swiss_gtfs.py          # Download and process GTFS
├── swiss_gtfs.zip                # Filtered GTFS data
├── README.md                      # This file
└── DEMARCHE_DETAILLEE.md         # Detailed processing steps
```

## Usage

### Automatic Download & Processing

The `ingest_swiss_gtfs.py` script:
1. Downloads GTFS from gtfs.geops.ch
2. Reads stop_times.txt to identify active stops
3. Filters stops.txt to remove infrastructure-only entries
4. Creates optimized GTFS ZIP

```bash
cd SWISS
python ingest_swiss_gtfs.py
```

### Local Testing (with SSL override for development)

```bash
export SWISS_SKIP_SSL_VERIFY=1
python ingest_swiss_gtfs.py
```

### Manual Processing

For analysis or debugging:

```python
import zipfile
import pandas as pd

with zipfile.ZipFile('swiss_gtfs.zip') as z:
    stops = pd.read_csv(z.open('stops.txt'))
    trips = pd.read_csv(z.open('trips.txt'))
    stop_times = pd.read_csv(z.open('stop_times.txt'))
```

## Station Matching

### UIC Codes

Swiss UIC codes follow the format: **85XYZZZ**
- `85`: Switzerland country code
- `X`: Stop type
- `Y`: Region identifier
- `ZZZ`: Station number

**Examples:**
- `8507000`: Zurich HB
- `8500010`: Geneva
- `8503000`: Bern
- `8505000`: Lausanne

### Integration with stations.csv

The Swiss GTFS stop_ids (UIC codes) are matched to:
1. **Existing UIC column** in stations.csv
2. **New ch_station_id column** (Swiss-specific mapping)

This allows automatic integration of Swiss trains into the unified network.

## Integration with build_network.py

### Station Matching Logic

```python
elif op == "SWISS":
    # Match via UIC codes
    rid = self.by_uic.get(stop_id)  # Primary: UIC
    if not rid:
        rid = self.by_ch_station_id.get(stop_id)  # Fallback
```

### Train Type Mapping

```python
SWISS_TYPES = {
    "S": "S-Bahn",
    "R": "Regional",
    "RE": "RegioExpress",
    "IC": "InterCity",
    "IC1": "InterCity",
    "EC": "EuroCity",
    "TGV": "TGV Thalys",
    "NJ": "Night Jet",
    # ... plus more variants
}
```

## GitHub Actions Automation

The workflow automatically:
1. Downloads Swiss GTFS weekly
2. Processes and filters to active stops
3. Compiles into network.bin
4. Commits updated network.bin.gz

### Workflow Configuration

```yaml
- name: Download Swiss GTFS
  run: python SWISS/ingest_swiss_gtfs.py

- name: Compile network
  run: python build_network.py --refresh
```

## Performance Optimization

### Space Savings
- Original GTFS: 200+ MB
- Filtered GTFS: 20.1 MB
- **89% reduction** through smart filtering

### Time Efficiency
- Only stops with actual train services included
- Faster matching in build_network.py
- Reduced network compilation time

### Data Quality
- Removes infrastructure-only entries
- Preserves all service relationships
- Maintains transfer information

## Statistics

```
Total Stops: 5,728
UIC Codes: 3,734
Trips: 243,501
Routes: 87,367
Stop Times: 2,677,355
Transfers: Preserved from source
File Size: 20.1 MB
```

## Troubleshooting

### SSL Certificate Issues (Local Development)

Windows development machines may have SSL certificate verification issues:

```bash
export SWISS_SKIP_SSL_VERIFY=1
python ingest_swiss_gtfs.py
```

**Note**: GitHub Actions automatically handles certificates correctly.

### Download Fails

- Check gtfs.geops.ch service status
- Verify network connectivity
- Retry the download

### Missing Stops

If a Swiss station doesn't appear:
1. Verify it has actual train services (not infrastructure-only)
2. Check the UIC code in swiss_gtfs.zip
3. Ensure stations.csv has the matching UIC or ch_station_id

## References

- **Data Source**: [gtfs.geops.ch](https://gtfs.geops.ch/)
- **GTFS Standard**: [gtfs.org](https://gtfs.org/)
- **Swiss Railways**: [SBB](https://www.sbb.ch/), [CFF](https://www.cff.ch/)
- **Processing Details**: See DEMARCHE_DETAILLEE.md

## Related Files

- `ingest_swiss_gtfs.py` - Download and process script
- `DEMARCHE_DETAILLEE.md` - Detailed processing steps
- `operators.json` - Swiss configuration (main gtfs folder)
- `build_network.py` - Main compilation pipeline (adds Swiss integration)
- `stations.csv` - Station reference database (Swiss UIC mapping)
