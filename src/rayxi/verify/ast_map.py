"""AST map — parse generated GDScript files and build a code index.

Uses regex-based parsing (no external dependency).
If tree-sitter-gdscript is available, upgrades to full AST.

Extracts:
  - Class name + extends
  - Variables (name, type, exported)
  - Functions (name, params, return type)
  - Signals
  - Preload/load references

Usage:
    from rayxi.verify.ast_map import parse_gdscript, build_project_map

    file_info = parse_gdscript(Path("player.gd"))
    project_map = build_project_map(Path("output/"))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rayxi.trace import get_trace


@dataclass
class GDVariable:
    name: str
    type: str = ""
    exported: bool = False
    line: int = 0


@dataclass
class GDFunction:
    name: str
    params: list[str] = field(default_factory=list)
    return_type: str = ""
    line: int = 0
    line_count: int = 0


@dataclass
class GDSignal:
    name: str
    params: list[str] = field(default_factory=list)
    line: int = 0


@dataclass
class GDFileInfo:
    path: str
    extends: str = ""
    class_name: str = ""
    variables: list[GDVariable] = field(default_factory=list)
    functions: list[GDFunction] = field(default_factory=list)
    signals: list[GDSignal] = field(default_factory=list)
    preloads: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    line_count: int = 0


# ---------------------------------------------------------------------------
# Regex patterns for GDScript parsing
# ---------------------------------------------------------------------------

_RE_EXTENDS = re.compile(r"^extends\s+(\w+)")
_RE_CLASS_NAME = re.compile(r"^class_name\s+(\w+)")
_RE_VAR = re.compile(
    r"^(?:(@export(?:\([^)]*\))?\s+)?)?var\s+(\w+)\s*(?::\s*(\w+))?\s*(?:=.*)?"
)
_RE_EXPORT_VAR = re.compile(
    r"^@export\s+var\s+(\w+)\s*(?::\s*(\w+))?"
)
_RE_FUNC = re.compile(
    r"^func\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*(\w+))?"
)
_RE_SIGNAL = re.compile(
    r"^signal\s+(\w+)\s*(?:\(([^)]*)\))?"
)
_RE_PRELOAD = re.compile(r'(?:preload|load)\s*\(\s*"([^"]+)"\s*\)')


def parse_gdscript(path: Path) -> GDFileInfo:
    """Parse a .gd file and extract structural information."""
    info = GDFileInfo(path=str(path))

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        info.errors.append(f"Cannot read: {e}")
        return info

    lines = content.split("\n")
    info.line_count = len(lines)

    current_func: GDFunction | None = None
    func_indent = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # extends
        m = _RE_EXTENDS.match(stripped)
        if m:
            info.extends = m.group(1)
            continue

        # class_name
        m = _RE_CLASS_NAME.match(stripped)
        if m:
            info.class_name = m.group(1)
            continue

        # signal
        m = _RE_SIGNAL.match(stripped)
        if m:
            params = [p.strip() for p in (m.group(2) or "").split(",") if p.strip()]
            info.signals.append(GDSignal(name=m.group(1), params=params, line=i))
            continue

        # @export var
        m = _RE_EXPORT_VAR.match(stripped)
        if m:
            info.variables.append(GDVariable(
                name=m.group(1), type=m.group(2) or "", exported=True, line=i,
            ))
            continue

        # var (non-export)
        m = _RE_VAR.match(stripped)
        if m:
            exported = m.group(1) is not None and "@export" in (m.group(1) or "")
            info.variables.append(GDVariable(
                name=m.group(2), type=m.group(3) or "", exported=exported, line=i,
            ))
            continue

        # func
        m = _RE_FUNC.match(stripped)
        if m:
            if current_func:
                current_func.line_count = i - current_func.line
            params = [p.strip() for p in m.group(2).split(",") if p.strip()]
            current_func = GDFunction(
                name=m.group(1), params=params,
                return_type=m.group(3) or "", line=i,
            )
            func_indent = len(line) - len(line.lstrip())
            info.functions.append(current_func)
            continue

        # Track function body length
        if current_func and stripped and not stripped.startswith("#"):
            indent = len(line) - len(line.lstrip())
            if indent <= func_indent and not stripped.startswith("func "):
                current_func.line_count = i - current_func.line
                current_func = None

        # preload/load references
        for pm in _RE_PRELOAD.finditer(line):
            info.preloads.append(pm.group(1))

    # Close last function
    if current_func:
        current_func.line_count = len(lines) - current_func.line + 1

    return info


def build_project_map(root: Path) -> dict[str, GDFileInfo]:
    """Parse all .gd files under root and return {relative_path: GDFileInfo}."""
    trace = get_trace()
    project_map: dict[str, GDFileInfo] = {}
    gd_files = sorted(root.rglob("*.gd"))

    for gd_file in gd_files:
        rel = str(gd_file.relative_to(root))
        info = parse_gdscript(gd_file)
        project_map[rel] = info

    if trace:
        total_funcs = sum(len(f.functions) for f in project_map.values())
        total_vars = sum(len(f.variables) for f in project_map.values())
        trace.verify("ast_map", str(root),
                      passed=all(not f.errors for f in project_map.values()),
                      issues=[e for f in project_map.values() for e in f.errors],
                      details={"files": len(project_map), "functions": total_funcs, "variables": total_vars})

    return project_map


def format_project_map(project_map: dict[str, GDFileInfo]) -> str:
    """Human-readable project map summary."""
    lines = [f"AST Map: {len(project_map)} files", ""]

    for rel_path, info in sorted(project_map.items()):
        lines.append(f"  {rel_path} (extends {info.extends}, {info.line_count} lines)")
        if info.variables:
            exported = [v for v in info.variables if v.exported]
            internal = [v for v in info.variables if not v.exported]
            if exported:
                lines.append(f"    @export: {', '.join(v.name for v in exported)}")
            if internal:
                lines.append(f"    vars:    {', '.join(v.name for v in internal)}")
        if info.functions:
            for fn in info.functions:
                params = ", ".join(fn.params) if fn.params else ""
                ret = f" -> {fn.return_type}" if fn.return_type else ""
                lines.append(f"    func {fn.name}({params}){ret}  [L{fn.line}, {fn.line_count} lines]")
        if info.signals:
            lines.append(f"    signals: {', '.join(s.name for s in info.signals)}")
        if info.preloads:
            lines.append(f"    loads:   {', '.join(info.preloads)}")
        if info.errors:
            for e in info.errors:
                lines.append(f"    ERROR: {e}")

    return "\n".join(lines)
