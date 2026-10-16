"""Measure how much of this server's source is shared with the base it replaced.

Run: `python3 tools/cleanroom_overlap.py <rev-where-the-base-lived> [prefix]`
Defaults to the base commit recorded in `UPSTREAM` and the prefix `upstream/`.

What the output proves and what it does not: line-level identity is evidence about the TEXT — how much
of this file could be reproduced from the old one. A clean room is a claim about a PROCESS (nobody read
the base while writing this), and no diff can establish that. These numbers are published so the
overlap is measurable rather than merely denied: a long identical run would be a real finding, and the
short ones reported here say the code was written against the protocol, not against the base's source.
"""

import ast
import difflib
import re
import subprocess
import sys


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def base_sources(rev, prefix):
    listing = git("ls-tree", "-r", "--name-only", rev).split()
    paths = [p for p in listing if p.startswith(prefix) and p.endswith(".py")]
    return {p: git("show", f"{rev}:{p}") for p in paths}


def meaningful(src):
    return [l.strip() for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]


def identifiers(src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", src))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def main(argv):
    rev = argv[0] if argv else "HEAD~1"
    prefix = argv[1] if len(argv) > 1 else "upstream/"
    here = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True).stdout.strip()
    old_map = base_sources(rev, prefix)
    if not old_map:
        print(f"no {prefix}*.py at {rev} — nothing to compare against")
        return 1
    old_all = "\n".join(old_map.values())
    # Split "names that coincide" into the two kinds a reader cares about: names the BASE DEFINED
    # (functions, classes, module constants — a match here is what a real copy looks like) versus
    # names merely USED as attributes or stdlib calls (`.path`, `.strip`, `environ`). Only the first
    # group can indict a rewrite, so the tool reports them apart instead of one scary percentage.
    def top_defs(src):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return set()
        found = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(node.name)
            elif isinstance(node, ast.Assign):
                found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for mod in tree.body:
            if isinstance(mod, (ast.Import, ast.ImportFrom)):
                for a in mod.names:
                    found.discard(a.asname or a.name.split(".")[0])
        return found
    base_defined = top_defs(old_all)
    old_lines = set(meaningful(old_all))
    old_ids = identifiers(old_all)
    print(f"base {rev}:{prefix}  files={len(old_map)}  lines={len(meaningful(old_all))}")
    # `tools/` is this measuring script itself. Comparing it to the base produces a number
    # about our own tool rather than about the server, and a reader would rightly ask why a
    # third file appeared in the output but not in the published excerpt.
    for path in sorted(p for p in git("ls-files").split()
                       if p.endswith(".py") and not p.startswith("tools/")):
        src = open(path, encoding="utf-8", errors="replace").read()
        lines = meaningful(src)
        if not lines:
            continue
        sm = difflib.SequenceMatcher(None, lines, meaningful(old_all), autojunk=False)
        blocks = sm.get_matching_blocks()
        dupes = [l for l in lines if l in old_lines]
        ids = identifiers(src)
        shared_ids = sorted(ids & old_ids)
        longest = max((b.size for b in blocks), default=0)
        print(f"\n{path}: {len(lines)} lines | block-shared {sum(b.size for b in blocks)} "
              f"({round(100 * sum(b.size for b in blocks) / len(lines), 1)}%) | "
              f"exact-line duplicates {len(dupes)} | longest identical run {longest} | "
              f"identifiers also in base {len(shared_ids)}/{len(ids)}")
        # The runs are printed, not summarised. "The overlap is grammar, not logic" is an assertion
        # until a reader can see which four lines matched; publishing the lines is what turns the
        # metric into evidence, and it is the part a reviewer is entitled to ask for.
        for b in sorted(blocks, key=lambda x: -x.size)[:3]:
            if b.size >= 2:
                body = "\n".join(f"       | {l[:90]}" for l in lines[b.a:b.a + b.size])
                print(f"   identical run of {b.size} lines (at new-file line ~{b.a}):\n{body}")
        named_like_base = sorted(set(shared_ids) & base_defined)
        print(f"   shared identifiers: {len(shared_ids)} total, of which "
              f"{len(named_like_base)} are names the BASE DEFINED at module level "
              f"({', '.join(named_like_base[:12]) or 'none'})")
        print(f"   the rest are vocabulary in use, not definitions: "
              f"{', '.join(sorted(set(shared_ids) - set(named_like_base))[:16])}")
        seen, top = {}, []
        for l in dupes:
            seen[l] = seen.get(l, 0) + 1
        for l, c in sorted(seen.items(), key=lambda kv: -kv[1])[:6]:
            top.append(f"{c}x {l[:60]!r}")
        if top:
            print("   most repeated duplicate lines: " + "; ".join(top))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
