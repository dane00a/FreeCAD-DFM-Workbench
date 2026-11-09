# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The machining engine's shape-level conventions, which it shares with
core/recognition: a per-shape cache keyed by fingerprint, configuration and
engine version, and a package that imports without FreeCAD."""

import os
import subprocess
import sys
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from freecad.DFM.core.analyzers.machining_analyzer import CONTEXT_KEY, MachiningAnalyzer
from freecad.DFM.core.machining._version import ENGINE_VERSION
from freecad.DFM.core.machining.cache import MACHINING_CACHE, MachiningCache
from freecad.DFM.core.machining.config import MachiningConfig
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _drilled_block(seam_deg: float = 0.0):
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    axis = gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0, 0, 1))
    if seam_deg:
        axis.Rotate(axis.Axis(), seam_deg * 3.141592653589793 / 180.0)
    hole = BRepPrimAPI_MakeCylinder(axis, 4.0, 22.0).Shape()
    return BRepAlgoAPI_Cut(box, hole).Shape()


def _context(shape, prefs=None):
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs=prefs or {}
    )
    return data[CONTEXT_KEY]


class TestMachiningCache(unittest.TestCase):
    def setUp(self):
        MACHINING_CACHE.clear()

    def tearDown(self):
        MACHINING_CACHE.clear()

    def test_the_same_part_is_recognised_once(self):
        shape = _drilled_block()
        first = _context(shape)
        again = _context(shape)
        # Recognised once: the copy handed back shares the graph and features.
        self.assertIs(again.graph, first.graph)
        self.assertIs(again.recognition, first.recognition)
        # A separately built copy of the same part is the same part.
        self.assertIs(_context(_drilled_block()).graph, first.graph)

    def test_a_caller_changing_its_context_does_not_change_the_next(self):
        shape = _drilled_block()
        first = _context(shape)
        first.config = MachiningConfig(machine_mode="5axis")
        self.assertEqual(_context(shape).config.machine_mode, "3axis")

    def test_a_turned_seam_is_a_different_part(self):
        # Same faces, same areas, same centroids: only the edges tell them
        # apart, and the context names edges.
        plain, turned = _drilled_block(), _drilled_block(seam_deg=37.0)
        self.assertNotEqual(
            MachiningCache.key(plain, MachiningConfig()),
            MachiningCache.key(turned, MachiningConfig()),
        )
        self.assertIsNot(_context(turned).graph, _context(plain).graph)

    def test_a_changed_configuration_misses(self):
        shape = _drilled_block()
        first = _context(shape)
        second = _context(shape, prefs={"MachiningMachineMode": "5axis"})
        self.assertIsNot(second.graph, first.graph)

    def test_the_engine_version_is_in_the_key(self):
        key = MachiningCache.key(_drilled_block(), MachiningConfig())
        self.assertEqual(key[-1], ENGINE_VERSION)

    def test_the_cache_is_bounded(self):
        cache = MachiningCache(maxsize=2)
        for i in range(3):
            cache.store(("k", i), object())
        self.assertEqual(len(cache), 2)
        self.assertIsNone(cache.lookup(("k", 0)))


class TestMachiningImportPurity(unittest.TestCase):
    """core/machining imports in a plain Python with no FreeCAD at all, as
    core/recognition does; the analyzer wrapping it is what needs FreeCAD."""

    def test_the_engine_imports_without_freecad(self):
        code = (
            "import sys, importlib, pkgutil\n"
            "import freecad.DFM.core.machining as m\n"
            "for info in pkgutil.walk_packages(m.__path__, m.__name__ + '.'):\n"
            "    importlib.import_module(info.name)\n"
            "sys.exit(1 if 'FreeCAD' in sys.modules or 'Part' in sys.modules else 0)\n"
        )
        env = dict(os.environ)
        # The repo root only: not tests/stubs, whose FreeCAD stand-in would
        # make any accidental import succeed.
        env["PYTHONPATH"] = ROOT
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
