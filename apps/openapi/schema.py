import ast
import importlib
import inspect
import logging
import re
import textwrap
import types
from http import HTTPStatus
from typing import TypedDict, Any
from collections.abc import Callable

from django.conf import settings
from django.forms import CharField, IntegerField, DateTimeField, ModelChoiceField, ChoiceField, DurationField
from django.forms import BooleanField as DjangoBooleanField
from django.utils.choices import flatten_choices, normalize_choices
from django_api_forms import (
    Form,
    BooleanField,
    EnumField,
    DictionaryField,
    ImageField,
    FileField,
    FormFieldList,
    FormField,
)
from django_api_forms.fields import AnyField, FieldList
from django_filters import (
    UUIDFilter,
    DateTimeFilter,
    CharFilter,
    BooleanFilter,
    NumberFilter,
    ChoiceFilter,
    MultipleChoiceFilter,
    FilterSet,
)
from pydantic import Field, BaseModel, ConfigDict, PrivateAttr

from apps.api.response import PaginationModel
from apps.core.fields.localized import LocalizedTextFormField
from apps.api.serializers import Serializer, schema_name
from apps.core.views import SecuredView
from apps.openapi import sections
from apps.openapi.analyzers import MethodAnalyzer
from apps.openapi.types import (
    ParameterLocation,
    InstanceDetails,
    OPENAPI_TYPES,
    ExtractionResult,
    HTTP_STATUS_DESCRIPTIONS,
    CONTENT_TYPE_MAPPINGS,
)

#: The handlers a path item can describe; `head`/`options` are Django's own.
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

#: `<uuid:entry_id>` and `<name>` (an untyped converter is a string).
DJANGO_CONVERTER = re.compile(r"<(?:(?P<type>[^:>]+):)?(?P<name>\w+)>")
#: `(?P<lcp_content_id>[a-f0-9-]+)` in a `re_path`; one level of nested groups is enough for our routes.
REGEX_GROUP = re.compile(r"\(\?P<(?P<name>\w+)>(?P<regex>(?:[^()]|\([^()]*\))*)\)")

API_DESCRIPTION = """
EvilFlowers Catalog is the server behind [ELVIRA](https://elvira.digital), an open-source digital library for
research and academia. It speaks [OPDS 1.2](https://specs.opds.io/opds-1.2) and OPDS 2.0 to reading apps, lends
DRM-protected titles through Readium LCP, and offers a REST API to manage everything in between.

## Surfaces

| Prefix | Audience | What it is |
|---|---|---|
| `/api/v1` | Portals, scripts, administrators | The management REST API (JSON) |
| `/opds/v1.2`, `/opds/v2` | Reading apps | OPDS catalog feeds |
| `/readium/v1` | Reading apps, the LCP server | Licenses, reservations, encrypted content |
| `/data/v1` | Everyone with access | File downloads: publications, covers, region images |
| `/assistant/v1` | Portals | The library assistant |
| `/extraction/v1` | Extraction workers | Run callbacks |
| `/oai`, `/resolve`, `/availability` | Aggregators, union catalogs | OAI-PMH and lookups |
| `/mcp/v1` | AI clients | The Model Context Protocol server |

## Authentication

Send `Authorization: Bearer <token>`. An access token comes from `POST /api/v1/token` and is renewed with
`POST /api/v1/token/refresh`; an API key (`POST /api/v1/api_keys`) is a long-lived token used the same way.
Reading apps that cannot do either use HTTP Basic with the user's credentials. What a caller sees is always
scoped to the catalogs they were granted.

## Errors

Errors are [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) problem details (`application/json`, with
`title` and `detail`). A request body that fails validation answers `422` with the offending fields.

## Pagination

Collections take `page` (from 1) and `limit` and answer `{"items": [...], "metadata": {"page", "limit",
"pages", "total"}}`.
""".strip()


class OpenApiInfo(TypedDict):
    title: str
    description: str
    version: str
    contact: dict[str, str]
    license: dict[str, str]


def _first_paragraph(doc: str | None) -> tuple[str | None, str]:
    """Split a docstring into its first line (a summary) and the rest (a description)."""
    if not doc:
        return None, ""
    lines = inspect.cleandoc(doc).splitlines()
    summary = lines[0].strip().rstrip(".") or None
    return summary, "\n".join(lines[1:]).strip()


