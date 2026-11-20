# EDRLab certification submission runbook (IP-003 Phase 7)

End-to-end checklist for handing the EvilFlowersCatalog LCP integration to
EDRLab. Run this on the **development instance** (no separate staging — per
Q5 resolution of [IP-003](../proposals/posts/ip-003-lcp-edrlab-certification.md)).

## Prerequisites

- The dev instance is up and reachable at `dev.evilflowers.elvira.stuba.sk`.
- All IP-003 Phase 0–6 code has been deployed.
- `EVILFLOWERS_NOTIFICATIONS_ENABLED=true` (so EDRLab can see the email flow).

## 1. Seed the dev catalog

Curate a small (~5–10) set of public-domain or STU-owned **PDF** titles in
a dedicated catalog used for the EDRLab review. Each must:

- Have `readium_enabled=True`
- Have a PDF acquisition
- Be successfully encrypted (run
  `python manage.py encrypt_readium_content --catalog <demo-catalog-uuid>`
  and confirm `EncryptedContent.status=registered`)

## 2. Create a guest account for EDRLab

```bash
python manage.py createsuperuser  # or your faculty's preferred procedure
# username: edrlab-review
# email: review@edrlab.org
```

Then set an LCP passphrase on the account so they can borrow. Document the
chosen passphrase in the email you send.

## 3. Run the compliance tool

```bash
# In a virtualenv on a separate machine:
pip install -r requirements.txt  # from edrlab/lcp-testing-tools
python -m lcp_test_tool --server https://dev.evilflowers.elvira.stuba.sk
```

Fix anything it reports as failing. Re-run until clean.

## 4. Generate the certification bundle

```bash
python manage.py generate_lcp_certification_bundle \
    --entry <demo-entry-uuid> \
    --output-dir docs/readium/certification-bundle \
    --passphrase 'edrlab-test-passphrase'
```

Inspect the artifacts:

```
docs/readium/certification-bundle/
├── README.md
├── buy_ready.lcpl
├── buy_cancelled.lcpl
├── buy_revoked.lcpl
├── loan_ready.lcpl
└── loan_expired.lcpl
```

Smoke-test each `.lcpl` in Thorium Reader with the passphrase above.

## 5. Send to EDRLab

Subject: `Readium LCP certification — Slovak University of Technology — bundle ready for review`

Body:
- Link to the dev instance.
- Guest account credentials.
- The bundle (either upload to a file-sharing service or attach the .zip).
- Re-confirm the LCP-for-PDF profile only (no EPUB).

Recipient: `priyanka.dalotra@edrlab.org` (see `EMAIL.txt`).

## 6. After EDRLab approval

1. Sign the LCP Agreement.
2. Receive production cert + patch material from EDRLab.
3. Apply per `deployment/production-patching.md` on the STU production host.
4. Re-run `python manage.py check_production_mode` — must pass.
5. Re-generate a fresh bundle against production and send to EDRLab.
6. EDRLab issues the final "LCP Certified" report.
