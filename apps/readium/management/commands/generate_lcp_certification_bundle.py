"""
Generate the EDRLab certification bundle (IP-003 Phase 1).

Produces five artifacts in `--output-dir`:

    buy_ready.lcpl           (license in `ready` state)
    buy_cancelled.lcpl       (driven through real LicenseService.cancel_license)
    buy_revoked.lcpl         (activated via device registration, then LicenseService.revoke_license)
    loan_ready.lcpl          (license in `ready` state, loan profile)
    loan_expired.lcpl        (license issued with rights.end already in the past)

Plus a README listing each artifact, the test user passphrase, and how to
verify with Thorium Reader.

Every state transition (create / cancel / revoke) goes through `LicenseService`
— the same high-level service the borrow API uses — and the `.lcpl` is
serialized through the service-level `fetch_fresh_license` (the download-gateway
path), so EDRLab sees exactly the surface production traffic produces. The only
non-service step is a local borrow-slot release (see `_release_local_slot`),
which is pure bookkeeping and never touches the upstream status of any artifact.

Q1 resolution: `--entry <uuid>` is required. Command fails fast if the entry
is not LCP-enabled or has no encrypted PDF acquisition.
"""

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import Entry, User
from apps.readium.models import EncryptedContent, License
from apps.readium.services import (
    LCPServerClient,
    LicenseService,
    StatusServerClient,
)

BUNDLE_README = """# Readium LCP — EDRLab Certification Bundle

This directory contains the artifacts produced for EDRLab compliance
review of the EvilFlowersCatalog LCP integration.

## Test user passphrase
{passphrase!r}

(Hash, SHA-256, stored on the user record and embedded in every license below.)

## Artifacts

| File | State | Notes |
|------|-------|-------|
| `buy_ready.lcpl`     | ready     | License available for activation, full rights window |
| `buy_cancelled.lcpl` | cancelled | Real PATCH /licenses/{{id}}/status to `cancelled` |
| `buy_revoked.lcpl`   | revoked   | Real PATCH /licenses/{{id}}/status to `revoked` |
| `loan_ready.lcpl`    | ready     | Loan-style license (short rights window) |
| `loan_expired.lcpl`  | expired   | License issued with rights.end in the past |

## Verification with Thorium Reader

1. Open Thorium Reader.
2. Import each `.lcpl` file via *File → Import licenses*.
3. Enter the passphrase above when prompted.
4. Expected: `buy_ready` and `loan_ready` open. `buy_cancelled`, `buy_revoked`,
   and `loan_expired` are refused with the appropriate error.

## How this bundle was produced

`python manage.py generate_lcp_certification_bundle --entry <uuid> --output-dir <dir>`

Entry: `{entry_id}`
Title: `{entry_title}`
Generated at: `{generated_at}`
"""