def _humanize(cls_name: str, method: str, templated_path: str) -> str:
    """`GET /readium/v1/hint` -> `Get hint`: a sidebar label better than `HintPageView.get`."""
    words = [part for part in templated_path.strip("/").split("/") if part and not part.startswith("{")]
    if not words or re.fullmatch(r"v\d+(\.\d+)?", words[-1]):
        return f"{'Get' if method == 'get' else method.upper()} {cls_name.removesuffix('View')}"
    noun = words[-1].replace("-", " ").replace("_", " ").replace(".json", "").replace(".xml", "")
    verb = {"get": "Get", "post": "Submit", "put": "Replace", "patch": "Update", "delete": "Delete"}[method]
    return f"{verb} {noun}"


class OpenApiBaseModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, use_enum_values=True)

    @staticmethod
    def _load_class(class_path: str) -> type:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls


class OpenApiParameter(OpenApiBaseModel):
    name: str
    location: ParameterLocation = Field(serialization_alias="in")
    description: str | None
    required: bool = Field(default=True)
    schema_type: dict[str, Any] | None = Field(serialization_alias="schema", default=None)


class OpenApiParameters:
    def __init__(self):
        self._params: dict[ParameterLocation, dict[str, OpenApiParameter]] = {}

    def upsert(
        self,
        name: str,
        location: ParameterLocation,
        description: str | None = "",
        schema: dict | None = None,
        required: bool = True,
    ):
        if location not in self._params:
            self._params[location] = {}

        if name in self._params[location]:
            self._params[location][name].description = self._params[location][name].description or description
            self._params[location][name].schema_type = self._params[location][name].schema_type or schema
            self._params[location][name].required = required
        else:
            self._params[location][name] = OpenApiParameter(
                name=name, location=location, description=description, schema_type=schema, required=required
            )

    def list(self) -> list:
        result = []
        for parameters in self._params.values():
            for parameter in parameters.values():
                result.append(parameter)
        return result


