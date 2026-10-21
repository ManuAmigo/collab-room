"""Rules of the code checked by reading it: imports keep `seam()` working, `seam()` itself refuses a name
that is not there, no module global is reassigned, and tmux never reads someone else's text as a format."""

import ast
import dataclasses
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _seam import seam, seam_paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

# Modules `seam()` replaces names in (other programs, time, opening and replacing files):
# `from` of them is refused, any other `from` of the standard library is the code's own choice.
SEAMED = {"subprocess", "time", "os", "os.path", "shutil", "tempfile"}


def sources(where):
    """The program: the package (src/collab) once it is there, the `collab` file, scripts."""
    return sorted((where / "src" / "collab").rglob("*.py")) + [where / "collab"] + sorted((where / "scripts").glob("*.py"))


def test_files():
    # Not this file: it holds the rules' own examples, not tests of the program.
    return [f for f in sorted(TESTS.glob("test_*.py")) if f.name != Path(__file__).name]


# A name a function reassigns with «global» — and why it still is one. What changes after the
# start is a field of one object (room(), paths()), never a reassigned module global.
GLOBAL_OK: dict = {}  # none: the chat's IN_CHAT and WARN_SINK became chat_mode()'s fields


def reassigned_globals(tree) -> dict:
    out: dict = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Global):
                    for name in node.names:
                        out.setdefault(name, set()).add(fn.name)
    return out


class GlobalsTest(unittest.TestCase):
    def test_no_module_global_is_reassigned(self):
        found = {}
        for f in sources(ROOT):
            found.update(reassigned_globals(ast.parse(f.read_bytes())))
        extra = {n: sorted(fns) for n, fns in found.items() if n not in GLOBAL_OK}
        self.assertEqual(extra, {}, "a field of room() or paths(), or GLOBAL_OK with why")
        self.assertEqual(sorted(set(GLOBAL_OK) - set(found)), [], "GLOBAL_OK to clean")

    def test_the_check_sees_a_global(self):
        tree = ast.parse("X = 1\ndef use(p):\n    global X\n    X = p\n")
        self.assertEqual(reassigned_globals(tree), {"X": {"use"}})


def called(c) -> str:
    """The function a call names: «f(…)», and on the package «m_x.f(…)» too."""
    if isinstance(c.func, ast.Name):
        return c.func.id
    if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) and c.func.value.id.startswith("m_"):
        return c.func.attr
    return ""


def room_changers(trees) -> set:
    """use_room and every function that calls it, through any depth — across all the modules at once:
    on the package cmd_home (cli) changes the room through m_lifecycle.cmd_start."""
    calls: dict = {}
    for tree in trees:
        for f in ast.walk(tree):
            if isinstance(f, ast.FunctionDef):
                calls.setdefault(f.name, set()).update(called(c) for c in ast.walk(f) if isinstance(c, ast.Call))
    change = {"use_room"}
    while more := {n for n, c in calls.items() if c & change} - change:
        change |= more
    return change


def stale_room_reads(tree, change=None) -> list:
    """(function, line) where rp — the room's set read once — is read after a call that changes the room
    (use_room, or what calls it) and before it is read again (once: cmd_home)."""
    fns = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    change = room_changers([tree]) if change is None else change
    out = []
    for name, fn in fns.items():
        names = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "rp"]
        for block in [
            b
            for node in ast.walk(fn)
            for b in (getattr(node, "body", None), getattr(node, "orelse", None))
            if isinstance(b, list)
        ]:
            for i, stmt in enumerate(block):
                if not isinstance(stmt, ast.Expr) and not isinstance(stmt, ast.Assign):
                    continue
                if not any(isinstance(c, ast.Call) and called(c) in change - {name} for c in ast.walk(stmt)):
                    continue
                if i + 1 < len(block) and isinstance(block[i + 1], ast.Return):
                    continue
                after = sorted((n.lineno, isinstance(n.ctx, ast.Store)) for n in names if n.lineno > stmt.end_lineno)
                if after and not after[0][1]:
                    out.append((name, after[0][0]))
    return out


