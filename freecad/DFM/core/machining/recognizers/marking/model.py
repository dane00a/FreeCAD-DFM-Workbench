# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The vocabulary the marking stages exchange.

Deliberately dumb data: every stage reads its input, adds its own fields and
passes it on, so the whole decision is inspectable at one place (the
orchestrator in ``marking_recognizer``) instead of being spread over four
passes that each re-derive the geometry.

The previous detector decided "is this text" inside each of four passes at the
same moment it decided "is there relief here". Separating "there is relief"
(cheap, high recall) from "what process made it" (back-side evidence) from
"does it look like lettering" (scored) is what makes each question answerable.

Vectors are plain ``(x, y, z)`` float tuples here rather than ``gp_Dir``: the
stages evaluate these few formulas millions of times on a large part, and the
arithmetic is the same either way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

Vec = tuple[float, float, float]


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def cross(a: Vec, b: Vec) -> Vec:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a: Vec) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def unit(a: Vec) -> Vec:
    n = norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)


def neg(a: Vec) -> Vec:
    return (-a[0], -a[1], -a[2])


def pnt(p) -> Vec:
    """A gp_Pnt / gp_Dir as a tuple."""
    return (p.X(), p.Y(), p.Z())


class Process(Enum):
    """What made the relief, decided from BACK-SIDE evidence only.

    Forming displaces the skin, so the wall is the same thickness under the
    feature as beside it; engraving removes material, so it is thinner by the
    depth (the constant-thickness property of a sheet deformation feature).
    """

    UNDECIDED = "undecided"
    FORMED = "formed"
    ENGRAVED = "engraved"
    RAISED = "raised"
    INDETERMINATE = "indeterminate"


class InkKind(Enum):
    """Which faces carry the measurable footprint of a mark.

    Naming the four cases is what let one generator replace four passes. The
    order is the reference's enum order, which breaks majority ties.
    """

    NONE = "none"
    FLOOR = "floor"          # engraved stroke floors at a common depth
    PROUD_TOP = "proud_top"  # raised stroke tops
    ISLAND = "island"        # background relieved around host-level letters
    WALL_GAP = "wall_gap"    # V-cut / ball-nose: walls only, no flat face


INK_ORDER = [InkKind.NONE, InkKind.FLOOR, InkKind.PROUD_TOP, InkKind.ISLAND,
             InkKind.WALL_GAP]


class Tier(Enum):
    A = "A"  # verified: read back, decoded, or note-backed with geometry
    B = "B"  # scored, unverified: one staff confirmation
    C = "C"  # rejected, with the failing gate recorded


class HostKind(Enum):
    """What a host surface is.

    A host must be DEVELOPABLE for its own (u, v) coordinates to measure the
    mark without distortion. A cylinder unrolls isometrically --
    (theta, z) -> (R*theta, z) preserves every length and angle -- so it is in.
    A sphere or freeform host cannot be (Theorema Egregium), and a cone, though
    developable, unrolls polar and would need a frame of its own.
    """

    PLANE = "plane"
    CYLINDER = "cylinder"


class HostSurface:
    """A support surface: the maximal set of coplanar (or coaxial co-radial)
    faces, treated as one.

    Grouping by surface rather than by face is the fix for host fragmentation:
    a logo's own small faces used to compete as hosts against the side face
    that actually carries it, each seeing a fraction of its strokes.
    """

    def __init__(self) -> None:
        self.kind = HostKind.PLANE
        self.origin: Vec = (0.0, 0.0, 0.0)
        self.normal: Vec = (0.0, 0.0, 1.0)
        self.u: Vec = (1.0, 0.0, 0.0)
        self.v: Vec = (0.0, 1.0, 0.0)
        # Cylinder parameters.
        self.axis_loc: Vec = (0.0, 0.0, 0.0)
        self.axis_dir: Vec = (0.0, 0.0, 1.0)
        self.radius = 0.0
        # +1 for material inside (shaft, collar), -1 for a bore: the sign that
        # makes signed_offset mean "outward is positive" on both.
        self.curv_sign = 1.0
        self.ref_dir: Vec = (1.0, 0.0, 0.0)
        self.ref_perp: Vec = (0.0, 1.0, 0.0)

        self.faces: list = []
        self.face_ids: list[int] = []
        self.primary_face_id: Optional[int] = None
        # Coplanar faces the grouping EJECTED because each lies strictly
        # inside a much larger face of the same plane: the letters of a
        # relieved plaque. Named rather than re-measured -- "at host level" has
        # exactly one answer, the one the grouping already gave.
        self.level_face_ids: set[int] = set()
        self.area = 0.0
        self.bbox = None  # Bnd_Box

        self.local_thickness_mm = 0.0
        self.thickness_resolved = False

        self.foot_u_lo = 1e18
        self.foot_u_hi = -1e18
        self.foot_v_lo = 1e18
        self.foot_v_hi = -1e18

    def curved(self) -> bool:
        return self.kind is not HostKind.PLANE

    def _radial(self, p: Vec) -> tuple[Vec, float]:
        d = sub(p, self.axis_loc)
        d = sub(d, scale(self.axis_dir, dot(d, self.axis_dir)))
        return d, norm(d)

    def signed_offset(self, p: Vec) -> float:
        """Distance from the surface, positive on the outward side."""
        if self.kind is HostKind.PLANE:
            o, n = self.origin, self.normal
            return (p[0] - o[0]) * n[0] + (p[1] - o[1]) * n[1] + (p[2] - o[2]) * n[2]
        _, rad = self._radial(p)
        return self.curv_sign * (rad - self.radius)

    def in_plane(self, p: Vec) -> tuple[float, float]:
        """Surface (u, v): the orthographic projection on a plane, the
        isometric unrolling (R*theta, axial) on a cylinder."""
        if self.kind is HostKind.PLANE:
            d = sub(p, self.origin)
            return dot(d, self.u), dot(d, self.v)
        d0 = sub(p, self.axis_loc)
        sv = dot(d0, self.axis_dir)
        w, rad = self._radial(p)
        if rad < 1e-9:
            return 0.0, sv
        theta = math.atan2(dot(w, self.ref_perp), dot(w, self.ref_dir))
        return self.curv_sign * self.radius * theta, sv

    def normal_at(self, p: Vec) -> Vec:
        """The OUTWARD normal at a point: constant on a plane, radial on a
        cylinder. "Faces the way the host faces" is a local statement on a
        curved host."""
        if self.kind is HostKind.PLANE:
            return self.normal
        w, rad = self._radial(p)
        if rad < 1e-9:
            return self.normal
        n = unit(w)
        if self.curv_sign < 0.0:
            n = neg(n)
        return n


