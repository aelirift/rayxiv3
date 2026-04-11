"""Typed expression AST for impact-map formulas.

Every formula in the impact map (derivation rule, effect update, guard condition,
initial value) is represented as a typed expression tree that parses 1:1 into
GDScript at codegen time. No prose math.

Grammar (intentionally small — enough for every game formula you'd reasonably write):

  Expr     := Literal | PropRef | BinOp | FnCall | Cond
  Literal  := {kind: "literal", type, value}           -- concrete value
  PropRef  := {kind: "ref", path}                      -- read from another property
  BinOp    := {kind: "op", op, left, right}            -- arithmetic/compare/logic
  FnCall   := {kind: "call", fn, args[]}               -- clamp/min/max/abs/floor/ceil/sign
  Cond     := {kind: "cond", condition, then_val, else_val}

Examples:

  # fighter.current_hp += -damage_taken
  {"kind": "op", "op": "sub", "left": {"kind": "ref", "path": "fighter.current_hp"},
   "right": {"kind": "ref", "path": "event.damage_taken"}}

  # hp_bar.fill_percent = current_hp / max_hp (clamped 0..1)
  {"kind": "call", "fn": "clamp", "args": [
    {"kind": "op", "op": "div",
     "left": {"kind": "ref", "path": "fighter.current_hp"},
     "right": {"kind": "ref", "path": "fighter.max_hp"}},
    {"kind": "literal", "type": "float", "value": 0.0},
    {"kind": "literal", "type": "float", "value": 1.0}]}
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Allowed symbols
# ---------------------------------------------------------------------------

BINOPS = {
    # arithmetic
    "add", "sub", "mul", "div", "mod",
    # comparison
    "lt", "le", "gt", "ge", "eq", "ne",
    # logic
    "and", "or",
}

FNS = {
    "clamp",   # args: (value, min, max)
    "min",     # args: (a, b) or (a, b, c, ...)
    "max",
    "abs",     # args: (x)
    "floor",
    "ceil",
    "sign",
    "not",     # args: (x)
}

LITERAL_TYPES = {
    "int", "float", "bool", "string",
    "vector2",  # [x, y]
    "color",    # "#rrggbb"
    "rect2",    # [x, y, w, h] — for hitboxes, hurtboxes, ui rects
    "list",     # arrays (circular buffers, input history)
    "dict",     # freeform structured data (rare)
}


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------


class LiteralExpr(BaseModel):
    kind: Literal["literal"] = "literal"
    type: str
    value: Any

    def validate_shape(self) -> list[str]:
        errors: list[str] = []
        if self.type not in LITERAL_TYPES:
            errors.append(f"literal type '{self.type}' not in {sorted(LITERAL_TYPES)}")
        return errors


class RefExpr(BaseModel):
    kind: Literal["ref"] = "ref"
    path: str   # e.g. "fighter.current_hp", "const.max_rage_stacks", "event.damage_taken"

    def validate_shape(self) -> list[str]:
        if "." not in self.path:
            return [f"ref path '{self.path}' must be 'namespace.name' (e.g. 'fighter.current_hp')"]
        return []


class BinOpExpr(BaseModel):
    kind: Literal["op"] = "op"
    op: str
    left: "Expr"
    right: "Expr"

    def validate_shape(self) -> list[str]:
        errors: list[str] = []
        if self.op not in BINOPS:
            errors.append(f"binop '{self.op}' not in {sorted(BINOPS)}")
        errors.extend(validate_expr(self.left))
        errors.extend(validate_expr(self.right))
        return errors


class FnCallExpr(BaseModel):
    kind: Literal["call"] = "call"
    fn: str
    args: list["Expr"] = Field(default_factory=list)

    def validate_shape(self) -> list[str]:
        errors: list[str] = []
        if self.fn not in FNS:
            errors.append(f"function '{self.fn}' not in {sorted(FNS)}")
        expected_arity = {
            "abs": 1, "floor": 1, "ceil": 1, "sign": 1, "not": 1,
            "clamp": 3,
        }
        if self.fn in expected_arity and len(self.args) != expected_arity[self.fn]:
            errors.append(
                f"fn '{self.fn}' expects {expected_arity[self.fn]} args, got {len(self.args)}"
            )
        for a in self.args:
            errors.extend(validate_expr(a))
        return errors


class CondExpr(BaseModel):
    kind: Literal["cond"] = "cond"
    condition: "Expr"
    then_val: "Expr"
    else_val: "Expr"

    def validate_shape(self) -> list[str]:
        return (
            validate_expr(self.condition)
            + validate_expr(self.then_val)
            + validate_expr(self.else_val)
        )


Expr = Union[LiteralExpr, RefExpr, BinOpExpr, FnCallExpr, CondExpr]

# Rebuild forward refs
BinOpExpr.model_rebuild()
FnCallExpr.model_rebuild()
CondExpr.model_rebuild()


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------


def parse_expr(data: Any) -> Expr:
    """Parse a dict/list into a typed Expr. Raises ValueError if malformed."""
    if not isinstance(data, dict):
        raise ValueError(f"expr must be a dict, got {type(data).__name__}")
    kind = data.get("kind")
    if kind == "literal":
        return LiteralExpr.model_validate(data)
    if kind == "ref":
        return RefExpr.model_validate(data)
    if kind == "op":
        return BinOpExpr.model_validate(data)
    if kind == "call":
        return FnCallExpr.model_validate(data)
    if kind == "cond":
        return CondExpr.model_validate(data)
    raise ValueError(f"unknown expr kind: {kind!r}")


def validate_expr(expr: Expr | None) -> list[str]:
    """Recursively validate an expression tree. Returns list of error messages."""
    if expr is None:
        return []
    return expr.validate_shape()


def expr_refs(expr: Expr | None) -> list[str]:
    """Collect every property path referenced by this expression."""
    if expr is None:
        return []
    if isinstance(expr, RefExpr):
        return [expr.path]
    if isinstance(expr, LiteralExpr):
        return []
    if isinstance(expr, BinOpExpr):
        return expr_refs(expr.left) + expr_refs(expr.right)
    if isinstance(expr, FnCallExpr):
        refs: list[str] = []
        for a in expr.args:
            refs.extend(expr_refs(a))
        return refs
    if isinstance(expr, CondExpr):
        return expr_refs(expr.condition) + expr_refs(expr.then_val) + expr_refs(expr.else_val)
    return []


def format_expr(expr: Expr | None) -> str:
    """Pretty-print an expression as human-readable pseudocode. For diagnostics."""
    if expr is None:
        return "(none)"
    if isinstance(expr, LiteralExpr):
        if expr.type == "string":
            return f'"{expr.value}"'
        return str(expr.value)
    if isinstance(expr, RefExpr):
        return expr.path
    if isinstance(expr, BinOpExpr):
        sym = {
            "add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%",
            "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!=",
            "and": "&&", "or": "||",
        }.get(expr.op, expr.op)
        return f"({format_expr(expr.left)} {sym} {format_expr(expr.right)})"
    if isinstance(expr, FnCallExpr):
        args = ", ".join(format_expr(a) for a in expr.args)
        return f"{expr.fn}({args})"
    if isinstance(expr, CondExpr):
        return (
            f"({format_expr(expr.condition)} "
            f"? {format_expr(expr.then_val)} "
            f": {format_expr(expr.else_val)})"
        )
    return "?"
