"""What an app imports to contribute jobs (IP-031 rev. 2) — and nothing else from `apps.tasks`.

    from apps.tasks.contract import JobDefinition, Spec, Ready, Blocked, Refused, Scope, registry

    @registry.register
    class OcrDefinition(JobDefinition[Acquisition, OcrSpec]):
        kind = "files.ocr"
        task = "evilflowers_ocr_worker.ocr"
        ...

To declare a job from domain code (an upload starting OCR), use `declare()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.tasks.application.registry import registry
from apps.tasks.domain.contract import Dispatch, DispatchError, EmptySpec, JobDefinition, Report, Spec
from apps.tasks.domain.values import Blocked, JobState, Observation, Ready, Refused, Scope


def declare(kind: str, subject_id: Any = None, *, spec: Mapping | None = None, cause_id: Any = None):
    """Declare a job on behalf of the system (no user, no permission check). Raises `DispatchError`."""
    from apps.tasks.application.facade import facade

    return facade.apply(kind, subject_id, spec=spec, cause_id=cause_id, check_permission=False)


def maintenance(kind: str, task: str, title: str, description: str) -> type[JobDefinition]:
    """Register a subjectless kind for a beat task: one job for the whole kind; applying it runs it now."""
    definition = type(
        "".join(part.title() for part in kind.replace(".", "_").split("_")) + "Definition",
        (JobDefinition,),
        {"kind": kind, "task": task, "title": title, "description": description},
    )
    return registry.register(definition)


def run_only(kind: str, task: str, title: str, description: str, subject_model=None, subject_arg=None):
    """Register a kind whose runs are recorded but never declared (many runs per subject: e-mails, batches)."""
    attributes = {
        "kind": kind,
        "task": task,
        "title": title,
        "description": description,
        "keyed": False,
        "subject_model": subject_model,
    }
    if subject_arg is not None:
        name, position = subject_arg
        attributes["subject_from_call"] = lambda self, args, kwargs: uuid_arg(args, kwargs, name, position)
    definition = type(
        "".join(part.title() for part in kind.replace(".", "_").split("_")) + "Definition",
        (JobDefinition,),
        attributes,
    )
    return registry.register(definition)


def entry_scope(entry) -> Scope:
    return Scope(catalog_id=entry.catalog_id, entry_id=entry.pk) if entry is not None else Scope()


def uuid_arg(args, kwargs, name: str, position: int = 0) -> str | None:
    """A subject id passed as keyword `name` or as positional argument `position`."""
    import uuid

    value = kwargs.get(name) if isinstance(kwargs, Mapping) else None
    if value is None and isinstance(args, (list, tuple)) and len(args) > position:
        value = args[position]
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


__all__ = [
    "Blocked",
    "Dispatch",
    "DispatchError",
    "EmptySpec",
    "JobDefinition",
    "JobState",
    "Observation",
    "Ready",
    "Refused",
    "Report",
    "Scope",
    "Spec",
    "declare",
    "entry_scope",
    "maintenance",
    "registry",
    "run_only",
    "uuid_arg",
]
