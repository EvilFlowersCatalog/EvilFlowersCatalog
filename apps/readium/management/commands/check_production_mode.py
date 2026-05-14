"""
Smoke test for the production LCP deployment (IP-003 Phase 6).

Asserts:
- The host-mount secret directory exists with the right permissions.
- The LCP server is reachable and reports a v2.x profile identifier.

Run on the production host after applying the EDRLab patch.
"""

import os
import stat
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

SECRETS_DIR = Path("/etc/evilflowers/lcp-secrets")
EXPECTED_DIR_MODE = 0o500
EXPECTED_FILE_MODE = 0o400


class Command(BaseCommand):
    help = "Verify the LCP production deployment is correctly patched and secured."

    def handle(self, *args, **options):
        failures = []

        if not SECRETS_DIR.is_dir():
            failures.append(f"{SECRETS_DIR} does not exist")
        else:
            dir_mode = stat.S_IMODE(SECRETS_DIR.stat().st_mode)
            if dir_mode != EXPECTED_DIR_MODE:
                failures.append(f"{SECRETS_DIR} mode is {oct(dir_mode)}, expected {oct(EXPECTED_DIR_MODE)}")

            for name in ("cert.pem", "key.pem"):
                f = SECRETS_DIR / name
                if not f.is_file():
                    failures.append(f"{f} missing")
                else:
                    file_mode = stat.S_IMODE(f.stat().st_mode)
                    if file_mode != EXPECTED_FILE_MODE:
                        failures.append(f"{f} mode is {oct(file_mode)}, expected {oct(EXPECTED_FILE_MODE)}")

        url = settings.EVILFLOWERS_READIUM_LCPSV_URL.rstrip("/")
        try:
            r = requests.get(f"{url}/", timeout=5)
            r.raise_for_status()
            body = r.text
            if "v2" not in body and "2." not in body:
                failures.append(f"LCP Server at {url} does not advertise a v2 profile (got: {body[:200]!r})")
        except requests.RequestException as e:
            failures.append(f"Cannot reach LCP Server at {url}: {e}")

        if failures:
            self.stdout.write(self.style.ERROR("Production mode smoke test FAILED:"))
            for f in failures:
                self.stdout.write(self.style.ERROR(f"  - {f}"))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Production mode smoke test passed."))
