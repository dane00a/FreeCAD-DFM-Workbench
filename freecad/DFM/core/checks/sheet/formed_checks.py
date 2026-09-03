# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about features drawn out of the plane: embosses, louvers and lances.

Forming is a third process alongside cutting and bending, and it is the one
that needs tooling made for the part. That is why the census finding here fires
on every formed feature rather than only on bad ones: it is a line in the quote
before it is a fault.

The rest is about how far metal stretches. An emboss draws material out of the
surrounding sheet, a louver hood stretches along a sheared edge, and two formed
features too close together each pull on the same web. All three fail by
thinning and then tearing.
"""

from __future__ import annotations

import math

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_EDGE, TopAbs_OUT
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import (
    SHEET_EMBOSS_MAX_DEPTH_FACTOR,
    SHEET_FORMED_BEND_CLEARANCE_FACTOR,
    SHEET_FORMED_MIN_PITCH_FACTOR,
    SHEET_LOUVER_MAX_HEIGHT_FACTOR,
    SheetCheck,
    bend_axial_span,
    bends_of,
    box_of_faces,
    distance_to_box,
    sorted_features,
    threshold,
)


# Samples taken along each bounding edge of a trimmed face, and the interior
# UV grid laid over it. The extremes of a trimmed face are nearly always on its
# boundary, but a hood's apex is a strictly interior point that no edge carries.
_FOOTPRINT_EDGE_SAMPLES = 32
_FOOTPRINT_GRID = 8

# Points sampled along a bend's axis when measuring how near a formed feature
# comes to the fold.
_BEND_AXIS_SAMPLES = 8


@register_check(Rulebook.SHEET_FORMED_FEATURE)
class SheetFormedFeatureCheck(SheetCheck):
    """Every formed feature, listed, because each one is tooling.

    Nothing here is wrong. A laser and a brake between them make most sheet
    parts; an emboss, a louver or a lance needs a punch and die ground for that
    shape, and whether the shop has one is a question to settle before the
    quote rather than after it.
    """

    @property
    def name(self) -> str:
        return "Sheet Formed Feature Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.SHEET_FORMED):
            subtype = str(feature.param("subtype", "formed"))
            height = feature.number("height_mm", 0.0) or 0.0
            open_edges = feature.number("open_edges", 0.0) or 0.0

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{subtype}, {height:.1f} mm proud",
                    self.render(
                        feedback,
                        Severity.INFO,
                        height,
                        height,
                        height,
                        "mm",
                        f"This is a formed feature -- a {subtype} standing "
                        f"{height:.1f} mm proud of the panel, with "
                        f"{int(open_edges)} sheared edge(s). It needs a dedicated "
                        "punch and die in the turret; laser cutting and press-brake "
                        "bending alone will not produce it, and the forming "
                        "direction is one-sided, so every one of these has to be "
                        "raised from the same face. Worth confirming the tooling "
                        "exists before the part is quoted.",
                    ),
                    faces=feature.faces,
                    value=height,
                    limit=height,
                    comparison="",
                )
            )
        return results


@register_check(Rulebook.SHEET_EMBOSS_DEEP)
class SheetEmbossDeepCheck(SheetCheck):
    """An emboss drawn deeper than the material will stretch.

    All the metal in the raised shape came out of the sheet around it, and past
    about three gauges of draw a single hit has taken more than the material
    gives: the walls thin and then split at the corners.
    """

    @property
    def name(self) -> str:
        return "Sheet Emboss Depth Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(
                context, "sheet_emboss_max_depth_factor", SHEET_EMBOSS_MAX_DEPTH_FACTOR
            )
        maximum = gauge * factor

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.SHEET_FORMED):
            if str(feature.param("subtype", "")) != "emboss":
                continue
            height = feature.number("height_mm", 0.0) or 0.0
            if height <= maximum:
                continue
            ratio = self.ratio(height, gauge)

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{height:.1f} mm draw, {ratio:.1f}x gauge",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        ratio,
                        factor,
                        factor,
                        "",
                        f"This emboss is drawn {height:.1f} mm deep in "
                        f"{gauge:.2f} mm material, past the {factor:.0f} gauges a "
                        f"single hit will stretch ({maximum:.1f} mm). The walls "
                        "thin as the metal is pulled in and then split at the "
                        "corners. Reduce the draw, put a larger radius on the "
                        "corners, or form it progressively over more than one "
                        "station.",
                    ),
                    faces=feature.faces,
                    value=ratio,
                    limit=factor,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.SHEET_LOUVER_TALL)
class SheetLouverTallCheck(SheetCheck):
    """A louver or lance hood standing taller than the dies cover.

    The ceiling is a quarter inch at 14 gauge, scaled with the material rather
    than fixed: it came from a hood height the shop quotes, not from a
    property of steel, so it moves when the gauge does.
    """

    @property
    def name(self) -> str:
        return "Sheet Louver Height Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(
                context, "sheet_louver_max_height_factor", SHEET_LOUVER_MAX_HEIGHT_FACTOR
            )
        maximum = gauge * factor

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.SHEET_FORMED):
            subtype = str(feature.param("subtype", ""))
            if subtype not in ("louver", "lance"):
                continue
            height = feature.number("height_mm", 0.0) or 0.0
            if height <= maximum:
                continue
            ratio = self.ratio(height, gauge)

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{height:.1f} mm {subtype}",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        ratio,
                        factor,
                        factor,
                        "",
                        f"This {subtype} stands {height:.1f} mm proud, past the "
                        f"{maximum:.1f} mm standard louver dies reach in "
                        f"{gauge:.2f} mm material -- about a quarter inch at 14 "
                        "gauge, scaled. Beyond that the hood keeps stretching "
                        "along the sheared edge and tears at the ends of the cut. "
                        "Lower the hood, lengthen it so the same opening comes off "
                        "less height, or expect special tooling.",
                    ),
                    faces=feature.faces,
                    value=ratio,
                    limit=factor,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.SHEET_FORMED_PITCH)
class SheetFormedPitchCheck(SheetCheck):
    """Formed features close enough that forming one pulls on the next.

    The gap is measured between the trimmed faces rather than between their
    bounding boxes, and the difference is not academic. OpenCascade builds a
    spline face's bounding box from its control poles, and a lofted louver
    hood's poles stand clear of the surface they define -- so the box
    over-reaches the real footprint, the gap comes back short, and the rule
    fires on a vent panel whose spacing is already right. Telling a customer to
    re-space a part that is correct is the expensive kind of wrong.

    Every sample here lies on the surface, so the footprint can only ever
    under-cover. That errs toward reporting the gap slightly wide, which makes
    the rule fire late rather than early.
    """

    @property
    def name(self) -> str:
        return "Sheet Formed Pitch Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(
                context, "sheet_formed_min_pitch_factor", SHEET_FORMED_MIN_PITCH_FACTOR
            )
        minimum = gauge * factor

        formed = []
        for feature in sorted_features(context, FeatureType.SHEET_FORMED):
            box = self._footprint(context, feature)
            if box is not None:
                formed.append((feature, box))

        results: list[CheckResult] = []
        for index, (first, first_box) in enumerate(formed):
            for second, second_box in formed[index + 1 :]:
                gap = first_box.Distance(second_box)
                if gap <= 1e-9 or gap >= minimum:
                    continue

                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        f"{gap:.1f} mm between forms",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            gap,
                            minimum,
                            minimum,
                            "mm",
                            f"These two formed features leave only {gap:.1f} mm of "
                            f"flat between them. Keep at least {minimum:.1f} mm -- "
                            f"{factor:.0f} gauges -- or forming the first one pulls "
                            "the web into the second and the panel between them "
                            "ends up dished. Space them further apart, or make them "
                            "smaller.",
                        ),
                        faces=sorted(set(first.faces) | set(second.faces)),
                        value=gap,
                        limit=minimum,
                        comparison="<",
                    )
                )
        return results

    @staticmethod
    def _footprint(context, feature):
        """The feature's real extent, falling back to the pole box.

        A feature whose faces cannot all be resolved back to the shape gets the
        pole-box answer for the whole feature. That is the old answer rather
        than no answer, and it is right on analytic geometry, which is where
        most of these live.
        """
        poles = Bnd_Box()
        trimmed = Bnd_Box()
        all_trimmed = True
        found = False

        for face_id in sorted(feature.faces):
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if not node.bbox.IsVoid():
                poles.Add(node.bbox)
                found = True
            face = _face_at(context, face_id)
            if face is None or not _add_trimmed_face_extent(face, trimmed):
                all_trimmed = False

        if all_trimmed and not trimmed.IsVoid():
            return trimmed
        return poles if found and not poles.IsVoid() else None


@register_check(Rulebook.SHEET_FORMED_NEAR_BEND)
class SheetFormedNearBendCheck(SheetCheck):
    """A formed feature sitting in the metal a fold is going to move.

    Forming leaves the material around the feature work-hardened and already
    stretched. Folding through that region gives an uneven bend line and
    distorts the formed shape at the same time, so both features come out
    wrong. One finding per formed feature is enough -- the fix is to move it,
    and which bend it was nearest to does not change that.
    """

    @property
    def name(self) -> str:
        return "Sheet Formed Near Bend Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(
                context,
                "sheet_formed_bend_clearance_factor",
                SHEET_FORMED_BEND_CLEARANCE_FACTOR,
            )

        bends = bends_of(context)
        if not bends:
            return []

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.SHEET_FORMED):
            box = box_of_faces(context, feature.faces)
            if box is None:
                continue

            for bend in bends:
                span = bend_axial_span(context, bend)
                if span is None:
                    continue
                clearance = gauge * factor + bend.inner_radius
                nearest = self._nearest_along(bend, span, box)
                if nearest >= clearance:
                    continue

                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        f"{nearest:.1f} mm from a bend",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            nearest,
                            clearance,
                            clearance,
                            "mm",
                            f"This formed feature sits {nearest:.1f} mm from a bend "
                            f"line, inside the {clearance:.1f} mm the fold works "
                            f"({factor:.0f} gauges plus the bend radius). The metal "
                            "there is already stretched and hardened, so the bend "
                            "comes out uneven and the formed shape distorts with "
                            "it. Move the feature clear of the bend zone, or form "
                            "it after the fold.",
                        ),
                        faces=sorted(set(feature.faces) | set(bend.feature.faces)),
                        value=nearest,
                        limit=clearance,
                        comparison="<",
                    )
                )
                break
        return results

    @staticmethod
    def _nearest_along(bend, span, box) -> float:
        """Closest approach of the fold line to a box, sampled along the axis."""
        low, high = span
        best = math.inf
        for step in range(_BEND_AXIS_SAMPLES + 1):
            point = bend.point_at(low + (high - low) * step / _BEND_AXIS_SAMPLES)
            best = min(best, distance_to_box(box, point))
        return best


# =============================================================================
# Trimmed-face footprints
# =============================================================================


def _face_at(context, face_id: int):
    """The topological face behind a graph node, or None when it is out of reach."""
    index = getattr(context, "face_index", None)
    if index is None:
        return None
    try:
        if face_id < 1 or face_id > len(index):
            return None
        return index.face_at(face_id)
    except Exception:  # a shape the index was not built from
        return None


def _add_trimmed_face_extent(face, box: Bnd_Box) -> bool:
    """Add the on-surface extent of one face to a box.

    Boundary edges first, then an interior grid classified against the face's
    own trim, because the extremes of a trimmed face are usually on its
    boundary but not always: the crest of a drawn hood is strictly interior.
    """
    if face is None or face.IsNull():
        return False

    found = False
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        try:
            if BRep_Tool.Degenerated_s(edge):
                continue
            curve = BRepAdaptor_Curve(edge)
            first = curve.FirstParameter()
            last = curve.LastParameter()
            if not last > first or not math.isfinite(first) or not math.isfinite(last):
                continue
            for step in range(_FOOTPRINT_EDGE_SAMPLES + 1):
                box.Add(curve.Value(first + (last - first) * step / _FOOTPRINT_EDGE_SAMPLES))
                found = True
        except Exception:  # an edge carrying no 3D curve
            continue

    try:
        surface = BRepAdaptor_Surface(face)
        u0, u1 = surface.FirstUParameter(), surface.LastUParameter()
        v0, v1 = surface.FirstVParameter(), surface.LastVParameter()
        if all(math.isfinite(value) for value in (u0, u1, v0, v1)) and u1 > u0 and v1 > v0:
            classifier = BRepTopAdaptor_FClass2d(face, Precision.Confusion_s())
            for i in range(_FOOTPRINT_GRID):
                for j in range(_FOOTPRINT_GRID):
                    u = u0 + (u1 - u0) * (i + 0.5) / _FOOTPRINT_GRID
                    v = v0 + (v1 - v0) * (j + 0.5) / _FOOTPRINT_GRID
                    if classifier.Perform(gp_Pnt2d(u, v)) == TopAbs_OUT:
                        continue
                    box.Add(surface.Value(u, v))
                    found = True
    except Exception:  # a surface the adaptor cannot evaluate
        return found

    return found
