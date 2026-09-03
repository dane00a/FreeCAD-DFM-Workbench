# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The walls the planar pass cannot see.

Two flat faces fronting each other is the obvious shape of a wall and not the
only one. A drill leaves a ligament between its bore and whatever it ran
alongside; two drills close together leave a web; two faces leaning towards
each other pinch a wedge. None of those is a pair of opposed planes, and the
first two are not planar at all.

Each fixture here is built so the right answer is not in doubt: one dimension
is set to a known number and everything else is generous.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeWedge,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


RULE = Rulebook.THIN_WALL


def _cut(a, b):
    return BRepAlgoAPI_Cut(a, b).Shape()


def _fuse(a, b):
    return BRepAlgoAPI_Fuse(a, b).Shape()


def _box(a, b):
    return BRepPrimAPI_MakeBox(gp_Pnt(*a), gp_Pnt(*b)).Shape()


def _drill(radius, height, at, direction=(0.0, 0.0, 1.0)):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*at), gp_Dir(*direction)), radius, height
    ).Shape()


def findings(shape, target="1.5", limit="0.8"):
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs={}
    )
    return get_check_class(RULE)().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity="WARNING"),
        RULE,
        feedback=RuleFeedback(),
    )


def thicknesses(results):
    return sorted(round(r.value, 2) for r in results)


def block(size=(60.0, 60.0, 40.0)):
    return _box((0.0, 0.0, 0.0), size)


class BoreAgainstAWallTests(unittest.TestCase):
    """A hole drilled close to a face."""

    @staticmethod
    def _near_the_outside(gap: float):
        """A bore whose ligament to the part's own outer face is `gap`."""
        radius = 5.0
        return _cut(block(), _drill(radius, 60.0, (60.0 - gap - radius, 30.0, -10.0)))

    @staticmethod
    def _near_an_inside_face(gap: float):
        """The same ligament, but to a face cut into the part."""
        radius = 5.0
        stepped = _cut(
            block((80.0, 60.0, 40.0)), _box((40.0, -1.0, 20.0), (81.0, 61.0, 41.0))
        )
        return _cut(stepped, _drill(radius, 60.0, (40.0 - gap - radius, 30.0, -10.0)))

    def test_a_bore_close_to_an_inside_face_is_reported(self):
        results = findings(self._near_an_inside_face(1.0))
        self.assertTrue(results, "a 1 mm ligament is a thin wall")
        self.assertIn(1.0, thicknesses(results))

    def test_the_measurement_is_the_ligament_not_the_centre_distance(self):
        results = findings(self._near_an_inside_face(1.0))
        self.assertNotIn(6.0, thicknesses(results), "measured from the axis")

    def test_a_bore_with_room_around_it_is_not_reported(self):
        self.assertEqual(findings(self._near_an_inside_face(6.0)), [])

    def test_severity_follows_the_ligament(self):
        inside = findings(self._near_an_inside_face(0.5))
        self.assertIn(Severity.ERROR, [r.severity for r in inside])
        warned = findings(self._near_an_inside_face(1.2))
        self.assertEqual([r.severity for r in warned], [Severity.WARNING])

    def test_a_hole_near_the_outside_is_left_to_the_edge_rule(self):
        """Two rules can see the same ligament; only one should speak.

        A bore close to the outside of the part is what the edge-distance
        rule measures, and it says it better -- it knows the number a shop
        actually calls out. But that rule has no way to call anything
        critical, so below the error floor this one keeps it.
        """
        self.assertEqual(findings(self._near_the_outside(1.0)), [])
        self.assertEqual(findings(self._near_the_outside(1.4)), [])
        critical = findings(self._near_the_outside(0.5))
        self.assertTrue(critical, "nothing else can call this critical")
        self.assertEqual([r.severity for r in critical], [Severity.ERROR])

    def test_the_face_a_drill_breaks_through_is_not_a_wall(self):
        """Every through hole is zero distance from its own entry and exit."""
        self.assertEqual(findings(self._near_an_inside_face(6.0)), [])

    def test_a_plain_block_has_no_bore_walls(self):
        self.assertEqual(findings(block()), [])


