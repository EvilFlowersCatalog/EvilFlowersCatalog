import ast
import importlib
import inspect
import logging
import re
import textwrap
import types
from http import HTTPStatus
from typing import TypedDict, Type, Dict, List, Optional, Any, Tuple, Callable

from django.conf import settings
from django.forms import CharField, IntegerField, DateTimeField, ModelChoiceField, ChoiceField, DurationField
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
from django_filters import UUIDFilter, DateTimeFilter, CharFilter, BooleanFilter, NumberFilter, ChoiceFilter, FilterSet
from pydantic import Field, BaseModel

from apps.api.response import PaginationModel
from apps.api.serializers import Serializer
from apps.openapi.analyzers import MethodAnalyzer
from apps.openapi.types import (
    ParameterLocation,
    InstanceDetails,
    OPENAPI_TYPES,
    ExtractionResult,
    COMMON_SCHEMAS,
    HTTP_STATUS_DESCRIPTIONS,
    CONTENT_TYPE_MAPPINGS,
)


class OpenApiInfo(TypedDict):
    title: str
    description: str
    version: str
    contact: Dict[str, str]
    license: Dict[str, str]


class OpenApiBaseModel(BaseModel):
    class Config:
        arbitrary_types_allowed = True
        use_enum_values = True

    @staticmethod
    def _load_class(class_path: str) -> Type:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls


class OpenApiParameter(OpenApiBaseModel):
    name: str
    location: ParameterLocation = Field(serialization_alias="in")
    description: Optional[str]
    required: bool = Field(default=True)
    schema_type: Dict = Field(serialization_alias="schema")


