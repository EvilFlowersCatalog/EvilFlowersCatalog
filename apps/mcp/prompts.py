"""MCP prompts: the librarian workflows worth having on a menu.

A prompt is a template the *user* invokes — a slash command in the client —
that expands into a message sequence for the model. These encode the multi-step
sequences that get good answers out of this catalog and which a model otherwise
has to rediscover each session: resolve vocabulary before filtering, check
availability before recommending, look before you write.

Prompts are plain text generation. They read nothing and change nothing, so
they need no access control of their own — whatever the model does *next* runs
through the tools, which are controlled.
"""

from typing import Callable, Dict, List, Optional

from django.utils.translation import gettext as _

from apps.mcp.errors import ToolError


class Prompt:
    def __init__(self, name: str, title: str, description: str, arguments: List[dict], builder: Callable):
        self.name = name
        self.title = title
        self.description = description
        self.arguments = arguments
        self.builder = builder

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": self.arguments,
        }

    def render(self, arguments: Dict[str, str]) -> dict:
        for argument in self.arguments:
            if argument.get("required") and not arguments.get(argument["name"]):
                raise ToolError(
                    _("Prompt '%(prompt)s' requires the argument '%(argument)s'.")
                    % {"prompt": self.name, "argument": argument["name"]}
                )

        return {
            "description": self.description,
            "messages": [
                {"role": "user", "content": {"type": "text", "text": self.builder(arguments)}},
            ],
        }


def _argument(name: str, description: str, required: bool = False) -> dict:
    return {"name": name, "description": description, "required": required}


def _reading_list(arguments: Dict[str, str]) -> str:
    topic = arguments["topic"]
    audience = arguments.get("audience") or "a general reader"
    size = arguments.get("size") or "5"

    return (
        f"Build a reading list of about {size} publications on **{topic}** from this library, "
        f"suited to {audience}.\n\n"
        "Work in this order:\n"
        "1. Call `list_categories` with a query related to the topic, and `list_authors` if the "
        "request names a person. Filtering by the ids you get back is far more reliable than "
        "guessing at spellings.\n"
        "2. Call `search_entries` — combine `query` with any ids you resolved. Try a couple of "
        "phrasings if the first returns little; the search is keyword-based, not semantic.\n"
        "3. Call `get_entry` on the strongest candidates to check what each actually covers "
        "before recommending it.\n\n"
        "Present the list with, for each title: the authors, the year, one sentence on why it "
        "belongs here, and whether it can be borrowed right now (the `availability` field — say "
        "so plainly if a title is fully borrowed). If the library has little on this topic, say "
        "that instead of padding the list with weak matches."
    )


def _availability_report(arguments: Dict[str, str]) -> str:
    scope = arguments.get("catalog") or "every catalog I can read"

    return (
        f"Report on Readium LCP borrowing pressure across {scope}.\n\n"
        "1. Call `list_catalogs` to resolve the scope.\n"
        '2. Call `search_entries` with `lcp_states: ["fully_borrowed"]` to find titles with no '
        'copies left, and again with `lcp_states: ["available_now"]` for comparison. Page '
        "through the results rather than judging from the first page.\n\n"
        "Then summarise: how many titles are fully borrowed, which have the longest reservation "
        "queues (`availability.queue_length`), and which look like candidates for buying more "
        "licences. Give the numbers you actually observed and say how many pages you checked — "
        "do not extrapolate silently."
    )


def _organise_catalog(arguments: Dict[str, str]) -> str:
    catalog = arguments["catalog"]
    goal = arguments.get("goal") or "make the catalog easier to browse"

    return (
        f"Help me organise the catalog **{catalog}**. Goal: {goal}.\n\n"
        "Survey before proposing:\n"
        "1. `list_catalogs` to resolve the catalog id, then `whoami` to confirm I actually have "
        "manage rights (`manageable_catalogs`) — if I do not, say so and stop.\n"
        "2. `list_feeds` for the current navigation tree and `list_categories` for the subject "
        "vocabulary.\n"
        "3. `search_entries` scoped to the catalog to see what is actually in it, including how "
        "much is uncategorised.\n\n"
        "Then propose a concrete plan: which feeds to create, which categories are duplicates or "
        "near-duplicates worth merging, what is missing. **Show me the plan and wait for my "
        "approval before calling any create, update or delete tool.** When I approve, work "
        "through it one change at a time and tell me what each call did. For any deletion, tell "
        "me first how many publications it affects."
    )


