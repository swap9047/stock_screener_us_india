"""
User-defined computed columns -- e.g. "52W Distance %" = (week52_high -
last_close) / week52_high * 100. Each one is a small arithmetic formula
over the SAME metric keys used everywhere else in the app (the ones from
stock_data.get_filterable_metrics()), evaluated once per ticker right
after the normal metrics are computed (see stock_data.fetch_all_markets),
so the result flows automatically into the watchlist table, the column
picker, the custom filter builder, and alert conditions -- every consumer
already works off whatever's in a row's dict plus a label->key registry,
so a custom column just needs to (a) exist as a key in that dict and (b)
be added to those registries. No special-casing needed downstream.

Formulas are NOT evaluated with Python's eval()/exec() -- see
safe_eval_formula()/_Evaluator below for why and how. Only plain
arithmetic (+ - * / ** parentheses unary minus) over known variable names
and numeric literals is allowed; anything else (function calls, attribute
access, subscripts, comparisons, imports, ...) is rejected before
evaluation ever runs.

Formulas may only reference the BUILT-IN metric keys (last_close,
week52_high, rsi14_daily, ...) -- not other custom columns -- to avoid
needing dependency ordering / cycle detection between custom columns.
"""

import ast
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_COLUMNS_FILE = os.path.join(SCRIPT_DIR, "custom_columns.json")

FORMAT_CHOICES = {
    "number": "Plain number (2 decimals)",
    "percent": "Percentage (1 decimal, % suffix)",
    "price": "Price (whole number)",
}

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


class FormulaError(ValueError):
    pass


def _check_ast(node, valid_names):
    """Walks the parsed formula, raising FormulaError on the first node
    that isn't one of the small set of arithmetic node types this module
    allows. Called before every evaluation (not just at authoring time in
    the UI) so a formula that referenced a metric key later removed from
    the app, or was hand-edited in custom_columns.json, can't silently do
    something unexpected."""
    if isinstance(node, ast.Expression):
        _check_ast(node.body, valid_names)
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise FormulaError(f"Operator {type(node.op).__name__} isn't allowed.")
        _check_ast(node.left, valid_names)
        _check_ast(node.right, valid_names)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise FormulaError(f"Operator {type(node.op).__name__} isn't allowed.")
        _check_ast(node.operand, valid_names)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise FormulaError("Only numeric literals are allowed.")
    elif isinstance(node, ast.Name):
        if node.id not in valid_names:
            raise FormulaError(
                f"Unknown metric '{node.id}'. Use one of the metric keys shown in the reference list."
            )
    else:
        raise FormulaError(
            f"'{type(node).__name__}' isn't allowed in a formula -- only numbers, metric names, "
            "and + - * / ** ( ) are."
        )


def validate_formula(formula, valid_names):
    """Parses and validates `formula` without evaluating it. Returns
    (ok, error_message) -- error_message is "" when ok is True. Used by
    the UI for immediate feedback while a formula is being typed, and
    internally by safe_eval_formula() before every real evaluation."""
    if not formula or not formula.strip():
        return False, "Formula is empty."
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        return False, f"Syntax error: {e.msg}"
    try:
        _check_ast(tree, valid_names)
    except FormulaError as e:
        return False, str(e)
    return True, ""


class _Evaluator(ast.NodeVisitor):
    """Manual recursive evaluator -- deliberately NOT eval()/exec(), even
    with a restricted globals/locals dict, so there's no reliance on
    Python's builtins being fully locked down correctly. Only handles the
    handful of node types _check_ast() already allowed; anything else
    raises (should be unreachable in practice since validate_formula() is
    always called first, but this is the actual safety boundary, not the
    validator -- keep both in sync if the allowed node set ever changes)."""

    def __init__(self, variables):
        self.variables = variables

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right = self.visit(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right if right != 0 else None
        if isinstance(node.op, ast.Mod):
            return left % right if right != 0 else None
        if isinstance(node.op, ast.Pow):
            return left ** right
        raise FormulaError(f"Operator {type(node.op).__name__} isn't allowed.")

    def visit_UnaryOp(self, node):
        val = self.visit(node.operand)
        if val is None:
            return None
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        raise FormulaError(f"Operator {type(node.op).__name__} isn't allowed.")

    def visit_Constant(self, node):
        return node.value

    def visit_Name(self, node):
        return self.variables.get(node.id)

    def generic_visit(self, node):
        raise FormulaError(f"'{type(node).__name__}' isn't allowed in a formula.")


def safe_eval_formula(formula, variables):
    """Evaluates `formula` (a validated arithmetic expression) against
    `variables` (dict of metric key -> numeric value or None for this
    ticker). Returns a float, or None if the formula is invalid, any
    referenced variable is missing/None, or a division/modulo by zero was
    hit -- None propagates the same way the rest of the app already
    represents "not available" (e.g. RS when there's not enough history),
    rendering as "-" in the table rather than crashing or showing 0."""
    ok, _ = validate_formula(formula, set(variables.keys()))
    if not ok:
        return None
    try:
        tree = ast.parse(formula, mode="eval")
        result = _Evaluator(variables).visit(tree)
    except Exception:
        return None
    if result is None:
        return None
    try:
        return float(result)
    except (TypeError, ValueError):
        return None


def load_custom_columns():
    if not os.path.exists(CUSTOM_COLUMNS_FILE):
        return []
    try:
        with open(CUSTOM_COLUMNS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_custom_columns(columns):
    with open(CUSTOM_COLUMNS_FILE, "w") as f:
        json.dump(columns, f, indent=2)


def column_key(col):
    """Stable internal field/metric-key name for a custom column -- keyed
    off its `id` (assigned once at creation, never changes), NOT its
    display name, so renaming a custom column later doesn't silently break
    any alert rule or saved filter that already references it."""
    return f"custom_{col['id']}"


def apply_custom_columns(row, columns):
    """Computes every ENABLED custom column for one row (a plain dict, as
    produced by stock_data.fetch_snapshot) and adds each as a new key
    (custom_<id>) directly on that row, in place. Formulas only see the
    row's existing keys (its built-in metrics) as variables -- not other
    custom columns' results -- see the module docstring for why."""
    if not columns:
        return row
    variables = row
    for col in columns:
        if not col.get("enabled", True):
            continue
        key = column_key(col)
        row[key] = safe_eval_formula(col.get("formula", ""), variables)
    return row


def apply_custom_columns_to_rows(rows, columns=None):
    if columns is None:
        columns = load_custom_columns()
    if not columns:
        return rows
    for row in rows:
        apply_custom_columns(row, columns)
    return rows
