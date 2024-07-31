import inspect
import logging
import re
import textwrap
import types
from http import HTTPStatus
from typing import List, Dict, Any, Type, Tuple, Callable, Optional

import django_filters
from django.conf import settings

import ast

from django_api_forms import Form
from pydantic import Field

from apps.api.response import PaginationModel
from apps.api.serializers import Serializer
from apps.openapi.analyzers import MethodAnalyzer
from apps.openapi.types import (
    OPENAPI_TYPES,
    ExtractionResult,
    InstanceDetails,
    ParameterLocation,
    OpenApiInfo,
    OpenApiParameter,
    OpenApiBaseModel,
)


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


class OpenApiSpecification(OpenApiBaseModel):
    openapi: str = Field(default="3.0.0")
    info: OpenApiInfo = Field(
        default=(
            OpenApiInfo(
                title="EvilFlowers Catalog",
                version=settings.VERSION,
                description=f"Instance name: {settings.INSTANCE_NAME}",
            )
        )
    )
    paths: Dict[str, "OpenApiPathItem"] = Field(default_factory=dict)
    components: Dict[str, Dict] = Field(default_factory=lambda: {"schemas": {}})

    def __init__(self, **data: Any):
        super().__init__(**data)

        self._serializers: Dict[str, Type] = self._find_all_subclasses(Serializer)
        for serializer in self._serializers.values():
            self.add_serializer(serializer)

        self._forms: Dict[str, Type] = self._find_all_subclasses(Form)
        for name, form in self._forms.items():
            self.add_form(name, form)

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
        analyzer = MethodAnalyzer(self._find_all_subclasses(django_filters.FilterSet))
        analyzer.visit(tree)
        return analyzer.get_results()

    def add_form(self, name: str, form: Type[Form]) -> None:
        # Implement form addition logic if needed
        pass

    def add_serializer(self, serializer: Type[Serializer]) -> None:
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

        path_item = OpenApiPathItem(parameters=parameters.list())
        self.paths[templated_path] = path_item

        for name, method in methods:
            if name not in http_method_names:
                continue

            returns, raises, filters = self._extract_method_metadata(method)

            operation = OpenApiOperation(
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


class OpenApiOperation(OpenApiBaseModel):
    parameters: List[Dict[str, Any]] = Field(default_factory=list)
    responses: Dict[int, Dict[str, Any]] = Field(default_factory=dict)
    operation_id: str = Field(serialization_alias="operationId")
    summary: Optional[str]
    description: Optional[str]
    tags: List[str] = Field(default_factory=list)

    def __init__(self, **data: Any):
        super().__init__(**data)
        # FIXME: Optimize me! I am in terrible pain. Same shit is happening in OpenApiSpecification...
        self._serializers: Dict[str, Type] = self._find_all_subclasses(Serializer)

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

    def add_instance_details(self, details: InstanceDetails) -> None:
        match details["constructor"]:
            case "ValidationException":
                self.responses[422] = {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/apps__core__errors__ValidationError"}
                        }
                    }
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

                self.responses[status_code] = {
                    "content": {"application/json": {"schema": schema}},
                }

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

                self.responses[status_code] = {
                    "content": {"application/json": {"schema": schema}},
                }
            case _ as constructor:
                logging.warning(f"No ResponseFactory option for {constructor}")


class OpenApiPathItem(OpenApiBaseModel):
    parameters: List[OpenApiParameter] = Field(default_factory=list)
    get: Optional[OpenApiOperation] = None
    post: Optional[OpenApiOperation] = None
    put: Optional[OpenApiOperation] = None
    delete: Optional[OpenApiOperation] = None
    patch: Optional[OpenApiOperation] = None

    def add_operation(self, name: str, operation: OpenApiOperation) -> None:
        if name.lower() in ["get", "post", "put", "delete", "patch"]:
            setattr(self, name.lower(), operation)
        else:
            raise ValueError(f"Unsupported HTTP method: {name}")
