# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Groups repeated features into the one operation that makes them.

Twelve holes on a bolt circle are twelve holes and one drilling cycle. The
programmer writes the hole once and tells the control to index round; the
setter loads one drill; the estimator prices one operation. A part that
reports twelve of everything reads as twelve times the work, and a quote
built from it comes back double.

So this recognizer takes no geometry of its own. It runs last, over what
every other recognizer already found, and says which of those features are
the same feature repeated. Nothing is consumed and nothing is replaced --
the twelve holes stay twelve holes, each still individually reportable and
individually clickable, with a pattern laid over them saying they were made
together.

Three arrangements are recognized, most specific first. A bolt circle is
holes equidistant from a common centre, which is a rotary index. A grid is
two spacings at right angles, which is a canned cycle in X and Y. A linear
array is one spacing along a line. Anything else that is three or more of
the same hole is still a cluster: a programmer will still set that up once,
even if the arrangement has no name.

Steps are included because a ring of dovetails round the rim of a turbine
disc is a pattern in exactly the way a bolt circle is -- one broach, indexed
-- and reporting forty-eight of them as forty-eight unrelated steps is the
same arithmetic error in a different costume. They group by type alone,
because a dimensional match on rotationally symmetric slots is not reliable
enough to separate families with, and for the same reason they get no
unnamed-cluster fallback: without a size to distinguish them, any three
steps on a part would read as a group.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.gp import gp_Pnt, gp_Vec

from ..aag import AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


#: The hole family. A countersunk bolt circle and a ring of tapped cover
#: screws are patterns exactly as much as a ring of plain drills is.
_HOLE_FAMILY = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
    FeatureType.COUNTERBORE,
)

#: How closely two features' sizes must agree to be the same family. Half a
#: millimetre separates adjacent drill sizes without splitting a family over
#: modelling noise.
_SIZE_TOLERANCE_MM = 0.5

#: How far a hole may sit off the pitch circle and still be on it.
_BOLT_CIRCLE_TOLERANCE_MM = 1.0

#: A circle smaller than this is a group of coincident centres, not a circle.
_MIN_CIRCLE_RADIUS_MM = 1.0

#: How far a hole may sit off the line, and how much a spacing may vary.
_LINEAR_TOLERANCE_MM = 0.5

#: Below this the "spacing" is modelling noise rather than a pitch.
_MIN_SPACING_MM = 0.5

#: Two grid directions must be this close to perpendicular.
_GRID_PERPENDICULAR_MAX_DOT = 0.3

#: Fewer than this and there is no operation to share.
_MIN_MEMBERS = 3


