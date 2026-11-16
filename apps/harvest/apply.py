"""Applying a matched or unmatched `SourceRecord` to the target catalog, by the source's mode (IP-034).

| Mode | Matched | Unmatched | Deleted at source |
|---|---|---|---|
| `link_only` | `system_key` set, entry otherwise untouched | nothing | key removed |
| `enrich` | fields applied per `field_policy` | nothing | key removed |
| `create` | as `enrich` | new entry, `origin = harvest:<slug>` | key removed; the entry stays |

Metadata goes through `EntryForm` + `EntryService.populate(import_mode=True)` (Q2), so validation and the
post-save side effects (search, IP-033 sync) are a librarian's. Three things are written here instead:

- authors, because `AuthorForm` requires a given name and corporate or single-name authors have none;
- subjects, by `(catalog, scheme, term)` with every new one created (Q3); the form's `categories` matches on
  term only;
- key-only writes (link, unlink) and edits of entries without a language, which `EntryForm` refuses; these use
  `save(update_fields=…)`, so `post_save` still fires.

The licence (`540`) is not applied: `rights` written through the form are stamped `manual`, which other
synchronised sources then never overwrite, and `apps.content.rights` has no harvest source yet.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from typing import Optional

from django.db.models import Q

from apps.api.forms.entries import EntryForm
from apps.api.services.entry import EntryService
from apps.core.models import Acquisition, Author, Category, Entry, EntryAuthor, Language
from apps.harvest.matching import MatchIndex
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.records import NORMALIZERS, SourceRecord

logger = logging.getLogger(__name__)

Status = HarvestRecord.Status
Policy = HarvestSource.Policy

#: `Entry.identifiers` values are validated to 100 characters by `EntryForm`.
MAX_IDENTIFIER = 100

#: Extensions of 856 links worth an external-URL acquisition; anything else is an OPAC page, not a file.
LINK_MIMES = {
    ".pdf": Acquisition.AcquisitionMIME.PDF,
    ".epub": Acquisition.AcquisitionMIME.EPUB,
}


@dataclass
class Outcome:
    status: str
    entry: Optional[Entry] = None
    matched_on: Optional[str] = None
    message: Optional[str] = None
    changed: list[str] = field(default_factory=list)
    #: `sync` sources: the row's sync columns (`synced_record`, `remote_version`, `local_version`, `sync_state`).
    sync: Optional[dict] = None
    #: `sync` sources: fields both sides changed (`apps.harvest.merge.Conflict`), with both timestamps.
    conflicts: list = field(default_factory=list)
    conflict_times: tuple = (None, None)


def run_user(source: HarvestSource):
    """Who created entries and categories are attributed to: the source's creator, else the first superuser."""
    from django.contrib.auth import get_user_model

    if source.creator_id:
        return source.creator
    return get_user_model().objects.filter(is_superuser=True).order_by("created_at").first()


def store_record(source: HarvestSource, record: SourceRecord, outcome: Outcome, *, dry_run: bool) -> HarvestRecord:
    """Write the mirror row of `record` with what was decided, and the conflicts of a `sync` merge."""
    from django.utils import timezone

    from apps.harvest.models import HarvestConflict

    message = outcome.message
    if outcome.changed and not message:
        message = "changed: " + ", ".join(outcome.changed)
    entry = outcome.entry
    defaults = {
        "datestamp": _datestamp(record.datestamp),
        "deleted": record.deleted,
        "raw_hash": record.raw_hash(),
        "source_record": record.as_dict(),
        "entry": entry if entry is not None and entry.pk and not entry._state.adding else None,
        "status": outcome.status,
        "matched_on": outcome.matched_on,
        "message": message,
    }
    if outcome.sync is not None:
        defaults.update(outcome.sync)
        defaults["synced_at"] = timezone.now()
    row, _created = HarvestRecord.objects.update_or_create(
        source=source, record_id=record.record_id[:255], dry_run=dry_run, defaults=defaults
    )
    if outcome.conflicts and not dry_run and defaults["entry"] is not None:
        local_at, remote_at = outcome.conflict_times
        HarvestConflict.objects.bulk_create(
            HarvestConflict(
                source=source,
                record=row,
                entry=defaults["entry"],
                field=conflict.field,
                local_value=conflict.local_value,
                remote_value=conflict.remote_value,
                local_at=local_at,
                remote_at=remote_at,
                winner=conflict.winner,
            )
            for conflict in outcome.conflicts
        )
    return row


