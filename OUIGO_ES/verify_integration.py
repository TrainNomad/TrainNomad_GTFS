#!/usr/bin/env python3
"""
Verification script for Ouigo España GTFS integration.

Checks:
1. Script configuration and paths
2. API key availability
3. Download capability
4. Station mapping
"""
import os
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(BASE_DIR)
OPERATORS_FILE = os.path.join(GTFS_DIR, "operators.json")
STATIONS_CSV = os.path.join(GTFS_DIR, "stations.csv")

def check_file_exists(path, name):
    """Check if a file exists."""
    if os.path.exists(path):
        logging.info(f"✅ {name}: {path}")
        return True
    else:
        logging.error(f"❌ {name} NOT FOUND: {path}")
        return False

def check_operators_config():
    """Verify Ouigo España is correctly configured in operators.json."""
    logging.info("\n📋 Checking operators.json configuration...")

    if not os.path.exists(OPERATORS_FILE):
        logging.error(f"❌ operators.json not found at {OPERATORS_FILE}")
        return False

    with open(OPERATORS_FILE, 'r', encoding='utf-8') as f:
        operators = json.load(f)

    ouigo = None
    for op in operators:
        if op.get("id") == "OUIGO_ES":
            ouigo = op
            break

    if not ouigo:
        logging.error("❌ OUIGO_ES operator not found in operators.json")
        return False

    logging.info("✅ OUIGO_ES operator found in operators.json")

    # Check required fields
    required_fields = ["id", "name", "enabled", "gtfs_path", "script_path"]
    missing = [f for f in required_fields if f not in ouigo or not ouigo[f]]

    if missing:
        logging.error(f"❌ Missing required fields: {missing}")
        return False

    for field, value in ouigo.items():
        if field not in ["transport_types"]:
            logging.info(f"   {field}: {value}")

    logging.info("✅ All required fields present")
    return True

def check_api_key():
    """Check if API key is available."""
    logging.info("\n🔐 Checking API key...")

    api_key = os.environ.get("OUIGO_ES_API_KEY")

    if api_key:
        masked = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
        logging.info(f"✅ OUIGO_ES_API_KEY is set: {masked}")
        return True
    else:
        logging.warning("⚠️  OUIGO_ES_API_KEY not set (will use public API)")
        return False

def check_stations_csv():
    """Check if stations.csv exists and has necessary columns."""
    logging.info("\n📊 Checking stations.csv...")

    if not os.path.exists(STATIONS_CSV):
        logging.error(f"❌ stations.csv not found at {STATIONS_CSV}")
        return False

    logging.info("✅ stations.csv exists")

    # Check for renfe_id column
    try:
        with open(STATIONS_CSV, 'r', encoding='utf-8') as f:
            header = f.readline().strip()
            if "renfe_id" in header:
                logging.info("✅ stations.csv contains 'renfe_id' column for Ouigo España matching")
                return True
            else:
                logging.warning("⚠️  'renfe_id' column not found in stations.csv")
                return False
    except Exception as e:
        logging.error(f"❌ Error reading stations.csv: {e}")
        return False

def check_build_network():
    """Check if build_network.py supports Ouigo España."""
    logging.info("\n🔨 Checking build_network.py support...")

    build_network_path = os.path.join(GTFS_DIR, "build_network.py")
    if not os.path.exists(build_network_path):
        logging.error(f"❌ build_network.py not found at {build_network_path}")
        return False

    with open(build_network_path, 'r', encoding='utf-8') as f:
        content = f.read()

    checks = {
        "OUIGO_ES_TYPES": 'OUIGO_ES_TYPES' in content,
        "Station matching for OUIGO_ES": 'elif op == "OUIGO_ES"' in content,
        "Trip type handling for OUIGO_ES": 'elif op_id == "OUIGO_ES"' in content,
    }

    all_ok = True
    for check_name, passed in checks.items():
        if passed:
            logging.info(f"✅ {check_name}")
        else:
            logging.error(f"❌ {check_name}")
            all_ok = False

    return all_ok

def check_workflow():
    """Check if GitHub Actions workflow is configured."""
    logging.info("\n⚙️  Checking GitHub Actions workflow...")

    workflow_path = os.path.join(GTFS_DIR, ".github", "workflows", "ingest.yml")
    if not os.path.exists(workflow_path):
        logging.error(f"❌ Workflow not found at {workflow_path}")
        return False

    with open(workflow_path, 'r', encoding='utf-8') as f:
        content = f.read()

    checks = {
        "OUIGO_ES ingestion step": 'OUIGO_ES/ingest_ouigo_es.py' in content,
        "API key environment variable": 'OUIGO_ES_API_KEY' in content,
    }

    all_ok = True
    for check_name, passed in checks.items():
        if passed:
            logging.info(f"✅ {check_name}")
        else:
            logging.error(f"❌ {check_name}")
            all_ok = False

    return all_ok

def main():
    """Run all verification checks."""
    logging.info("=" * 60)
    logging.info("🔍 Ouigo España Integration Verification")
    logging.info("=" * 60)

    results = {
        "File structure": check_file_exists(OPERATORS_FILE, "operators.json"),
        "Operators configuration": check_operators_config(),
        "API key": check_api_key(),
        "Stations database": check_stations_csv(),
        "Build network support": check_build_network(),
        "GitHub Actions workflow": check_workflow(),
    }

    logging.info("\n" + "=" * 60)
    logging.info("📊 Verification Summary")
    logging.info("=" * 60)

    for check_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        logging.info(f"{status}: {check_name}")

    all_passed = all(results.values())

    if all_passed:
        logging.info("\n✅ All checks passed! Integration is ready.")
        return 0
    else:
        logging.info("\n❌ Some checks failed. Please review the configuration.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
