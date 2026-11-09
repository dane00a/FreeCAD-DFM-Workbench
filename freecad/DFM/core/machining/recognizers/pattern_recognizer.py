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
holes evenly spaced round a common centre in one plane, which is a rotary
index. A grid is two spacings at right angles, which is a canned cycle in X
and Y. A linear array is one spacing along a line. Anything that fits none
of those is not a pattern: the holes are already individually recognized,
and calling three same-sized holes anywhere on a part a "pattern" only
spends the word. Members must also share a drilling direction, since holes
down different axes are different setups.

Steps are included because a ring of dovetails round the rim of a turbine
disc is a pattern in exactly the way a bolt circle is -- one broach, indexed
-- and reporting forty-eight of them as forty-eight unrelated steps is the
same arithmetic error in a different costume. They group by type alone,
because a dimensional match on rotationally symmetric slots is not reliable
enough to separate families with.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


#: The hole family. A countersunk bolt circle and a ring of tapped cover
#: screws are patterns exactly as much as a ring of plain drills is.
_HOLE_FAMILY = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
    FeatureType.COUNTERBORE,
)

#: How closely two features' sizes must agree to be the same family.
#: Diameters come from the analytic B-rep surface, so two holes made by one
#: tool agree to floating point; there is no noise to absorb, and a looser
#: band only merges different tools (an M5 tap drill at 5.7 and a 6.0 dowel
#: on the same face).
_SIZE_TOLERANCE_MM = 0.05

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

#: Two members' axes must agree this closely (~2.5 degrees) to share a
#: setup. Tight enough to keep orthogonal and antiparallel faces apart,
#: loose enough for tessellation noise.
_AXIS_MATCH_MIN_DOT = 0.999