class Command(BaseCommand):
    help = "Produce the 5-artifact EDRLab certification bundle for an LCP-enabled entry."

    def add_arguments(self, parser):
        parser.add_argument("--entry", type=str, required=True, help="UUID of an LCP-enabled entry (required)")
        parser.add_argument("--output-dir", type=str, required=True, help="Directory to write artifacts into")
        parser.add_argument(
            "--passphrase",
            type=str,
            default="edrlab-test-passphrase",
            help="Plain passphrase to set on the test user and embed in the licenses",
        )
        parser.add_argument(
            "--test-user",
            type=str,
            default="edrlab-test",
            help="Username of the dedicated test user (created if missing)",
        )

    def handle(self, *args, **options):
        entry_id = options["entry"]
        output_dir = Path(options["output_dir"]).resolve()
        passphrase = options["passphrase"]
        username = options["test_user"]

        try:
            UUID(entry_id)
        except ValueError as e:
            raise CommandError(f"--entry must be a UUID: {e}") from e

        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist as e:
            raise CommandError(f"Entry {entry_id} not found") from e

        if not entry.read_config("readium_enabled"):
            raise CommandError(f"Entry {entry_id} is not LCP-enabled (readium_enabled=False)")

        acquisition = entry.acquisitions.filter(mime="application/pdf").first()
        if acquisition is None:
            raise CommandError(f"Entry {entry_id} has no PDF acquisition (LCP-for-PDF profile required)")

        if not hasattr(acquisition, "encrypted_content"):
            raise CommandError(
                f"Acquisition {acquisition.pk} is not yet encrypted. "
                f"Run `python manage.py encrypt_readium_content --entry {entry_id}` first."
            )

        encrypted = acquisition.encrypted_content
        if encrypted.status not in [
            EncryptedContent.EncryptionStatus.COMPLETED,
            EncryptedContent.EncryptionStatus.REGISTERED,
        ]:
            raise CommandError(
                f"Encrypted content not ready (status={encrypted.status}). "
                "Wait for encryption to complete and the LCP Server to register it."
            )

        output_dir.mkdir(parents=True, exist_ok=True)

        user = self._ensure_test_user(username, passphrase)

        self.stdout.write(f"Writing bundle to {output_dir} ...")

        self._produce_buy_ready(entry, user, passphrase, output_dir / "buy_ready.lcpl")
        self._produce_buy_cancelled(entry, user, passphrase, output_dir / "buy_cancelled.lcpl")
        self._produce_buy_revoked(entry, user, passphrase, output_dir / "buy_revoked.lcpl")
        self._produce_loan_ready(entry, user, passphrase, output_dir / "loan_ready.lcpl")
        self._produce_loan_expired(entry, user, passphrase, output_dir / "loan_expired.lcpl")

        readme = output_dir / "README.md"
        readme.write_text(
            BUNDLE_README.format(
                passphrase=passphrase,
                entry_id=entry.pk,
                entry_title=entry.title,
                generated_at=timezone.now().isoformat(),
            )
        )

        self.stdout.write(self.style.SUCCESS(f"Bundle written to {output_dir}"))

    # --- helpers --------------------------------------------------------

    def _ensure_test_user(self, username: str, passphrase: str) -> User:
        # User.auth_source is a required FK; pick an active local (database)
        # source, falling back to any source. Without this, creating the test
        # user violates the NOT NULL constraint on auth_source_id.
        from apps.core.models.auth_source import AuthSource

        auth_source = (
            AuthSource.objects.filter(driver=AuthSource.Driver.DATABASE, is_active=True).first()
            or AuthSource.objects.first()
        )
        if auth_source is None:
            raise CommandError("No AuthSource exists; create one before generating the certification bundle.")

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"name": "EDRLab", "surname": "Tester", "is_active": True, "auth_source": auth_source},
        )
        # Set passphrase hash (uppercased SHA-256 per LCP spec).
        user.lcp_passphrase_hash = LCPServerClient.hash_passphrase(passphrase)
        user.lcp_passphrase_hint = "EDRLab certification passphrase"
        user.save(update_fields=["lcp_passphrase_hash", "lcp_passphrase_hint"])
        if created:
            self.stdout.write(self.style.NOTICE(f"Created test user {username}"))
        return user

    def _produce_buy_ready(self, entry, user, passphrase, out_path: Path):
        license_obj = self._create(entry, user, passphrase, duration_days=365)
        self._fetch_and_save(license_obj, out_path)

    def _produce_buy_cancelled(self, entry, user, passphrase, out_path: Path):
        license_obj = self._create(entry, user, passphrase, duration_days=365)
        # Serialize the signed .lcpl while it is still valid, then cancel through
        # the same service the API uses so the LSD status becomes `cancelled`.
        self._fetch_and_save(license_obj, out_path)
        LicenseService.cancel_license(license_obj, reason="EDRLab certification — cancelled sample")

    def _produce_buy_revoked(self, entry, user, passphrase, out_path: Path):
        license_obj = self._create(entry, user, passphrase, duration_days=365)
        # `revoked` (as opposed to `cancelled`) requires the license to be ACTIVE
        # first: the Status Server derives the resulting status from the current
        # one (READY → cancelled, ACTIVE → revoked). Register a device through the
        # real status service to activate it, serialize, then revoke via the service.
        StatusServerClient().register_device(
            license_obj, device_id="edrlab-cert-device", device_name="EDRLab Certification"
        )
        license_obj.refresh_from_db()
        self._fetch_and_save(license_obj, out_path)
        LicenseService.revoke_license(license_obj, reason="EDRLab certification — revoked sample")

    def _produce_loan_ready(self, entry, user, passphrase, out_path: Path):
        license_obj = self._create(entry, user, passphrase, duration_days=14)
        self._fetch_and_save(license_obj, out_path)

    def _produce_loan_expired(self, entry, user, passphrase, out_path: Path):
        # Issue with a past end so the LCP Server stamps rights.end < now and the
        # Status Server reports `expired`. The service fetch refuses expired
        # licenses (like the download gateway), so _fetch_and_save falls back to a
        # raw read for the bytes.
        license_obj = self._create(entry, user, passphrase, duration_days=1, back_date_days=30)
        self._fetch_and_save(license_obj, out_path)

    def _create(self, entry, user, passphrase, duration_days: int, back_date_days: int = 0) -> License:
        """Mint a license through the real borrow service (`LicenseService.create_license`)."""
        self._release_local_slot(entry, user)
        start = timezone.now() - timedelta(days=back_date_days) if back_date_days else None
        return LicenseService.create_license(
            entry=entry,
            user=user,
            user_passphrase=passphrase,
            start_date=start,
            duration_days=duration_days,
        )

    def _fetch_and_save(self, license_obj: License, out_path: Path) -> None:
        """Serialize the `.lcpl` via the service-level fetch used by the download
        gateway. Expired/terminal licenses are refused there by design, so fall
        back to the raw signed document for those (a read-only operation that
        changes no state)."""
        try:
            lcp_license = LicenseService.fetch_fresh_license(license_obj)
        except ValueError:
            lcp_license = LCPServerClient().fetch_fresh_license(license_obj)
        out_path.write_text(json.dumps(lcp_license, indent=2))
        self.stdout.write(f"  ✓ {out_path.name}")

    def _release_local_slot(self, entry, user) -> None:
        """Free this test user's local borrow slot on the entry so the next
        license can be minted.

        This is local bookkeeping ONLY: the entry's `readium_amount` is typically
        1, and both the unique constraint and the per-entry capacity check would
        otherwise block a second `ready` license. It deliberately does NOT cancel
        upstream, so each already-written artifact keeps its own authoritative
        status on the Status Server. It never fabricates or alters an artifact's
        LSD status — those come exclusively from `LicenseService` transitions.
        """
        License.objects.filter(
            entry=entry,
            user=user,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
        ).update(state=License.LicenseState.CANCELLED)
