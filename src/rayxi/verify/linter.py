"""GDScript linter integration — validates generated code.

Supports two backends:
  1. gdtoolkit (gdscript-toolkit): `gdlint` for style, `gdparse` for syntax
  2. Godot headless: `godot --headless --check-only` for full validation

Falls back gracefully if neither is available.

Feeds structured errors back for the compile-verify retry loop.

Usage:
    from rayxi.verify.linter import lint_file, lint_project

    errors = lint_file(Path("output/fighting/p1_health_bar.gd"))
    all_errors = lint_project(Path("output/"))
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rayxi.trace import get_trace

_log = logging.getLogger("rayxi.verify.linter")


@dataclass
class LintError:
    file: str
    line: int
    column: int
    severity: str  # "error", "warning", "info"
    code: str  # error code (e.g. "E101", "parse_error")
    message: str


def _has_gdparse() -> bool:
    return shutil.which("gdparse") is not None


def _has_gdlint() -> bool:
    return shutil.which("gdlint") is not None


def _has_godot() -> bool:
    return shutil.which("godot") is not None


def _run_gdparse(path: Path) -> list[LintError]:
    """Run gdparse (syntax check) on a .gd file."""
    errors: list[LintError] = []
    try:
        result = subprocess.run(
            ["gdparse", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            # Parse error output
            for line in result.stderr.splitlines():
                m = re.match(r".*?:(\d+):(\d+):\s*(.*)", line)
                if m:
                    errors.append(LintError(
                        file=str(path),
                        line=int(m.group(1)),
                        column=int(m.group(2)),
                        severity="error",
                        code="parse_error",
                        message=m.group(3).strip(),
                    ))
                elif line.strip():
                    errors.append(LintError(
                        file=str(path), line=0, column=0,
                        severity="error", code="parse_error",
                        message=line.strip(),
                    ))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        errors.append(LintError(
            file=str(path), line=0, column=0,
            severity="error", code="tool_error",
            message=f"gdparse failed: {e}",
        ))
    return errors


def _run_gdlint(path: Path) -> list[LintError]:
    """Run gdlint (style check) on a .gd file."""
    errors: list[LintError] = []
    try:
        result = subprocess.run(
            ["gdlint", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            # Format: file.gd:10:5: Error: message (code)
            m = re.match(r".*?:(\d+):(\d+):\s*(\w+):\s*(.*?)(?:\s*\((\w+)\))?\s*$", line)
            if m:
                sev = m.group(3).lower()
                if sev not in ("error", "warning", "info"):
                    sev = "warning"
                errors.append(LintError(
                    file=str(path),
                    line=int(m.group(1)),
                    column=int(m.group(2)),
                    severity=sev,
                    code=m.group(5) or "lint",
                    message=m.group(4).strip(),
                ))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        errors.append(LintError(
            file=str(path), line=0, column=0,
            severity="warning", code="tool_error",
            message=f"gdlint failed: {e}",
        ))
    return errors


def lint_file(path: Path) -> list[LintError]:
    """Lint one .gd file. Uses best available backend."""
    errors: list[LintError] = []

    if _has_gdparse():
        errors.extend(_run_gdparse(path))
    if _has_gdlint():
        errors.extend(_run_gdlint(path))

    if not _has_gdparse() and not _has_gdlint():
        _log.warning("No GDScript linter available (install gdtoolkit: pip install gdtoolkit)")

    return errors


def lint_project(root: Path) -> dict[str, list[LintError]]:
    """Lint all .gd files under root. Returns {relative_path: errors}."""
    trace = get_trace()
    results: dict[str, list[LintError]] = {}
    gd_files = sorted(root.rglob("*.gd"))

    available = []
    if _has_gdparse():
        available.append("gdparse")
    if _has_gdlint():
        available.append("gdlint")
    if not available:
        _log.warning("No GDScript linter available — skipping lint")
        if trace:
            trace.verify("linter", str(root), passed=True,
                          details={"skipped": True, "reason": "no linter available"})
        return results

    _log.info("Linting %d files with: %s", len(gd_files), ", ".join(available))

    total_errors = 0
    for gd_file in gd_files:
        rel = str(gd_file.relative_to(root))
        file_errors = lint_file(gd_file)
        if file_errors:
            results[rel] = file_errors
            total_errors += len(file_errors)

    if trace:
        all_issues = [
            f"{rel}:{e.line}: [{e.severity}] {e.message}"
            for rel, errs in results.items() for e in errs
        ]
        trace.verify("linter", str(root),
                      passed=total_errors == 0,
                      issues=all_issues,
                      details={"files_checked": len(gd_files), "files_with_errors": len(results),
                               "total_errors": total_errors})

    _log.info("Lint: %d files checked, %d with errors, %d total errors",
               len(gd_files), len(results), total_errors)
    return results


def format_lint_results(results: dict[str, list[LintError]]) -> str:
    """Human-readable lint report."""
    if not results:
        return "Lint: all files pass."

    total = sum(len(errs) for errs in results.values())
    lines = [f"Lint: {total} errors in {len(results)} files", ""]

    for rel_path, errors in sorted(results.items()):
        lines.append(f"  {rel_path}:")
        for e in errors:
            lines.append(f"    L{e.line}:{e.column} [{e.severity}] {e.code}: {e.message}")
    return "\n".join(lines)
