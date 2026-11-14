"""Graph job kinds (IP-031): the nightly rebuild (#154)."""

from datetime import timedelta

from apps.graph.tasks import REBUILD_TIME_LIMIT
from apps.tasks.contract import maintenance

maintenance(
    "maintenance.graph_rebuild",
    "apps.graph.tasks.rebuild_graph",
    "Rebuild the library graph",
    "Drop and re-project the Apache AGE library graph from the relational tables (IP-030, #154).",
    time_limit=timedelta(seconds=REBUILD_TIME_LIMIT),
)
