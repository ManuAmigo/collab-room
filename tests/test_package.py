"""The package as the tests see it (tests/_package.py): on a small package made here, no collab, no tmux."""

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader, Package  # noqa: E402
from _seam import seam  # noqa: E402


class PackageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        name = f"pkg_{uuid.uuid4().hex[:8]}"
        root = Path(temp.name) / name
        root.mkdir()
        (root / "__init__.py").write_text("")
        (root / "text.py").write_text(
            "import os\nX = 1\nTYPED = {}\n\ndef visible(s):\n    return s\n\ndef shown(s):\n    return visible(s)\n"
        )
        (root / "store.py").write_text(
            f"import os\nimport {name}.text as m_text\n\ndef write(s):\n    return m_text.visible(s)\n"
        )
        (root / "twice.py").write_text("def other():\n    return 2\n")
        sys.path.insert(0, temp.name)
        self.addCleanup(sys.path.remove, temp.name)
        self.name, self.root = name, root
        self.mod = Package(importlib.import_module(name))
        self.text = importlib.import_module(f"{name}.text")
        self.store = importlib.import_module(f"{name}.store")

    def test_a_name_is_read_from_its_module_and_a_module_by_its_name(self):
        self.assertIs(self.mod.visible, self.text.visible)
        self.assertIs(self.mod.write, self.store.write)
        self.assertIs(self.mod.store, self.store)
        self.assertEqual(self.mod.write("a"), "a")

    def test_reading_is_live_and_a_dict_is_the_owners(self):
        setattr(self.text, "X", 5)  # noqa: B010 — a module the test made; ModuleType has no X for the type checker
        self.assertEqual(self.mod.X, 5, "read at the moment, not a copy from the start")
        self.mod.TYPED["kodi"] = [1]
        self.assertEqual(self.text.TYPED, {"kodi": [1]})

    def test_the_same_object_in_several_modules_is_read(self):
        import os

        self.assertIs(self.mod.os, os)

    def test_a_name_nowhere_or_different_in_two_is_an_error(self):
        with self.assertRaises(AttributeError):
            _ = self.mod.nothing_here
        (self.root / "dup.py").write_text("def visible(s):\n    return 'dup'\n")
        mod = Package(importlib.import_module(self.name))
        with self.assertRaises(AttributeError) as said:
            _ = mod.visible
        self.assertIn("dup", str(said.exception))
        self.assertIn("text", str(said.exception))

    def test_nothing_is_set_through_it(self):
        with self.assertRaises(AttributeError):
            setattr(self.mod, "visible", lambda s: "fake")  # noqa: B010 — the assignment under test
        self.assertEqual(self.store.write("a"), "a", "the module is untouched")

    def test_seam_through_it_reaches_the_callers_and_is_undone(self):
        with seam(self.mod, "text.visible", lambda s: "fake"):
            self.assertEqual(self.store.write("a"), "fake", "store calls m_text.visible — the replaced one")
            self.assertEqual(self.mod.shown("a"), "fake", "and text's own call of its name")
        self.assertEqual(self.store.write("a"), "a")
        with self.assertRaises(RuntimeError), seam(self.mod, "text.visible", lambda s: "fake"):
            raise RuntimeError("inside")
        self.assertEqual(self.store.write("a"), "a", "undone after an exception too")

    def test_seam_through_it_is_strict(self):
        # A module not there is an error, not skipped as on the single file; a bare name is not replaced
        # on the package object — callers would never see it.
        with self.assertRaises(AttributeError):
            seam(self.mod, "txt.visible", lambda s: "fake")
        with self.assertRaises(AttributeError):
            with seam(self.mod, "visible", lambda s: "fake"):
                pass
        self.assertEqual(self.store.write("a"), "a")


