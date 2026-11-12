---
draft: true
date: 2026-11-09
authors:
  - jdubec
categories:
  - Feature
tags:
  - notifications
  - ldap
  - branding
  - mcp
  - stu
---

# IP-025: Notification Branding and LDAP E-mail Import

<!-- more -->

## Status

**Draft.** Implemented on the working tree with the recommended answers and tested locally (directory mocked,
STU preview rendered). Not deployed. Not yet run: the backfill against `ldap.stuba.sk`, the IaC volume, and
the STU branding on dev.

Deployment: EvilFlowersCatalog/knowledge#23.

## Problem Statement

Notification e-mails (IP-002) work, but STU raised two gaps.

1. **Students do not get them.** The `stuba.sk` auth source maps `mail` to a notification contact
   (`NOTIFICATION_CONTACT_MAP`), but the address was imported only inside `BasicBackend._ldap`, i.e. when the
   user typed their password. On dev (checked 2026-11-09), 11 of 17 `stuba.sk` users have no contact. Every
   one of them last logged in before 2026-08-18, the date of the first imported contact. Any notification
   for them would be logged `no_contact`.
2. **The e-mails do not look like STU's.** The header is a blue band with the text name. The only knob is
   `EVILFLOWERS_NOTIFICATION_LIBRARY_NAME`. There is no logo, no colours and no footer, and a change needs a
   redeploy.

The login import also had a latent crash: `update_or_create(user=, type=)` raises `MultipleObjectsReturned`
once a user has added a second address through `POST /api/v1/notification-contacts`, and that breaks their
next LDAP login.

## Proposed Solution

### 1. LDAP e-mail import without a login

- **One shared import path.** `apps/core/services/ldap_directory.py` holds the code used by both the login
  path and a background sync: `apply_profile`, `search_profile` and `requested_attributes`.
- **Attributes are requested by name.** Probed from elvira.stuba.sk on 2026-11-09:
  - An anonymous search for `(uid=…)` on `ldap.stuba.sk` returns the DN but no attributes when asked for
    `*`.
  - The same search returns `mail`, `cn` and `uid` when they are requested explicitly.
  - So the sync needs **no service account**. It binds anonymously, or with `PROXY_USER_DN` when the source
    has one.
  - Login now asks for the mapped attributes plus `GROUP_ATTR` too, which also lets an operational
    `memberOf` come back.
- **Contacts carry a source.** `NotificationContact.source` is either `user` or `ldap`.
  `NotificationService.sync_directory_contact` applies these rules:
  - It *adopts* an existing row with the same address, which covers rows imported before this field existed.
  - Otherwise it *rewrites* its own earlier `ldap` row when the directory address changed, or *creates* a
    new one.
  - The directory address becomes primary only if the user has no primary contact of that type, or the
    current primary is itself `ldap`. **A primary address the user chose is never demoted.**
  - Stale `ldap` rows are deleted. `user` rows are never touched.
  - Addresses are lower-cased.
- **The sync touches names and contacts only.** Group membership and the superuser flag stay a login-time
  decision. A background job should not grant admin rights from a directory read.
- **Entry points:**
  - `python manage.py sync_ldap_profiles [--auth-source X] [--username U] [--dry-run]` for the backfill. Its
    report never prints an address.
  - The Celery beat task `core-sync-ldap-profiles`, controlled by `EVILFLOWERS_LDAP_SYNC_SCHEDULE` (default
    `15 4 * * *`; empty disables it).
  - The MCP tool `sync_ldap_profiles` (superuser).

### 2. Configurable branding in three layers

| Layer | Where | Who changes it | What |
|-------|-------|----------------|------|
| 1. Overrides volume | `EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR`, mounted by IaC from the knowledge repository | Ansible | `templates/notifications/*.mjml\|.txt` (whole-template overrides, found before the built-in ones); `assets/*.png\|jpg\|gif` (logo images) |
| 2. Branding tokens | `notification_branding` table (one row) | superuser over MCP | library name, logo, colours, footer text, website, From display name |
| 3. Defaults | settings + `branding.DEFAULTS` | deployment | what an untouched deployment sends |

- **Tokens, not template source, in the database.** The row holds name, logo, alt text, width and link,
  five hex colours, footer text, website URL and From name. An empty value inherits the default. Colours
  are validated against a strict hex regex because they end up in MJML attributes.
- **The logo is an asset on the volume or an absolute https URL.** Mail clients fetch images without
  credentials, so the catalog serves assets publicly at `/notifications/v1/assets/<name>`:
  - The name must match a strict pattern, with no path components.
  - Only png, jpg and gif are served. SVG is refused because Gmail and Outlook drop it.
  - Responses carry `Cache-Control: public, max-age=86400`.
  - The URL is built from `EVILFLOWERS_BASE_URL`, not from the request.
- **How the templates use the tokens:**
  - The built-in `base.mjml` consumes `{{ brand.* }}`: a logo header (or text header), an accent strip in
    `primary_color`, buttons and links in `primary_color`, and a footer with `footer_text` and
    `website_url`.
  - Every token has an in-template default, so `compile_templates` still renders with an empty context.
  - Plain-text templates share `notifications/_footer.txt`, which renders with autoescape off so `&` stays
    `&`.
