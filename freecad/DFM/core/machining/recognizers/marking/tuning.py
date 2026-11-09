# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Every constant the marking pipeline has, in one place.

The rule this module enforces: a constant that decides SHAPE is
dimensionless. The previous detector carried eight absolute-millimetre shape
constants, each tuned on some small part, and the same logo on a 60 mm plate
and on an 860 mm weldment is one mark -- any millimetre ceiling picks one of
them.

Four absolute numbers survive, each for a reason that is not about shape:
the B-rep noise floor, the coplanarity offset (same reason), the thickness
fallback used only when no back skin exists, and the marking-process depth
ceiling. Everything else is a ratio or an angle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# -- Absolute, and honestly so ------------------------------------------------

#: Below this a level difference is B-rep noise, not relief. It is a statement
#: about modelling precision, not a manufacturing threshold: the minimum
#: engraving depth is a rule evaluated on the reported depth, and a 0.15 mm
#: laser mark is still a marking operation.
NOISE_FLOOR_MM = 0.05

#: Two planar faces are one support surface when their outward normals agree
#: to COPLANAR_DOT and their offsets agree to this.
COPLANAR_OFFSET_MM = 0.05

#: Used only when no anti-parallel back skin overlaps the host at all (a solid
#: block face, a cast boss). The region then records that its thickness was
#: unresolved so no score claims a ratio it could not measure.
THICKNESS_FALLBACK_MM = 6.0

#: Marking depth comes from the TOOL, not the part: a V-bit, a laser, a
#: dot-peen pin and cast-in lettering all work in roughly 0.05-3 mm whatever
#: the part size. A purely dimensionless depth gate admitted a 40 mm cavity as
#: a "mark" because a scaled-up shape is scale-free-identical to the shape it
#: scales; only the process can separate them.
MAX_MARK_DEPTH_MM = 3.0

#: The Stage-1 search window is the depth ceiling plus headroom for a drafted
#: or round-bottomed stroke whose flat ledge sits above its true bottom.
SEARCH_MARGIN = 1.25

# -- Angles -------------------------------------------------------------------

#: About 15 degrees. Host-parallel (floor, top, island) against
#: host-perpendicular (stroke wall); everything tilted between is refused.
ALIGN_DOT = 0.966

#: Coplanarity of two planes' normals, about 1.8 degrees.
COPLANAR_DOT = 0.9995

#: Two stroke walls face each other across a V-cut when their in-plane outward
#: directions are this anti-parallel. Not the "faces away from the host" test,
#: which is a signed ALIGN_DOT against the host normal.
OPPOSING_DOT = -0.85

#: The share of the ink's in-plane rectangle that must lie over the host
#: claiming to carry it. A mark is applied TO a surface; ink running off its
#: own host means the host is a piece of the mark. The measured distribution
#: has no clean gap (the failure case reads 0.000, accepted marks 0.75-1.00,
#: and a thin continuum between), so it is placed for margin on the expensive
#: side: a false decline is a missed mark.
INK_WITHIN_HOST_MIN = 0.5

# -- Ratios: the depth family ---------------------------------------------------

#: A mark cannot remove or displace more than half the local wall.
DEPTH_THICKNESS_MAX = 0.5

#: depth / stroke width. Widened at the low end because a 0.1 mm laser mark on
#: a 3 mm stroke is still a marking operation.
DEPTH_STROKE_LO = 0.01
DEPTH_STROKE_LO_CORE = 0.05
DEPTH_STROKE_HI_CORE = 2.0
DEPTH_STROKE_HI = 5.0

#: One tool, one Z: a coefficient of variation over the regions' depths.
DEPTH_CV_MAX = 0.08

# -- Ratios: the shape family -------------------------------------------------

#: Slenderness: in-plane diagonal over stroke width (2A/P). A disc is 2.83,
#: Latin glyphs 3-25, a logotype 10-40, a hairline scribe or knurl groove above
#: 50. Real marking measured 4.8-21 against grooves and channels at 28-140.
SLENDER_LO = 2.0
SLENDER_LO_CORE = 3.0
SLENDER_HI_CORE = 25.0
SLENDER_HI = 45.0

#: Cluster fill: ink area over the in-plane box. Letter bands are sparse.
FILL_LO = 0.0
FILL_LO_CORE = 0.02
FILL_HI_CORE = 0.55
FILL_HI = 0.85

#: A fat, solid, single blob is a pocket. Both conditions must hold.
POCKET_SLENDER_MAX = 2.0
POCKET_FILL_MIN = 0.85

#: Component fill points the OTHER way from cluster fill: a glyph is a chunky
#: shape in its own box (0.40-0.59 measured), a long groove a ribbon through a
#: large box (0.06-0.17), a plateau or pocket floor fills it (0.79-1.00).
#: Scoring only the cluster scale produced fifteen false positives, nearly all
#: long thin grooves.
REGION_FILL_LO = 0.10
REGION_FILL_LO_CORE = 0.20
REGION_FILL_HI_CORE = 0.70
REGION_FILL_HI = 0.92

