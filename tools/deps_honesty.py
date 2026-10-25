#!/usr/bin/env python3
"""Assert that `pyproject.toml` declares exactly the third-party modules the shipped code imports.

Why this file exists. The publication audit of 2026-10-16 found two manifests describing a tree that
had already been replaced: a dependency on `httpx` (blender) and one on `requests` (comfyui) that no
shipped file imports, both left over from the third-party base that the independent rewrite deleted,
plus a `package-dir` still pointing into that deleted `upstream/`. A reviewer had to read the manifest
against the file list by hand to notice. This makes that comparison a command, so the defect class is
closed rather than fixed.

Three directions are checked, because each fails differently:
  * declared but not imported  -> the recipient installs a library for code that is not there, and the
    claim about what this work depends on is untrue on its face;
  * imported but not declared  -> `pip` installs nothing for it and the server raises ImportError at
    first use, which is the failure the old comment called "an install that succeeds and then dies";
  * a path named by the setuptools config that does not exist in the tree — invisible to an import
    scan, because nothing imports a `package-dir`; it is where the build looks, and it is the line
    that made this repository's build fail after `upstream/` was deleted.

Names this repository or the workstation provides by path (`sys.path.insert` to
`$AI_STATION_HOME/tools`) are excluded from both lists: they are not PyPI distributions, and a
manifest that demands `pip install gpu_gate` would be its own lie.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

# Directories that are not shipped source: build output, VCS, and the base this work replaced.
SKIP_DIRS = {"build", ".git", "upstream", "__pycache__", ".venv"}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def tracked_py(root: Path) -> list[Path]:
    """The Python files this repository actually publishes: `git ls-files`, nothing else.

    Measured on 2026-10-16: `tools/work_order.py` in every MCP repository is a SYMLINK to the station's
    `mcp/push_gate.py`, excluded via `.git/info/exclude` so a clone never sees it. A filesystem walk
    followed that link and judged the audit by code that does not ship — which would pass here and
    mean something else on the machine that clones this repo. Tracked files are the published surface,
    and they are the only thing a manifest may be compared against.
    """
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "*.py"],
                           capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — git unreadable is not "no files"
        return []
    if r.returncode != 0:
        return []
    return [root / name for name in r.stdout.split("\0") if name.strip()]


def repo_files(root: Path) -> list[Path]:
    """Tracked source, or — with no git to ask — the working tree minus build and vendored paths.

    The fallback never runs on a published repository, so it is written to fail loudly instead of
    returning an empty list that reads as "no imports".
    """
    files = tracked_py(root)
    if files:
        return files
    return [p for p in sorted(root.rglob("*.py"))
            if not (SKIP_DIRS & set(p.parts)) and p.is_file()
            and root in p.resolve().parents]


def tracked_modules(root: Path) -> set[str]:
    """Top-level names this repository itself provides, plus the station's by path."""
    names = {p.stem for p in repo_files(root)}
    station = Path(os.environ.get("AI_STATION_HOME", os.path.expanduser("~/.ai-station")))
    tools = station / "tools"
    if tools.is_dir():
        names |= {p.stem for p in tools.glob("*.py")}
    names |= set(sys.stdlib_module_names)
    return names


def imported_names(root: Path) -> set[str]:
    """Every top-level module name imported by shipped source, read from the AST, not from grep."""
    out: set[str] = set()
    for path in repo_files(root):
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            # An unparseable file is not "no imports"; the caller must see the gap, not a clean set.
            out.add(f"!UNPARSEABLE:{path.name}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    out.add(node.module.split(".")[0])
    return out


def declared_names(root: Path) -> set[str]:
    """Distribution names from `[project].dependencies`, reduced to their import name."""
    data = tomllib.loads((root / "pyproject.toml").read_text())
    out = set()
    for spec in data.get("project", {}).get("dependencies", []) or []:
        name = str(spec).split("[", 1)[0]
        for sep in (">", "<", "=", "!", "~", ";"):
            name = name.split(sep, 1)[0]
        out.add(name.strip().replace("-", "_"))
    return out


def missing_packaging_paths(root: Path) -> list[str]:
    """Every path the setuptools config names that does not exist in the tree.

    This is the half of the manifest the dependency list cannot see. The audit found
    `package-dir = {"blender_mcp" = "upstream/src/blender_mcp"}` still declared after `upstream/` was
    deleted: the dependency list was wrong too, but this line is what made the BUILD fail, and it is
    invisible to an import scan because the path is never imported — it is where setuptools looks.
    """
    data = tomllib.loads((root / "pyproject.toml").read_text())
    st = data.get("tool", {}).get("setuptools", {}) or {}
    refs: list[str] = []
    pkg_dir = st.get("package-dir", {}) or {}
    for _name, path in pkg_dir.items():
        refs.append(str(path))
    for mod in st.get("py-modules", []) or []:
        refs.append(f"{mod}.py")
    for pkg in st.get("packages", []) or []:
        here = pkg_dir.get(pkg, pkg.replace(".", "/"))
        refs.append(str(Path(here) / "__init__.py") if here else pkg)
    return sorted({r for r in refs if r and not (root / r).exists()})


def audit(root: Path | None = None) -> dict:
    root = root or repo_root()
    known = tracked_modules(root)
    imports = imported_names(root)
    imported = {n for n in imports if n not in known and not n.startswith("!")}
    unparseable = sorted(n.split(":", 1)[1] for n in imports if n.startswith("!"))
    declared = declared_names(root)
    # A distribution named `zope.interface` imports as `zope`; first segment is the import name.
    declared_imports = {n.split(".")[0] for n in declared}
    paths = missing_packaging_paths(root)
    return {
        "ok": not unparseable and not paths and imported == declared_imports,
        "declared": sorted(declared),
        "imported_third_party": sorted(imported),
        "declared_but_not_imported": sorted(declared_imports - imported),
        "imported_but_not_declared": sorted(imported - declared_imports),
        "missing_packaging_paths": paths,
        "unparseable_files": unparseable,
    }


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else repo_root()
    report = audit(root)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
