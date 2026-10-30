#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / ".engineering" / "architecture-guard.json"
SKIP_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    "build",
    "coverage",
    "dist",
    "generated",
    "node_modules",
    "target",
    "vendor",
}


def relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def is_skipped(path: Path) -> bool:
    return any(part in SKIP_PARTS for part in path.relative_to(PROJECT_ROOT).parts)


def matches_any(path: Path, patterns: Iterable[str]) -> bool:
    rel = Path(relative(path))
    return any(rel.match(pattern) for pattern in patterns)


def files_for(pattern: str, excludes: Iterable[str] = ()) -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.glob(pattern)
        if path.is_file() and not is_skipped(path) and not matches_any(path, excludes)
    )


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise SystemExit(f"Missing architecture guard config: {relative(CONFIG_PATH)}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def validated_exceptions(config: dict[str, Any], failures: list[str]) -> set[tuple[str, str]]:
    required = {"rule", "path", "reason", "owner", "regression_evidence", "removal_condition"}
    result: set[tuple[str, str]] = set()
    for index, item in enumerate(config.get("exceptions", []), start=1):
        missing = sorted(key for key in required if not str(item.get(key, "")).strip())
        if missing:
            failures.append(f"guard-config: exception {index} is missing {', '.join(missing)}")
            continue
        result.add((str(item["rule"]), str(item["path"]).replace("\\", "/")))
    return result


def add_failure(
    failures: list[str], exceptions: set[tuple[str, str]], rule: str, path: str, message: str
) -> None:
    normalized = path.replace("\\", "/")
    if (rule, normalized) not in exceptions:
        failures.append(f"{rule}: {normalized}: {message}")


def check_required_and_decisions(
    config: dict[str, Any], failures: list[str], exceptions: set[tuple[str, str]]
) -> None:
    for name in config.get("required_files", []):
        if not (PROJECT_ROOT / name).is_file():
            add_failure(failures, exceptions, "required-file", name, "file is missing")
    for name in config.get("decision_files", []):
        path = PROJECT_ROOT / name
        if path.is_file() and "REQUIRED_DECISION" in path.read_text(encoding="utf-8", errors="replace"):
            add_failure(failures, exceptions, "unresolved-decision", name, "resolve every REQUIRED_DECISION")


def check_budgets(
    config: dict[str, Any], failures: list[str], exceptions: set[tuple[str, str]]
) -> None:
    for budget in config.get("budgets", []):
        limit = int(budget["max_lines"])
        for path in files_for(str(budget["glob"]), budget.get("exclude", [])):
            count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if count > limit:
                add_failure(
                    failures,
                    exceptions,
                    str(budget["id"]),
                    relative(path),
                    f"{count} lines exceeds the {limit}-line review budget",
                )


def check_forbidden_patterns(
    config: dict[str, Any], failures: list[str], exceptions: set[tuple[str, str]]
) -> None:
    for rule in config.get("forbidden_patterns", []):
        pattern = re.compile(str(rule["regex"]), re.MULTILINE)
        for path in files_for(str(rule["glob"]), rule.get("exclude", [])):
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                add_failure(failures, exceptions, str(rule["id"]), relative(path), str(rule["message"]))


def python_function_ranges(path: Path) -> Iterable[tuple[str, int, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError as error:
        yield f"syntax-error:{error.msg}", error.lineno or 1, error.lineno or 1
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
            yield node.name, node.lineno, node.end_lineno


def check_python_functions(
    config: dict[str, Any], failures: list[str], exceptions: set[tuple[str, str]]
) -> None:
    limit = int(config.get("python_function_max_lines", 0))
    if not limit:
        return
    for path in files_for("**/*.py", ["**/migrations/**", "**/alembic/versions/**"]):
        for name, start, end in python_function_ranges(path):
            size = end - start + 1
            if name.startswith("syntax-error:"):
                add_failure(failures, exceptions, "python-syntax", relative(path), name)
            elif size > limit:
                add_failure(
                    failures,
                    exceptions,
                    "python-function-lines",
                    relative(path),
                    f"{name} spans {size} lines ({start}-{end}); budget is {limit}",
                )


TS_IMPORT = re.compile(r"(?:from\s+|import\s*\()(['\"])(?P<target>[^'\"]+)\1")


def domain_for(path: Path, root: Path) -> tuple[str, Path] | None:
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    return (parts[0], root / parts[0]) if parts else None


def typescript_cross_imports(path: Path, root: Path) -> Iterable[tuple[str, str]]:
    source = domain_for(path, root)
    if not source:
        return
    for match in TS_IMPORT.finditer(path.read_text(encoding="utf-8", errors="replace")):
        target = match.group("target")
        if not target.startswith("."):
            continue
        resolved = (path.parent / target).resolve()
        destination = domain_for(resolved, root)
        if destination and destination[0] != source[0]:
            relative_target = resolved.relative_to(destination[1].resolve()).as_posix().strip("/")
            yield target, relative_target or "index"


def python_cross_imports(path: Path, root: Path) -> Iterable[tuple[str, str]]:
    source = domain_for(path, root)
    if not source:
        return
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level < 2 or not node.module:
            continue
        target_parts = node.module.split(".")
        if target_parts and target_parts[0] != source[0] and (root / target_parts[0]).is_dir():
            yield node.module, ".".join(target_parts[1:]) or "__init__"


def check_module_boundaries(
    config: dict[str, Any], failures: list[str], exceptions: set[tuple[str, str]]
) -> None:
    for rule in config.get("module_boundaries", []):
        root = PROJECT_ROOT / str(rule["root"])
        if not root.is_dir():
            continue
        public = set(rule.get("public_names", []))
        suffixes = {".ts", ".tsx"} if rule["kind"] == "typescript" else {".py"}
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes or is_skipped(path):
                continue
            imports = typescript_cross_imports(path, root) if rule["kind"] == "typescript" else python_cross_imports(path, root)
            for target, public_name in imports:
                first = Path(public_name).parts[0].split(".")[0]
                if first not in public:
                    add_failure(
                        failures,
                        exceptions,
                        str(rule["id"]),
                        relative(path),
                        f"cross-module import {target!r} bypasses the destination public surface",
                    )


def main() -> int:
    config = load_config()
    failures: list[str] = []
    exceptions = validated_exceptions(config, failures)
    check_required_and_decisions(config, failures, exceptions)
    check_budgets(config, failures, exceptions)
    check_forbidden_patterns(config, failures, exceptions)
    check_python_functions(config, failures, exceptions)
    check_module_boundaries(config, failures, exceptions)
    if failures:
        print("Architecture guard failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Architecture guard passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
