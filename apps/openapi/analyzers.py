import ast
from typing import List, Optional

from apps.openapi.types import InstanceDetails, ExtractionResult


class MethodAnalyzer(ast.NodeVisitor):
    def __init__(self, filter_classes, form_classes):
        self._filter_classes = filter_classes
        self._form_classes = form_classes
        self.returns: List[InstanceDetails] = []
        self.raises: List[InstanceDetails] = []
        self.filters: List[str] = []
        self.form: Optional[str] = None

    def visit_Return(self, node):
        constructor, args, kwargs = self._parse_call(node.value)
        self.returns.append({"constructor": constructor, "args": args, "kwargs": kwargs})
        self.generic_visit(node)

    def visit_Raise(self, node):
        constructor, args, kwargs = self._parse_call(node.exc)
        self.raises.append({"constructor": constructor, "args": args, "kwargs": kwargs})
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            class_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            class_name = self._get_full_expression(node.func)
        else:
            class_name = None

        if class_name in self._filter_classes:
            self.filters.append(self._filter_classes[class_name])

        if class_name and class_name.split(".")[0] in self._form_classes:
            self.form = self._form_classes[class_name.split(".")[0]]

        self.generic_visit(node)

    def _parse_call(self, node):
        if isinstance(node, ast.Call):
            constructor = self._get_full_expression(node.func)
            args = [self._get_full_expression(arg) for arg in node.args]
            kwargs = {kw.arg: self._get_full_expression(kw.value) for kw in node.keywords}
            return constructor, args, kwargs
        return None, [], {}

    def _get_full_expression(self, node):
        if node is None:
            return None
        return ast.unparse(node)

    def get_results(self) -> ExtractionResult:
        return self.returns, self.raises, self.filters, self.form
