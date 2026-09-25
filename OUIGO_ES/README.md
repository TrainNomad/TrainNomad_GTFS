# Ouigo España GTFS Integration

## Description

Ouigo España is a low-cost train operator in Spain, part of the Renfe group. The GTFS data is provided through the Spanish National Transport Data Access Point (NAP - Punto de Acceso Nacional): https://nap.transportes.gob.es/

## API Access

### Public API
The GTFS dataset can be downloaded publicly from:
```
https://nap.transportes.gob.es/api/v1/dataset/1515/download
```

### Authenticated API (Recommended)
For production systems, authenticated access is recommended for higher rate limits and reliability:
- **API Key Storage**: GitHub Actions Secret `OUIGO_ES_API_KEY`
- **Headers**: 
  - `Authorization: Bearer {API_KEY}`
  - `X-API-KEY: {API_KEY}`

## Setup

### 1. Add GitHub Secret

In your GitHub repository settings under `Secrets and variables` → `Actions secrets`, add:

```
Name: OUIGO_ES_API_KEY
Value: <your-api-key-from-nap-transportes>
```

### 2. Automatic Ingestion

The ingestion pipeline automatically:
1. Runs the `ingest_ouigo_es.py` script via GitHub Actions
2. Downloads the GTFS zip file using the API key
3. Integrates it into `network.bin` via `build_network.py`
4. Commits the updated network to the repository

### 3. Manual Testing

```bash
# With authentication
export OUIGO_ES_API_KEY="your-api-key-here"
python OUIGO_ES/ingest_ouigo_es.py

# Or without (uses public API)
python OUIGO_ES/ingest_ouigo_es.py
```

## Station Matching

Ouigo España stations are matched to the unified stations.csv using:
1. **Renfe ID** (primary): `renfe_id` column
2. **UIC Code**: `71` (Spain prefix) + stop_id from zero-padded format

This works because Ouigo trains operate on the same physical infrastructure as Renfe services.

## File Structure

```
OUIGO_ES/
├── ingest_ouigo_es.py      # Download and process GTFS
├── ouigo_es_gtfs.zip       # Downloaded GTFS (generated)
└── README.md               # This file
```

## Troubleshooting

### API Key Issues
- Ensure `OUIGO_ES_API_KEY` is correctly set in GitHub secrets
- Check that the key is still valid with the NAP service
- Verify authentication headers are being sent correctly

### Download Failures
- Check NAP service status: https://nap.transportes.gob.es/
- Verify network connectivity
- Check logs in GitHub Actions workflow runs

### Station Matching Problems
- Ensure `stations.csv` has recent Renfe ID mappings
- Run the enrichment process if IDs are missing
- Check that stop codes in GTFS match the expected format

## Related Files

- `operators.json` - Configuration for all GTFS operators
- `build_network.py` - Main compilation pipeline
- `.github/workflows/ingest.yml` - GitHub Actions workflow
- `stations.csv` - Unified station reference database

## References

- [Punto de Acceso Nacional](https://nap.transportes.gob.es/)
- [GTFS Standard](https://gtfs.org/)
- [Ouigo España](https://www.ouigo.es/)
