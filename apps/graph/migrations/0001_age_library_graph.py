"""Apache AGE and the `library` graph (IP-030 Phase 3b).

`CREATE EXTENSION age` needs a superuser (like `vector` and `pg_search` in the release notes) and the
evilflowers-postgresql image with AGE (evilflowers-postgresql#3, knowledge#45). Where the extension is not
installable — plain PostgreSQL in development — or the role may not create it, the migration says so and
passes: the graph is optional, and `apps.graph.age.available()` turns its uses off. Run
`manage.py graph_rebuild` once the extension exists to create and fill the graph.
"""

import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)


def enable(apps, schema_editor):
    from apps.graph import age

    with schema_editor.connection.cursor() as cursor:
        if not age.extension_installable(cursor):
            logger.warning("Apache AGE is not available in this PostgreSQL; the library graph is off.")
            return
    try:
        with transaction.atomic():
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS age")
    except Exception as error:
        logger.warning("Could not create the AGE extension (%s); the library graph is off.", error)
        return
    age.create_graph()


class Migration(migrations.Migration):
    dependencies = []

    operations = [migrations.RunPython(enable, migrations.RunPython.noop)]
