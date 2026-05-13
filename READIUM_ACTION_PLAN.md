# Readium LCP — Action Plan toward EDRLab Certification

Internal punch list of what is still missing in `apps/readium/` and the surrounding deployment before we can submit the EvilFlowersCatalog LCP integration to EDRLab for compliance certification and move from test to production mode at the Slovak University of Technology.

References:
- `docs/readium/lcp-server-wiki/` (vendored upstream wiki)
- EDRLab 8-step license-provider process: https://www.edrlab.org/readium-lcp/become-lcp-license-provider/
- LCP testing tools: https://github.com/edrlab/lcp-testing-tools

## State of play

Already implemented and exercised in test mode:

- LCP Server client — `apps/readium/services/lcp_server_client.py`
- Status Server client + proxy — `apps/readium/services/status_server_client.py`, `apps/readium/views/status_proxy.py`
- License service (loan/buy flows, storage) — `apps/readium/services/license_service.py`, `apps/readium/views/licenses.py`
- License Gateway behavior — `apps/readium/views/licenses.py` + `apps/readium/views/manifest.py`
- Encryption pipeline — `apps/readium/services/content_encryption_service.py` + `management/commands/encrypt_readium_content.py`
- Passphrase hint page — `apps/readium/views/hint.py`
- Webhooks, availability, content/download — `apps/readium/views/{hooks,availability,content,download}.py`

## Work remaining

### 1. Sample-license generation command
Management command that produces the artifacts EDRLab requires for certification:
- 3 "buy" licenses: `ready`, `cancelled`, `revoked`
- 2 "loan" licenses: `ready`, `expired`
- 1 protected EPUB (license embedded in `META-INF/license.lcpl`)

The `cancelled` and `revoked` states must be driven through real Status Server `PATCH` calls — not direct DB writes — so they go through the same path EDRLab will exercise. The `expired` loan state should be produced by issuing a license with a past `end` and letting the Status Server transition it naturally.

### 2. Oversharing detection & revocation tool
Wire a management command (or staff-only admin view) that:
- calls Status Server `GET /licenses?registered>=N` with a configurable threshold
- presents the paginated list to an operator
- triggers `PATCH /licenses/{id}/revoke` on confirmation

Spec: wiki "Develop a tool to check overshared licenses and to revoke licenses when required".

### 3. License Server network isolation
Audit and harden:
- License Server bound to localhost / private network only
- Reachable only from the Provider Server (EvilFlowersCatalog) and the Encryption Tool, with auth
- `iptables` / security-group rules documented in deployment notes
- Public ingress goes only to Status Server + License Gateway

### 4. HTTPS-only enforcement
LCP LSD §2.1 Content Conformance requires HTTPS for both the Status Server and the License Gateway. Tasks:
- Validate the Nginx reverse proxy config against `docs/readium/lcp-server-wiki/Server-deployment.md`
- HSTS + TLS 1.2/1.3 only
- Cert renewal automated (certbot or STU PKI)
- Reject any plain-HTTP request with redirect-only on port 80

### 5. Passphrase handling audit
- Confirm we only persist the SHA-256 hash of the passphrase, never the plaintext
- Verify the hint page flow lets a user reset the passphrase, and that the resulting license update propagates to the Status Document
- Add a test covering hash-mismatch error path

### 6. Renew policy decision
Pick one of three options from the LSD spec and configure the Status Server accordingly:
- default `renew_days` window (simplest, no extra code)
- `renew_page_url` — HTML form on EvilFlowersCatalog handling renewals
- `renew_custom_url` — REST callback enforcing our own rules (queueing, embargo, etc.)

Recommendation for STU: `renew_custom_url` so library policy (loan queues, lecturer overrides) lives on our side.

### 7. OPDS feeds carry LCP license links
Verify (and, if needed, add) the `application/vnd.readium.lcp.license.v1.0+json` link in:
- OPDS 1.2 acquisition entries (`apps/opds/`)
- OPDS 2.0 publications (`apps/opds2/`)

So Thorium and other Readium-toolkit-based readers can import directly from a personal OPDS feed instead of requiring a manual `.lcpl` download. This is the recommended UX per the upstream wiki.

### 8. Run `lcp-testing-tools` against staging
Before EDRLab submission, run https://github.com/edrlab/lcp-testing-tools against our test-mode deployment and fix everything it flags. This is the same harness EDRLab uses, so we should pass it locally first.

### 9. Production-mode patching procedure
Once we receive the X509 certificate, private key, and patch script from EDRLab:
- Document the deploy procedure (Ansible role or compose override) for applying the patch to a freshly-built License Server
- Store the X509 material in a sealed secret (Vault / sealed-secrets / 1Password vault — TBD with STU IT)
- Make the procedure reusable so future Slovak universities can apply the same patch without re-exposing the secret
- Add a smoke test that asserts the deployed server reports production mode

### 10. Staging access for EDRLab
EDRLab needs to exercise the LCP features end-to-end. Pick and prepare one of:
- a pre-prod web instance with a few demo titles, demo user account, and an OPDS feed (preferred — most realistic)
- direct access to the LCP test frontend
- a pre-defined bundle of licenses (fallback)

Decision: stand up `lcp-staging.{stuba.sk subdomain}` with a curated demo catalog and a guest account for EDRLab.

### 11. Internal compliance review
Final pre-submission checklist:
- All Status Server / License Gateway error responses are RFC 7807 problem-details JSON
- HTTP status codes match the LSD spec (400 / 403 / 404 / 5xx)
- Hint page reachable and informative
- Helper page URL present in every generated license
- Cache headers on encrypted publication downloads
- Logging does not leak passphrases or content keys

## Parallel org / legal track

- Refill the EDRLab prospect form with current STU details if requested
- Confirm STU legal entity, signing authority, and invoicing details for the LCP Agreement
- Confirm EDRLab membership form requirements
- Budget the annual LCP fee + EDRLab membership in the 2026 STU digital-library budget
- Confirm the digital-library annual budget bracket has not changed since the 2024 quote (≈ 50 k EUR), as it drives the fee tier

## Submission gate

We submit to EDRLab only when items 1–8 are green and item 9 is documented. Items 10–11 are required for the actual certification run after the agreement is signed and the production patch is applied.
