# IP-003 Phase 6 — Production-mode patching procedure

After EDRLab signs the LCP Agreement and issues production credentials, the
LCP servers must be switched from `test` mode (`v1` profile, open-source
cert) to `production` mode (`v2.x` profile, the X509 certificate signed by
EDRLab's CA).

Resolution of [Review Q4][q4]: production secrets live in a restricted
host-mount, **not** in Vault / sealed-secrets / 1Password Connect.

[q4]: ../proposals/posts/ip-003-lcp-edrlab-certification.md

## Directory layout

```
/etc/evilflowers/lcp-secrets/        (mode 0500, owned by `lcp:lcp`)
├── cert.pem                          (mode 0400) — X509 production certificate
├── key.pem                           (mode 0400) — private key
├── ca.pem                            (mode 0400) — EDRLab CA bundle
└── patch/                            (mode 0500)
    ├── lcpserver.patch               (mode 0400) — server patch script from EDRLab
    └── README.txt                    (mode 0400) — release notes from EDRLab
```

Mount this directory **read-only** into the LCP containers (see
`docker-compose.production.yml`):
```yaml
volumes:
  - /etc/evilflowers/lcp-secrets:/lcp/secrets:ro
```

## Initial install

```bash
# Create dedicated service user.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lcp

# Create the directory tree.
sudo install -d -m 0500 -o lcp -g lcp /etc/evilflowers/lcp-secrets
sudo install -d -m 0500 -o lcp -g lcp /etc/evilflowers/lcp-secrets/patch

# Drop the EDRLab-delivered files in, then lock perms.
sudo install -m 0400 -o lcp -g lcp /tmp/edrlab/cert.pem /etc/evilflowers/lcp-secrets/cert.pem
sudo install -m 0400 -o lcp -g lcp /tmp/edrlab/key.pem  /etc/evilflowers/lcp-secrets/key.pem
sudo install -m 0400 -o lcp -g lcp /tmp/edrlab/ca.pem   /etc/evilflowers/lcp-secrets/ca.pem
sudo install -m 0400 -o lcp -g lcp /tmp/edrlab/patch/*  /etc/evilflowers/lcp-secrets/patch/

# Wipe the staging copy.
shred -u /tmp/edrlab/*.pem
rm -rf /tmp/edrlab
```

## Configure LCP server to use the production profile

In `/etc/evilflowers/lcpserver.yaml`:

```yaml
certificate:
  cert:    "/lcp/secrets/cert.pem"
  private_key: "/lcp/secrets/key.pem"

# EDRLab issues a profile identifier with the cert package; record it here.
profile: "v2.X"   # e.g. "2.3"
```

Restart and verify:

```bash
sudo docker compose -f /etc/evilflowers/docker-compose.production.yml restart lcpserver
sudo docker compose -f /etc/evilflowers/docker-compose.production.yml exec lcpserver \
    /lcp/bin/lcpserver --version
```

The version output should include the production profile identifier.

## Backup

Encrypted tarball to STU IT's backup vault:

```bash
sudo tar czf - /etc/evilflowers/lcp-secrets \
    | gpg --encrypt -r lcp-backup-key@stuba.sk \
    > /var/backups/evilflowers/lcp-secrets-$(date +%Y%m%d).tar.gz.gpg
```

Schedule via cron once after every rotation.

## Rotation

When EDRLab re-issues credentials:

1. Verify the new bundle parses: `openssl x509 -in cert.pem -noout -text`.
2. Stage in `/etc/evilflowers/lcp-secrets-staging/` with same perms.
3. `mv /etc/evilflowers/lcp-secrets /etc/evilflowers/lcp-secrets.old`.
4. `mv /etc/evilflowers/lcp-secrets-staging /etc/evilflowers/lcp-secrets`.
5. Restart `lcpserver` and `lsdserver` (see install).
6. Verify with the bundle generator:
   `python manage.py generate_lcp_certification_bundle --entry <uuid> --output-dir /tmp/post-rotate-check`
   Inspect one license, confirm new cert's serial appears in `lcp:signature`.
7. After 24h with no incident, `rm -rf /etc/evilflowers/lcp-secrets.old`.

## Recovery

Lost the secrets directory? You cannot regenerate it — EDRLab must re-issue.
Contact `priyanka.dalotra@edrlab.org`. Existing licenses remain valid; no
new licenses can be signed until restore.

## Smoke test

```bash
python manage.py check_production_mode  # see deployment/scripts/check_production_mode.py
```

The script verifies:
- `/etc/evilflowers/lcp-secrets/` exists with mode `0500` owned by `lcp:lcp`
- Cert file is mode `0400`
- LCP server reports a `v2.*` profile identifier
- A test license can be issued and parses with the new cert's signature