class OpenApiParameters:
    def __init__(self):
        self._params: Dict[ParameterLocation, Dict[str, OpenApiParameter]] = {}

    def upsert(
        self,
        name: str,
        location: ParameterLocation,
        description: Optional[str] = "",
        schema: Optional[Dict] = None,
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

    def list(self) -> List:
        result = []
        for parameters in self._params.values():
            for parameter in parameters.values():
                result.append(parameter)
        return result


class OpenApiDocument(OpenApiBaseModel):
    openapi: str = Field(default="3.0.0")
    info: OpenApiInfo = Field(
        default=(
            OpenApiInfo(
                title="EvilFlowers Catalog",
                version=settings.VERSION,
                description="A e-book catalog server compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2), "
                "written in Python with a straightforward management REST API for CRUD operations.",
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
    paths: Dict[str, "OpenApiPathItem"] = Field(default_factory=dict)
    components: Dict[str, Dict] = Field(default_factory=lambda: {"schemas": {}, "securitySchemes": {}})
    security: List[Dict[str, List]] = Field(default_factory=list)

    _serializers: Dict[str, Type] = {}
    _forms: Dict[str, Type] = {}
    _filters: Dict[str, Type] = {}

    def __init__(self, **data: Any):
        super().__init__(**data)

    def _convert_django_path(self, django_path: str) -> Tuple[str, Dict[str, Dict[str, str]]]:
        pattern = re.compile(r"<(?P<type>[^:]+):(?P<name>[^>]+)>")
        matches = pattern.findall(django_path)
        templated_path = django_path
        type_mapping: Dict[str, Dict[str, str]] = {}

        for var_type, var_name in matches:
            templated_path = templated_path.replace(f"<{var_type}:{var_name}>", f"{{{var_name}}}")
            type_mapping[var_name] = OPENAPI_TYPES[var_type]

        return templated_path, type_mapping

    @staticmethod
    def _get_methods(cls: Type) -> List[Tuple[str, types.FunctionType]]:
        methods: List[Tuple[str, types.FunctionType]] = []
        for name, obj in cls.__dict__.items():
            if isinstance(obj, types.FunctionType):
                methods.append((name, obj))
        return methods

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

    def add_form(self, name: str, form: Type[Form]) -> None:
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
                        model_field = field.choices.queryset.model._meta.get_field(field.to_field_name or "id")
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
                    self.components["schemas"][name]["properties"][field_name]["enum"] = field.enum.values
                case DictionaryField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                case ImageField() | FileField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["format"] = "uri"
                case ChoiceField():
                    self.components["schemas"][name]["properties"][field_name]["type"] = "string"
                    self.components["schemas"][name]["properties"][field_name]["enum"] = [
                        item[0] for item in field.choices
                    ]
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

    def add_serializer(self, name: str, serializer: Type[Serializer]) -> None:
        self._serializers[name] = serializer

        schema_name: str = ".".join((serializer.__module__, serializer.__qualname__)).replace(".", "__")
        if schema_name in self.components["schemas"]:
            return

        schema: Dict[str, Any] = serializer.model_json_schema(ref_template="#/components/schemas/{model}")

        if "$defs" in schema:
            for key in schema["$defs"].keys():
                if "__" not in key and key not in self.components["schemas"]:
                    self.components["schemas"][key] = schema["$defs"][key]
            del schema["$defs"]

        self.components["schemas"][schema_name] = schema

    def add_filter(self, name: str, django_filter: Type[FilterSet]):
        self._filters[name] = django_filter

    def add_path(self, django_path: str, class_path: str) -> None:
        templated_path, type_mapping = self._convert_django_path(django_path)
        parameters = OpenApiParameters()

        for parameter, parameter_type in type_mapping.items():
            parameters.upsert(name=parameter, location=ParameterLocation.PATH, schema=parameter_type, required=True)

        cls = self._load_class(class_path)
        methods = self._get_methods(cls)
        http_method_names: List[str] = getattr(cls, "http_method_names", [])

        if hasattr(cls, "_openapi_parameters"):
            for parameter in getattr(cls, "_openapi_parameters", []):
                parameters.upsert(**parameter)

        path_item = OpenApiPathItem(
            parameters=parameters.list(),
        )
        self.paths[templated_path] = path_item

        for name, method in methods:
            if name not in http_method_names:
                continue

            returns, raises, filters, form = self._extract_method_metadata(method)

            operation = OpenApiOperation(
                serializers=self._serializers,
                operation_id=method.__qualname__,
                description=getattr(method, "_openapi_description", ""),
                tags=getattr(method, "_openapi_tags", []),
                summary=getattr(method, "_openapi_summary", None),
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


class OpenApiOperation(OpenApiBaseModel):
    parameters: List[OpenApiParameter] = Field(default_factory=list)
    responses: Dict[int, Dict[str, Any]] = Field(default_factory=dict)
    request_body: Optional[Dict] = Field(serialization_alias="requestBody", default=None)
    operation_id: str = Field(serialization_alias="operationId")
    summary: Optional[str]
    description: Optional[str]
    tags: List[str] = Field(default_factory=list)
    _serializers: Dict[str, Type] = {}

    def __init__(self, serializers: Dict[str, Type], **data: Any):
        super().__init__(**data)
        self._serializers = serializers

    @staticmethod
    def _parse_http_status(value: str) -> HTTPStatus:
        member_name = value.split(".")[1]
        return getattr(HTTPStatus, member_name)

    @staticmethod
    def _parse_object_name(input_string: str) -> Optional[str]:
        # Regular expression to match the __qualname__ of an object
        pattern = r"(\b\w+\.\w+\b)"
        match = re.search(pattern, input_string)
        if match:
            return match.group(1)
        return None

    def add_filter(self, filterset: Type[FilterSet]):
        for key, definition in filterset.base_filters.items():
            # Use help_text if available, otherwise fall back to label with lookup expression
            description = (
                getattr(definition, "help_text", None)
                or f"{definition.label} ({', '.join(definition.lookup_expr.split('__'))})"
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
                case ChoiceFilter():
                    parameter["schema_type"] = {
                        "type": "string",
                        "enum": [item[0] for item in definition.extra.get("choices", [])],
                    }
                case _ as constructor:
                    logging.warning(f"No add_filter option for {constructor}")
                    return

            self.parameters.append(OpenApiParameter(**parameter))

    def add_request(self, form: Type[Form]):
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

                schema = {
                    "type": "object",
                    "properties": {
                        "metadata": {
                            "$ref": "#/components/schemas/"
                            + ".".join(
                                (
                                    PaginationModel.__module__,
                                    PaginationModel.__qualname__,
                                )
                            ).replace(".", "__"),
                        },
                        "items": {"type": "array", "items": {"type": "object"}},
                    },
                }

                if "serializer" in details["kwargs"]:
                    serializer = self._parse_object_name(details["kwargs"].get("serializer"))

                    if serializer in self._serializers:
                        schema["properties"]["items"] = {
                            "type": "array",
                            "items": {
                                "$ref": "#/components/schemas/"
                                + ".".join(
                                    (
                                        self._serializers[serializer].__module__,
                                        self._serializers[serializer].__qualname__,
                                    )
                                ).replace(".", "__")
                            },
                        }

                self.responses[status_code] = {"content": {"application/json": {"schema": schema}}, "description": ""}

            case "SingleResponse":
                status_code: HTTPStatus = HTTPStatus.OK

                if "data" in details["kwargs"] and details["kwargs"]["data"] is not None:
                    serializer = self._parse_object_name(details["kwargs"].get("data"))
                    if serializer in self._serializers:
                        schema = {
                            "type": "object",
                            "properties": {
                                "response": {
                                    "$ref": "#/components/schemas/"
                                    + ".".join(
                                        (
                                            self._serializers[serializer].__module__,
                                            self._serializers[serializer].__qualname__,
                                        )
                                    ).replace(".", "__")
                                }
                            },
                        }
                    else:
                        schema = {"type": "object"}
                else:
                    status_code = HTTPStatus.NO_CONTENT
                    schema = {"type": "object"}

                if "status" in details["kwargs"]:
                    status_code = self._parse_http_status(details["kwargs"]["status"])

                self.responses[status_code] = {"content": {"application/json": {"schema": schema}}, "description": ""}

            case "SeeOtherResponse":
                self.responses[HTTPStatus.SEE_OTHER] = {
                    "description": "See Other",
                    "headers": {
                        "Location": {
                            "description": "The URL to which the client should be redirected",
                            "schema": {"type": "string", "format": "url"},
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
    parameters: List[OpenApiParameter] = Field(default_factory=list)
    get: Optional[OpenApiOperation] = Field(default=None)
    post: Optional[OpenApiOperation] = Field(default=None)
    put: Optional[OpenApiOperation] = Field(default=None)
    delete: Optional[OpenApiOperation] = Field(default=None)
    patch: Optional[OpenApiOperation] = Field(default=None)

    def add_operation(self, name: str, operation: OpenApiOperation) -> None:
        if name.lower() in ["get", "post", "put", "delete", "patch"]:
            setattr(self, name.lower(), operation)
        else:
            raise ValueError(f"Unsupported HTTP method: {name}")
