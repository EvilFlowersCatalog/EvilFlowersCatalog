"""The registry of job definitions (IP-031 rev. 2): kind → definition, task → definition.

Apps register their definitions in `apps/<app>/jobs.py` with `@registry.register`; the core finds
them here and never imports an app.
"""

from __future__ import annotations

import logging
from importlib import import_module

from apps.tasks.domain.contract import JobDefinition

logger = logging.getLogger(__name__)


class Registry:
    def __init__(self) -> None:
        self._by_kind: dict[str, JobDefinition] = {}
        self._by_task: dict[str, JobDefinition] = {}
        self._loaded = False

    def register[T: type[JobDefinition]](self, definition_class: T) -> T:
        definition = definition_class()
        if definition.kind in self._by_kind:
            raise ValueError(f"job kind {definition.kind} registered twice")
        if definition.task in self._by_task:
            raise ValueError(f"task {definition.task} already belongs to {self._by_task[definition.task].kind}")
        self._by_kind[definition.kind] = definition
        self._by_task[definition.task] = definition
        return definition_class

    def load(self) -> None:
        """Import every installed app's `jobs` module once."""
        if self._loaded:
            return
        self._loaded = True
        from django.apps import apps

        for config in apps.get_app_configs():
            try:
                import_module(f"{config.name}.jobs")
            except ModuleNotFoundError as error:
                if error.name != f"{config.name}.jobs":
                    raise

    def get(self, kind: str | None) -> JobDefinition | None:
        self.load()
        return self._by_kind.get(kind) if kind else None

    def for_task(self, task: str | None) -> JobDefinition | None:
        self.load()
        return self._by_task.get(task) if task else None

    def all(self) -> list[JobDefinition]:
        self.load()
        return sorted(self._by_kind.values(), key=lambda definition: definition.kind)


registry = Registry()

__all__ = ["Registry", "registry"]