def _classify_collection(arguments: Dict[str, str]) -> str:
    catalog = arguments["catalog"]
    scheme = arguments.get("scheme") or "the classification scheme I supply"
    scope = arguments.get("scope") or "every publication in the catalog"

    return (
        f"Classify {scope} in the catalog **{catalog}** against {scheme}.\n\n"
        "Do the classification yourself, from each publication's own metadata. There is no "
        "classifier service behind these tools — `classify_entries` only records the decision "
        "you make.\n\n"
        "Work in this order:\n"
        "1. `list_catalogs` to resolve the catalog id and confirm `access` is 'manage'. If it is "
        "not, say so and stop — nothing below will work.\n"
        "2. `list_categories` for that catalog. Whatever already exists is the vocabulary; only "
        "the genuinely missing terms need creating.\n"
        "3. `create_categories` for the missing ones, in one call. Give each a stable `term` and "
        "a human-readable `label`, and set `scheme` so the vocabulary is identifiable later. "
        "Existing terms are skipped, so this is safe to re-run.\n"
        "4. Page through `search_entries` scoped to the catalog. Work in batches — the response "
        "carries every entry's title, authors, summary and current categories, which is what you "
        "classify from. `get_entry` gives you the full description when a title is ambiguous.\n"
        '5. For each batch, call `classify_entries` with `mode: "add"` so nothing already filed '
        'is lost. Use `mode: "replace"` only if I explicitly asked for a re-classification.\n\n'
        "Rules for the classification itself:\n"
        "- Assign the most specific term that clearly fits, plus a broader one where it genuinely "
        "helps browsing. Two or three per publication is usually right.\n"
        "- When a publication does not clearly fit anything, leave it unclassified and list it "
        "for me at the end. A wrong classification is worse than none — it is invisible until "
        "someone browses the wrong subject and finds the wrong book.\n"
        "- Never invent a term that is not in the scheme.\n\n"
        "Before the first write, show me: how many publications are in scope, which categories "
        "you would create, and your classification of the first ten as a sample. **Wait for my "
        "approval.** Then work through the batches, reporting `changed` / `unchanged` counts as "
        "you go, and finish with the list of anything you could not place."
    )


def _catalog_overview(arguments: Dict[str, str]) -> str:
    return (
        "Give me an overview of this digital library.\n\n"
        "Call `whoami` (to see what this session can reach), `list_catalogs`, `list_categories` "
        "and `list_feeds`, plus `search_entries` with a small `limit` to sample the holdings and "
        "read the `total`.\n\n"
        "Summarise: how many catalogs and roughly how many publications, the main subjects, the "
        "languages present, how the navigation is structured, and how much of it is "
        "DRM-protected versus freely downloadable. Note anything that looks like a gap or an "
        "inconsistency worth a librarian's attention."
    )


_PROMPTS = [
    Prompt(
        name="reading_list",
        title="Build a reading list",
        description="Find and vet publications on a topic, then present them with availability.",
        arguments=[
            _argument("topic", "The subject to build the list around.", required=True),
            _argument("audience", "Who it is for, e.g. 'first-year undergraduates'."),
            _argument("size", "Roughly how many titles. Defaults to 5."),
        ],
        builder=_reading_list,
    ),
    Prompt(
        name="availability_report",
        title="Borrowing pressure report",
        description="Summarise which DRM-protected titles are fully borrowed and where queues are longest.",
        arguments=[_argument("catalog", "Restrict to one catalog by name. Defaults to all readable catalogs.")],
        builder=_availability_report,
    ),
    Prompt(
        name="organise_catalog",
        title="Plan catalog organisation",
        description="Survey a catalog's feeds and categories, then propose a curation plan before changing anything.",
        arguments=[
            _argument("catalog", "The catalog to organise, by name.", required=True),
            _argument("goal", "What you are trying to achieve."),
        ],
        builder=_organise_catalog,
    ),
    Prompt(
        name="classify_collection",
        title="Classify a collection against a scheme",
        description=(
            "Import a subject vocabulary (UDC/MDT, BISAC, a faculty's own list) into a catalog "
            "and file its publications under it, in reviewed batches."
        ),
        arguments=[
            _argument("catalog", "The catalog to classify, by name.", required=True),
            _argument("scheme", "The classification scheme, e.g. 'the MDT/UDC selection agreed for STU'."),
            _argument("scope", "Which publications, e.g. 'everything added since 2025'. Defaults to all of them."),
        ],
        builder=_classify_collection,
    ),
    Prompt(
        name="catalog_overview",
        title="Describe this library",
        description="A tour of what the library holds, how it is organised, and what this session can reach.",
        arguments=[],
        builder=_catalog_overview,
    ),
]

CATALOGUE: Dict[str, Prompt] = {prompt.name: prompt for prompt in _PROMPTS}


def list_prompts() -> dict:
    return {"prompts": [prompt.descriptor() for prompt in _PROMPTS]}


def get_prompt(name: str, arguments: Optional[dict]) -> dict:
    prompt = CATALOGUE.get(name)
    if prompt is None:
        raise ToolError(
            _("Unknown prompt '%(name)s'. Available: %(available)s.")
            % {"name": name, "available": ", ".join(sorted(CATALOGUE))}
        )
    return prompt.render(arguments or {})


__all__ = ["CATALOGUE", "Prompt", "get_prompt", "list_prompts"]