class BoreAgainstBoreTests(unittest.TestCase):
    """Two holes drilled close together."""

    @staticmethod
    def _two_bores(web: float):
        """Two bores whose nearest surfaces are `web` apart.

        Deliberately small. Two bores whose axes come within the sum of
        their radii have merged into one cavity, and the check that spots
        that carries five per cent of slack -- which for a pair of 5 mm
        bores is half a millimetre, exactly the web this asks about.
        """
        radius = 2.0
        first = 20.0
        second = first + radius * 2.0 + web
        shape = _cut(block((80.0, 60.0, 40.0)), _drill(radius, 60.0, (first, 30.0, -10.0)))
        return _cut(shape, _drill(radius, 60.0, (second, 30.0, -10.0)))

    def test_a_thin_web_between_two_holes_is_reported(self):
        results = findings(self._two_bores(1.0))
        self.assertTrue(results, "a 1 mm web between bores is thin")
        self.assertIn(1.0, thicknesses(results))

    def test_a_generous_web_is_not(self):
        self.assertEqual(findings(self._two_bores(8.0)), [])

    def test_a_web_under_the_floor_is_an_error(self):
        results = findings(self._two_bores(0.5))
        self.assertTrue(results)
        self.assertIn(Severity.ERROR, [r.severity for r in results])

    def test_the_message_is_about_the_web_not_a_face(self):
        results = findings(self._two_bores(1.0))
        self.assertTrue(any("bores" in r.message for r in results))

    def test_a_counterbore_is_not_a_web_against_its_own_pilot(self):
        """A counterbore and its pilot share an axis.

        The step between them is a shoulder, and the material outside them is
        the wall. Reporting the difference in their radii as a thin web would
        fire on every counterbored hole ever drilled.
        """
        shape = _cut(block(), _drill(4.0, 60.0, (30.0, 30.0, -10.0)))
        shape = _cut(shape, _drill(4.8, 12.0, (30.0, 30.0, 30.0)))
        for result in findings(shape):
            self.assertNotAlmostEqual(
                result.value, 0.8, places=1,
                msg="the counterbore step was read as a web",
            )


class ConvergingFaceTests(unittest.TestCase):
    """Faces that lean towards each other rather than facing off."""

    def test_a_wedge_closing_to_a_thin_edge_is_reported(self):
        """A tapered rib, thick at the base and closing to a millimetre.

        Neither face is opposed to the other, so the planar pass cannot see
        this at all -- and it is a real wall that gets thinner the further
        up it you cut.
        """
        wedge = BRepPrimAPI_MakeWedge(
            gp_Ax2(gp_Pnt(20.0, 0.0, 40.0), gp_Dir(0.0, 0.0, 1.0)),
            14.0, 60.0, 30.0, 6.5, 0.0, 7.5, 60.0,
        ).Shape()
        results = findings(_fuse(block((60.0, 60.0, 40.0)), wedge))
        self.assertTrue(results, "the top of the wedge is a millimetre thick")
        self.assertTrue(
            any(r.value < 1.5 for r in results),
            f"measured {thicknesses(results)}",
        )

    def test_two_faces_meeting_at_a_corner_are_not_a_wall(self):
        """An L has two faces a millimetre apart at the corner and no wall.

        Nothing lies between them along the line joining them -- they meet.
        Reporting that would put a thin-wall finding on every corner of every
        part.
        """
        shape = _fuse(
            _box((0.0, 0.0, 0.0), (60.0, 60.0, 10.0)),
            _box((0.0, 0.0, 0.0), (10.0, 60.0, 50.0)),
        )
        self.assertEqual(findings(shape), [])


class ExteriorFaceTests(unittest.TestCase):
    """What counts as the outside of a part rather than the side of a wall."""

    def test_a_bare_plate_is_stock_not_a_thin_wall(self):
        """Both of its faces are the part.

        A 2 mm plate on its own is what the shop buys, not a wall somebody
        has to machine. The reference engine says nothing about its own
        thin-sheet fixture either.
        """
        self.assertEqual(findings(_box((0.0, 0.0, 0.0), (200.0, 120.0, 2.0))), [])

    def test_but_a_thin_floor_under_a_recess_is(self):
        shape = _cut(
            _box((0.0, 0.0, 0.0), (200.0, 120.0, 80.0)),
            _box((20.0, 20.0, 2.0), (180.0, 100.0, 81.0)),
        )
        results = findings(shape)
        self.assertTrue(results, "2 mm under a deep recess is a thin floor")
        self.assertIn(2.0, thicknesses(results))


if __name__ == "__main__":
    unittest.main()