- **Rendering is separate from sending.** `NotificationService.render(type, context, brand)` returns
  subject, text and HTML without a recipient or a log entry, so previews never send mail.
  `NotificationService.from_address()` adds the From display name.
- **MCP tools.** All are superuser only and refuse with "restricted to administrators" (checked by the write
  sweep in `test_curation`):
  - `get_notification_branding` (read) returns:
    - the effective, stored and default tokens;
    - the assets and template overrides on the volume;
    - the notification types;
    - warnings, such as a missing asset or an unset volume.
  - `update_notification_branding` (write) does a partial update; `reset: [...]` returns tokens to their
    defaults. All invalid fields are refused in one message, which lists the available assets.
  - `preview_notification` (read) returns the subject, text and compiled HTML, rendered with sample data.
  - `send_test_notification` (write, not idempotent) sends to **the caller's own** primary contact only.
  - `sync_ldap_profiles` (write) has a dry run and reports usernames and actions, never addresses.

### STU configuration (target)

| Token | Value |
|-------|-------|
| `library_name` | `Digitálna knižnica Elvíra / Digital Library Elvira` |
| `logo` | `stu-logo.png` (on the volume; source: elvira-portal `public/assets/stu/stu/logo-dark.png`, 1030×220, dark text, red dots) |
| `logo_width` | `240` |
| `header_background` / `header_text_color` | `#ffffff` / `#333333` (the dark-text logo needs a white header; `logo-light.png` has white text) |
| `primary_color` | `#a60c2f` (sampled from the STU logo dots) |
| `footer_text` | `Slovenská technická univerzita v Bratislave` + address |
| `website_url` | `https://elvira.stuba.sk` |

## Implementation Plan

### Phase 1 — LDAP import

- [x] `apps/core/services/ldap_directory.py`; `BasicBackend._ldap` uses it and requests attributes by name
- [x] `NotificationContact.source` and `NotificationService.sync_directory_contact`, which fixes the
  `MultipleObjectsReturned` login crash
- [x] `sync_ldap_profiles` management command, Celery task, beat entry and `EVILFLOWERS_LDAP_SYNC_SCHEDULE`
- [x] Tests: the contact rule, sync (anonymous and proxy bind, dry run, by name never `*`), login regression
- [ ] Run `sync_ldap_profiles --dry-run` on dev, then without `--dry-run`, and confirm the 11 users get a
  contact
- [ ] Confirm prod's `stuba.sk` auth source has `NOTIFICATION_CONTACT_MAP: {"email": "mail"}` (not checked:
  prod read access was not granted in this session)

### Phase 2 — Branding

- [x] `NotificationBranding` model and migration `notifications/0004`; `apps/notifications/branding.py`
  (resolution, validation, assets)
- [x] `EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR`, prepended to `TEMPLATES[0]["DIRS"]`; public asset view
- [x] `base.mjml` on tokens; shared `_footer.txt`; `render()` / `from_address()`
- [x] MCP tools, server instructions and tool inventories in tests
- [x] Tests: resolution, validation, asset view (traversal, svg), every type renders branded, a volume
  override wins, `compile_templates` is clean, and the MCP end-to-end for STU

### Phase 3 — Deployment (knowledge repository, Ansible — knowledge#23)

- [ ] Add `stu-logo.png` (= portal `logo-dark.png`) to the knowledge repository and mount it read-only, plus
  an empty `templates/` directory, at e.g. `/srv/notifications`; set
  `EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR=/srv/notifications` on the catalog **and** the Celery worker (the
  worker renders the e-mails)
- [ ] Migrate, then set the STU tokens with `update_notification_branding`, check with
  `preview_notification` and `send_test_notification`
- [ ] Check in Gmail, Outlook (web and desktop) and the STU webmail that the logo loads

## Technical Details

- **Files:**
  - `apps/core/services/ldap_directory.py`, `apps/core/auth.py`
  - `apps/core/management/commands/sync_ldap_profiles.py`, `apps/tasks/tasks.py`, `evil_flowers_catalog/celery.py`
  - `apps/notifications/{branding.py, views.py, services.py, registry.py}`,
    `models/{notification_branding.py, notification_contact.py}`, `migrations/0004_*`
  - templates: `base.mjml`, `_footer.txt`, all 12 `.txt` templates
  - `apps/mcp/tools/notifications.py`, `apps/mcp/server.py`
  - `evil_flowers_catalog/{settings/base.py, urls.py}`
- **The worker renders.** `send_notification` runs in Celery, so the volume and the environment variable
  must be on the worker. `branding.resolve()` reads the database row on every send, with no cache, so an
  edit applies to the next e-mail on every process.
- **No address leaves through MCP or logs.** The sync reports usernames and actions only.
  `send_test_notification` can only target the caller.

## Alternatives Considered

- **Derive `login@stuba.sk`.** Rejected. Only 1 of 6 known addresses has that form; the rest are
  `name.surname@stuba.sk`, and these e-mails carry license links.