class OpenApiDocument(OpenApiBaseModel):
    # 3.1: the schema dialect is JSON Schema 2020-12, which is what pydantic writes, so serializer schemas are
    # published as they are (no `nullable` rewriting) and mean the same in evilflowers-protocol-python.
    openapi: str = Field(default="3.1.0")
    json_schema_dialect: str = Field(
        serialization_alias="jsonSchemaDialect", default="https://spec.openapis.org/oas/3.1/dialect/base"
    )
    info: OpenApiInfo = Field(
        default=(
            OpenApiInfo(
                title="EvilFlowers Catalog",
                version=settings.VERSION,
                description=API_DESCRIPTION,
                license={
                    "url": "https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/blob/master/LICENSE",
                    "name": "MIT",
                },
                contact={
                    "name": "EvilFlowersCatalog",
                    "url": "https://github.com/EvilFlowersCatalog/EvilFlowersCatalog",
                    "email": "jakub.dubec@stuba.sk",
                },
            )
        )
    )
    external_docs: dict[str, str] = Field(
        serialization_alias="externalDocs",
        default_factory=lambda: {"url": "https://elvira.digital/docs/intro", "description": "ELVIRA documentation"},
    )
    servers: list[dict[str, Any]] = Field(
        default_factory=lambda: [
            {
                "url": "{baseUrl}",
                "description": "A catalog instance; every path below is relative to its base URL",
                "variables": {
                    "baseUrl": {
                        "default": settings.EVILFLOWERS_BASE_URL.rstrip("/"),
                        "description": "The instance's public base URL (`EVILFLOWERS_BASE_URL`)",
                    }
                },
            }
        ]
    )
    tags: list[dict[str, Any]] = Field(default_factory=list)
    tag_groups: list[dict[str, Any]] = Field(serialization_alias="x-tagGroups", default_factory=list)
    paths: dict[str, "OpenApiPathItem"] = Field(default_factory=dict)
    components: dict[str, dict] = Field(default_factory=lambda: {"schemas": {}, "securitySchemes": {}})
    security: list[dict[str, list]] = Field(default_factory=list)

    _serializers: dict[str, type] = {}
    _forms: dict[str, type] = {}
    _filters: dict[str, type] = {}
    _operation_ids: set = PrivateAttr(default_factory=set)
    _mounts: dict[str, str] = PrivateAttr(default_factory=dict)

    def __init__(self, **data: Any):
        super().__init__(**data)

    def _convert_django_path(self, django_path: str) -> tuple[str, dict[str, dict[str, str]]]:
        type_mapping: dict[str, dict[str, str]] = {}

        def converter(match: re.Match) -> str:
            type_mapping[match["name"]] = OPENAPI_TYPES.get(match["type"] or "str", OPENAPI_TYPES["str"])
            return f"{{{match['name']}}}"

        def regex_group(match: re.Match) -> str:
            type_mapping[match["name"]] = {"type": "string", "pattern": f"^{match['regex']}$"}
            return f"{{{match['name']}}}"

        # A `re_path` route: named groups become parameters, anchors and escapes go.
        templated_path = REGEX_GROUP.sub(regex_group, django_path)
        templated_path = templated_path.replace("^", "").replace("\\Z", "").rstrip("$").replace("\\", "")
        templated_path = DJANGO_CONVERTER.sub(converter, templated_path)

        return templated_path, type_mapping

    @staticmethod
    def _get_methods(view: type) -> list[tuple[str, types.FunctionType]]:
        """The HTTP handlers of a view, inherited ones included (`LatestFeedView` gets `get` from its base)."""
        methods: dict[str, types.FunctionType] = {}
        for klass in view.__mro__:
            if klass is object or klass.__module__.startswith("django."):
                continue
            for name, obj in klass.__dict__.items():
                if name in HTTP_METHODS and isinstance(obj, types.FunctionType) and name not in methods:
                    methods[name] = obj
        return [(name, methods[name]) for name in HTTP_METHODS if name in methods]

    def _unique_operation_id(self, operation_id: str) -> str:
        candidate, n = operation_id, 2
        while candidate in self._operation_ids:
            candidate, n = f"{operation_id}_{n}", n + 1
        self._operation_ids.add(candidate)
        return candidate

    def finalize(self) -> None:
        """Declare every used tag, in the order and groups of `sections.GROUPS`.

        A tag no group lists is still declared, in an `Other` group, so its operations never vanish from the
        sidebar; the tests keep that group empty.
        """
        # Insertion-ordered set of the tags operations use.
        used: dict[str, None] = {}
        for item in self.paths.values():
            for operation in item.operations():
                used.update(dict.fromkeys(operation.tags))

        declared = sections.all_tags()
        groups: list[tuple[str, list[str]]] = [
            (group.name, [tag.name for tag in group.tags if tag.name in used]) for group in sections.GROUPS
        ]
        undeclared = [tag for tag in used if tag not in declared]
        if undeclared:
            logging.warning("OpenAPI tags missing from apps/openapi/sections.py: %s", ", ".join(undeclared))
            groups.append(("Other", undeclared))

        self.tags, self.tag_groups = [], []
        for group_name, names in groups:
            if not names:
                continue
            self.tag_groups.append({"name": group_name, "tags": names})
            for name in names:
                spec = declared.get(name)
                tag: dict[str, Any] = {"name": name}
                if spec:
                    tag["description"] = spec.description
                    if spec.external_docs:
                        tag["externalDocs"] = {"url": spec.external_docs[0], "description": spec.external_docs[1]}
                self.tags.append(tag)

    def _extract_method_metadata(self, method: Callable) -> ExtractionResult:
        source = inspect.getsource(method)
        dedented_source = textwrap.dedent(source)
        tree = ast.parse(dedented_source)
        analyzer = MethodAnalyzer(
            filter_classes=self._filters,
            form_classes=self._forms,
        )
        analyzer.visit(tree)
        return analyzer.get_results()

    def add_security(self, name: str, definition: dict):
        self.components["securitySchemes"][name] = definition
        self.security.append({name: []})

    def add_form(self, name: str, form: type[Form]) -> None:
        self._forms[name] = form

        self.components["schemas"][name] = {"type": "object", "title": name, "properties": {}}
        for field_name, field in form.base_fields.items():
            self.components["schemas"][name]["properties"][field_name] = {
                "title": field_name,
                "description": field.help_text,
            }

            match field:
                case CharField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    if field.min_length:
                        self.components["schemas"][name]["properties"][field_name]["minLength"] = field.min_length
                    if field.max_length:
                        self.components["schemas"][name]["properties"][field_name]["maxLength"] = field.max_length
                case BooleanField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "boolean"
                case IntegerField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "integer"
                    if field.min_value:
                        self.components["schemas"][name]["properties"][field_name]["minimum"] = field.min_value
                    if field.max_value:
                        self.components["schemas"][name]["properties"][field_name]["maximum"] = field.max_value
                    if field.step_size:
                        self.components["schemas"][name]["properties"][field_name]["multipleOf"] = field.step_size
                case DateTimeField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["format"] = "date-time"
                case ModelChoiceField():
                    # Improved field type detection based on the model field type
                    try:
                        if field.queryset is None:
                            raise ValueError("ModelChoiceField without a queryset")
                        model_field = field.queryset.model._meta.get_field(field.to_field_name or "id")
                        field_type_name = model_field.get_internal_type()

                        # Map Django field types to OpenAPI types
                        field_type_mapping = {
                            "AutoField": {"type": "integer"},
                            "BigAutoField": {"type": "integer", "format": "int64"},
                            "CharField": {"type": "string"},
                            "TextField": {"type": "string"},
                            "SlugField": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
                            "EmailField": {"type": "string", "format": "email"},
                            "URLField": {"type": "string", "format": "uri"},
                            "UUIDField": {"type": "string", "format": "uuid"},
                            "IntegerField": {"type": "integer"},
                            "BigIntegerField": {"type": "integer", "format": "int64"},
                            "FloatField": {"type": "number", "format": "float"},
                            "DecimalField": {"type": "string", "format": "decimal"},
                            "BooleanField": {"type": "boolean"},
                            "DateTimeField": {"type": "string", "format": "date-time"},
                            "DateField": {"type": "string", "format": "date"},
                            "TimeField": {"type": "string", "format": "time"},
                        }

                        field_schema = field_type_mapping.get(field_type_name, {"type": "string"})
                        self.components["schemas"][name]["properties"][field_name].update(field_schema)

                        # Add description based on the model field
                        if hasattr(model_field, "help_text") and model_field.help_text:
                            self.components["schemas"][name]["properties"][field_name][
                                "description"
                            ] = model_field.help_text

                    except Exception as e:
                        logging.warning(f"Could not determine field type for {field_name}: {e}")
                        self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                case EnumField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["enum"] = [
                        member.value for member in field.enum
                    ]  # `.values` exists only on Django `Choices`; iterating works for any `Enum`
                case LocalizedTextFormField():
                    self.components["schemas"][name]["properties"][field_name].update(
                        {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Translations keyed by ISO 639-1 language code",
                            "example": {"sk": "Informatika", "en": "Computer science"},
                        }
                    )
                case DictionaryField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                case ImageField() | FileField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["format"] = "uri"
                case ChoiceField():
                    # Grouped choices (`("Group", [(value, label), ...])`) contribute their members, not the group.
                    choices: list[tuple[Any, Any]] = list(flatten_choices(normalize_choices(field.choices)))
                    values = [value for value, _label in choices]
                    # `TypedChoiceField(coerce=int)`, e.g. a rating of 1 / -1: the enum is typed as its values are.
                    is_int = values and all(isinstance(v, int) and not isinstance(v, bool) for v in values)
                    self.components["schemas"][name]["properties"][field_name]["type"] = (
                        "integer" if is_int else "string"
                    )
                    self.components["schemas"][name]["properties"][field_name]["enum"] = values
                case DjangoBooleanField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "boolean"
                case AnyField():
                    pass  # Any JSON value: an empty schema says exactly that.
                case FieldList():
                    item_schema = OPENAPI_TYPES["str"]
                    if isinstance(field._field, IntegerField):
                        item_schema = OPENAPI_TYPES["int"]
                    self.components["schemas"][name]["properties"][field_name]["type"] = "array"
                    self.components["schemas"][name]["properties"][field_name]["items"] = dict(item_schema)
                case FormFieldList():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "array"
                    self.components["schemas"][name]["properties"][field_name]["items"] = {
                        "$ref": f"#/components/schemas/{field.form.__qualname__}"
                    }
                case FormField():
                    self.components["schemas"][name]["properties"][field_name][
                        "$ref"
                    ] = f"#/components/schemas/{field.form.__qualname__}"
                case DurationField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["example"] = "P14D"
                    self.components["schemas"][name]["properties"][field_name]["description"] = (
                        'Duration in ISO 8601 format. Example: "PT1H30M" for 1 hour and 30 minutes.\n'
                        + "The format is defined as follows:\n"
                        + '- "P" indicates the period.\n'
                        + '- "Y" for years, "M" for months, "D" for days.\n'
                        + '- "T" indicates the start of the time section.\n'
                        + '- "H" for hours, "M" for minutes, "S" for seconds.\n'
                        + "- Valid examples include:\n"
                        + '  - "PT30M" (30 minutes)\n'
                        + '  - "P1Y2M10DT5H30M" (1 year, 2 months, 10 days, 5 hours, and 30 minutes)'
                    )
                    self.components["schemas"][name]["properties"][field_name][
                        "pattern"
                    ] = "^P(?!P)(?:(\\d+Y)?(\\d+M)?(\\d+D)?(T(?:(\\d+H)?(\\d+M)?(\\d+S)?))?)?$"

                case _ as field_type:
                    logging.warning(f"No case for creating Form property for {field_type.__class__}")

            if field.required:
                if "required" not in self.components["schemas"][name]:
                    self.components["schemas"][name]["required"] = []
                self.components["schemas"][name]["required"].append(field_name)

    def add_serializer(self, name: str, serializer: type[Serializer]) -> None:
        self._serializers[name] = serializer

        name = serializer.protocol_name()
        if name in self.components["schemas"]:
            return

        # Serialization mode, nested definitions named as the protocol names them (`ProtocolJsonSchema`): every
        # `$ref` resolves to the model it means, and a nested serializer's definition is its top-level component.
        schema: dict[str, Any] = serializer.protocol_schema()
        for key, definition in schema.pop("$defs", {}).items():
            self.components["schemas"].setdefault(key, definition)
        self.components["schemas"][name] = schema

    def add_filter(self, name: str, django_filter: type[FilterSet]):
        self._filters[name] = django_filter

    def add_path(self, django_path: str, class_path: str) -> None:
        templated_path, type_mapping = self._convert_django_path(django_path)
        parameters = OpenApiParameters()

        for parameter, parameter_type in type_mapping.items():
            parameters.upsert(name=parameter, location=ParameterLocation.PATH, schema=parameter_type, required=True)

        cls = self._load_class(class_path)
        if not isinstance(cls, type):
            logging.warning("OpenAPI skips %s (%s): only class-based views are documented", templated_path, class_path)
            return
        methods = self._get_methods(cls)
        http_method_names: list[str] = getattr(cls, "http_method_names", [])
        rule = sections.rule_for(templated_path)
        # The same view mounted twice (an alias such as `/harvest/connectors`) documents both paths.
        alias_of = self._mounts.get(class_path)
        self._mounts.setdefault(class_path, templated_path)
        # Only a `SecuredView` that authenticates reads `Authorization`; anything else (webhooks with their own
        # secret, signed links, public documents) is published with `security: []`.
        authenticates = issubclass(cls, SecuredView) and getattr(cls, "authenticates", True)

        if hasattr(cls, "_openapi_parameters"):
            for parameter in getattr(cls, "_openapi_parameters", []):
                parameters.upsert(**parameter)

        if templated_path in self.paths:
            logging.warning("OpenAPI path %s is routed twice; the later route %s wins", templated_path, class_path)

        path_item = OpenApiPathItem(
            parameters=parameters.list(),
        )
        self.paths[templated_path] = path_item

        for name, method in methods:
            if name not in http_method_names:
                continue

            returns, raises, filters, form = self._extract_method_metadata(method)

            # A docstring's first line is a summary when it reads like one; otherwise the whole docstring describes.
            doc_summary, doc_description = _first_paragraph(method.__doc__ or cls.__doc__)
            if doc_summary and (len(doc_summary) > 80 or doc_summary.endswith(":") or doc_summary.startswith("`")):
                doc_summary, doc_description = None, inspect.cleandoc(method.__doc__ or cls.__doc__ or "")
            description = getattr(method, "_openapi_description", "") or doc_description
            # `LatestFeedView` serves `_TopEntriesFeedView.get`: name the operation after the routed view.
            qualname = method.__qualname__ if name in cls.__dict__ else f"{cls.__qualname__}.{name}"
            if alias_of:
                description = f"{description}\n\nAn alias of `{alias_of}`; both paths answer the same.".strip()

            operation_id = self._unique_operation_id(
                getattr(method, "_openapi_operation_id", None) or f"{rule.operation_prefix if rule else ''}{qualname}"
            )
            operation = OpenApiOperation(
                serializers=self._serializers,
                operation_id=operation_id,
                description=description,
                tags=getattr(method, "_openapi_tags", None) or ([rule.tag] if rule else []),
                summary=getattr(method, "_openapi_summary", None)
                or sections.SUMMARIES.get(operation_id)
                or doc_summary
                or _humanize(cls.__name__, name, templated_path),
                deprecated=getattr(method, "_openapi_deprecated", None),
                external_docs=getattr(method, "_openapi_external_docs", None),
                # `@openapi.security([])` wins, even an empty one; else public unless the view authenticates.
                security=vars(method).get("_openapi_security", None if authenticates else []),
            )
            path_item.add_operation(name, operation)

            for exception in raises:
                operation.add_instance_details(exception)

            for return_type in returns:
                operation.add_instance_details(return_type)

            for filter_type in filters:
                operation.add_filter(filter_type)

            if form:
                operation.add_request(form)

            operation.add_declared_responses(getattr(method, "_openapi", {}).get("responses", {}))
            operation.complete_responses(rule.media_type if rule else None, authenticates=authenticates)


