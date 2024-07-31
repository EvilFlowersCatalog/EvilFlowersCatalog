import ast
from typing import List

from apps.openapi.types import InstanceDetails, ExtractionResult


class MethodAnalyzer(ast.NodeVisitor):
    def __init__(self, filter_classes):
        self.filter_classes = filter_classes
        self.returns: List[InstanceDetails] = []
        self.raises: List[InstanceDetails] = []
        self.filters: List[str] = []

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
            filter_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            filter_name = self._get_full_expression(node.func)
        else:
            filter_name = None

        if filter_name in self.filter_classes:
            self.filters.append(filter_name)
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
        return self.returns, self.raises, self.filters
