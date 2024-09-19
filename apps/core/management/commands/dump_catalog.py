import tarfile
from io import BytesIO
from uuid import UUID
from collections import defaultdict, deque

from django.conf import settings
from django.core.management import BaseCommand
from django.core.serializers import serialize
from django.utils import timezone

from apps.core.models import Catalog, Entry, Acquisition, Feed, Category, Price, Author, EntryAuthor


class Command(BaseCommand):
    help = "Export catalog as TAR with related metadata"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=UUID, default=None, help="Catalog UUID")
        parser.add_argument("--name", type=str, default=None, help="Catalog unique url_name")
        parser.add_argument("--skip-files", action="store_true", help="Do not include static files")
        parser.add_argument("--output", type=str, default=None, help="Path to save the tar archive")

    @classmethod
    def _add_file_to_tar(cls, tar: tarfile.TarFile, filename: str, content: BytesIO) -> None:
        tarinfo = tarfile.TarInfo(name=filename)
        tarinfo.size = len(content.getvalue())
        tar.addfile(tarinfo, content)

    @staticmethod
    def _topological_sort_feeds(feeds):
        """Perform a topological sort of feeds based on parent-child relationships."""
        # Build the graph
        graph = defaultdict(list)
        in_degree = defaultdict(int)

        # Initialize the in-degree count and graph
        for feed in feeds:
            # Ensure every feed has an entry in the graph
            in_degree[feed.pk] = in_degree.get(feed.pk, 0)

            # Process parent-child relationships
            for parent in feed.parents.all():
                graph[parent.pk].append(feed.pk)
                in_degree[feed.pk] += 1

        # Perform Kahn's Algorithm for topological sorting
        sorted_feeds = []
        zero_in_degree_queue = deque([feed.pk for feed in feeds if in_degree[feed.pk] == 0])

        while zero_in_degree_queue:
            current_feed_pk = zero_in_degree_queue.popleft()
            current_feed = next(feed for feed in feeds if feed.pk == current_feed_pk)
            sorted_feeds.append(current_feed)

            for child_pk in graph[current_feed_pk]:
                in_degree[child_pk] -= 1
                if in_degree[child_pk] == 0:
                    zero_in_degree_queue.append(child_pk)

        if len(sorted_feeds) != len(feeds):
            raise ValueError("Cycle detected in feeds! Backup process aborted.")

        return sorted_feeds

    def handle(self, *args, **options):
        started_at = timezone.now()
        self.stdout.write(f"Started: {started_at.isoformat()}")

        conditions = {}
        if options.get("id"):
            conditions["pk"] = options["id"]
        elif options.get("name"):
            conditions["url_name"] = options["name"]
        else:
            self.stderr.write(
                self.style.ERROR(
                    f"Catalog 'id' or 'name' (url_name) have to be provided. Use --help for more information"
                )
            )
            return

        try:
            catalog = Catalog.objects.get(**conditions)
        except Catalog.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"Catalog {options['catalog']} does not exist!"))
            return

        self.stdout.write(f"Preparing to backup catalog {catalog.title} ({catalog.pk})")

        try:
            sorted_feeds = self._topological_sort_feeds(
                Feed.objects.filter(catalog=catalog).prefetch_related("parents")
            )
        except ValueError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        with tarfile.open(options["output"] or f"{catalog.url_name}.tar", "w") as tar:
            self._add_file_to_tar(
                tar,
                "entities.json",
                BytesIO(
                    serialize(
                        "json",
                        [
                            *Catalog.objects.filter(pk=catalog.pk),
                            *Category.objects.filter(catalog=catalog),
                            *Author.objects.filter(catalog=catalog),
                            *Entry.objects.filter(catalog=catalog),
                            *EntryAuthor.objects.filter(entry__catalog=catalog),
                            *Acquisition.objects.filter(entry__catalog=catalog),
                            *Price.objects.filter(acquisition__entry__catalog=catalog),
                            *sorted_feeds,
                        ],
                    ).encode("utf-8")
                ),
            )

            # Related files
            if not options["skip_files"]:
                if settings.EVILFLOWERS_STORAGE_DRIVER == "apps.files.storage.filesystem.FileSystemStorage":
                    tar.add(
                        f"{settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR}/catalogs/{catalog.url_name}",
                        f"storage/catalogs/{catalog.url_name}",
                    )

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
