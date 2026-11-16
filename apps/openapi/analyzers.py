import ast

from django_api_forms import Form
from django_filters import FilterSet

from apps.openapi.types import ExtractionResult, InstanceDetails


class MethodAnalyzer(ast.NodeVisitor):
    """Reads a view method's source: what it returns and raises, which filter set and form it instantiates."""

    def __init__(self, filter_classes: dict[str, type[FilterSet]], form_classes: dict[str, type[Form]]) -> None:
        self._filter_classes = filter_classes
        self._form_classes = form_classes
        self.returns: list[InstanceDetails] = []
        self.raises: list[InstanceDetails] = []
        self.filters: list[type[FilterSet]] = []
        self.form: type[Form] | None = None

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(self._parse_call(node.value))
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self.raises.append(self._parse_call(node.exc))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        match node.func:
            case ast.Name(id=name):
                class_name: str | None = name
            case ast.Attribute():
                class_name = ast.unparse(node.func)
            case _:
                class_name = None

        if class_name in self._filter_classes:
            self.filters.append(self._filter_classes[class_name])

        if class_name and (form := self._form_classes.get(class_name.split(".")[0])):
            self.form = form

        self.generic_visit(node)

    @staticmethod
    def _parse_call(node: ast.expr | None) -> InstanceDetails:
        if not isinstance(node, ast.Call):
            return InstanceDetails(constructor=None, args=[], kwargs={})
        return InstanceDetails(
            constructor=ast.unparse(node.func),
            args=[ast.unparse(arg) for arg in node.args],
            # `**extra` has no keyword name and says nothing a reader could use.
            kwargs={kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg is not None},
        )

    def get_results(self) -> ExtractionResult:
        return ExtractionResult(self.returns, self.raises, self.filters, self.form)