class StaleRoomTest(unittest.TestCase):
    def test_the_rooms_set_is_read_again_after_the_room_changes(self):
        trees = {f: ast.parse(f.read_bytes()) for f in sources(ROOT)}
        change = room_changers(trees.values())
        found = [(f.name, *x) for f, tree in trees.items() for x in stale_room_reads(tree, change)]
        self.assertEqual(found, [], "rp = room().paths again after use_room / move_room_home / cmd_start")

    def test_the_check_sees_a_stale_read_across_modules(self):
        # On the package the room changes through another module: cli's cmd_home calls m_lifecycle.cmd_start,
        # which calls use_room. One module alone does not know cmd_start changes the room.
        low = ast.parse("def use_room(p): pass\ndef cmd_start(f):\n    use_room(f)\n")
        high = ast.parse("def home(f):\n    rp = room().paths\n    m_low.cmd_start(f)\n    print(rp.root)\n")
        self.assertEqual(stale_room_reads(high, room_changers([low, high])), [("home", 4)])
        self.assertEqual(stale_room_reads(high), [])

    def test_the_check_sees_a_stale_read(self):
        bad = "def use_room(p): pass\ndef home(f):\n    rp = room().paths\n    use_room(f)\n    print(rp.root)\n"
        good = bad.replace("    use_room(f)\n", "    use_room(f)\n    rp = room().paths\n")
        self.assertEqual(stale_room_reads(ast.parse(bad)), [("home", 5)])
        self.assertEqual(stale_room_reads(ast.parse(good)), [])


# What tmux reads as a format, not as text: «-T» (a pane's or a popup's title) and these options. Someone
# else's text there runs: an agent named «x #(touch f)» ran the command at the next start on tmux 3.4.
# Such an argument is a constant or goes through tmux_text().
FORMAT_ARGS = {
    "-T",
    "set-titles-string",
    "status-left",
    "status-right",
    "status-format",
    "pane-border-format",
    "window-status-format",
    "window-status-current-format",
}
FORMAT_OK = {
    # Keyed by the function and the expression: the same name anywhere else is not let through.
    "view_options:agent_program(entry)": "one of three fixed names: Claude Code, Codex, CollabRoom",
    "view_options:back": "the hint: every caller passes a constant (test_view_options_gets_a_constant_hint)",
    "view_options:right": "status-format[0]'s width: columns built of constants in the same function",
    "room_options:right": "status-format[0]'s width: columns built of constants in the same function",
}


def module_constants(tree) -> set:
    return {
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        for t in n.targets
        if isinstance(t, ast.Name)
    }


def format_safe(node, constants: set, function: str) -> bool:
    if isinstance(node, ast.Constant) or (isinstance(node, ast.Name) and node.id in constants):
        return True
    if isinstance(node, ast.Call) and called(node) == "tmux_text":
        return True
    if isinstance(node, ast.FormattedValue):
        return format_safe(node.value, constants, function)
    if isinstance(node, ast.JoinedStr):
        return all(format_safe(v, constants, function) for v in node.values)
    return f"{function}:{ast.unparse(node)}" in FORMAT_OK


def unsafe_formats(tree) -> list:
    """(line, the format argument, what is put there) for every argument tmux reads as a format and gets
    neither a constant nor tmux_text(): in a call's arguments and in a tuple or list built for a chain."""
    constants, out = module_constants(tree), []
    function: dict = {}
    for fn in sorted((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)), key=lambda f: f.lineno):
        for n in ast.walk(fn):  # the innermost function is the last to write
            function[id(n)] = fn.name
    for node in ast.walk(tree):
        seq = node.args if isinstance(node, ast.Call) else node.elts if isinstance(node, (ast.Tuple, ast.List)) else []
        for el, value in zip(seq, seq[1:]):
            # «status-format[0]» is status-format too: by its name before «[».
            option = el.value if isinstance(el, ast.Constant) and isinstance(el.value, str) else ""
            if option.split("[")[0] in FORMAT_ARGS and not format_safe(value, constants, function.get(id(node), "")):
                out.append((el.lineno, option, ast.unparse(value)))
    return out


class TmuxFormatTest(unittest.TestCase):
    def test_no_one_elses_text_where_tmux_reads_a_format(self):
        found = [(f.name, *u) for f in sources(ROOT) for u in unsafe_formats(ast.parse(f.read_bytes()))]
        self.assertEqual(found, [], "tmux_text() around it, or FORMAT_OK with why")

    def test_view_options_gets_a_constant_hint(self):
        # FORMAT_OK's «back» holds only while every caller passes a constant.
        tree = ast.parse((ROOT / "collab").read_bytes())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "view_options"]
        self.assertTrue(calls)
        self.assertEqual([ast.unparse(c.args[3]) for c in calls if not isinstance(c.args[3], ast.Constant)], [])

    def test_the_check_sees_a_name_put_in_a_title(self):
        # The positive pair: text, tmux_text() and constants pass; a label does not.
        tree = ast.parse(
            'K = "keys"\n'
            'tmux("select-pane", "-t", pane, "-T", label)\n'
            'tmux("select-pane", "-t", pane, "-T", tmux_text(label))\n'
            'chain = ["bind-key", "-T", K, "set-titles-string", f"{tmux_text(name)} · x", "status-left", name]\n'
            'opt = ("status-format[0]", f"#[align=left]{label}")\n'
            "def view_options(right):\n"
            '    return ("status-format[0]", f"#{{T:{right}}}")\n'
            "def elsewhere(right):\n"
            '    return ("status-format[0]", f"#{{T:{right}}}")\n'
        )
        found = [(line, arg) for line, arg, _ in unsafe_formats(tree)]
        # view_options' right is let through; the same right in another function is not.
        self.assertEqual(found, [(2, "-T"), (4, "status-left"), (5, "status-format[0]"), (9, "status-format[0]")])