#: Floor on a bolt circle's angular-pitch tolerance, in radians, so a huge
#: ring cannot demand sub-degree precision.
_MIN_PITCH_TOLERANCE_RAD = 0.05


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

        Members must also point the same way. Direction, not just the line:
        +Y and -Y are holes drilled into opposite faces, and a group that
        spans them is not something one operation makes. Axis-less features
        (steps) still group with each other; one-sided or divergent axes do
        not.
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
            axis = self._axis_of(feature)

            for group in groups:
                if group["type"] != feature.type:
                    continue
                if by_size and not (
                    abs(group["primary"] - primary) < _SIZE_TOLERANCE_MM
                    and abs(group["secondary"] - secondary) < _SIZE_TOLERANCE_MM
                ):
                    continue
                if group["axis"] is None or axis is None:
                    if group["axis"] is not None or axis is not None:
                        continue
                elif not group["axis"].Dot(axis) > _AXIS_MATCH_MIN_DOT:
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
                        "axis": axis,
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
    def _axis_of(feature) -> Optional[gp_Dir]:
        """The feature's own drilling direction, when it records one."""
        raw = feature.parameters.get("axis")
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            return None
        try:
            x, y, z = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        if math.sqrt(x * x + y * y + z * z) <= 1e-9:
            return None
        return gp_Dir(x, y, z)

    @staticmethod
    def _centre_of(graph, feature) -> Optional[gp_Pnt]:
        """Where a feature sits, for the purpose of seeing an arrangement.

        A bore's centre is its cylinder's centroid projected onto the
        cylinder's axis. The centroid alone is the centre only while the
        wall is an unbroken tube: cross-drill the bore and its partial walls'
        centroids sit off the axis by up to the radius, enough to fail a
        textbook bolt circle. The axis does not move when the wall is cut.
        Anything else averages the faces it owns, which is coarse but only
        has to be consistent between members of one group.
        """
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.surface_type is SurfaceType.CYLINDER:
                ax = node.cyl_cone_axis
                if ax is None:
                    return node.centroid  # no usable axis
                direction = gp_Vec(ax.Direction().XYZ())
                if direction.Magnitude() <= 1e-9:
                    return node.centroid
                origin = ax.Location()
                t = gp_Vec(origin, node.centroid).Dot(direction)
                return gp_Pnt(
                    origin.X() + direction.X() * t,
                    origin.Y() + direction.Y() * t,
                    origin.Z() + direction.Z() * t,
                )

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

        # Evaluate the arrangement in the plane the holes were drilled into.
        # With a shared axis, how far down each bore its surviving wall sits
        # is not part of the arrangement: a cross-drilled bore's fragments
        # sit lower than its neighbours' walls, and in raw 3D the ring would
        # fail the coplanarity test. Axis-less groups (steps) keep raw
        # positions and still face that test.
        if group["axis"] is not None and centres:
            n = float(len(centres))
            middle = gp_Pnt(
                sum(p.X() for p in centres) / n,
                sum(p.Y() for p in centres) / n,
                sum(p.Z() for p in centres) / n,
            )
            ax = gp_Vec(group["axis"].XYZ())
            flattened = []
            for p in centres:
                t = gp_Vec(middle, p).Dot(ax)
                flattened.append(
                    gp_Pnt(p.X() - ax.X() * t, p.Y() - ax.Y() * t, p.Z() - ax.Z() * t)
                )
            centres = flattened

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
            # Staggered before linear: one row of a triangular array would
            # otherwise be claimed as a line.
            or (self._as_staggered_grid(centres) if len(centres) >= 6 else None)
        )
        if arrangement is None:
            # Last of the grid family, after the whole-group test had its go,
            # so a complete grid is still described as one. Emitted over the
            # block's members only; the strays stay individually recognized.
            subset = self._as_grid_subset(centres)
            if subset is not None:
                chosen, sub_arrangement = subset
                chosen_members = [members[i] for i in chosen]
                sub_parameters: dict = {
                    "child_ids": [f.instance_id for f in chosen_members],
                    "count": len(chosen_members),
                    "child_type": group["type"],
                }
                if group["type"] in _HOLE_FAMILY:
                    sub_parameters["hole_diameter_mm"] = round(group["primary"], 6)
                    if group["secondary"] > 0.0:
                        sub_parameters["outer_diameter_mm"] = round(group["secondary"], 6)
                sub_parameters.update(sub_arrangement)
                # Stated, because a grid of 16 inside a group of 18 is a
                # materially different claim from a grid of 18.
                sub_parameters["group_size"] = len(members)
                sub_faces: list[int] = []
                for feature in chosen_members:
                    sub_faces.extend(feature.faces)
                return FeatureInstance(
                    instance_id=self.instance_id(index),
                    type=FeatureType.PATTERN,
                    faces=sorted(set(sub_faces)),
                    parameters=sub_parameters,
                )
            arrangement = self._as_linear(centres)
        if arrangement is None:
            # No unnamed fallback. There used to be a "cluster" for hole
            # groups that fit no arrangement and, with no locality test, it
            # made any three equal holes anywhere on a part one "pattern" --
            # fourteen of them across opposite faces of one part. A pattern
            # is an arrangement; holes that fit none are already reported.
            return None
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
        """Holes evenly spaced round one centre, in one plane: a rotary index.

        Equidistance alone is not enough: the corners of any rectangle are
        equidistant from its centre, so four corner mounting holes used to
        read as a bolt circle. The ring must also be planar and evenly
        pitched.
        """
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
        tol = _BOLT_CIRCLE_TOLERANCE_MM
        if any(abs(r - average) > tol for r in radii):
            return None

        # Coplanarity. The plane is spanned by the most independent pair of
        # successive spokes; without it a ring on a cylinder's OD and a ring
        # on its face are the same "circle" to the radius test.
        normal = gp_Vec(0.0, 0.0, 0.0)
        for a_pt, b_pt in zip(centres, centres[1:]):
            cross = gp_Vec(middle, a_pt).Crossed(gp_Vec(middle, b_pt))
            if cross.Magnitude() > normal.Magnitude():
                normal = cross
        if normal.Magnitude() < 1e-9:
            return None  # collinear: a line, not a circle
        plane_n = gp_Dir(normal.XYZ())
        plane_v = gp_Vec(plane_n.XYZ())
        if any(abs(gp_Vec(middle, p).Dot(plane_v)) > tol for p in centres):
            return None

        # Equal angular pitch: what separates a bolt circle from points that
        # happen to share a radius. The wrap-around gap counts, or a
        # half-populated ring would pass on its populated half. The
        # tolerance is an arc length, so it scales with the ring.
        ref_x = gp_Dir(gp_Vec(middle, centres[0]).XYZ())
        vx = gp_Vec(ref_x.XYZ())
        vy = gp_Vec(plane_n.Crossed(ref_x).XYZ())
        angles = sorted(
            math.atan2(gp_Vec(middle, p).Dot(vy), gp_Vec(middle, p).Dot(vx))
            for p in centres
        )
        expected = 2.0 * math.pi / len(angles)
        pitch_tol = max(tol / average, _MIN_PITCH_TOLERANCE_RAD)
        for i, angle in enumerate(angles):
            if i + 1 < len(angles):
                gap = angles[i + 1] - angle
            else:
                gap = angles[0] + 2.0 * math.pi - angle
            if abs(gap - expected) > pitch_tol:
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
    def _as_staggered_grid(centres) -> Optional[dict]:
        """Rows offset half a pitch: a triangular-pitch array.

        Rows evenly spaced, evenly pitched within each row, every other row
        shifted half a pitch -- the tube-sheet layout, and perforated plate
        and honeycomb vents besides. It is not a grid (rows alternate in
        count), a circle or a line, so without this it is no pattern at all.
        Rectangular grids are tried first, so only a genuinely staggered
        array lands here.
        """
        tol = _LINEAR_TOLERANCE_MM
        n = len(centres)
        if n < 6:
            return None  # two rows of three is the smallest honest one
        middle = gp_Pnt(
            sum(p.X() for p in centres) / n,
            sum(p.Y() for p in centres) / n,
            sum(p.Z() for p in centres) / n,
        )
        normal = gp_Vec(0.0, 0.0, 0.0)
        for a_pt, b_pt in zip(centres, centres[1:]):
            cross = gp_Vec(middle, a_pt).Crossed(gp_Vec(middle, b_pt))
            if cross.Magnitude() > normal.Magnitude():
                normal = cross
        if normal.Magnitude() < 1e-9:
            return None
        plane_n = gp_Dir(normal.XYZ())
        plane_v = gp_Vec(plane_n.XYZ())
        if any(abs(gp_Vec(middle, p).Dot(plane_v)) > tol for p in centres):
            return None

        # Candidate in-row directions: the shortest inter-point vectors. In
        # a triangular array six neighbours sit at one pitch, so the nearest
        # direction is ambiguous; try several and keep the first that
        # explains the whole set.
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                v = gp_Vec(centres[i], centres[j])
                if v.Magnitude() > 1e-6:
                    pairs.append((v.Magnitude(), v))
        pairs.sort(key=lambda pair: pair[0])
        candidates: list[gp_Dir] = []
        for _, vector in pairs:
            d = gp_Dir(vector.XYZ())
            if not any(abs(c.Dot(d)) > 0.995 for c in candidates):
                candidates.append(d)
            if len(candidates) >= 4:
                break

        for u in candidates:
            w = plane_n.Crossed(u)
            uv, wv = gp_Vec(u.XYZ()), gp_Vec(w.XYZ())
            points = sorted(
                (
                    (gp_Vec(middle, p).Dot(uv), gp_Vec(middle, p).Dot(wv))
                    for p in centres
                ),
                key=lambda pt: pt[1],
            )

            # Cluster into rows across w.
            rows: list[list[float]] = [[points[0][0]]]
            row_b = [points[0][1]]
            for a_val, b_val in points[1:]:
                if abs(b_val - row_b[-1]) <= tol:
                    rows[-1].append(a_val)
                else:
                    rows.append([a_val])
                    row_b.append(b_val)
            if len(rows) < 2:
                continue

            # Row spacing must be uniform.
            spacing = row_b[1] - row_b[0]
            if spacing < 1e-6:
                continue
            if any(
                abs((row_b[r] - row_b[r - 1]) - spacing) > tol
                for r in range(1, len(row_b))
            ):
                continue

            # Every row evenly pitched, all rows sharing one pitch.
            ok = True
            pitch = 0.0
            for row in rows:
                if len(row) < 2:
                    ok = False
                    break
                row.sort()
                for previous, current in zip(row, row[1:]):
                    gap = current - previous
                    if pitch == 0.0:
                        pitch = gap
                    elif abs(gap - pitch) > tol:
                        ok = False
                        break
                if not ok:
                    break
            if not ok or pitch < 1e-6:
                continue

            # The stagger: consecutive rows offset by about half a pitch. A
            # zero offset is a rectangular grid, already tried and the better
            # description, so it is rejected here rather than claimed.
            staggered = False
            for r in range(1, len(rows)):
                offset = math.fmod(abs(rows[r][0] - rows[r - 1][0]), pitch)
                if offset > pitch / 2.0:
                    offset = pitch - offset
                if abs(offset - pitch / 2.0) <= tol:
                    staggered = True
                elif offset > tol:
                    ok = False
                    break
            if not ok or not staggered:
                continue

            return {
                "pattern_type": "staggered_grid",
                "rows": len(rows),
                "pitch_mm": round(pitch, 6),
                "row_spacing_mm": round(spacing, 6),
            }
        return None

    @staticmethod
    def _as_grid_subset(centres) -> Optional[tuple[list[int], dict]]:
        """The largest complete rectangular block within a set of points.

        The whole-group grid test is all-or-nothing, which is wrong for the
        common real part where a grid of ports shares its diameter and axis
        with a couple of unrelated holes on the same face. Build the lattice
        from the points, mark the occupied cells, and keep the largest fully
        populated block; points outside it simply are not members.
        """
        tol = _LINEAR_TOLERANCE_MM
        n = len(centres)
        if n < 6:
            return None

        # Lattice directions by how OFTEN a direction occurs, not by which
        # vector is shortest: with strays present the shortest vector can
        # belong to a stray, while every same-row and same-column pair of
        # the real grid votes for its axes.
        #
        # A bucket's direction is the consensus of its members (each flipped
        # into the bucket's half-space, since v and -v are one lattice
        # direction), not whichever vector arrived first. The bucket band is
        # loose (~8 degrees) on purpose, and with first-seen a stray's vector
        # a couple of degrees off an axis captured every true pair on that
        # axis, tilting the lattice enough to smear each column into several
        # levels.
        raw: list[list] = []  # [sum vector, count]
        for i in range(n):
            for j in range(i + 1, n):
                v = gp_Vec(centres[i], centres[j])
                if v.Magnitude() < 1e-6:
                    continue
                d = gp_Dir(v.XYZ())
                dv = gp_Vec(d.XYZ())
                for bucket in raw:
                    rep = gp_Dir(bucket[0].XYZ())
                    dot = rep.Dot(d)
                    if abs(dot) > 0.99:
                        bucket[0] = bucket[0] + (dv.Reversed() if dot < 0 else dv)
                        bucket[1] += 1
                        break
                else:
                    raw.append([dv, 1])
        buckets = [
            [gp_Dir(total.XYZ()), count]
            for total, count in raw
            if total.Magnitude() > 1e-9
        ]
        buckets.sort(key=lambda bucket: -bucket[1])
        directions: list[gp_Dir] = []
        for d, count in buckets:
            if count < 2:
                break  # a direction seen once is noise
            if not any(abs(e.Dot(d)) > 0.9 for e in directions):
                directions.append(d)
            if len(directions) >= 2:
                break
        if len(directions) < 2:
            return None

        def project(d: gp_Dir) -> list[float]:
            return [p.X() * d.X() + p.Y() * d.Y() + p.Z() * d.Z() for p in centres]

        pu = project(directions[0])
        pv = project(directions[1])

        # Pitch is the most common gap between adjacent distinct levels: a
        # stray contributes one odd gap among many regular ones.
        def pitch_of(values: list[float]) -> float:
            distinct: list[float] = []
            for x in sorted(values):
                if not distinct or abs(x - distinct[-1]) > tol:
                    distinct.append(x)
            if len(distinct) < 2:
                return 0.0
            gaps = [b - a for a, b in zip(distinct, distinct[1:])]
            best, best_n = 0.0, 0
            for g in gaps:
                k = sum(1 for h in gaps if abs(h - g) <= tol)
                if k > best_n or (k == best_n and g < best):
                    best_n, best = k, g
            return best if best_n >= 2 else 0.0

        pitch_u, pitch_v = pitch_of(pu), pitch_of(pv)
        if pitch_u < 1e-6 or pitch_v < 1e-6:
            return None

        def lround(x: float) -> int:
            # C lround: halves away from zero.
            return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)

        # Snap to the lattice from every possible origin, since the lowest
        # projection may itself be a stray.
        best_area = 0
        best_members: list[int] = []
        best_r = best_c = 0
        for o in range(n):
            u0, v0 = pu[o], pv[o]
            cells: dict[tuple[int, int], int] = {}
            rmin = cmin = 1 << 30
            rmax = cmax = -(1 << 30)
            for i in range(n):
                ci = lround((pu[i] - u0) / pitch_u)
                ri = lround((pv[i] - v0) / pitch_v)
                if abs(pu[i] - (u0 + ci * pitch_u)) > tol:
                    continue
                if abs(pv[i] - (v0 + ri * pitch_v)) > tol:
                    continue
                cells.setdefault((ri, ci), i)
                rmin, rmax = min(rmin, ri), max(rmax, ri)
                cmin, cmax = min(cmin, ci), max(cmax, ci)
            if len(cells) < 6:
                continue
            rows_n, cols_n = rmax - rmin + 1, cmax - cmin + 1
            if rows_n < 2 or cols_n < 2 or rows_n > 512 or cols_n > 512:
                continue

            # Largest fully occupied rectangle: histogram scan.
            heights = [0] * cols_n
            for r in range(rows_n):
                for c in range(cols_n):
                    heights[c] = heights[c] + 1 if (r + rmin, c + cmin) in cells else 0
                for c in range(cols_n):
                    min_h = heights[c]
                    for c2 in range(c, -1, -1):
                        min_h = min(min_h, heights[c2])
                        if min_h == 0:
                            break
                        width = c - c2 + 1
                        area = min_h * width
                        if area > best_area and min_h >= 2 and width >= 2 and area >= 6:
                            best_area, best_r, best_c = area, min_h, width
                            best_members = [
                                cells[(rr + rmin, cc + cmin)]
                                for rr in range(r - min_h + 1, r + 1)
                                for cc in range(c2, c + 1)
                            ]

        if best_area < 6:
            return None
        if best_area == n:
            return None  # the whole set: the grid test owns that
        return best_members, {
            "pattern_type": "grid",
            "rows": best_r,
            "cols": best_c,
            "row_spacing_mm": round(pitch_v, 6),
            "col_spacing_mm": round(pitch_u, 6),
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