- **Editable template source in the database** (Django template strings edited over MCP). Rejected for now.
  It is a large surface, both for injection and for breaking every e-mail in one call. Structural changes
  are reviewed IaC changes on the volume.
- **Logo upload over a signed MCP link** (like `create_upload_link`). Not needed once the image lives on the
  IaC volume, and it would need a second redeem path in `apps/mcp/uploads.py`, which is entry-scoped.
- **Inline (CID) logo attachment.** It needs `multipart/related`, which conflicts with the `.lcpl`
  attachment of `license_created`, and some clients show it as an attachment.
- **Hot-link the portal's logo** (`https://elvira.stuba.sk/assets/stu/stu/logo-dark.png`). It works today
  and is allowed as a `logo` URL, but the portal can rename hashed assets on any release. The volume copy is
  owned by IaC.

## Trade-offs and Risks

- **Anonymous LDAP read** depends on STU keeping `mail` readable anonymously by name. If that changes, the
  sync reports `changed=0` and login import still works. A `PROXY_USER_DN` fixes it without code changes.
- **The asset endpoint is public** by necessity. It is limited to an IaC-owned, read-only directory, image
  types only and no path components.
- **Adopting legacy rows** marks an address as `ldap` when it equals the directory address, even if the user
  typed it. The address is the same, so nothing a user sees changes.
- **Autoescape.** Existing `.txt` templates and subjects still HTML-escape event data (`&` in a title becomes
  `&amp;`). That is pre-existing and not fixed here; only the new footer renders unescaped.

## Success Criteria

- On dev, all 17 `stuba.sk` users who still exist in the directory have an e-mail contact after one sync,
  and `no_contact` logs stop for them.
- An STU e-mail shows the STU logo, the STU red and the university footer in Gmail and Outlook.
- A superuser can change the branding, preview it and send it to themselves over MCP without a redeploy.

## Future Considerations

- Per-catalog branding (a faculty catalog with its faculty's logo; the portal already has `fad`, `fei`,
  `fiit`, … asset sets). See Q2.
- Slovak/English variants of the e-mail body (today English with the bilingual library name).
- Fix autoescape across all `.txt`/subject templates.

## References

- IP-002 Notification Engine with MJML Templates
- IP-014 Model Context Protocol Server
- elvira-portal `public/assets/stu/stu/logo-{light,dark}.png`

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation:

---

### Q1: 🔴 Should the directory address override a user-chosen primary address?

**Issue**: `sync_directory_contact` never demotes a primary address the user entered. A student who once
saved a private Gmail address keeps receiving there even after the directory has a `@stuba.sk` address.

**Context**: The university may want official mail to go to the university address.

**Question**: Should a user's own primary address win over the directory address?

**Options**:
- [ ] **A**: Yes, the user's choice wins (implemented) (recommended)
- [ ] **B**: The directory always wins for LDAP users
- [ ] **C**: A per-auth-source flag (`NOTIFICATION_CONTACT_OVERRIDE: true`)

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: ⚠️ Branding per deployment or per catalog?

**Issue**: Branding is one row for the whole deployment. STU faculties have their own logos in the portal.

**Context**: A loan from the FIIT catalog could carry the FIIT logo. That needs a nullable `catalog` FK and
for every notification context to name its catalog.

**Question**: Is deployment-wide branding enough for now?

**Options**:
- [ ] **A**: Deployment-wide now, per-catalog later as a follow-up (implemented) (recommended)
- [ ] **B**: Per-catalog in this proposal

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: ⚠️ What should happen to LDAP users the directory no longer knows?

**Issue**: The sync reports them as `not_found` and changes nothing. Graduates keep active accounts and
contacts.

**Context**: Deactivating accounts affects loans and reservations, so it is a policy decision, not a sync
side effect.

**Question**: Should the sync deactivate users who are missing from the directory?

**Options**:
- [ ] **A**: Report only (implemented) (recommended)
- [ ] **B**: Deactivate after N consecutive `not_found` runs
- [ ] **C**: Deactivate immediately

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: ℹ️ What should the sync schedule be?

**Issue**: The sync runs daily at 04:15 by default, binds once and makes one search per active LDAP user.
That is about 1,000 searches for STU-scale use.

**Question**: Is the default schedule acceptable?

**Options**:
- [ ] **A**: Daily at 04:15 (implemented) (recommended)
- [ ] **B**: Weekly
- [ ] **C**: Off by default; backfill by command only

**Answer**:
```
```

**Resolution**:
```
```

---

### Q5: ℹ️ Should the e-mails be bilingual?

**Issue**: The bodies are English. Only the library name is bilingual.

**Question**: Should the e-mails be Slovak, English, or both?

**Options**:
- [ ] **A**: Keep English; Slovak versions can be added as volume overrides (recommended for now)
- [ ] **B**: Built-in Slovak + English bodies, chosen per user language

**Answer**:
```
```

**Resolution**:
```
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-09 | jdubec | Initial draft; Phases 1–2 implemented with recommended answers and tested locally; added Review Questions section |
| 2026-11-09 | jdubec | Case-insensitive adoption of legacy contacts; `from_name` single-line; LDAP search timeout passed by keyword (was positional `all`); deployment filed as knowledge#23 |