def collab_names(tree) -> set:
    """Names a test holds collab by: given _Loader(…, str(COLLAB)).load_module() or _collab_at_default_places()."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.value, ast.Call):
            value, func = ast.unparse(n.value), n.value.func
            loaded = isinstance(func, ast.Attribute) and func.attr == "load_module" and "COLLAB" in value
            if loaded or (isinstance(func, ast.Attribute) and func.attr == "_collab_at_default_places"):
                out.add(ast.unparse(n.targets[0]))
    return out


def replaced_past_seam(tree) -> list:
    """(line, what) for each replacement in collab made otherwise than through seam(): patch.object of it or of a
    module seen through it, or an assignment to one of its names. On the package such a replacement misses the
    callers, who look the name up in their own module, and the test stays green checking nothing."""
    names, out = collab_names(tree), []

    def held(node) -> bool:
        while isinstance(node, ast.Attribute):
            node = node.value
        return ast.unparse(node) in names or (isinstance(node, ast.Attribute) and ast.unparse(node) in names)

    def names_of(node) -> bool:
        """vars(<collab>) or <collab>.__dict__: the namespace itself."""
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "vars" and node.args:
            return ast.unparse(node.args[0]) in names or held(node.args[0])
        return (
            isinstance(node, ast.Attribute) and node.attr == "__dict__" and (ast.unparse(node.value) in names or held(node.value))
        )

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "object" and n.args:
            base = n.args[0]
            if ast.unparse(base) in names or (isinstance(base, ast.Attribute) and held(base)):
                out.append((n.lineno, ast.unparse(n)[:60]))
        elif isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("setattr", "delattr") and n.args:
            # setattr(mod, name, value) in a loop over names
            if ast.unparse(n.args[0]) in names or held(n.args[0]):
                out.append((n.lineno, ast.unparse(n)[:60]))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "update":
            if names_of(n.func.value):  # vars(mod).update(…), mod.__dict__.update(…)
                out.append((n.lineno, ast.unparse(n)[:60]))
        elif isinstance(n, (ast.Assign, ast.AugAssign)):
            for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
                if isinstance(t, ast.Attribute) and (ast.unparse(t.value) in names or held(t.value)):
                    out.append((n.lineno, ast.unparse(t)))
                elif isinstance(t, ast.Subscript) and names_of(t.value):  # mod.__dict__["x"] = …
                    out.append((n.lineno, ast.unparse(t)))
    return out


class SeamOnlyTest(unittest.TestCase):
    def test_tests_replace_collab_only_through_seam(self):
        found = [(f.name, *r) for f in test_files() for r in replaced_past_seam(ast.parse(f.read_bytes()))]
        self.assertEqual(found, [], 'seam(mod, "name", new) — or self.enterContext(seam(…)) for the rest of the test')

    def test_the_check_sees_each_way_past_seam(self):
        # The positive pair: the three ways past seam() are caught; seam(), a field of an object
        # collab gives (mod.room().paths), a dict's item and another module are not.
        tree = ast.parse(
            "mod = _Loader('m', str(COLLAB)).load_module()\n"
            "other = _Loader('o', 'scripts/x.py').load_module()\n"
            "mock.patch.object(mod, 'tmux', f)\n"
            "mock.patch.object(mod.os, 'replace', f)\n"
            "mod.tmux = f\n"
            "mod.shutil.which = f\n"
            "seam(mod, 'tmux', f)\n"
            "mod.TYPED['x'] = []\n"
            "mod.room().paths.root.write_text('x')\n"
            "mock.patch.object(other, 'tmux', f)\n"
            "other.tmux = f\n"
            "setattr(mod, 'tmux', f)\n"
            "vars(mod).update(tmux=f)\n"
            "mod.__dict__.update(tmux=f)\n"
            "mod.__dict__['tmux'] = f\n"
            "setattr(other, 'tmux', f)\n"
            "vars(other).update(tmux=f)\n"
            "{}.update(tmux=f)\n"
        )
        self.assertEqual(sorted(line for line, _ in replaced_past_seam(tree)), [3, 4, 5, 6, 12, 13, 14, 15])


class ImportsTest(unittest.TestCase):
    def imports_from(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return [(n.lineno, n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]

    def test_no_from_import_of_the_package(self):
        # `from collab.x import y` copies y: a replacement of collab.x.y in a test misses this caller,
        # and the build cannot tell the name from a local one. Only `import collab.x as x`.
        bad = [
            (f.name, line, m)
            for f in sources(ROOT)
            for line, m in self.imports_from(f)
            if m == "collab" or m.startswith("collab.")
        ]
        self.assertFalse(bad)

    def test_what_seam_replaces_is_not_taken_with_from(self):
        # `from subprocess import run` would keep its own run when a test replaces subprocess.run.
        bad = [(f.name, line, m) for f in sources(ROOT) for line, m in self.imports_from(f) if m in SEAMED]
        self.assertFalse(bad, "import the module and call module.name")

    def test_the_check_sees_a_from_import(self):
        tree = ast.parse("from subprocess import run\nimport collab.tmuxio as tmuxio\nfrom collab.log import read\n")
        found = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertEqual(found, ["subprocess", "collab.log"])


class SeamTest(unittest.TestCase):
    def setUp(self):
        self.tmuxio = types.SimpleNamespace(tmux=lambda: "real")
        # As the package: collab.tmuxio.tmux; as the single file: everything in one module.
        self.package = types.ModuleType("collab")
        self.package.__path__ = []  # what makes a module a package
        setattr(self.package, "tmuxio", self.tmuxio)  # noqa: B010 — ModuleType has no such attribute for the type checker
        self.single = types.SimpleNamespace(tmux=lambda: "real")

    def test_reaches_the_module_on_the_package_and_the_single_file(self):
        with seam(self.package, "tmuxio.tmux", lambda: "fake"):
            self.assertEqual(self.tmuxio.tmux(), "fake")
        self.assertEqual(self.tmuxio.tmux(), "real")
        with seam(self.single, "tmuxio.tmux", lambda: "fake"):
            self.assertEqual(self.single.tmux(), "fake")
        self.assertEqual(self.single.tmux(), "real")

    def test_a_module_missing_from_the_package_fails(self):
        # tmuxio renamed tmux_io, and the root still has a tmux: the root's would be replaced, the
        # callers in tmux_io would keep the real one, and the test would check nothing.
        package = types.ModuleType("collab")
        package.__path__ = []
        setattr(package, "tmux_io", self.tmuxio)  # noqa: B010
        setattr(package, "tmux", lambda: "root")  # noqa: B010
        with self.assertRaises(AttributeError):
            seam(package, "tmuxio.tmux", lambda: "fake")
        self.assertEqual(getattr(package, "tmux")(), "root")  # noqa: B009

    def test_a_name_not_there_fails_instead_of_being_added(self):
        with self.assertRaises(AttributeError):
            seam(self.single, "tmuxio.tmux_renamed", lambda: "fake")
        self.assertFalse(hasattr(self.single, "tmux_renamed"))

    def test_seam_paths_swaps_the_frozen_set_and_puts_it_back(self):
        @dataclasses.dataclass(frozen=True)
        class Paths:
            safe_copies: tuple = ("/a",)
            installed_file: str = "/x"

        holder = types.SimpleNamespace(paths=Paths())
        mod = types.SimpleNamespace(install=lambda: holder, paths=lambda: holder.paths)
        before = holder.paths
        with seam_paths(mod, safe_copies=("/b",)):
            self.assertEqual((mod.paths().safe_copies, mod.paths().installed_file), (("/b",), "/x"))
        self.assertIs(mod.paths(), before)
        with self.assertRaises(TypeError):
            seam_paths(mod, no_such_field=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(before, "safe_copies", ("/c",))  # noqa: B010 — plain assignment is a type error on a frozen class

    def test_on_collab_itself(self):
        from importlib.machinery import SourceFileLoader

        mod = SourceFileLoader("collab_seam_check", str(ROOT / "collab")).load_module()
        with seam(mod, "tmuxio.tmux", lambda *_a, **_k: "fake"):
            self.assertEqual(mod.tmux("list-panes"), "fake")
        self.assertIsNot(mod.tmux, None)
        with self.assertRaises(AttributeError):
            seam(mod, "no_such_function", None)


if __name__ == "__main__":
    unittest.main()