class OpenApiOperation(OpenApiBaseModel):
    parameters: list[OpenApiParameter] = Field(default_factory=list)
    responses: dict[int, dict[str, Any]] = Field(default_factory=dict)
    request_body: dict | None = Field(serialization_alias="requestBody", default=None)
    operation_id: str = Field(serialization_alias="operationId")
    summary: str | None
    description: str | None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool | None = None
    external_docs: dict[str, str] | None = Field(serialization_alias="externalDocs", default=None)
    security: list[dict[str, list[str]]] | None = None
    _serializers: dict[str, type] = {}

    def __init__(self, serializers: dict[str, type], **data: Any):
        super().__init__(**data)
        self._serializers = serializers

    @staticmethod
    def _schema_ref(model: type) -> str:
        return f"#/components/schemas/{schema_name(model)}"

    def _serializer_in(self, source: str | None) -> type | None:
        """The serializer a `data=`/`serializer=` argument names (`EntrySerializer.Detailed.model_validate(e)`)."""
        if not source:
            return None
        return self._serializers.get(self._parse_object_name(source) or "")

    def add_declared_responses(self, declared: dict[HTTPStatus, dict[str, Any]]) -> None:
        """`@openapi.response(status, description, serializer)`: fills what the source analysis could not."""
        for status, declaration in declared.items():
            response = self.responses.setdefault(int(status), {"description": ""})
            response["description"] = response.get("description") or declaration["description"]
            serializer = declaration.get("serializer")
            if serializer is not None and "content" not in response:
                response["content"] = {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"response": {"$ref": self._schema_ref(serializer)}},
                        }
                    }
                }

    def complete_responses(self, media_type: str | None, authenticates: bool = False) -> None:
        """Every operation answers something, and every answer is described (both are required by OpenAPI).

        A view that authenticates (`SecuredView`) refuses credentials it cannot verify with 401, whatever else
        it documents.
        """
        if authenticates and 401 not in self.responses:
            self.responses[HTTPStatus.UNAUTHORIZED] = {
                "description": "The credentials sent are missing, expired or invalid",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/apps__core__errors__ProblemDetail"}}
                },
            }
        # No success documented, or only a redirect where the route's rule says a body comes too (a page that
        # renders or redirects, a download served locally or redirected): document the body.
        has_success = any(200 <= int(status) < 300 for status in self.responses)
        has_redirect = any(300 <= int(status) < 400 for status in self.responses)
        if not has_success and (not has_redirect or media_type):
            media_type = media_type or "application/json"
            schema = CONTENT_TYPE_MAPPINGS.get(media_type, {"schema": {"type": "string"}})["schema"]
            self.responses[HTTPStatus.OK] = {
                "description": HTTP_STATUS_DESCRIPTIONS[200],
                "content": {media_type: {"schema": dict(schema)}},
            }
        for status, response in self.responses.items():
            # `HttpResponse(feed, content_type=…)` is seen as a 200 without a body; the route's rule names it.
            if media_type and int(status) == 200 and "content" not in response:
                schema = CONTENT_TYPE_MAPPINGS.get(media_type, {"schema": {"type": "string"}})["schema"]
                response["content"] = {media_type: {"schema": dict(schema)}}
            if not response.get("description"):
                response["description"] = HTTP_STATUS_DESCRIPTIONS.get(int(status), HTTPStatus(int(status)).phrase)

    @staticmethod
    def _status_code(value: str) -> HTTPStatus | None:
        """`HTTPStatus.GONE`, `410` or `status.GONE` as written in the source; None for a variable."""
        value = value.strip()
        if value.isdigit():
            return HTTPStatus(int(value))
        member = value.rsplit(".", 1)[-1]
        return getattr(HTTPStatus, member, None) if "." in value else None

    @staticmethod
    def _parse_http_status(value: str) -> HTTPStatus:
        member_name = value.split(".")[1]
        return getattr(HTTPStatus, member_name)

    @staticmethod
    def _parse_object_name(input_string: str) -> str | None:
        # Regular expression to match the __qualname__ of an object
        pattern = r"(\b\w+\.\w+\b)"
        match = re.search(pattern, input_string)
        if match:
            return match.group(1)
        return None

    def add_filter(self, filterset: type[FilterSet]):
        for key, definition in filterset.base_filters.items():
            # Use help_text if available, otherwise fall back to field name with lookup expression
            description = (
                getattr(definition, "help_text", None)
                or f"Filter by {key} ({', '.join(definition.lookup_expr.split('__'))})"
            )

            parameter = {
                "name": key,
                "location": ParameterLocation.QUERY,
                "description": description,
                "required": definition.extra.get("required", False),
            }

            # match type(definition).__qualname__:
            match definition:
                case UUIDFilter():
                    parameter["schema_type"] = OPENAPI_TYPES["uuid"]
                case DateTimeFilter():
                    parameter["schema_type"] = OPENAPI_TYPES["datetime"]
                case CharFilter():
                    parameter["schema_type"] = OPENAPI_TYPES["str"]
                case BooleanFilter():
                    parameter["schema_type"] = OPENAPI_TYPES["bool"]
                case NumberFilter():
                    parameter["schema_type"] = OPENAPI_TYPES["int"]
                case MultipleChoiceFilter():
                    # `?status=a&status=b`: OpenAPI's default `form` style with `explode` says exactly that.
                    parameter["schema_type"] = {
                        "type": "array",
                        "items": {"type": "string", "enum": [item[0] for item in definition.extra.get("choices", [])]},
                    }
                case ChoiceFilter():
                    parameter["schema_type"] = {
                        "type": "string",
                        "enum": [item[0] for item in definition.extra.get("choices", [])],
                    }
                case _ as constructor:
                    logging.warning(f"No add_filter option for {constructor}")
                    return

            self.parameters.append(OpenApiParameter(**parameter))

    def add_request(self, form: type[Form]):
        self.request_body = {
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{form.__qualname__}"}}}
        }

    def add_instance_details(self, details: InstanceDetails) -> None:
        match details["constructor"]:
            case "ValidationException":
                self.responses[422] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ValidationError"}
                        }
                    },
                    "description": "Validation error",
                }
            case "ProblemDetailException":
                self.responses[self._parse_http_status(details["kwargs"]["status"])] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ProblemDetail"}
                        }
                    },
                    "description": "".join(details["args"]),
                }
            case "UnauthorizedException":
                self.responses[401] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ProblemDetail"}
                        }
                    },
                    "description": "".join(details["args"]),
                }
            case "AuthorizationException":
                self.responses[401] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ProblemDetail"}
                        }
                    },
                    "description": "".join(details["args"]),
                }
                self.responses[403] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ProblemDetail"}
                        }
                    },
                    "description": "".join(details["args"]),
                }
            case "PaginationResponse":
                status_code: HTTPStatus = HTTPStatus.OK

                if "status" in details["kwargs"]:
                    status_code = self._parse_http_status(details["kwargs"]["status"])

                item = self._serializer_in(details["kwargs"].get("serializer"))
                schema: dict[str, Any] = {
                    "type": "object",
                    "properties": {
                        "metadata": {"$ref": self._schema_ref(PaginationModel)},
                        "items": {"type": "array", "items": {"$ref": self._schema_ref(item)} if item else {}},
                    },
                }

                self.responses[status_code] = {"content": {"application/json": {"schema": schema}}, "description": ""}

            case "SingleResponse":
                status_code = HTTPStatus.OK

                # `SingleResponse(request)` / `data=None` answers 204 without a body; anything else `{"response": …}`.
                source = details["kwargs"].get("data", "None")
                if "status" in details["kwargs"]:
                    status_code = self._parse_http_status(details["kwargs"]["status"])
                elif source == "None":
                    status_code = HTTPStatus.NO_CONTENT

                if source == "None":
                    self.responses[status_code] = {"description": ""}
                    return

                schema = {"type": "object"}
                if data := self._serializer_in(source):
                    schema["properties"] = {"response": {"$ref": self._schema_ref(data)}}
                    schema["required"] = ["response"]
                self.responses[status_code] = {"content": {"application/json": {"schema": schema}}, "description": ""}

            case "SeeOtherResponse":
                self.responses[HTTPStatus.SEE_OTHER] = {
                    "description": "See Other",
                    "headers": {
                        "Location": {
                            "description": "The URL to which the client should be redirected",
                            "schema": {"type": "string", "format": "uri"},
                        }
                    },
                }

            case "JsonResponse" | "HttpResponse" | "StreamingHttpResponse":
                # `JsonResponse({...}, status=401)`: the status when the source spells it, 200 otherwise.
                status = details["kwargs"].get("status")
                code = self._status_code(status) if status else HTTPStatus.OK
                if code is None:
                    return
                response = self.responses.setdefault(code, {"description": ""})
                if details["constructor"] == "JsonResponse" and "content" not in response:
                    response["content"] = {"application/json": {"schema": {"type": "object"}}}

            case "Http404":
                self.responses.setdefault(
                    HTTPStatus.NOT_FOUND,
                    {"description": "".join(details["args"]) or HTTP_STATUS_DESCRIPTIONS[404]},
                )

            case "HttpResponseRedirect" | "redirect":
                self.responses[HTTPStatus.FOUND] = {
                    "description": "Found: the answer lives at `Location`",
                    "headers": {
                        "Location": {
                            "description": "Where to go instead",
                            "schema": {"type": "string", "format": "uri"},
                        }
                    },
                }

            case "FileResponse":
                # TODO: Find a way how to describe content type (decorator?)
                self.responses[HTTPStatus.OK] = {
                    "description": "File response",
                    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
                }

            case _ as constructor:
                logging.warning(f"No ResponseFactory option for {constructor}")


class OpenApiPathItem(OpenApiBaseModel):
    parameters: list[OpenApiParameter] = Field(default_factory=list)
    get: OpenApiOperation | None = Field(default=None)
    post: OpenApiOperation | None = Field(default=None)
    put: OpenApiOperation | None = Field(default=None)
    delete: OpenApiOperation | None = Field(default=None)
    patch: OpenApiOperation | None = Field(default=None)

    def add_operation(self, name: str, operation: OpenApiOperation) -> None:
        if name.lower() not in HTTP_METHODS:
            raise ValueError(f"Unsupported HTTP method: {name}")
        setattr(self, name.lower(), operation)

    def operations(self) -> list[OpenApiOperation]:
        return [operation for name in HTTP_METHODS if (operation := getattr(self, name)) is not None]