@dataclass
class ReliefRegion:
    """One connected relief component on one host (Stage 1 output)."""

    faces: list = field(default_factory=list)
    ink: list = field(default_factory=list)
    walls: list = field(default_factory=list)
    ink_kind: InkKind = InkKind.NONE

    proud: bool = False
    depth_mm: float = 0.0
    level_mean: float = 0.0
    level_spread_mm: float = 0.0
    reach_mm: float = 0.0

    ink_area_mm2: float = 0.0
    ink_perimeter_mm: float = 0.0
    stroke_w_mm: float = 0.0
    max_patch_w_mm: float = 0.0
    diag_mm: float = 0.0
    extent_u_mm: float = 0.0
    extent_v_mm: float = 0.0
    bbox_area_mm2: float = 0.0
    fill: float = 0.0
    slenderness: float = 0.0
    hole_count: int = 0
    curved_faces: int = 0
    centroid_u: float = 0.0
    centroid_v: float = 0.0
    host_coverage: float = 1.0

    process: Process = Process.UNDECIDED
    process_evidence: list[str] = field(default_factory=list)
    mirror_gap_mm: float = 0.0

    ink_carries_pmi: bool = False
    distinct_colour: bool = False

    # Position in the scene's region list: the identity the reference's
    # pointer comparisons and pointer-keyed ordered containers stand for.
    index: int = -1

    def face_ids(self) -> list[int]:
        return [node.face_id for node in self.faces]


@dataclass
class MarkingCluster:
    """A scored cluster of regions (Stage 3 output)."""

    host: Optional[HostSurface] = None
    regions: list = field(default_factory=list)
    process: Process = Process.UNDECIDED
    proud: bool = False

    depth_mm: float = 0.0
    stroke_w_mm: float = 0.0
    cap_height_mm: float = 0.0
    # The run's own tracking: median nearest-neighbour edge gap between the
    # clustered regions. Zero when the cluster is not a run.
    char_gap_mm: float = 0.0
    diag_mm: float = 0.0
    fill: float = 0.0
    footprint_host_ratio: float = 0.0

    depth_cv: float = 0.0
    cap_height_cv: float = 0.0
    baseline_residual_ratio: float = 0.0
    spacing_regularity: float = 0.0
    slenderness_med: float = 0.0
    baseline_dir: tuple = (0.0, 0.0, 0.0)

    # Punctuation-scale companions, counted but not clustered.
    satellite_count: int = 0
    satellites: list = field(default_factory=list)
    # The satellites whose faces this cluster claims: a separate list on
    # purpose, so deduplicating a claim never decrements the character count.
    claimed_satellites: list = field(default_factory=list)
    # Regions the mark's EXTENT absorbed by containment. They contribute
    # faces to the claim and nothing to any measurement.
    absorbed: list = field(default_factory=list)

    text_score: float = 0.0
    mark_score: float = 0.0
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)

    note_backed: bool = False
    tier: Tier = Tier.C
    confidence: float = 0.0
    decoded_text: str = ""
    verification_method: str = ""

    def face_ids(self) -> list[int]:
        out = [n.face_id for r in self.regions for n in r.faces]
        out += [n.face_id for r in self.absorbed for n in r.faces]
        out += [n.face_id for r in self.claimed_satellites for n in r.faces]
        return out


@dataclass
class Rejection:
    """A Tier-C record: which gate failed and by how much. Diagnostic only."""

    stage: str
    gate: str
    detail: str
    margin: float = 0.0
    host_face_id: Optional[int] = None
    face_ids: list[int] = field(default_factory=list)
