# E-mail notifications — current texts

Rendered from `apps/notifications/templates/notifications/*.txt` with sample values (header/footer name from `EVILFLOWERS_NOTIFICATION_LIBRARY_NAME`). Regenerate with `python manage.py test_notification` against a dev mailbox, or re-run the render script in `docs/notifications/`. The HTML version (MJML) carries the same wording with a coloured header and a button where the plain text shows a link.

| # | Notification | Sent when |
|---|---|---|
| 1 | `license_created` | Reader borrows a book (or claims a reservation) — a new loan is issued. |
| 2 | `license_expiring_soon` | 3 days before a loan ends (daily sweep, once per loan per day). |
| 3 | `license_renewed` | Reader renews a loan. |
| 4 | `license_returned` | Reader returns a loan early (from the reading app or the portal). |
| 5 | `license_revoked` | Library staff revokes a loan. |
| 6 | `reservation_placed` | Reader joins the waiting list for a fully-borrowed book. |
| 7 | `reservation_available` | A copy is freed and the reader is first in line — the 48-hour claim window opens. |
| 8 | `reservation_claim_reminder` | 6 hours before an unclaimed reservation's window closes (sent once). |
| 9 | `reservation_expired` | Claim window missed — the copy is passed to the next reader in line. |
| 10 | `reservation_cancelled` | Reader (or staff) cancels a reservation. |
| 11 | `reservation_promoted` | Reader moves up the waiting list (optional — off by default, max 3 per book per day). |
| 12 | `passphrase_changed` | Reader sets or changes their LCP passphrase. |

## 1. `license_created`

**Sent when:** Reader borrows a book (or claims a reservation) — a new loan is issued.

**Subject:** New e-book loan: Managing Protected Areas in Central and Eastern Europe Under Climate Change

```text
New e-book loan
===============

Hello Alena Václavová,

A new loan has been created for the following publication:

  Title:  Managing Protected Areas in Central and Eastern Europe Under Climate Change
  Author: Sven Rannow
  Valid:  20.08.2026 00:00 - 03.09.2026 00:00

Download your license file (.lcpl):
https://dev.evilflowers.elvira.stuba.sk/readium/v1/licenses/9c1e….lcpl?token=…

Open the file in Thorium Reader (or another LCP-compatible reading app) and enter your passphrase to start reading.
The license file is also attached to this e-mail.

This download link expires in 72 hours.

Passphrase hint: Your student ID number

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 2. `license_expiring_soon`

**Sent when:** 3 days before a loan ends (daily sweep, once per loan per day).

**Subject:** Your loan of "Managing Protected Areas in Central and Eastern Europe Under Climate Change" expires soon

```text
Loan expiring soon
==================

Hello Alena Václavová,

Your loan for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow expires on 03.09.2026 00:00 (in 3 days).

If you need more time, renew the loan from your library shelf.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 3. `license_renewed`

**Sent when:** Reader renews a loan.

**Subject:** Loan renewed: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Loan renewed
============

Hello Alena Václavová,

Your loan of "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow has been renewed.
New expiration: 17.09.2026 00:00.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 4. `license_returned`

**Sent when:** Reader returns a loan early (from the reading app or the portal).

**Subject:** Loan returned: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Loan returned
=============

Hello Alena Václavová,

Your loan of "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow has been returned and is no longer available for reading.

Thank you for using the library.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 5. `license_revoked`

**Sent when:** Library staff revokes a loan.

**Subject:** License revoked: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
License revoked
===============

Hello Alena Václavová,

Your license for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow has been revoked. You can no longer open this publication.

If you believe this is an error, contact the library.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 6. `reservation_placed`

**Sent when:** Reader joins the waiting list for a fully-borrowed book.

**Subject:** Reservation placed: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Reservation placed
==================

Hello Alena Václavová,

You have been added to the queue for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow.

Your position in line: #1

Expected to become available after 10.09.2026 08:49, when the current loan ends.
This is only an estimate: the book may come back earlier if the current reader returns it, or later if the loan is renewed.

We will email you again as soon as a slot opens up. You will have 48 hours to claim it.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 7. `reservation_available`

**Sent when:** A copy is freed and the reader is first in line — the 48-hour claim window opens.

**Subject:** Your reservation is ready: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Your reservation is ready
=========================

Hello Alena Václavová,

A slot has opened up for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow.

Claim it before 12.09.2026 08:49 or it will be passed to the next user in line.

Claim here: https://dev.evilflowers.elvira.stuba.sk/readium/v1/reservations/2f1c…/claim?access_token=…

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 8. `reservation_claim_reminder`

**Sent when:** 6 hours before an unclaimed reservation's window closes (sent once).

**Subject:** Reminder: claim "Managing Protected Areas in Central and Eastern Europe Under Climate Change" before your window closes

```text
Don't forget to claim your reservation
======================================

Hello Alena Václavová,

Your reservation for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow is still waiting to be claimed.

The claim window closes at 12.09.2026 08:49. If you don't claim it before then, your slot will pass to the next user in line.

Claim here: https://dev.evilflowers.elvira.stuba.sk/readium/v1/reservations/2f1c…/claim?access_token=…

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 9. `reservation_expired`

**Sent when:** Claim window missed — the copy is passed to the next reader in line.

**Subject:** Reservation expired: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Reservation expired
===================

Hello Alena Václavová,

Your reservation for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow was not claimed before the deadline and has been passed to the next user in line.

You are welcome to place a new reservation at any time.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 10. `reservation_cancelled`

**Sent when:** Reader (or staff) cancels a reservation.

**Subject:** Reservation cancelled: "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
Reservation cancelled
=====================

Hello Alena Václavová,

Your reservation for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow has been cancelled.

You can place a new reservation at any time if you change your mind.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 11. `reservation_promoted`

**Sent when:** Reader moves up the waiting list (optional — off by default, max 3 per book per day).

**Subject:** You've moved up the queue for "Managing Protected Areas in Central and Eastern Europe Under Climate Change"

```text
You've moved up the queue
=========================

Hello Alena Václavová,

Good news — you've moved up the queue for "Managing Protected Areas in Central and Eastern Europe Under Climate Change" by Sven Rannow.

Your new position in line: #2 (previously #3).

We will email you again as soon as your turn comes up.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```

## 12. `passphrase_changed`

**Sent when:** Reader sets or changes their LCP passphrase.

**Subject:** Your LCP passphrase was changed

```text
Passphrase changed
==================

Hello Alena Václavová,

Your LCP passphrase was updated. Existing loans now require the new passphrase when opened in your reader.

If this was not you, please contact the library immediately.

--
This is an automated notification from Digitálna knižnica Elvíra / Digital Library Elvira. Please do not reply to this e-mail.
```