#: Boundary complexity: ink perimeter over twice the diagonal, which is a
#: font-independent count of how many strokes the mark is made of.
COMPLEXITY_LO = 0.8
COMPLEXITY_LO_CORE = 1.5
COMPLEXITY_HI_CORE = 100.0
COMPLEXITY_HI = 400.0

#: With fewer than three regions there is no baseline, so the geometry carries
#: the whole claim: a line of text measures about 2, a simple band about 1.
NO_BASELINE_COMPLEXITY_MIN = 1.75

#: And one connected region has no repetition evidence at all, so it must read
#: as several strokes outright (a floorless logo measured 2.9, a lone annular
#: proud face 1.8-2.3).
SINGLE_REGION_COMPLEXITY_MIN = 2.5

#: The absolute stroke ceiling, now only the FALLBACK for a candidate with no
#: cap height to divide by. Enforced on the widest single ink patch, never the
#: union mean, which averages a too-wide patch away. Every genuine mark in the
#: reference corpus measured at or under 3.13 mm; structural pedestals 4.0.
MAX_STROKE_WIDTH_MM = 3.5

#: Where a cap height exists, the ceiling is stroke over cap: stroke width is a
#: property of the TYPEFACE and scales with the lettering, so an absolute
#: ceiling rejected an ordinary 22 mm bold nameplate for being set large.
#: Genuine letterforms measured at or under 0.218, the nearest thing that must
#: not pass 0.347; the raster verifier derives the same 0.30 independently.
STROKE_CAP_MAX = 0.30

#: A minimum B-rep face count is one-sided-safe where a maximum is not:
#: splitting can only ever increase the count.
MIN_MEMBER_FACES = 6

#: Ink does not fill its own footprint, at the cluster level.
CLUSTER_FILL_MAX = 0.85

#: Typographic consistency; these feed the text hypothesis and gate nothing.
BASELINE_RESIDUAL_HI_CORE = 0.15
BASELINE_RESIDUAL_HI = 0.50
CAP_HEIGHT_CV_HI_CORE = 0.25
CAP_HEIGHT_CV_HI = 0.60

#: SWT's pairing constraint: stroke widths and heights within a factor of two.
PAIR_RATIO_MAX = 2.0

#: Clustering reach, in units of the region's own size. Deliberately generous:
#: related legends on one panel are one marking operation.
LINK_SPAN_FACTOR = 8.0

#: Footprint smallness, relative to the host surface OR to the part.
FOOTPRINT_HOST_HI_CORE = 0.35
FOOTPRINT_HOST_HI = 0.85
PART_DIAG_HI_CORE = 0.25
PART_DIAG_HI = 0.60


@dataclass(frozen=True)
class Weights:
    slenderness: float
    fill: float
    region_fill: float
    complexity: float
    depth_stroke: float
    depth_thickness: float
    level: float
    topology: float
    footprint: float
    baseline: float
    cap_height: float
    spacing: float
    member_count: float


#: Each table sums to 1.00, so a score reads as the share of the available
#: evidence the candidate has. The mark hypothesis needs no baseline, which is
#: the structural fix for merged logotypes being missed.
MARK_WEIGHTS = Weights(0.16, 0.16, 0.18, 0.12, 0.10, 0.06, 0.08, 0.04, 0.10,
                       0.00, 0.00, 0.00, 0.00)
TEXT_WEIGHTS = Weights(0.11, 0.08, 0.10, 0.05, 0.05, 0.03, 0.07, 0.02, 0.09,
                       0.16, 0.11, 0.06, 0.07)

#: File-side corroboration only ever RAISES a score.
COLOUR_BONUS = 0.08
NOTE_BONUS = 0.10

#: Tier B is where the human is.
TIER_B_SCORE = 0.62
#: Note-backed plus corroborating geometry reaches Tier A without a verifier.
TIER_A_NOTE_SCORE = 0.62


def band(x: float, lo: float, lo_core: float, hi_core: float, hi: float) -> float:
    """A trapezoid membership: soft edges instead of cliffs."""
    if x != x:  # NaN
        return 0.0
    if x <= lo or x >= hi:
        return 0.0
    if lo_core <= x <= hi_core:
        return 1.0
    if x < lo_core:
        return (x - lo) / (lo_core - lo) if lo_core > lo else 1.0
    return (hi - x) / (hi - hi_core) if hi > hi_core else 1.0


def band_low(x: float, hi_core: float, hi: float) -> float:
    """A one-sided "smaller is better" membership."""
    return band(x, -1e18, -1e18 + 1.0, hi_core, hi)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def median(values) -> float:
    """The upper median, as the reference takes it: v[n // 2] after sorting."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[len(ordered) // 2]


def cv_of(values) -> float:
    """Coefficient of variation, guarded against a zero mean."""
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if abs(mean) < 1e-9:
        return 0.0
    var = sum((x - mean) * (x - mean) for x in values) / len(values)
    return math.sqrt(var) / abs(mean)