def _datestamp(value: Optional[str]):
    """An ISO date or datetime (OAI datestamp, a remote version) as an aware datetime; None otherwise."""
    from datetime import datetime, timezone as dt_timezone

    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime

    if not value:
        return None
    try:
        parsed = parse_datetime(str(value))
        if parsed is None:
            day = parse_date(str(value)[:10])
            if day is None:
                return None
            parsed = datetime(day.year, day.month, day.day)
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt_timezone.utc)


class Applier:
    def __init__(self, source: HarvestSource, index: MatchIndex, *, user, dry_run: bool = False):
        self.source = source
        self.catalog = source.target_catalog
        self.index = index
        self.user = user
        self.dry_run = dry_run
        self.key = source.system_key
        self._languages: dict[str, Optional[Language]] = {}
        self._categories: dict[tuple, Category] = {}

    # -- entry points --------------------------------------------------------------------------------------

    def apply(self, record: SourceRecord) -> Outcome:
        if not record.record_id or len(record.record_id) > MAX_IDENTIFIER:
            return Outcome(Status.FAILED, message="record id missing or longer than 100 characters")
        match = self.index.match(record)
        if match.status == "ambiguous":
            return Outcome(Status.AMBIGUOUS, matched_on=match.matched_on, message=match.message)
        if match.status == "unmatched":
            if self.source.mode != HarvestSource.Mode.CREATE:
                return Outcome(Status.UNMATCHED)
            return self.create(record)

        entry = Entry.objects.select_related("language").get(pk=match.entry_id)
        if self.source.mode == HarvestSource.Mode.LINK_ONLY:
            changed = self.link(entry, record.record_id)
            return Outcome(Status.MATCHED, entry, match.matched_on, changed=changed)
        return self.enrich(entry, record, match.matched_on)

    def remove(self, record_id: str) -> Outcome:
        """Deleted at source: the key goes, the entry stays (a librarian decides about it)."""
        holders = sorted(self.index.holders(record_id))
        entry = None
        for entry_id in holders:
            entry = Entry.objects.get(pk=entry_id)
            if not self.dry_run:
                identifiers = dict(entry.identifiers or {})
                identifiers.pop(self.key, None)
                entry.identifiers = identifiers
                entry.save(update_fields=["identifiers", "updated_at", "touched_at"])
                self.index.unlink(entry_id)
        message = (
            f"{self.key} removed from {len(holders)} entr{'y' if len(holders) == 1 else 'ies'}" if holders else None
        )
        return Outcome(Status.DELETED, entry, message=message, changed=[self.key] if holders else [])

    # -- link only -----------------------------------------------------------------------------------------

    def link(self, entry: Entry, record_id: str) -> list[str]:
        if (entry.identifiers or {}).get(self.key) == record_id:
            return []
        if not self.dry_run:
            entry.identifiers = {**(entry.identifiers or {}), self.key: record_id}
            entry.save(update_fields=["identifiers", "updated_at", "touched_at"])
            self.index.link(str(entry.pk), record_id)
        return [self.key]

    # -- enrich and create ---------------------------------------------------------------------------------

    def enrich(self, entry: Entry, record: SourceRecord, matched_on: Optional[str]) -> Outcome:
        plan = self.plan(entry, record)
        status = Status.ENRICHED if self._enriches(entry, plan) else Status.MATCHED
        if self.dry_run:
            return Outcome(status, entry, matched_on, changed=sorted(plan))
        try:
            self.write(entry, plan, record)
        except EntryService.AlreadyExists:
            return Outcome(Status.FAILED, entry, matched_on, message=self._conflict(entry, plan))
        except ValueError as error:
            return Outcome(Status.FAILED, entry, matched_on, message=str(error))
        self.index.link(str(entry.pk), record.record_id)
        return Outcome(status, entry, matched_on, changed=sorted(plan))

    def create(self, record: SourceRecord) -> Outcome:
        title = (record.full_title or "").strip()[:255]
        if not title:
            return Outcome(Status.SKIPPED, message="no title to create an entry from")
        language = self.language(record.language)
        if language is None:
            return Outcome(Status.FAILED, message=f"no known language ({record.language or 'none'})")
        entry = Entry(creator=self.user, catalog=self.catalog, origin=self.source.origin)
        plan = self.plan(entry, record, creating=True)
        if self.dry_run:
            return Outcome(Status.CREATED, message="dry run: would create", changed=sorted(plan))
        try:
            self.write(entry, plan, record, creating=True)
        except EntryService.AlreadyExists:
            return Outcome(Status.FAILED, message=self._conflict(entry, plan))
        except ValueError as error:
            return Outcome(Status.FAILED, message=str(error))
        self.index.add(str(entry.pk), entry.identifiers or {})
        return Outcome(Status.CREATED, entry, changed=sorted(plan))

    def _overwrites(self, field_name: str) -> bool:
        return self.source.policy(field_name) == Policy.OVERWRITE

    def _enriches(self, entry: Entry, plan: dict) -> bool:
        """More than the link itself: a field, or an identifier besides `system_key`."""
        if set(plan) - {"identifiers"}:
            return True
        if "identifiers" not in plan:
            return False
        without_key = {k: v for k, v in plan["identifiers"].items() if k != self.key}
        return without_key != {k: v for k, v in (entry.identifiers or {}).items() if k != self.key}

    def wants(self, field_name: str, current, creating: bool) -> bool:
        """Whether the policy lets the record's value replace `current`."""
        policy = self.source.policy(field_name)
        if policy == Policy.IGNORE:
            return False
        if creating or policy == Policy.OVERWRITE:
            return True
        return current in (None, "", [], {})

    def plan(self, entry: Entry, record: SourceRecord, *, creating: bool = False) -> dict:
        """Field → new value, only for what changes."""
        plan: dict = {}
        simple = {
            "title": (entry.title, (record.full_title or "").strip()[:255] or None),
            "publisher": (entry.publisher, (record.publisher or "")[:255] or None),
            "summary": (entry.summary, record.summary),
            "edition": (entry.edition, (record.edition or "")[:255] or None),
            "page_count": (entry.page_count, record.page_count),
        }
        for name, (current, value) in simple.items():
            if value not in (None, "") and value != current and self.wants(name, current, creating):
                plan[name] = value

        if record.published and self.wants("published_at", entry.published_at, creating):
            current = str(entry.published_at) if entry.published_at else ""
            if not current.startswith(record.published):
                plan["published_at"] = record.published

        language = self.language(record.language)
        if language is not None and language.pk != entry.language_id:
            if self.wants("language", entry.language_id, creating):
                plan["language"] = language

        identifiers = dict(entry.identifiers or {})
        if self.source.policy("identifiers") != Policy.IGNORE:
            for key, value in record.identifiers.items():
                if len(value) > MAX_IDENTIFIER:
                    continue
                stored = identifiers.get(key)
                if stored and NORMALIZERS[key](stored) == value:
                    continue  # the stored spelling (hyphens, ISBN-10) stays
                if self.wants("identifiers", stored, creating):
                    identifiers[key] = value
        identifiers[self.key] = record.record_id
        if identifiers != (entry.identifiers or {}):
            plan["identifiers"] = identifiers

        if record.authors:
            current = [] if creating else [(a.surname, a.name) for a in self._authors(entry)]
            wanted = [(a.surname[:255], (a.name or "")[:255]) for a in record.authors if a.surname]
            if wanted and wanted != current and self.wants("authors", current, creating):
                plan["authors"] = wanted

        if record.subjects:
            current = [] if creating else list(entry.categories.values_list("pk", flat=True))
            if self.wants("subjects", current, creating):
                wanted = [(s.scheme, s.term) for s in record.subjects]
                # A term names one category per catalog, whatever its scheme (`unique (catalog, term)`).
                existing = set() if creating else set(entry.categories.values_list("term", flat=True))
                if {term for _scheme, term in wanted} != existing:
                    if not existing.issuperset(term for _scheme, term in wanted) or self._overwrites("subjects"):
                        plan["subjects"] = wanted

        if record.links and self.source.policy("links") != Policy.IGNORE:
            known = set() if creating else set(entry.acquisitions.values_list("file_url", flat=True))
            links = [url for url in record.links if url not in known and self._link_mime(url)]
            if links:
                plan["links"] = links
        return plan

    def write(self, entry: Entry, plan: dict, record: SourceRecord, *, creating: bool = False) -> None:
        form_fields = {"title", "publisher", "summary", "edition", "published_at", "language", "identifiers"}
        language = plan.get("language") or entry.language
        if "page_count" in plan:
            entry.page_count = plan["page_count"]

        if creating or (language is not None and form_fields & set(plan)):
            payload = {
                "title": plan.get("title", entry.title),
                "language_code": language.alpha2 or language.alpha3,
            }
            for name in ("publisher", "summary", "edition", "published_at", "identifiers"):
                if name in plan:
                    payload[name] = plan[name]
            if creating:
                # The portal renders `summary` as a string and crashes on null.
                payload.setdefault("summary", "")
            form = EntryForm(payload)
            if not form.is_valid():
                problems = "; ".join(
                    f"{'.'.join(map(str, getattr(e, 'path', None) or []))}: {e.message}" for e in form.errors
                )
                raise ValueError(f"invalid metadata: {problems}")
            EntryService(self.catalog, self.user).populate(entry, form, import_mode=True)
        elif set(plan) & (form_fields | {"page_count"}):
            # No language: `EntryForm` refuses the entry, so only the changed columns are written.
            columns = []
            for name in ("title", "publisher", "summary", "edition", "published_at", "identifiers", "page_count"):
                if name in plan:
                    setattr(entry, name, plan[name])
                    columns.append(name)
            if "language" in plan:
                entry.language = plan["language"]
                columns.append("language")
            entry.save(update_fields=[*columns, "updated_at", "touched_at"])

        if "authors" in plan:
            EntryAuthor.objects.filter(entry=entry).delete()
            for position, (surname, name) in enumerate(plan["authors"]):
                author = Author.objects.filter(catalog=self.catalog, surname=surname, name=name).first()
                author = author or Author.objects.create(catalog=self.catalog, surname=surname, name=name)
                EntryAuthor.objects.create(entry=entry, author=author, position=position)

        if "subjects" in plan:
            categories = [self.category(scheme, term) for scheme, term in plan["subjects"]]
            if self.source.policy("subjects") == Policy.OVERWRITE and not creating:
                entry.categories.set(categories)
            else:
                entry.categories.add(*categories)

        for url in plan.get("links", []):
            Acquisition.objects.create(
                entry=entry,
                relation=Acquisition.AcquisitionType.ACQUISITION,
                mime=self._link_mime(url),
                file_url=url,
                storage_backend=Acquisition.StorageBackend.EXTERNAL_URL,
            )

    # -- two-way sync -------------------------------------------------------------------------------------

    def apply_values(self, entry: Entry, values: dict) -> list[str]:
        """Write canonical values (`apps.harvest.projection`) onto `entry`, exactly: an empty value clears the
        field, except the title, which an entry always has. Returns the fields written.

        Scalars go through one `save(update_fields=…)` (entries without a language included), authors and
        categories through the related managers, so search and IP-033's OAI sync see an ordinary edit."""
        columns: list[str] = []
        identifiers = dict(entry.identifiers or {})
        for name, value in values.items():
            if name == "title":
                if value:
                    entry.title = value[:255]
                    columns.append("title")
            elif name in ("publisher", "edition"):
                setattr(entry, name, (value or None) and value[:255])
                columns.append(name)
            elif name == "summary":
                entry.summary = value or ""  # the portal renders `summary` as a string
                columns.append("summary")
            elif name == "published":
                entry.published_at = Entry._meta.get_field("published_at").to_python(value) if value else None
                columns.append("published_at")
            elif name == "language":
                language = self.language(value)
                if language is not None or not value:
                    entry.language = language
                    columns.append("language")
            elif name == "page_count":
                entry.page_count = value or None
                columns.append("page_count")
            elif name in ("isbn", "issn", "ccnb", "doi"):
                if value:
                    identifiers[name] = str(value)[:MAX_IDENTIFIER]
                else:
                    identifiers.pop(name, None)
        if identifiers != (entry.identifiers or {}):
            entry.identifiers = identifiers
            columns.append("identifiers")
        if columns:
            entry.save(update_fields=[*columns, "updated_at", "touched_at"])

        written = list(columns)
        if "authors" in values:
            from apps.harvest.records import SourceAuthor

            EntryAuthor.objects.filter(entry=entry).delete()
            for position, value in enumerate(values["authors"] or []):
                parsed = SourceAuthor.parse(value)
                author = Author.objects.filter(catalog=self.catalog, surname=parsed.surname, name=parsed.name).first()
                author = author or Author.objects.create(
                    catalog=self.catalog, surname=parsed.surname, name=parsed.name
                )
                EntryAuthor.objects.create(entry=entry, author=author, position=position)
            written.append("authors")
        for name, udc in (("udc", True), ("subjects", False)):
            if name not in values:
                continue
            from apps.harvest.records import UDC_SCHEME

            wanted = set(values[name] or [])
            current = [c for c in entry.categories.all() if (c.scheme == UDC_SCHEME) == udc]
            stale = [c for c in current if c.term not in wanted]
            if stale:
                entry.categories.remove(*stale)
            have = {c.term for c in current}
            missing = [self.category(UDC_SCHEME if udc else self.source.slug, term) for term in sorted(wanted - have)]
            if missing:
                entry.categories.add(*missing)
            written.append(name)
        return written

    # -- lookups -------------------------------------------------------------------------------------------

    def language(self, code: Optional[str]) -> Optional[Language]:
        if not code:
            return None
        if code not in self._languages:
            self._languages[code] = Language.objects.filter(Q(alpha2=code) | Q(alpha3=code)).first()
        return self._languages[code]

    def category(self, scheme: Optional[str], term: str) -> Category:
        """By `(catalog, scheme, term)`, created when missing (Q3). `Category` is unique on `(catalog, term)`, so a
        term that exists under another scheme (or none) is that category: a second row cannot exist."""
        cache_key = (scheme, term)
        if cache_key not in self._categories:
            category = (
                Category.objects.filter(catalog=self.catalog, scheme=scheme, term=term).first()
                or Category.objects.filter(catalog=self.catalog, term=term).first()
            )
            if category is None:
                category = Category.objects.create(catalog=self.catalog, scheme=scheme, term=term, creator=self.user)
            self._categories[cache_key] = category
        return self._categories[cache_key]

    @staticmethod
    def _authors(entry: Entry) -> list[Author]:
        return [
            link.author
            for link in EntryAuthor.objects.filter(entry=entry).select_related("author").order_by("position")
        ]

    @staticmethod
    def _link_mime(url: str) -> Optional[str]:
        path = url.split("?", 1)[0].lower()
        for extension, mime in LINK_MIMES.items():
            if path.endswith(extension):
                return mime
        guessed, _encoding = mimetypes.guess_type(path)
        return guessed if guessed in Acquisition.AcquisitionMIME.values else None

    def _conflict(self, entry: Entry, plan: dict) -> str:
        identifiers = plan.get("identifiers") or entry.identifiers or {}
        conditions = Q()
        for key in ("isbn", "doi"):
            if identifiers.get(key):
                conditions |= Q(**{f"identifiers__{key}": identifiers[key]})
        if not (identifiers.get("isbn") or identifiers.get("doi")):
            conditions |= Q(title=plan.get("title", entry.title))
        clash = Entry.objects.filter(catalog=self.catalog).exclude(pk=entry.pk).filter(conditions).first()
        if clash is None:
            return "an entry with the same title, ISBN or DOI exists"
        return f"entry {clash.pk} ('{clash.title}') has the same ISBN, DOI or title"


__all__ = ["Applier", "Outcome"]
