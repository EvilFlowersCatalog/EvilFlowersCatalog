"""`harvest_attach_files --source <slug> --dir <path> --match isbn|record_id|filename [--dry-run]` (IP-034).

Files after metadata: a partner delivers PDFs/EPUBs named by ISBN, by its record number, or as its MARC `856`
links name them. Each file goes to exactly one entry of the source's catalog through `attach_acquisition`
(the REST upload's path: checks, LCP, extraction); an entry that already has a stored file is left alone.
"""

import json
from collections import defaultdict
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.api.services.entry import InvalidAcquisitionFile, attach_acquisition
from apps.core.models import Acquisition, Entry
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.records import normalize_isbn

MIMES = {".pdf": Acquisition.AcquisitionMIME.PDF, ".epub": Acquisition.AcquisitionMIME.EPUB}


class Command(BaseCommand):
    help = "Attach a directory of PDF/EPUB files to the entries of a harvest source's catalog."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="The harvest source's slug.")
        parser.add_argument("--dir", required=True, help="Directory with the files (not recursive).")
        parser.add_argument("--match", required=True, choices=("isbn", "record_id", "filename"))
        parser.add_argument("--dry-run", action="store_true", help="Report what would be attached.")

    def handle(self, *args, **options):
        source = HarvestSource.objects.select_related("target_catalog").filter(slug=options["source"]).first()
        if source is None:
            raise CommandError(f"No harvest source '{options['source']}'.")
        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"'{directory}' is not a directory.")

        lookup = self._lookup(source, options["match"])
        report = defaultdict(list)
        for path in sorted(directory.iterdir()):
            mime = MIMES.get(path.suffix.lower())
            if not path.is_file() or mime is None:
                continue
            key = path.name if options["match"] == "filename" else path.stem
            if options["match"] == "isbn":
                key = normalize_isbn(key) or key
            entries = lookup.get(key, set())
            if len(entries) != 1:
                report["unmatched" if not entries else "ambiguous"].append(path.name)
                continue
            entry = Entry.objects.get(pk=next(iter(entries)))
            if any(item.content for item in entry.acquisitions.all()):
                report["has_file"].append(path.name)
                continue
            if options["dry_run"]:
                report["would_attach"].append(f"{path.name} → {entry.pk}")
                continue
            try:
                with transaction.atomic(), path.open("rb") as handle:
                    attach_acquisition(entry, File(handle, name=path.name), mime=mime)
            except InvalidAcquisitionFile as error:
                report["refused"].append(f"{path.name}: {error}")
                continue
            report["attached"].append(f"{path.name} → {entry.pk}")
        self.stdout.write(json.dumps({key: value for key, value in report.items()}, indent=2, ensure_ascii=False))

    @staticmethod
    def _lookup(source: HarvestSource, match: str) -> dict[str, set[str]]:
        """Key (normalised ISBN, record id or file name) → entry ids of the source's catalog."""
        lookup: dict[str, set[str]] = defaultdict(set)
        if match in ("isbn", "record_id"):
            rows = Entry.objects.filter(catalog=source.target_catalog).values_list("pk", "identifiers")
            for pk, identifiers in rows.iterator():
                identifiers = identifiers or {}
                if match == "isbn":
                    isbn = normalize_isbn(identifiers.get("isbn"))
                    if isbn:
                        lookup[isbn].add(str(pk))
                elif identifiers.get(source.system_key):
                    lookup[identifiers[source.system_key]].add(str(pk))
            return lookup
        rows = HarvestRecord.objects.filter(source=source, dry_run=False, entry__isnull=False)
        for record_links, entry_id in rows.values_list("source_record__links", "entry_id").iterator():
            for link in record_links or []:
                name = str(link).rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                if name:
                    lookup[name].add(str(entry_id))
        return lookup
