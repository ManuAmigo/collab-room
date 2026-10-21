#!/usr/bin/env python3
"""Build the one file people and Codex run from the package of modules.

The modules are joined in layer order. Between them only two things change, found by `ast` and edited
in the source text by position: a line «import collab.x as x» becomes an empty line, and «x.name» on
a module of the package becomes «name». Everything else — comments, empty lines, spacing — is kept
byte for byte: `ast.unparse` would drop the comments, and with them the reasons and the
decisions that tell the next reader not to «fix» what is meant.

This file is security code too: its output is the Codex copy. So it refuses loudly rather
than guess — any name it would have to rename, any import it would have to understand, stops the build.

    python3 tools/build_collab.py           # write the one file
    python3 tools/build_collab.py --check   # exit 1 if the committed file is not the fresh build
"""

from __future__ import annotations

import argparse
import ast
import builtins
import io
import os
import shutil
import stat
import subprocess
import symtable
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "collab"
ALIAS_PREFIX = "m_"
# The modules in layer order: a module may use only those above it in this list. The package is
# src/collab.
LAYERS = [
    "boot",
    "text",
    "term",
    "store",
    "tmuxio",
    "log",
    "tasks",
    "screen",
    "usage",
    "installer",
    "ledger",
    "deliver",
    "agents",
    "lifecycle",
    "chat_base",
    "chat_help",
    "chat_menu",
    "chat_views",
    "chat_wizard",
    "chat_cmds",
    "chat_feed",
    "chat_input",
    "chat",
    "dm",
    "cli",
]
MODULES: list[tuple[str, Path]] = [(f"collab.{m}", ROOT / "src" / "collab" / f"{m}.py") for m in LAYERS]
OUTPUT = ROOT / "collab"
# Places that guard the person's machine: the build must keep every one of them.
GUARDS = ("O_NOFOLLOW", "visible(", "clean_field(", "tmux_text(", "tmux_dir(", "pane_guard(", "shell=True")


class BuildError(Exception):
    """The package can't be built into one file as it is; the message says where and why."""


def package_imports(tree: ast.Module, where: str, earlier: set[str]) -> dict[str, ast.Import]:
    """«import collab.x as x» at the top of a module, as alias → its node. Any other way of taking
    from the package is refused, and so is a module not earlier in layer order."""
    aliases: dict[str, ast.Import] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            raise BuildError(f"{where}:{node.lineno}: «from {node.module} import *» binds names no one sees — name them")
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").split(".")[0] == PACKAGE):
            raise BuildError(f"{where}:{node.lineno}: «from {node.module or '.'} import …» — only «import {PACKAGE}.x as x»")
        if not isinstance(node, ast.Import):
            continue
        for name in node.names:
            if name.name.split(".")[0] != PACKAGE:
                continue
            if node not in tree.body or len(node.names) != 1 or not name.asname:
                raise BuildError(
                    f"{where}:{node.lineno}: «import {name.name}» — one «import {PACKAGE}.x as x» per line, "
                    "at the top of the module"
                )
            if name.name not in earlier:
                raise BuildError(f"{where}:{node.lineno}: {name.name} is not a module below this one in layer order")
            # m_<module>, and m_ for nothing else: no local «state» or «log» is ever taken for a module.
            if name.asname != ALIAS_PREFIX + name.name.rsplit(".", 1)[-1]:
                raise BuildError(f"{where}:{node.lineno}: import {name.name} as {ALIAS_PREFIX}{name.name.rsplit('.', 1)[-1]}")
            aliases[name.asname] = node
    return aliases


