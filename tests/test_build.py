"""The build of the one file from the package (tools/build_collab.py): what it keeps
byte for byte, what it rewrites, and what it refuses. No tmux, no collab run — only the builder."""

import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader as _Loader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = _Loader("build_collab", str(ROOT / "tools" / "build_collab.py")).load_module()


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def package(self, **modules: str) -> list:
        """Modules in layer order, as the builder takes them: name → its text."""
        out = []
        for name, text in modules.items():
            path = self.dir / f"{name}.py"
            path.write_bytes(text.encode("utf-8"))
            out.append((f"collab.{name}", path))
        return out

    def refused(self, needle: str, **modules: str) -> None:
        with self.assertRaises(BUILD.BuildError) as caught:
            BUILD.build(self.package(**modules))
        self.assertIn(needle, str(caught.exception))

    def test_the_committed_file_is_the_fresh_build(self):
        # Rules 1.5: the file people and Codex run is never edited by hand.
        self.assertEqual(BUILD.build(BUILD.MODULES), BUILD.OUTPUT.read_bytes())

    def test_module_names_are_cut_by_position_in_bytes_and_everything_else_stays(self):
        # ast counts columns in UTF-8 bytes: cut by characters, «a.» after Cyrillic or an emoji left
        # the wrong letters. Comments carry the reasons for guards — none may go.
        text = "from __future__ import annotations\n# the text layer\ndef f(x):  # 😀 ↻ Привет\n    return x + 1\n"
        top = (
            "from __future__ import annotations\n"
            "import collab.text as m_text\n"
            "# keep this: the reason\n"
            's = "Привет ↻ 😀"; y = m_text.f(1) + m_text.f(2)  # guard\n'
            'z = [m_text.f(len(c)) for c in "日本"]\n'
        )
        out = BUILD.build(self.package(text=text, top=top))
        self.assertEqual(
            out.decode("utf-8"),
            text + '\n\n# keep this: the reason\ns = "Привет ↻ 😀"; y = f(1) + f(2)  # guard\nz = [f(len(c)) for c in "日本"]\n',
        )
        scope: dict = {}
        exec(compile(out, "built", "exec"), scope)
        self.assertEqual((scope["y"], scope["z"]), (5, [2, 2]))
        self.assertEqual(BUILD.build(self.package(text=text, top=top)), out, "the same input, the same bytes")

    def test_every_module_asks_the_same_future_as_the_first(self):
        # One «from __future__ import annotations» stays, the first module's. A later module
        # asking it while the first does not would lose it in the one file: «X | None» runs on 3.9.
        fut = "from __future__ import annotations\n"
        later = "import collab.a as m_a\ndef g(x: int | None) -> int:\n    return m_a.f()\n"
        self.refused("«from __future__» asks", a="def f():\n    return 1\n", b=fut + later)
        self.refused("«from __future__» asks", a=fut + "def f():\n    return 1\n", b=later)
        # The positive pair: both ask it — one stays at the top, the other is an empty line.
        out = BUILD.build(self.package(a=fut + "def f():\n    return 1\n", b=fut + later)).decode()
        self.assertEqual(out.count("from __future__"), 1)
        self.assertTrue(out.startswith(fut))

    def test_the_line_numbers_of_a_module_stay_its_own(self):
        # An import becomes an empty line, not a missing one: a traceback from the one file points
        # to the same line of the module.
        out = BUILD.build(self.package(a="def f():\n    return 1\n", b="import collab.a as m_a\nx = m_a.f()\nboom = 2\n"))
        lines = out.decode().splitlines()
        self.assertEqual(lines[2:], ["", "x = f()", "boom = 2"])

    def test_what_the_build_cannot_be_sure_of_is_refused(self):
        lib = "def f():\n    return 1\n"
        self.refused("only «import collab.x as x»", a=lib, b="from collab.a import f\n")
        self.refused("is only for the modules", a=lib, b="import collab.a as m_a\ndef g(m_a):\n    return m_a\n")
        self.refused("is only for the modules", a=lib, b="import collab.a as m_a\ndef g():\n    m_a = 1\n    return m_a\n")
        self.refused("is assigned", a=lib, b="import collab.a as m_a\nm_a.f = None\n")
        self.refused("is only for the modules", a=lib, b="import collab.a as m_a\nprint(m_a)\n")
        self.refused("not a module below this one", a="import collab.b as m_b\n", b=lib)
        self.refused("one name, one definition", a=lib, b=lib)
        self.refused("would be lost", a=lib, b="import collab.a as m_a  # why\nx = m_a.f()\n")
        self.refused("shares its line", a=lib, b="import collab.a as m_a; x = 1\n")
        self.refused(
            "is only for the modules", a="def m_b():\n    return 1\n", b="x = 1\n", c="import collab.b as m_b\ny = m_b.x\n"
        )
        # m_<module> and nothing else: a local «state» is never taken for the module.
        self.refused("import collab.a as m_a", a=lib, b="import collab.a as a\nx = a.f()\n")
        self.refused("is only for the modules", a="m_count = 1\n")
        # A parameter named like the other module's function — «m_text.visible(visible)»
        # became «visible(visible)», the parameter called instead of the cleaning, and the build went through.
        clean = 'def visible(s):\n    return "CLEAN:" + s\n'
        self.refused(
            "is also a name here",
            text=clean,
            deliver="import collab.text as m_text\ndef show(visible):\n    return m_text.visible(visible)\n",
        )
        self.refused(
            "is also a name here",
            text=clean,
            deliver="import collab.text as m_text\ndef show(s):\n    visible = s\n    return m_text.visible(visible)\n",
        )
        self.refused("defines no «open»", text=clean, deliver="import collab.text as m_text\nf = m_text.open\n")
        self.refused("only in the last module", a='def main():\n    pass\nif __name__ == "__main__":\n    main()\n', b="x = 1\n")
        self.refused("one «import collab.x as x» per line", a=lib, b="import collab.a\n")

    def test_top_bindings_are_what_python_itself_counts(self):
        # Count them as symtable does. Python's own table of the module's scope — assigned,
        # imported, and «global» from functions — is the measure; on collab itself and on the hard cases.
        import ast
        import symtable

        def python_counts(src: str) -> set:
            table = symtable.symtable(src, "m", "exec")
            names = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
            # 3.12 inlines the module's list, set and dict comprehensions into it (PEP 709) and counts
            # their variables there; 3.11 keeps each a table of its own. The build counts them, as 3.12 does.
            inlined = [c for c in table.get_children() if c.get_name() in ("listcomp", "setcomp", "dictcomp")]
            while inlined and sys.version_info < (3, 12):
                comp = inlined.pop()
                names |= {s.get_name() for s in comp.get_symbols() if s.is_assigned() and s.get_name() != ".0"}
                inlined.extend(c for c in comp.get_children() if c.get_name() in ("listcomp", "setcomp", "dictcomp"))
            stack = list(table.get_children())
            while stack:
                child = stack.pop()
                names |= {s.get_name() for s in child.get_symbols() if s.is_declared_global()}
                stack.extend(child.get_children())
            return names

        cases = [
            BUILD.OUTPUT.read_text(encoding="utf-8"),
            "try:\n    import json\nexcept ImportError:\n    json = None\nif True:\n    X = 1\n"
            "for i in range(2):\n    pass\nwith open(__file__) as fh:\n    pass\n"
            "def init():\n    global G\n    G = 1\nclass K:\n    inner = 1\n"
            "squares = [n * n for n in range(3)]\nimport os.path\n",
        ]
        for src in cases:
            self.assertEqual(set(BUILD.top_bindings(ast.parse(src))), python_counts(src))

    def test_without_python_3_9_the_known_newer_forms_are_still_refused(self):
        # On a machine with no 3.9 (Linux CI may have none) the build looks for what ast lets through.
        if __import__("sys").version_info >= (3, 12):
            self.assertIn("same quote", BUILD.newer_than_39(b'x = f"{"a"}"\n'))
        self.assertIn("several items", BUILD.newer_than_39(b"with (open(a) as x, open(b) as y):\n    pass\n"))
        for fine in (
            b"x = f\"{'a'}\"\n",
            b"with (open(a)) as x, open(b) as y:\n    pass\n",
            b"with open(a) as x, open(b) as y:\n    pass\n",
        ):
            self.assertEqual(BUILD.newer_than_39(fine), "", fine)

    def test_a_module_reads_its_own_name_doc_or_file_only_where_the_one_file_keeps_it(self):
        # In the one file __name__ and __doc__ are the entry's, __file__ is the whole file's.
        self.refused("would be another module's", a="x = __name__\n", b="y = 1\n")
        self.refused("would be another module's", a="d = __doc__\n", b="y = 1\n")
        self.refused("would be another module's", a="x = 1\n", b="p = __file__\n")
        BUILD.build(self.package(a="p = __file__\n", b="n = __name__\nd = __doc__\n"))  # the positive pair

    def test_a_name_read_but_bound_nowhere_in_the_module_is_refused(self):
        # A lower module read «later», bound only in an upper one — the package: NameError,
        # the one file: 5. Read at the top, in a function, in a class body.
        for lower in (
            "def g():\n    return later\n",
            "x = later\n",
            "class K:\n    v = later\n",
        ):
            self.refused("«later» is read here but bound nowhere", a=lower, b="import collab.a as m_a\nlater = 5\n")
        # The positive pair: its own, a builtin, a local, an import of its own, a module alias — all build.
        BUILD.build(
            self.package(
                a="import os\nlater = 1\ndef g(y):\n    z = len(os.sep)\n    return later + y + z\n",
                b="import collab.a as m_a\nr = m_a.g(1)\n",
            )
        )

    def test_two_modules_may_import_the_same_thing(self):
        # One binding for both in the one file — not a clash; a different binding of that name is.
        out = BUILD.build(
            self.package(
                a="import os\ndef sep():\n    return os.sep\n", b="import os\nimport collab.a as m_a\nr = m_a.sep() + os.sep\n"
            )
        )
        scope: dict = {}
        exec(compile(out, "built", "exec"), scope)
        self.assertEqual(scope["r"], "//")
        self.refused("one name, one definition", a="import os\n", b="import os.path as os\n")

    def test_a_comment_or_a_guard_lost_in_the_build_stops_it(self):
        sources = [b"x = 1  # guard\n", b"fd = os.open(p, os.O_NOFOLLOW)\n"]
        with self.assertRaises(BUILD.BuildError):
            BUILD.check_output(b"x = 1\n" + sources[1], sources)
        with self.assertRaises(BUILD.BuildError):
            BUILD.check_output(sources[0] + b"fd = os.open(p, 0)\n", sources)
        BUILD.check_output(b"".join(sources), sources)


