"""
Regression test: the EDRLab certification bundle is PDF-only (#69).

EvilFlowers is a PDF-only platform, so the standalone `protected.lcpdf`
artifact was dropped (see #68/#69 — the shipped file carried no embedded
`META-INF/license.lcpl` and EDRLab only requires a standalone protected file
for platforms that embed LCP licenses inside EPUBs). This test locks in the
new contract: `generate_lcp_certification_bundle` must write **exactly** the
five `.lcpl` samples plus a `README.md`, and never a `protected.lcpdf`.

The DB, the test user, and the per-license service transitions are mocked —
each `_produce_*` helper is replaced with a stub that writes its target path,
so the test asserts the *set of artifacts* the command lays down rather than
re-exercising the LCP Server. `_copy_protected_pdf` no longer exists, so a
`protected.lcpdf` can only appear if the artifact is reintroduced.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.readium.management.commands.generate_lcp_certification_bundle import Command
from apps.readium.models import EncryptedContent

CMD_MODULE = "apps.readium.management.commands.generate_lcp_certification_bundle"
ENTRY_UUID = "ce40e042-1491-434f-a0b4-593c0a867b99"

EXPECTED_ARTIFACTS = {
    "README.md",
    "buy_ready.lcpl",
    "buy_cancelled.lcpl",
    "buy_revoked.lcpl",
    "loan_ready.lcpl",
    "loan_expired.lcpl",
}


def _stub_produce(self, *args):
    """Stand in for a `_produce_*` helper: write the target `.lcpl` path."""
    Path(args[-1]).write_text("{}")


class CertificationBundleArtifactsTests(SimpleTestCase):
    def _fake_entry(self):
        entry = MagicMock()
        entry.pk = ENTRY_UUID
        entry.title = "Managing Protected Areas in Central and Eastern Europe Under Climate Change"
        entry.read_config.return_value = True

        acquisition = MagicMock()
        acquisition.encrypted_content.status = EncryptedContent.EncryptionStatus.COMPLETED
        entry.acquisitions.filter.return_value.first.return_value = acquisition
        return entry

    def _run_bundle(self, output_dir):
        with (
            patch(f"{CMD_MODULE}.Entry") as mock_entry_model,
            patch.object(Command, "_ensure_test_user", return_value=MagicMock()),
            patch.object(Command, "_produce_buy_ready", new=_stub_produce),
            patch.object(Command, "_produce_buy_cancelled", new=_stub_produce),
            patch.object(Command, "_produce_buy_revoked", new=_stub_produce),
            patch.object(Command, "_produce_loan_ready", new=_stub_produce),
            patch.object(Command, "_produce_loan_expired", new=_stub_produce),
        ):
            mock_entry_model.objects.get.return_value = self._fake_entry()
            call_command(
                "generate_lcp_certification_bundle",
                "--entry",
                ENTRY_UUID,
                "--output-dir",
                str(output_dir),
            )

    def test_bundle_produces_exactly_five_lcpl_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_bundle(tmp)
            produced = {p.name for p in Path(tmp).iterdir()}

        self.assertEqual(
            produced,
            EXPECTED_ARTIFACTS,
            "Bundle must contain exactly the five .lcpl samples plus README.md.",
        )

    def test_bundle_does_not_contain_protected_lcpdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_bundle(tmp)
            produced = {p.name for p in Path(tmp).iterdir()}

        self.assertNotIn(
            "protected.lcpdf",
            produced,
            "EvilFlowers is PDF-only; the standalone protected.lcpdf artifact was dropped (#69).",
        )

    def test_readme_omits_protected_artifact_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_bundle(tmp)
            readme = (Path(tmp) / "README.md").read_text()

        self.assertNotIn("protected.lcpdf", readme)
        self.assertNotIn("META-INF/license.lcpl", readme)

    def test_copy_protected_pdf_helper_removed(self):
        # The removed artifact was written by `_copy_protected_pdf`; asserting the
        # method is gone guards against it silently returning.
        self.assertFalse(
            hasattr(Command, "_copy_protected_pdf"),
            "_copy_protected_pdf must be removed so protected.lcpdf cannot reappear.",
        )