def check_aliases(tree: ast.Module, aliases: dict[str, ast.Import], where: str) -> list[ast.Attribute]:
    """The «x.name» to rewrite. Refused: an alias also used as another name (a parameter, a local —
    the build would take it for the module), a bare alias, and «x.name = …» (after the build it would
    be a local of the function, not the other module's name)."""
    uses: list[ast.Attribute] = []
    as_value: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            if not isinstance(node.ctx, ast.Load):
                raise BuildError(
                    f"{where}:{node.lineno}: «{node.value.id}.{node.attr}» is assigned — a module's name "
                    "is changed only in that module"
                )
            if node.lineno != node.end_lineno:
                raise BuildError(f"{where}:{node.lineno}: «{node.value.id}.{node.attr}» spans lines")
            uses.append(node)
            as_value.add(id(node.value))
    for node in ast.walk(tree):
        named = []
        if isinstance(node, ast.Name) and (node.id in aliases or node.id.startswith(ALIAS_PREFIX)) and id(node) not in as_value:
            named.append(node.id)
        elif isinstance(node, ast.arg):
            named.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            named.append(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            named.append(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            named.extend(node.names)
        elif isinstance(node, ast.alias) and not any(node in imp.names for imp in aliases.values()):
            named.append(node.asname or node.name)
        clash = [n for n in named if n in aliases or n.startswith(ALIAS_PREFIX)]
        if clash:
            raise BuildError(
                f"{where}:{getattr(node, 'lineno', '?')}: «{clash[0]}» — the prefix {ALIAS_PREFIX} is only "
                "for the modules of the package"
            )
    return uses


def bound_names(tree: ast.Module) -> set[str]:
    """Every name a module binds anywhere: parameters, assignments and loop, with, except, comprehension
    targets, def and class, imports, global and nonlocal."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def check_members(
    tree: ast.Module, uses: list[ast.Attribute], aliases: dict[str, ast.Import], tops: dict[str, set[str]], where: str
) -> None:
    """«m_text.visible» becomes «visible»: refused if this module binds «visible» itself — a parameter or
    a local would be called instead of the other module's function, silently — and if
    the other module does not define it at its top level («m_text.open» would become the builtin open)."""
    own = bound_names(tree)
    for use in uses:
        alias = use.value.id if isinstance(use.value, ast.Name) else ""
        module = aliases[alias].names[0].name
        if use.attr in own:
            raise BuildError(
                f"{where}:{use.lineno}: «{use.attr}» is also a name here — after the build "
                f"«{alias}.{use.attr}» would mean this one; rename it"
            )
        if use.attr not in tops[module]:
            raise BuildError(f"{where}:{use.lineno}: {module} defines no «{use.attr}» at its top level")


def top_bindings(tree: ast.Module) -> dict[str, str]:
    """Every name a module binds at its top level, as name → what binds it: «import json» → «import json»
    (two modules importing the same thing do not clash), anything else → «def». Not only def and = in the
    body: under if, try and with too, loop variables, and a function's «global» (once
    the build let the last one win, and the one file answered otherwise than the package)."""
    out: dict[str, str] = {}

    def bind(name: str, what: str) -> None:
        if out.get(name, what) != what:
            what = "def"  # bound two ways in one module: not a plain shared import
        out[name] = what

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == PACKAGE:
                        continue  # the package's own import: gone in the build
                    bind((a.asname or a.name).split(".")[0], f"import {a.name}" if a.asname is None else f"import {a.name} as")
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    bind(a.asname or a.name, f"from {node.module} import {a.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, "def")
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Global):
                        for name in inner.names:
                            bind(name, "def")
            else:
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                        continue
                    if isinstance(inner, ast.Name) and not isinstance(inner.ctx, ast.Load):
                        bind(inner.id, "def")
                    elif isinstance(inner, ast.ExceptHandler) and inner.name:
                        bind(inner.name, "def")
                for field in ("body", "orelse", "finalbody", "handlers"):
                    block = getattr(node, field, None)
                    if isinstance(block, list):
                        visit(
                            [n for n in block if isinstance(n, ast.stmt)]
                            + [b for h in block if isinstance(h, ast.ExceptHandler) for b in h.body]
                        )

    visit(tree.body)
    return out


def free_names(source: bytes, where: str) -> dict[str, int]:
    """Names a module reads as its own globals: at the top, in a class body, or in a function without
    a local of that name — as Python's own symbol table counts them. Name → the scope's first line."""
    out: dict[str, int] = {}
    stack = [symtable.symtable(source.decode("utf-8"), where, "exec")]
    while stack:
        table = stack.pop()
        stack.extend(table.get_children())
        for sym in table.get_symbols():
            top = table.get_type() == "module"
            if sym.is_referenced() and (top and not sym.is_imported() or not top and sym.is_global()):
                out.setdefault(sym.get_name(), table.get_lineno())
    return out


def top_names(tree: ast.Module) -> set[str]:
    """Names a module binds at its top level."""
    return set(top_bindings(tree))


def rewrite(source: bytes, imports: list[ast.stmt], uses: list[ast.Attribute], where: str) -> bytes:
    """Edit by position: an import line → empty, «x.» cut from each use. ast counts columns in UTF-8
    bytes, so the edits are on bytes; right to left within a line, so one cut does not move the next."""
    lines = source.splitlines(keepends=True)
    cuts: dict[int, list[tuple[int, int]]] = {}
    for use in uses:
        cuts.setdefault(use.lineno - 1, []).append((use.col_offset, (use.end_col_offset or 0) - len(use.attr.encode())))
    for imp in imports:
        end_line, end_col = imp.end_lineno or imp.lineno, imp.end_col_offset or 0
        first, last = lines[imp.lineno - 1], lines[end_line - 1]
        if first[: imp.col_offset].strip() or last[end_col:].split(b"#")[0].strip():
            raise BuildError(f"{where}:{imp.lineno}: the import shares its line — put it on its own")
        for i in range(imp.lineno - 1, end_line):
            if b"#" in lines[i]:
                raise BuildError(f"{where}:{i + 1}: a comment on the import line would be lost — put it above")
            lines[i] = b"\n"
    for i, spans in cuts.items():
        text = lines[i]
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + text[end:]
        lines[i] = text
    return b"".join(lines)


def comments(source: bytes) -> list[str]:
    return [tok.string for tok in tokenize.tokenize(io.BytesIO(source).readline) if tok.type == tokenize.COMMENT]


def build(modules: list[tuple[str, Path]]) -> bytes:
    """The one file, from the modules in layer order — or BuildError."""
    parts: list[bytes] = []
    defined: dict[str, str] = {}
    all_aliases: set[str] = set()
    tops: dict[str, set[str]] = {}
    bindings: dict[str, str] = {}
    first_futures: list[str] = []
    for k, (name, path) in enumerate(modules):
        source = path.read_bytes()
        where = str(path)
        tree = ast.parse(source, filename=where)
        aliases = package_imports(tree, where, {n for n, _ in modules[:k]})
        all_aliases.update(aliases)
        uses = check_aliases(tree, aliases, where)
        check_members(tree, uses, aliases, tops, where)
        tops[name] = top_names(tree)
        if k < len(modules) - 1 and any(isinstance(node, ast.If) and "__main__" in ast.dump(node.test) for node in tree.body):
            raise BuildError(
                f"{where}: «if __name__ == '__main__'» only in the last module — in the one file it "
                "would run before the modules after it are defined"
            )
        # In the one file a module's own __name__, __doc__ and __file__ are the file's: __file__ is boot's (the
        # first), __name__ and __doc__ the entry's (the last) — elsewhere they would quietly mean another module.
        for dunder, allowed in (("__file__", k == 0), ("__name__", k == len(modules) - 1), ("__doc__", k == len(modules) - 1)):
            seen = next((n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == dunder), None)
            if seen is not None and not allowed:
                raise BuildError(f"{where}:{seen.lineno}: «{dunder}» here would be another module's in the one file")
        for top, what in sorted(top_bindings(tree).items()):
            if top in vars(builtins) and not top.startswith("__"):
                raise BuildError(f"{where}: «{top}» is a builtin — in the one file every module would get this one")
            if top in defined and not (what.startswith(("import ", "from ")) and what == bindings[top]):
                raise BuildError(f"{where}: «{top}» is defined in {defined[top]} too — one name, one definition")
            defined[top], bindings[top] = name, what
        # A name a module reads but binds nowhere: the package stops on it with NameError, and the one
        # file quietly gives it a later module's — a lower layer leaning on an upper one.
        own = set(top_bindings(tree)) | set(aliases)
        for free, line in sorted(free_names(source, where).items()):
            if free not in own and free not in vars(builtins) and free not in ("__file__", "__name__", "__doc__"):
                raise BuildError(f"{where}:{line}: «{free}» is read here but bound nowhere in this module")
        imports: list[ast.stmt] = list(aliases.values())
        # One «from __future__» for the whole file, the first module's: a later module's goes, so each
        # must ask the same — one without «annotations» under a first with it would get it unasked, and
        # one with it under a first without would lose it (X | None evaluated on 3.9).
        futures = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
        asked = sorted(a.name for node in futures for a in node.names)
        if k == 0:
            first_futures = asked
        elif asked != first_futures:
            raise BuildError(
                f"{where}: «from __future__» asks {asked or 'nothing'}, the first module {first_futures or 'nothing'}"
                " — in the one file only the first module's stays"
            )
        if k:
            imports += futures
        part = rewrite(source, imports, uses, where)
        left = sorted({n.id for n in ast.walk(ast.parse(part)) if isinstance(n, ast.Name) and n.id in aliases})
        if left:
            raise BuildError(f"{where}: still uses a module of the package after the build: {', '.join(left)}")
        parts.append(part if part.endswith(b"\n") or not part else part + b"\n")
    clash = sorted(all_aliases & set(defined))
    if clash:
        raise BuildError(f"«{clash[0]}» names both a module of the package and a name defined in {defined[clash[0]]}")
    out = b"".join(parts)
    check_output(out, [path.read_bytes() for _, path in modules])
    return out


def python39() -> str:
    """A real Python 3.9 here, or "": macOS's own /usr/bin/python3 is one, and the file must run on it
    long enough to hand over to 3.11+."""
    for candidate in (shutil.which("python3.9") or "", "/usr/bin/python3"):
        if candidate:
            try:
                version = subprocess.run(
                    [candidate, "-I", "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True, timeout=10
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if version == "(3, 9)":
                return candidate
    return ""


def grouped(text: bytes) -> bool:
    """«(a as x, b as y)» — a comma or an «as» inside the first parenthesis itself, not deeper."""
    depth = 0
    for i, ch in enumerate(text):
        depth += {40: 1, 41: -1, 91: 1, 93: -1, 123: 1, 125: -1}.get(ch, 0)  # ( ) [ ] { }
        if depth == 0:
            return False
        if depth == 1 and (ch == 44 or text[i : i + 4] == b" as "):  # «,» or « as »
            return True
    return True  # still open at the line's end: «with (» spanning lines is the grouped form


def newer_than_39(out: bytes) -> str:
    """What ast's feature_version=(3, 9) lets through, found without a 3.9: an f-string
    with its own quote inside (3.12), «with (a as x, b as y)». "" — none of them."""
    if sys.version_info >= (3, 12):
        quotes: list[str] = []
        for tok in tokenize.tokenize(io.BytesIO(out).readline):
            if tok.type == tokenize.FSTRING_START:  # type: ignore[attr-defined]
                quote = tok.string.lstrip("rfRFbB")
                if quotes and quote[0] == quotes[-1][0]:
                    return f"line {tok.start[0]}: an f-string inside an f-string with the same quote"
                quotes.append(quote)
            elif tok.type == tokenize.FSTRING_END:  # type: ignore[attr-defined]
                quotes.pop()
            elif tok.type == tokenize.STRING and quotes and tok.string.lstrip("rRbBuU")[0] == quotes[-1][0]:
                return f"line {tok.start[0]}: a string inside an f-string with the same quote"
    lines = out.splitlines()
    for node in ast.walk(ast.parse(out)):
        if isinstance(node, (ast.With, ast.AsyncWith)) and len(node.items) > 1:
            head = lines[node.lineno - 1][node.col_offset :]
            head = head[len(b"async") :].lstrip() if head.startswith(b"async") else head
            head = head[len(b"with") :].lstrip()
            if head.startswith(b"(") and grouped(head):
                return f"line {node.lineno}: «with (…)» around several items"
    return ""


def check_python39(out: bytes) -> None:
    """The built file reads on Python 3.9: compiled by a real 3.9 when there is one, else ast's check
    plus the forms it lets through."""
    try:
        ast.parse(out, feature_version=(3, 9))
    except SyntaxError as e:  # «match» and the like: a word, not a traceback
        raise BuildError(f"newer than Python 3.9 — line {e.lineno}: {e.msg}") from None
    old = python39()
    if old:
        run = subprocess.run(
            [old, "-I", "-c", "import sys; compile(sys.stdin.buffer.read(), 'collab', 'exec')"],
            input=out,
            capture_output=True,
            timeout=60,
        )
        if run.returncode:
            last = run.stderr.decode(errors="replace").strip().splitlines()[-1:]
            raise BuildError(f"Python 3.9 ({old}) can't read the built file: {last}")
        return
    newer = newer_than_39(out)
    if newer:
        raise BuildError(f"newer than Python 3.9 — {newer}")


def check_output(out: bytes, sources: list[bytes]) -> None:
    """What the build must keep: Python 3.9 syntax, every comment, every guard."""
    check_python39(out)
    kept = comments(out)
    for comment in (c for source in sources for c in comments(source)):
        if comment not in kept:
            raise BuildError(f"a comment was lost in the build: {comment[:60]!r}")
        kept.remove(comment)
    joined = b"".join(sources)
    for guard in GUARDS:
        if out.count(guard.encode()) != joined.count(guard.encode()):
            raise BuildError(f"the build changed the number of «{guard}»")


def write_whole(path: Path, data: bytes) -> None:
    """The file replaced at once, never rewritten in place: a test reading collab while it was built read
    half of it — a SyntaxError mid-line. Its mode (executable) stays."""
    tmp = path.with_name(f".{path.name}.build-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the one-file collab from the package.")
    parser.add_argument("--check", action="store_true", help="only compare the committed file with a fresh build")
    args = parser.parse_args()
    try:
        out = build(MODULES)
    except BuildError as exc:
        print(f"build refused: {exc}", file=sys.stderr)
        return 2
    if OUTPUT.read_bytes() == out:
        print(f"{OUTPUT.name}: up to date")
        return 0
    if args.check:
        print(f"{OUTPUT.name} is not the fresh build — run tools/build_collab.py and commit it", file=sys.stderr)
        return 1
    write_whole(OUTPUT, out)
    print(f"{OUTPUT.name}: built from {len(MODULES)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
