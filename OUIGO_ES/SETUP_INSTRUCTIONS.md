# Ouigo España GTFS Integration - Setup Instructions

## ✅ Integration Status

The Ouigo España GTFS integration has been successfully configured. Here's what was done:

### 1. ✅ Created OUIGO_ES Folder Structure
```
OUIGO_ES/
├── ingest_ouigo_es.py           # Download script with API key support
├── verify_integration.py          # Verification script
├── README.md                      # Technical documentation
├── SETUP_INSTRUCTIONS.md          # This file
└── ouigo_es_gtfs.zip             # Downloaded GTFS (generated)
```

### 2. ✅ Updated Configuration Files

**operators.json** - Added/updated Ouigo España configuration:
```json
{
  "id": "OUIGO_ES",
  "name": "Ouigo España",
  "enabled": true,
  "gtfs_dir": "./OUIGO_ES",
  "gtfs_path": "./OUIGO_ES/ouigo_es_gtfs.zip",
  "script_path": "./OUIGO_ES/ingest_ouigo_es.py",
  "gtfs_url": "https://nap.transportes.gob.es/api/v1/dataset/1515/download",
  "transport_types": ["Ouigo España"]
}
```

### 3. ✅ Updated GitHub Actions Workflow

**.github/workflows/ingest.yml** - Added Ouigo España ingestion step:
```yaml
- name: Télécharger le GTFS Ouigo España
  run: python OUIGO_ES/ingest_ouigo_es.py
  env:
    OUIGO_ES_API_KEY: ${{ secrets.OUIGO_ES_API_KEY }}
```

### 4. ✅ Build Pipeline Support

**build_network.py** already includes:
- Station matching for OUIGO_ES (uses Renfe IDs)
- Trip type handling for Ouigo España trains
- API key support from environment variables

## 🔐 Next Step: Add GitHub Secret

### For GitHub Actions to work, you MUST add your API key as a GitHub secret:

1. **Go to your GitHub repository**
2. **Navigate to**: Settings → Secrets and variables → Actions
3. **Click "New repository secret"**
4. **Add:**
   - Name: `OUIGO_ES_API_KEY`
   - Value: `<your-api-key-from-nap-transportes>`
5. **Click "Add secret"**

> **Note**: Get your API key from https://nap.transportes.gob.es/ after registering for developer access.

## 🚀 How It Works

### Local Testing
```bash
# With API key (if you have one)
export OUIGO_ES_API_KEY="your-api-key-here"
python OUIGO_ES/ingest_ouigo_es.py

# Or without (uses public API)
python OUIGO_ES/ingest_ouigo_es.py

# Full pipeline build
python build_network.py --refresh
```

### GitHub Actions Automation
The workflow runs on a schedule (weekly by default) and:
1. **Downloads** Ouigo España GTFS via the NAP API
2. **Integrates** it into the network compilation
3. **Commits** the updated network.bin.gz to the repository

## 📊 Verification

Run the verification script to check everything is properly configured:

```bash
python OUIGO_ES/verify_integration.py
```

Expected output:
```
✅ PASS: File structure
✅ PASS: Operators configuration
⚠️  FAIL: API key (expected - only in CI/CD)
✅ PASS: Stations database
✅ PASS: Build network support
✅ PASS: GitHub Actions workflow
```

## 🔧 Configuration Details

### How API Keys Are Used

The system looks for the API key in this order:
1. **Environment variable**: `OUIGO_ES_API_KEY`
2. **operators.json field**: `api_key` (not set currently)
3. **Falls back** to public API if neither is available

### How Stations Are Matched

Ouigo España uses the same stations as Renfe. The matching works by:
1. Looking up `renfe_id` in stations.csv
2. Falling back to UIC codes (Spain prefix: 71)

This is handled automatically by `build_network.py` when processing the GTFS.

### Download Behavior

The `ingest_ouigo_es.py` script:
- Downloads from: `https://nap.transportes.gob.es/api/v1/dataset/1515/download`
- Sends authentication headers if API key is available
- Falls back gracefully to public API
- Validates the ZIP file before saving
- Shows download progress and file size

## 📋 Troubleshooting

### "OUIGO_ES_API_KEY not set" warning
**This is normal locally.** The key only needs to be set:
- In GitHub Actions (via the secret)
- When testing locally with `export OUIGO_ES_API_KEY="..."`

### Download fails
1. Check NAP service status: https://nap.transportes.gob.es/
2. Verify API key is correct (if using authenticated API)
3. Check network connectivity
4. The script will still work with the public API

### Station IDs not matching
1. Ensure `stations.csv` has recent Renfe ID mappings
2. Run the station enrichment process if needed
3. Check GTFS stop_id format in the downloaded file

## 📚 Related Files

- `build_network.py` - Main compilation pipeline (already supports OUIGO_ES)
- `operators.json` - All operator configurations
- `stations.csv` - Unified station database with Renfe IDs
- `.github/workflows/ingest.yml` - CI/CD automation
- `.github/workflows/ingest_gtfs.yml` - Alternative workflow if needed

## ✨ Features

✅ **Full API Key Support** - Authenticated and public API access  
✅ **Automatic Updates** - Weekly or manual GitHub Actions trigger  
✅ **Graceful Fallback** - Works with or without API key  
✅ **Station Integration** - Auto-matches with Renfe infrastructure  
✅ **Build Pipeline** - Integrated into network.bin compilation  
✅ **Verification Tools** - Included integration checker  

## 🎯 Next Steps

1. ✅ **Configuration Complete** (just done)
2. ⏳ **Add GitHub Secret** (you need to do this)
3. ⏳ **Test Locally** (optional, but recommended):
   ```bash
   python OUIGO_ES/ingest_ouigo_es.py
   python build_network.py --refresh
   ```
4. ⏳ **Push to GitHub** and let the workflow run
5. ✨ **Enjoy Ouigo España routing!**

---

**Questions?** Check the README.md file in this directory for more technical details.