class WriteTest(unittest.TestCase):
    def test_a_program_without_its_entry_point_is_not_built(self):
        # Such a file runs nothing and exits 0: the Codex copy made of one answered no say at all.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "repo"
        shutil.copytree(BUILD.ROOT / "src", root / "src")
        (root / "tools").mkdir()
        shutil.copy(BUILD.ROOT / "tools" / "build_collab.py", root / "tools")
        shutil.copy(BUILD.OUTPUT, root / "collab")
        cli = root / "src" / "collab" / "cli.py"
        cli.write_text(
            cli.read_text(encoding="utf-8").replace('\nif __name__ == "__main__":\n    main()\n', "\n"), encoding="utf-8"
        )
        run = subprocess.run([sys.executable, str(root / "tools" / "build_collab.py")], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("must end in", run.stderr)
        self.assertEqual((root / "collab").read_bytes(), BUILD.OUTPUT.read_bytes(), "the file people run is left as it was")

    def test_a_reader_of_collab_during_the_build_reads_a_whole_file(self):
        # A test read collab while it was being rebuilt in place — half a file, a
        # SyntaxError mid-line. Replaced at once, the file a reader opened stays whole; its mode stays.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "src", root / "src")
            (root / "tools").mkdir()
            shutil.copy2(ROOT / "tools" / "build_collab.py", root / "tools" / "build_collab.py")
            stale = BUILD.OUTPUT.read_bytes() + b"# stale\n"
            (root / "collab").write_bytes(stale)
            (root / "collab").chmod(0o755)
            with open(root / "collab", "rb") as reader:
                r = subprocess.run([sys.executable, str(root / "tools" / "build_collab.py")], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(reader.read(), stale, "what a reader opened before the build is still the whole old file")
            self.assertEqual((root / "collab").read_bytes(), BUILD.OUTPUT.read_bytes(), "the fresh build is there")
            self.assertEqual((root / "collab").stat().st_mode & 0o777, 0o755, "still executable")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["collab", "src", "tools"], "no temporary file left")


class BuildMeansThePackageTest(unittest.TestCase):
    """The one file does what the package does, or the build refuses. Each case below
    built and then broke or quietly answered otherwise: `x.name` → `name` meets another `name` of the
    place it lands in."""

    LIB = "def f():\n    return 1\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def write(self, **modules: str) -> list:
        self.dir = Path(tempfile.mkdtemp(dir=self.temp.name))  # a package of its own for each case of a test
        (self.dir / "collab").mkdir()
        (self.dir / "collab" / "__init__.py").write_text("")
        out = []
        for name, text in modules.items():
            path = self.dir / "collab" / f"{name}.py"
            path.write_text(text, encoding="utf-8")
            out.append((f"collab.{name}", path))
        return out

    def package_answer(self, last: str) -> str:
        """`r` of the last module, run as the real package."""
        code = f"import sys; sys.path.insert(0, sys.argv[1]); import collab.{last} as m; print(repr(m.r))"
        run = subprocess.run([sys.executable, "-I", "-c", code, str(self.dir)], capture_output=True, text=True)
        return run.stdout.strip() or run.stderr.strip().splitlines()[-1]

    def built_answer(self, out: bytes) -> str:
        scope: dict = {"__name__": "__main__"}
        try:
            exec(compile(out, "built", "exec"), scope)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return repr(scope["r"])

    def same_or_refused(self, **modules: str) -> None:
        mods = self.write(**modules)
        try:
            out = BUILD.build(mods)
        except BUILD.BuildError:
            return
        self.assertEqual(
            self.built_answer(out),
            self.package_answer(list(modules)[-1]),
            "built, and the one file answers otherwise than the package",
        )

    def test_a_local_name_where_the_module_name_lands(self):
        self.same_or_refused(a=self.LIB, b="import collab.a as m_a\ndef g():\n    f = m_a.f()\n    return f\nr = g()\n")

    def test_a_parameter_where_the_module_name_lands(self):
        self.same_or_refused(a=self.LIB, b="import collab.a as m_a\ndef g(f=0):\n    return m_a.f() + f\nr = g()\n")

    def test_a_comprehension_variable_where_the_module_name_lands(self):
        self.same_or_refused(a=self.LIB, b="import collab.a as m_a\nr = [m_a.f() for f in range(2)]\n")

    def test_a_class_attribute_where_the_module_name_lands(self):
        self.same_or_refused(a=self.LIB, b="import collab.a as m_a\nclass C:\n    f = 5\n    v = m_a.f()\nr = C.v\n")

    def test_a_name_the_module_does_not_have(self):
        self.same_or_refused(
            a=self.LIB, b="import collab.a as m_a\nr = m_a.g() if False else 0\ndef h():\n    return m_a.nothere()\n"
        )
        mods = (self.dir / "collab" / "a.py", self.dir / "collab" / "b.py")
        with self.assertRaises(BUILD.BuildError, msg="m_a.nothere() breaks only when called — the build must see it"):
            BUILD.build([("collab.a", mods[0]), ("collab.b", mods[1])])

    def test_an_import_in_one_module_and_a_definition_in_another(self):
        self.same_or_refused(
            a="import json\ndef g():\n    return json.dumps(1)\n",
            b="import collab.a as m_a\ndef json():\n    return 0\nr = m_a.g()\n",
        )

    def test_one_name_defined_under_a_top_level_try_or_if(self):
        # In the package each module keeps its own f; in one file the later one wins.
        self.same_or_refused(
            a="try:\n    def f():\n        return 1\nexcept Exception:\n    pass\ndef a_f():\n    return f()\n",
            b="import collab.a as m_a\ndef f():\n    return 2\nr = m_a.a_f()\n",
        )
        self.same_or_refused(
            a="if True:\n    X = 1\ndef a_x():\n    return X\n", b="import collab.a as m_a\nX = 2\nr = m_a.a_x()\n"
        )

    def test_a_global_set_from_a_function(self):
        self.same_or_refused(a="def init():\n    global G\n    G = 1\n", b="import collab.a as m_a\nG = 2\nm_a.init()\nr = G\n")

    def test_a_loop_variable_at_the_top(self):
        self.same_or_refused(
            a="for i in range(1):\n    pass\ndef a_i():\n    return i\n", b="import collab.a as m_a\ni = 5\nr = m_a.a_i()\n"
        )

    def test_a_builtin_name_defined_at_the_top_of_a_module(self):
        # In the package b's len is the builtin; in one file a's len is everybody's.
        self.same_or_refused(
            a="def len(x):\n    return 99\ndef g():\n    return 1\n", b="import collab.a as m_a\nr = len([1]) + m_a.g()\n"
        )

    def test_a_star_import(self):
        # «from os.path import *» binds names nobody sees in the source; in one file they land on a's.
        self.same_or_refused(
            a="def exists(p):\n    return 5\ndef g():\n    return exists('/')\n",
            b="from os.path import *\nimport collab.a as m_a\nr = m_a.g()\n",
        )

    def test_no_module_name_is_left_after_the_build(self):
        # Rules 1.5 (3): every «m_x.name» is rewritten — in a decorator, a default, a lambda, an f-string.
        mods = self.write(
            a="def f(x=None):\n    return 1\ndef deco(fn):\n    return fn\n",
            b="import collab.a as m_a\n@m_a.deco\ndef g(x: int = m_a.f()) -> int:\n"
            '    return (lambda: m_a.f())() + x + len(f"{m_a.f()}")\nr = g()\n',
        )
        out = BUILD.build(mods)
        left = [n.id for n in ast.walk(ast.parse(out)) if isinstance(n, ast.Name) and n.id.startswith("m_")]
        self.assertEqual(left, [])
        self.assertEqual(self.built_answer(out), self.package_answer("b"))
        self.assertEqual(self.built_answer(out), "3")

    def test_the_check_passes_what_is_the_same(self):
        # The positive pair: what the build may do it does, and the answers match.
        mods = self.write(
            a=self.LIB,
            b='import collab.a as m_a\ndef g(n=0):\n    return m_a.f() + n\nr = (g(), f"{m_a.f()}-{ m_a.f() }", f"{3:{m_a.f()}}")\n',
        )
        out = BUILD.build(mods)
        self.assertEqual(self.built_answer(out), "(1, '1-1', '3')")
        self.assertEqual(self.package_answer("b"), "(1, '1-1', '3')")


def python_3_9():
    """A real Python 3.9 — macOS has one at /usr/bin/python3 — or None."""
    for path in ("/usr/bin/python3", shutil.which("python3.9")):
        if (
            path
            and subprocess.run(
                [path, "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 9))"], capture_output=True
            ).returncode
            == 0
        ):
            return path
    return None


class OldPythonTest(unittest.TestCase):
    """The one file must compile on 3.9 as a whole: Python compiles the file before running the part
    that hands over to 3.11+. `ast.parse(feature_version=(3, 9))` is not that: it passes
    `with (a as x, b as y)` and the 3.12 f-string `f"{"a"}"`."""

    PY = python_3_9()

    def compiles(self, source: bytes) -> bool:
        code = "import sys; compile(sys.stdin.buffer.read(), 'collab', 'exec')"
        return subprocess.run([self.PY or "", "-I", "-c", code], input=source, capture_output=True).returncode == 0

    def test_a_module_with_newer_syntax_is_refused(self):
        # Any Python: «match» is 3.10, and build() itself must check what it made.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("def f(x):\n    match x:\n        case 1:\n            return 1\n", encoding="utf-8")
            with self.assertRaises(BUILD.BuildError):
                BUILD.build([("collab.a", path)])
            path.write_text("def f(x):\n    return 1 if x == 1 else None\n", encoding="utf-8")
            BUILD.build([("collab.a", path)])  # the positive pair: the same without «match» builds

    @unittest.skipUnless(PY, "no Python 3.9 here")
    def test_the_build_compiles_on_python_3_9(self):
        self.assertTrue(self.compiles(BUILD.build(BUILD.MODULES)))

    @unittest.skipUnless(PY, "no Python 3.9 here")
    def test_newer_syntax_is_refused_by_the_build(self):
        newer = b'x = f"{"a"}"\n'
        self.assertFalse(self.compiles(newer), "the positive pair: 3.9 refuses it")
        with self.assertRaises((BUILD.BuildError, SyntaxError)):
            BUILD.check_output(newer, [newer])


if __name__ == "__main__":
    unittest.main()


class LayersTest(unittest.TestCase):
    def test_the_builders_layers_are_the_packages_modules(self):
        self.assertEqual(
            sorted(p.stem for p in (ROOT / "src" / "collab").glob("*.py") if p.stem != "__init__"), sorted(BUILD.LAYERS)
        )