class PatternRecognizer(FeatureRecognizer):
    """Groups repeated features into patterns."""

    prefix = "pat"

    @property
    def name(self) -> str:
        return "Pattern Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        if not prior:
            return []

        found: list[FeatureInstance] = []
        for group in self._group(graph, prior):
            if len(group["members"]) < _MIN_MEMBERS:
                continue
            feature = self._describe(group, len(found))
            if feature is not None:
                found.append(feature)
        return found

    # -- grouping -----------------------------------------------------------

    def _group(self, graph, prior) -> list[dict]:
        """Gather features that are the same thing made more than once.

        Holes group by diameter, so two bolt circles of different sizes on
        one flange stay two patterns. A counterbore matches on its seat as
        well as its clearance hole, so two bolt families that share a
        clearance size but take different heads never merge.
        """
        groups: list[dict] = []
        for feature in prior:
            sizing = self._sizing(feature)
            if sizing is None:
                continue
            primary, secondary, by_size = sizing
            centre = self._centre_of(graph, feature)
            if centre is None:
                continue

            for group in groups:
                if group["type"] != feature.type:
                    continue
                if by_size and not (
                    abs(group["primary"] - primary) < _SIZE_TOLERANCE_MM
                    and abs(group["secondary"] - secondary) < _SIZE_TOLERANCE_MM
                ):
                    continue
                group["members"].append(feature)
                group["centres"].append(centre)
                break
            else:
                groups.append(
                    {
                        "type": feature.type,
                        "primary": primary,
                        "secondary": secondary,
                        "by_size": by_size,
                        "members": [feature],
                        "centres": [centre],
                    }
                )
        return groups

    @staticmethod
    def _sizing(feature) -> Optional[tuple[float, float, bool]]:
        """What a feature is measured by for grouping, if it can be at all."""
        if feature.type == FeatureType.COUNTERBORE:
            return (
                feature.number("diameter_mm") or 0.0,
                feature.number("outer_diameter_mm") or 0.0,
                True,
            )
        if feature.type in _HOLE_FAMILY:
            return (feature.number("diameter_mm") or 0.0, 0.0, True)
        if feature.type == FeatureType.STEP:
            # By type alone: a dimensional match on rotationally symmetric
            # slots is not reliable enough to separate families with.
            return (feature.number("step_width_mm") or 0.0, 0.0, False)
        return None

    @staticmethod
    def _centre_of(graph, feature) -> Optional[gp_Pnt]:
        """Where a feature sits, for the purpose of seeing an arrangement.

        A bore's centre is its cylinder's centroid. Anything else averages
        the faces it owns, which is coarse but only has to be consistent
        between members of one group.
        """
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.surface_type is SurfaceType.CYLINDER:
                return node.centroid

        total = [0.0, 0.0, 0.0]
        counted = 0
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            centroid = graph.node(face_id).centroid
            total[0] += centroid.X()
            total[1] += centroid.Y()
            total[2] += centroid.Z()
            counted += 1
        if not counted:
            return None
        return gp_Pnt(total[0] / counted, total[1] / counted, total[2] / counted)

    # -- classification -----------------------------------------------------

    def _describe(self, group, index: int) -> Optional[FeatureInstance]:
        """Name the arrangement, most specific reading first."""
        members = group["members"]
        centres = group["centres"]

        parameters: dict = {
            "child_ids": [feature.instance_id for feature in members],
            "count": len(members),
            "child_type": group["type"],
        }
        if group["type"] in _HOLE_FAMILY:
            parameters["hole_diameter_mm"] = round(group["primary"], 6)
            if group["secondary"] > 0.0:
                parameters["outer_diameter_mm"] = round(group["secondary"], 6)
        else:
            parameters["step_width_mm"] = round(group["primary"] or 0.0, 6)

        arrangement = (
            self._as_bolt_circle(centres)
            or (self._as_grid(centres) if len(centres) >= 4 else None)
            or self._as_linear(centres)
        )
        if arrangement is None:
            # Three of the same hole in no particular arrangement is still
            # one setup and one tool. A step group is not: grouped by type
            # alone, any three steps on the part would land here.
            if group["type"] not in _HOLE_FAMILY:
                return None
            arrangement = {"pattern_type": "cluster"}
        parameters.update(arrangement)

        faces: list[int] = []
        for feature in members:
            faces.extend(feature.faces)

        return FeatureInstance(
            instance_id=self.instance_id(index),
            type=FeatureType.PATTERN,
            faces=sorted(set(faces)),
            parameters=parameters,
        )

    @staticmethod
    def _as_bolt_circle(centres) -> Optional[dict]:
        """Holes all the same distance from one centre: a rotary index."""
        if len(centres) < 3:
            return None
        middle = gp_Pnt(
            sum(p.X() for p in centres) / len(centres),
            sum(p.Y() for p in centres) / len(centres),
            sum(p.Z() for p in centres) / len(centres),
        )
        radii = [middle.Distance(p) for p in centres]
        average = sum(radii) / len(radii)
        if average < _MIN_CIRCLE_RADIUS_MM:
            return None  # coincident centres, not a circle
        if any(abs(r - average) > _BOLT_CIRCLE_TOLERANCE_MM for r in radii):
            return None
        return {
            "pattern_type": "bolt_circle",
            "circle_radius_mm": round(average, 6),
            "pcd_mm": round(average * 2.0, 6),
            "center": (round(middle.X(), 6), round(middle.Y(), 6), round(middle.Z(), 6)),
        }

    @staticmethod
    def _as_grid(centres) -> Optional[dict]:
        """Two spacings at right angles: a canned cycle in X and Y."""
        if len(centres) < 4:
            return None
        origin = centres[0]
        # Keyed on the distance alone. Sorting the pairs compares the
        # vectors whenever two neighbours tie, and a grid is nothing but
        # ties.
        neighbours = sorted(
            ((origin.Distance(p), gp_Vec(origin, p)) for p in centres[1:]),
            key=lambda pair: pair[0],
        )
        if len(neighbours) < 2:
            return None

        first_spacing, first = neighbours[0]
        if first_spacing < _MIN_SPACING_MM:
            return None
        first_dir = gp_Vec(first.XYZ())
        first_dir.Normalize()

        second_spacing = None
        second_dir = None
        for spacing, vector in neighbours[1:]:
            candidate = gp_Vec(vector.XYZ())
            if candidate.Magnitude() < 1e-9:
                continue
            candidate.Normalize()
            if abs(candidate.Dot(first_dir)) < _GRID_PERPENDICULAR_MAX_DOT:
                second_spacing = spacing
                second_dir = candidate
                break
        if second_dir is None:
            return None

        def distinct(along) -> int:
            values = sorted(gp_Vec(origin, p).Dot(along) for p in centres)
            count = 1
            for previous, current in zip(values, values[1:]):
                if current - previous > _LINEAR_TOLERANCE_MM:
                    count += 1
            return count

        columns = distinct(first_dir)
        rows = distinct(second_dir)
        if rows < 2 or columns < 2:
            return None
        # One missing corner is still a grid; two is a different shape.
        if len(centres) < rows * columns - 1:
            return None
        return {
            "pattern_type": "grid",
            "rows": rows,
            "cols": columns,
            "row_spacing_mm": round(second_spacing, 6),
            "col_spacing_mm": round(first_spacing, 6),
        }

    @staticmethod
    def _as_linear(centres) -> Optional[dict]:
        """One spacing along a line."""
        if len(centres) < 3:
            return None
        origin = centres[0]
        farthest = max(centres[1:], key=origin.Distance)
        reach = origin.Distance(farthest)
        if reach < _MIN_CIRCLE_RADIUS_MM:
            return None
        direction = gp_Vec(origin, farthest)
        direction.Normalize()

        projections = []
        for point in centres:
            offset = gp_Vec(origin, point)
            along = offset.Dot(direction)
            across = offset - gp_Vec(direction.XYZ()).Multiplied(along)
            if across.Magnitude() > _LINEAR_TOLERANCE_MM:
                return None  # not on the line
            projections.append(along)

        projections.sort()
        spacings = [b - a for a, b in zip(projections, projections[1:])]
        if not spacings:
            return None
        average = sum(spacings) / len(spacings)
        if average < _MIN_SPACING_MM:
            return None
        if any(abs(s - average) > _LINEAR_TOLERANCE_MM for s in spacings):
            return None
        return {
            "pattern_type": "linear",
            "spacing_mm": round(average, 6),
            "direction": (
                round(direction.X(), 6),
                round(direction.Y(), 6),
                round(direction.Z(), 6),
            ),
        }
