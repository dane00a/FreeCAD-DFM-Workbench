# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Canonical recognition: a primitive stored as a spline is still the
primitive, within a measured deviation. Promotion itself ships off."""

import unittest

from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.aag import SurfaceType
from freecad.DFM.core.machining.canonical import (
    CYLINDER,
    PLANE,
    SPHERE,
    recognize_canonical,
)
from freecad.DFM.core.utils.geometry import FaceIndex


def _nurbs(shape):
    return BRepBuilderAPI_NurbsConvert(shape, True).Shape()


def _faces(shape):
    index = FaceIndex(shape)
    graph = AagBuilder(shape, index).build()
    return [(index.face_at(n.face_id), n) for n in graph.nodes]


class CanonicalTests(unittest.TestCase):
    def test_a_spline_box_side_is_a_plane(self):
        kinds = {recognize_canonical(f).kind for f, n in _faces(_nurbs(BRepPrimAPI_MakeBox(10, 20, 30).Shape()))}
        self.assertEqual(kinds, {PLANE})

    def test_a_spline_bore_is_a_cylinder_of_its_radius(self):
        shape = _nurbs(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5.0, 12.0).Shape())
        fits = [recognize_canonical(f) for f, n in _faces(shape) if n.surface_type is not SurfaceType.PLANE]
        cylinders = [fit for fit in fits if fit.kind == CYLINDER]
        self.assertTrue(cylinders)
        self.assertAlmostEqual(cylinders[0].radius, 5.0, places=3)
        self.assertLessEqual(cylinders[0].max_dev_mm, 0.05)

    def test_a_spline_ball_is_a_sphere(self):
        shape = _nurbs(BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), 8.0).Shape())
        fits = [recognize_canonical(f) for f, n in _faces(shape)]
        self.assertIn(SPHERE, {fit.kind for fit in fits})

    def test_promotion_is_off_by_default(self):
        shape = _nurbs(BRepPrimAPI_MakeCylinder(5.0, 12.0).Shape())
        for _face, node in _faces(shape):
            self.assertFalse(node.canonical_promoted)


if __name__ == "__main__":
    unittest.main()
