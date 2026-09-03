# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Can a tool get there?

The only ray casting in the machining stack. A three-axis machine approaches
along one of six directions with the part fixtured; a surface no ray can reach
from any of them needs a different setup, a shaped cutter, or a different
process altogether.

Deliberately cardinal-only. Modelling a real tool -- its diameter, its holder,
the fixture around it -- would be more faithful and far slower, and the
question being asked is a screening one: is there any approach at all.
"""

from __future__ import annotations

from typing import Optional

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
from OCP.TopoDS import TopoDS_Shape


#: The six directions a three-axis machine can approach from.
CARDINAL_DIRECTIONS = (
    gp_Dir(1, 0, 0),
    gp_Dir(-1, 0, 0),
    gp_Dir(0, 1, 0),
    gp_Dir(0, -1, 0),
    gp_Dir(0, 0, 1),
    gp_Dir(0, 0, -1),
)

# Start the ray just clear of the surface, so the face it leaves is not
# counted as blocking it.
_LIFT_OFF_MM = 1e-3

# Reach far enough past the part that nothing beyond it matters.
_REACH_MULTIPLE = 4.0


def part_diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 1000.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    diagonal = gp_Pnt(xmin, ymin, zmin).Distance(gp_Pnt(xmax, ymax, zmax))
    return max(diagonal, 1.0)


class Reachability:
    """Answers approach questions for one shape.

    Holds the intersector and the part size so a recognizer asking about many
    faces pays the setup cost once.
    """

    def __init__(self, shape: TopoDS_Shape):
        self.shape = shape
        self.diagonal = part_diagonal(shape)
        self.max_reach = self.diagonal * _REACH_MULTIPLE
        self._intersector = BRepIntCurveSurface_Inter()
        self._intersector.Load(shape, 1e-6)

    def clears_in_direction(
        self, point: gp_Pnt, outward: gp_Dir, direction: gp_Dir
    ) -> bool:
        """Whether a ray leaves the part without hitting anything."""
        origin = gp_Pnt(
            point.X() + outward.X() * _LIFT_OFF_MM,
            point.Y() + outward.Y() * _LIFT_OFF_MM,
            point.Z() + outward.Z() * _LIFT_OFF_MM,
        )
        try:
            self._intersector.Init(self.shape, gp_Lin(origin, direction), 1e-6)
        except Exception:
            return True  # cannot tell: do not claim it is blocked

        while self._intersector.More():
            distance = self._intersector.W()
            if _LIFT_OFF_MM < distance < self.max_reach:
                return False
            self._intersector.Next()
        return True

    def reachable_from_any_cardinal(self, point: gp_Pnt, outward: gp_Dir) -> bool:
        """Whether any of the six approaches reaches this point.

        Directions pointing into the face are skipped: a tool coming from
        behind would have to cut through the part to arrive.
        """
        for direction in CARDINAL_DIRECTIONS:
            if outward.Dot(direction) < -1e-3:
                continue
            if self.clears_in_direction(point, outward, direction):
                return True
        return False

    def blocked_direction(self, point: gp_Pnt, outward: gp_Dir) -> Optional[gp_Dir]:
        """The approach a blocked face most nearly faces, for reporting.

        Not an answer to "can it be reached" -- that is the question above --
        but the direction a machinist would have tried first.
        """
        best: Optional[gp_Dir] = None
        best_alignment = -2.0
        for direction in CARDINAL_DIRECTIONS:
            alignment = outward.Dot(direction)
            if alignment > best_alignment:
                best_alignment, best = alignment, direction
        return best
