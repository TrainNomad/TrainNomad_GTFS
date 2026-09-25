#!/usr/bin/env python3
"""
Test API connectivity and authentication for Ouigo España GTFS.

This script verifies that:
1. The NAP API is reachable
2. The GTFS dataset is available
3. Authentication (if provided) works correctly
4. The downloaded file is a valid ZIP
"""
import os
import sys
import requests
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

OUIGO_ES_API_URL = "https://nap.transportes.gob.es/api/v1/dataset/1515/download"
TIMEOUT = 30

def test_api_connectivity():
    """Test if API is reachable and returns the GTFS file."""
    logging.info("=" * 60)
    logging.info("🧪 Testing Ouigo España API Connectivity")
    logging.info("=" * 60)

    logging.info(f"\n📡 API URL: {OUIGO_ES_API_URL}")

    # Prepare headers
    api_key = os.environ.get("OUIGO_ES_API_KEY")
    headers = {"User-Agent": "Mozilla/5.0"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-KEY"] = api_key
        logging.info("🔐 Using authenticated API access")
    else:
        logging.info("ℹ️  Using public API access (no key)")

    try:
        # First, test with HEAD request to check availability without downloading
        logging.info("\n1️⃣  Checking API availability (HEAD request)...")
        head_response = requests.head(OUIGO_ES_API_URL, headers=headers, timeout=TIMEOUT)

        if head_response.status_code == 200:
            logging.info(f"   ✅ API is reachable (HTTP {head_response.status_code})")
            size_mb = int(head_response.headers.get("content-length", 0)) / 1024 / 1024
            if size_mb > 0:
                logging.info(f"   📦 File size: {size_mb:.1f} MB")
        elif head_response.status_code in [301, 302]:
            logging.info(f"   ⏩ Redirect detected (HTTP {head_response.status_code})")
        else:
            logging.warning(f"   ⚠️  Unexpected status: {head_response.status_code}")

        # Test actual download with a partial read
        logging.info("\n2️⃣  Testing download (first 1 KB)...")

        response = requests.get(
            OUIGO_ES_API_URL,
            headers=headers,
            timeout=TIMEOUT,
            stream=True
        )
        response.raise_for_status()

        logging.info(f"   ✅ Download successful (HTTP {response.status_code})")

        # Read first chunk to verify it's a ZIP
        first_chunk = b""
        for chunk in response.iter_content(chunk_size=1024):
            first_chunk += chunk
            break  # Just get the first chunk

        # Check for ZIP magic number
        if first_chunk.startswith(b"PK\x03\x04"):
            logging.info("   ✅ File is a valid ZIP archive")
        else:
            logging.error("   ❌ File doesn't appear to be a ZIP archive")
            return False

        # Get actual file size
        content_length = response.headers.get("content-length")
        if content_length:
            size_mb = int(content_length) / 1024 / 1024
            logging.info(f"   📦 Total file size: {size_mb:.1f} MB")

        logging.info("\n3️⃣  Response headers:")
        important_headers = [
            "content-type",
            "content-length",
            "content-disposition",
            "last-modified",
            "cache-control"
        ]
        for header in important_headers:
            value = response.headers.get(header)
            if value:
                logging.info(f"   {header}: {value}")

        return True

    except requests.exceptions.Timeout:
        logging.error(f"   ❌ Timeout after {TIMEOUT}s - API is not responding")
        return False
    except requests.exceptions.ConnectionError as e:
        logging.error(f"   ❌ Connection error: {e}")
        return False
    except requests.exceptions.HTTPError as e:
        logging.error(f"   ❌ HTTP error {e.response.status_code}: {e}")
        if e.response.status_code == 401:
            logging.error("      → Authentication failed. Check your API key.")
        elif e.response.status_code == 403:
            logging.error("      → Access forbidden. Check your API key permissions.")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"   ❌ Request error: {e}")
        return False

def test_authentication():
    """Test if authentication is properly configured."""
    logging.info("\n" + "=" * 60)
    logging.info("🔐 Authentication Configuration")
    logging.info("=" * 60)

    api_key = os.environ.get("OUIGO_ES_API_KEY")

    if api_key:
        masked = api_key[:4] + "..." + api_key[-4:]
        logging.info(f"✅ OUIGO_ES_API_KEY is set: {masked}")
        logging.info("   This key will be used for authenticated API access")
        return True
    else:
        logging.info("ℹ️  OUIGO_ES_API_KEY is not set")
        logging.info("   Public API will be used (may have rate limits)")
        logging.info("   To use authenticated access, set:")
        logging.info("   export OUIGO_ES_API_KEY='your-key-here'")
        return False

def test_github_action_setup():
    """Check if GitHub Actions secret is expected to be set."""
    logging.info("\n" + "=" * 60)
    logging.info("⚙️  GitHub Actions Setup")
    logging.info("=" * 60)

    logging.info("Expected GitHub secret name: OUIGO_ES_API_KEY")
    logging.info("Location: Repository Settings → Secrets and variables → Actions")
    logging.info("\nTo add the secret:")
    logging.info("1. Go to your GitHub repository")
    logging.info("2. Click Settings")
    logging.info("3. Go to Secrets and variables → Actions")
    logging.info("4. Click 'New repository secret'")
    logging.info("5. Name: OUIGO_ES_API_KEY")
    logging.info("6. Value: <your-api-key-from-nap>")
    logging.info("7. Click 'Add secret'")

def main():
    """Run all tests."""
    try:
        # Check authentication setup
        has_auth = test_authentication()

        # Test API connectivity
        api_ok = test_api_connectivity()

        # Show GitHub Actions setup info
        test_github_action_setup()

        # Summary
        logging.info("\n" + "=" * 60)
        logging.info("📊 Test Summary")
        logging.info("=" * 60)

        if api_ok:
            logging.info("✅ API connectivity test PASSED")
            if has_auth:
                logging.info("✅ Authentication is properly configured")
            else:
                logging.info("⚠️  Using public API (no authentication)")
            logging.info("\n✨ Ready to download Ouigo España GTFS!")
            return 0
        else:
            logging.error("❌ API connectivity test FAILED")
            logging.error("Please check the errors above and review:")
            logging.error("- Network connectivity")
            logging.error("- API key validity (if using authenticated access)")
            logging.error("- NAP service status at https://nap.transportes.gob.es/")
            return 1

    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