class LoaderTest(unittest.TestCase):
    """The tests' one switch: the file by default, the package — fresh for each load — with COLLAB_TEST_PACKAGE."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.src = Path(temp.name) / "src"
        (self.src / "collab").mkdir(parents=True)
        (self.src / "collab" / "__init__.py").write_text("")
        (self.src / "collab" / "text.py").write_text('X = 1\nTYPED = {}\nROOM = {"root": None}\n\ndef room():\n    return ROOM\n')
        self.file = Path(temp.name) / "collab_file.py"
        self.file.write_text("X = 7\n")
        self.addCleanup(self.forget)

    def forget(self):
        for key in [k for k in sys.modules if k == "collab" or k.startswith("collab.")]:
            del sys.modules[key]
        if str(self.src) in sys.path:
            sys.path.remove(str(self.src))

    def test_without_the_switch_it_is_the_file(self):
        with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": ""}):
            mod = Loader(f"f_{uuid.uuid4().hex[:6]}", str(self.file)).load_module()
        self.assertNotIsInstance(mod, Package)
        self.assertEqual(mod.X, 7)

    def test_with_it_the_package_afresh_for_each_load(self):
        with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": str(self.src)}):
            first = Loader("x", str(self.file)).load_module()
            self.assertIsInstance(first, Package)
            self.assertEqual(first.X, 1)
            setattr(first.text, "X", 2)  # noqa: B010 — a module the test made
            second = Loader("y", str(self.file)).load_module()
        self.assertEqual(second.X, 1, "a fresh package, as each test gets a fresh module")
        self.assertIsNot(second.text, first.text)

    def test_the_switch_outlives_the_isolation(self):
        # tests/_isolation.py drops every COLLAB_* a room's agent may have; this one is the suite's own.
        import subprocess

        code = "import os, sys; sys.path.insert(0, 'tests'); import _isolation; print(os.environ.get('COLLAB_TEST_PACKAGE'), os.environ.get('COLLAB_ROOT'))"
        env = {**os.environ, "COLLAB_TEST_PACKAGE": "1", "COLLAB_ROOT": "/some/room"}
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, env=env, capture_output=True, text=True
        )
        self.assertEqual(out.stdout.split(), ["1", "None"], out.stderr)

    def test_a_run_on_the_package_says_so_at_its_start(self):
        import subprocess

        code = "import sys; sys.path.insert(0, 'tests'); import _isolation, _package"
        root = Path(__file__).resolve().parent.parent
        on = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env={**os.environ, "COLLAB_TEST_PACKAGE": "1"}, capture_output=True, text=True
        )
        self.assertIn(f"package: {root / 'src'}", on.stderr)
        off = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env={**os.environ, "COLLAB_TEST_PACKAGE": ""}, capture_output=True, text=True
        )
        self.assertNotIn("package:", off.stderr, "the positive pair: on the file, nothing said")

    def test_package_calls_out_to_the_built_executable_and_restores_the_fixture(self):
        # Package functions call the same executable as subprocess tests, never a copied boot.py.
        import gc

        boot = self.src / "collab" / "boot.py"
        boot.write_text("SELF = __file__\n\ndef executable():\n    return SELF\n")
        with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": str(self.src)}):
            mod = Loader("x", str(self.file)).load_module()
        owner = mod.boot
        self.assertEqual(mod.executable(), str(self.file.resolve()))
        del mod
        gc.collect()
        self.assertEqual(owner.SELF, str(boot), "fixture is undone when this package is released")

    def test_two_loads_share_no_state(self):
        # TYPED, room(), the locks — what one test sets, the next one's collab never sees.
        with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": str(self.src)}):
            first = Loader("x", str(self.file)).load_module()
            first.TYPED["kodi"] = [1]
            first.room()["root"] = "/room/a"
            second = Loader("y", str(self.file)).load_module()
        self.assertEqual((second.TYPED, second.room()), ({}, {"root": None}))
        self.assertEqual(first.TYPED, {"kodi": [1]}, "the first keeps its own, it is not cleared")

    def test_switching_package_sources_back_loads_the_selected_source_afresh(self):
        other = self.src.parent / "other"
        (other / "collab").mkdir(parents=True)
        (other / "collab" / "__init__.py").write_text("")
        (other / "collab" / "text.py").write_text("X = 2\n")
        self.addCleanup(lambda: sys.path.remove(str(other)) if str(other) in sys.path else None)
        with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": str(self.src)}):
            first = Loader("a", str(self.file)).load_module()
            self.assertEqual(first.X, 1)
            first.TYPED["probe"] = [1]
            with mock.patch.dict(os.environ, {"COLLAB_TEST_PACKAGE": str(other)}):
                second = Loader("b", str(self.file)).load_module()
                self.assertEqual(second.X, 2)
            third = Loader("a_again", str(self.file)).load_module()
        self.assertEqual(third.X, 1, "returning to A must not load B left earlier on sys.path")
        self.assertIsNot(third.text, first.text)
        self.assertEqual(third.TYPED, {}, "returning to A loads fresh state")
        self.assertEqual(first.TYPED, {"probe": [1]}, "the first load keeps its own state")
        self.assertEqual(second.X, 2, "the other source remains independent")


if __name__ == "__main__":
    unittest.main()
