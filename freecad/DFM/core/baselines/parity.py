# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""How far this engine stands from the one it was ported from.

``dfm_baseline.json`` and :mod:`.spec` answer "did we change", which is a
question about yesterday. This answers "are we right", which is a question
about the reference: the C++ engine's own approved answers, one JSON file
per fixture, recorded by its ``expected_checker.cpp`` in a schema this
workbench deliberately kept key for key. A recorded baseline can be green
for a year while every number in it disagrees with the reference, and
nothing in the regression harness can ever say so. That is not a defect in
it -- a regression gate that also failed on a known, catalogued,
deliberately-parked difference would be turned off within a week -- but it
does leave the other half of the question unasked, which is what this is
for.

Two things have to be settled before any comparison here means anything.

**The rule identifiers are spelled differently on the two sides.** Ours are
``THIN_WALL``, the reference's are ``thin_wall``, and compared literally
every single rule reads as a total disagreement in both directions at once.
Case is most of it and is handled mechanically. Seven rules were also
genuinely renamed during the port and have to be stated by hand; see
:data:`RENAMED_RULES`.

**An absent field is not a zero.** The reference's schema omits what it does
not assert, and its own checker honours that: ``feature_count``,
``severity``, ``features``, ``pmi`` and ``part_process_type`` are compared
only where present, while ``by_rule`` and ``by_category`` are compared
unconditionally because an absent map there means "this part published
nothing", which is a claim rather than a silence. Treating a missing
``features`` block as an assertion that the part has no features would
manufacture a disagreement on the 182 goldens that simply predate the
block.

And one thing cannot be settled at all, only declared: see
:data:`COUNTING_CAVEAT`. The two sides do not count the same objects.

Nothing here runs a rule, opens a shape or touches the reference tree. It
compares two recordings, and the reference tree is read-only in every sense
that matters: it is somebody else's repository, it is the standard being
measured against, and a measuring instrument that edits what it measures is
not one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .spec import BaselineSpec


# =============================================================================
# The reference's rule vocabulary
# =============================================================================

#: Every rule identifier the reference engine can emit, read out of the
#: ``rule_id()`` overrides in its ``include/dfm_rules/rules/*.h`` at
#: ``fa18e7fa`` on 2026-11-08. Ninety-two of them: the three added since the
#: 2026-09-14 snapshot are ``hole_breakout_scar``, ``knurl_present`` and
#: ``sheet_bend_operations``.
#:
#: This is a **snapshot of somebody else's repository**, copied here on
#: purpose rather than read live. Two things come of that. The tests stay
#: hermetic -- the reference tree is not checked out beside this one on
#: every machine, and a suite that silently skips its most important
#: assertion when a sibling directory is missing is worse than no assertion.
#: And a rule appearing in the reference that is not in this list becomes
#: visible: :func:`coverage` reports it, which is the only way anyone finds
#: out that the engine being ported from grew a rule while nobody was
#: looking. Refresh it deliberately, and read what changed before you do.
REFERENCE_RULE_IDS: frozenset[str] = frozenset(
    {
        "boss_height_risk",
        "boss_undercut",
        "boss_wall_thickness",
        "broached_slot_process",
        "casting_draft_angle",
        "chamfer_nonstandard_angle",
        "cutter_radius_infeasible",
        "cutter_radius_suboptimal",
        "feature_complexity",
        "flexure_slit_process",
        "freeform_finishing",
        "freeform_internal_radius",
        "gdt_datum_unresolved",
        "gdt_datum_valid",
        "gdt_feature_tolerance_mismatch",
        "gdt_surface_finish_conflict",
        "gdt_tolerance_achievable",
        "groove_square_corner",
        "hole_breakout_scar",
        "hole_countersink_angle",
        "hole_deep_risk",
        "hole_edge_distance",
        "hole_flat_bottom",
        "hole_intersecting",
        "hole_intersects_cavity",
        "hole_multi_pass",
        "hole_nonstandard_diameter",
        "hole_partial_entry",
        "hole_web_thickness",
        "knurl_present",
        "material_removal_high",
        "metal_seal_witness",
        "minimum_feature_size",
        "no_datum_face",
        "no_orthogonal_datum_trio",
        "no_parallel_datum_pair",
        "note_surface_finish_demanding",
        "part_aspect_ratio",
        "part_marking",
        "pocket_aspect_ratio",
        "pocket_deep_risk",
        "pocket_narrow_opening",
        "pocket_square_corner",
        "raised_text_machined_face",
        "rib_draft_angle",
        "rib_height_aspect",
        "sealed_void",
        "setup_count_high",
        "sharp_internal_edge",
        "sheet_bend_angle_extreme",
        "sheet_bend_longer_than_body",
        "sheet_bend_operations",
        "sheet_bend_radius_small",
        "sheet_bend_relief_missing",
        "sheet_closed_flange_loop",
        "sheet_corner_relief_missing",
        "sheet_countersink_deep",
        "sheet_emboss_deep",
        "sheet_feature_complexity",
        "sheet_flange_short",
        "sheet_formed_feature",
        "sheet_formed_near_bend",
        "sheet_formed_pitch",
        "sheet_hem_dimensions",
        "sheet_hole_near_bend",
        "sheet_hole_pitch",
        "sheet_hole_small",
        "sheet_intent_sharp_corners",
        "sheet_louver_tall",
        "sheet_machined_feature",
        "sheet_notch_narrow",
        "sheet_sharp_fold",
        "sheet_tab_narrow",
        "sheet_thickness_out_of_range",
        "slot_deep_risk",
        "slot_nonstandard_width",
        "slot_overhang",
        "small_part_holding",
        "spherical_pocket_undercut",
        "surface_finish_conflict",
        "surface_finish_per_face_demanding",
        "thin_clamping_dimension",
        "thin_wall",
        "thread_pmi_geometry_mismatch",
        "thread_relief_width",
        "thread_runout_insufficient",
        "thread_shoulder_proximity",
        "thread_wall_thickness",
        "tool_access_blocked",
        "tool_access_special_setup",
        "turned_profile_radius",
        "undercut_present",
    }
)


# =============================================================================
# The alias map
# =============================================================================

#: Rules that changed name during the port, reference spelling to ours.
#:
#: Everything else pairs up mechanically -- lower-case our enum member and
#: you have the reference's identifier -- and :func:`alias_map` derives those
#: from the ``Rulebook`` rather than restating them here, so a rule added on
#: either side needs no edit to this file to be paired. These seven are the
#: ones no mechanical rule can reach, and each is a deliberate rename with a
#: reason worth keeping.
#:
#: Six of the seven share one reason. Every rule in this workbench is an
#: editable row in the material editor, and the row is named after *the
#: quantity somebody types a number into*, not after the verdict the rule
#: reaches. "Boss Height Risk" is not something you can enter a figure for;
#: "Boss Height Ratio" is, and the figure is the ratio. ``pocket_square_corner``
#: is the clearest of them: the editable value is a corner radius in
#: millimetres, and "square corner" names only the failure you get without
#: one.
#:
#: Getting one of these wrong is expensive and quiet. An entry pointing at
#: the wrong rule reads as two rules in perfect agreement that are not being
#: compared at all; a missing entry inflates the apparent gap by the full
#: count of the rule in both directions at once, which is roughly 300
#: occurrences over this corpus for the six that were already known.
RENAMED_RULES: dict[str, str] = {
    "boss_height_risk": "BOSS_HEIGHT_RATIO",
    "hole_deep_risk": "HOLE_DEPTH_RATIO",
    "material_removal_high": "MATERIAL_REMOVAL",
    "pocket_deep_risk": "POCKET_DEPTH_RATIO",
    "pocket_square_corner": "POCKET_CORNER_RADIUS",
    "slot_deep_risk": "SLOT_DEPTH_RATIO",
    "thread_runout_insufficient": "THREAD_RUNOUT",
}

#: Why each rename happened, for whoever has to decide whether a future one
#: belongs in the table above or is a genuinely different rule.
RENAME_NOTES: dict[str, str] = {
    "boss_height_risk": (
        "the editable figure is the height-to-width ratio, not a risk; the "
        "material editor renders it as a target and a ceiling"
    ),
    "hole_deep_risk": (
        "same rename as the boss: depth over diameter is the number a shop "
        "sets, 'deep' is the verdict it produces"
    ),
    "material_removal_high": (
        "the row is a percentage of stock removed with its own target and "
        "ceiling, so 'high' was already carried by the ceiling"
    ),
    "pocket_deep_risk": "depth over width, named after the ratio it measures",
    "pocket_square_corner": (
        "the editable value is a minimum corner radius in millimetres; a "
        "square corner is what you get when it is not met"
    ),
    "slot_deep_risk": "depth over width, named after the ratio it measures",
    "thread_runout_insufficient": (
        "the row is a clearance in millimetres below a tapped blind hole; "
        "the two rules compute the same pitch-multiple requirement with the "
        "same diameter fallback"
    ),
}

#: Reference rules with no counterpart here, and why not. Listed so the
#: report can separate "known, understood, and here is the reason" from "a
#: rule appeared on the other side and nobody has looked at it yet" -- which
#: is the whole point of an alias map that refuses to swallow strangers.
UNPAIRED_REFERENCE: dict[str, str] = {
    # Added to the reference between 2026-09-14 and fa18e7fa. These are
    # not exemptions: each entry is deleted in the same change that ports the
    # rule, and an entry still here is a rule still owed.
}

#: Ours with no counterpart in the reference, and why not. All eight predate
#: the machining port: they are the moulding and 3D-printing rules the addon
#: shipped with, and the reference engine has never had a moulding or an FDM
#: process to run them under. See docs/parity-tasks.md §6 -- registered,
#: tested, and correctly unreachable from any process this corpus uses.
UNPAIRED_OURS: dict[str, str] = {
    "MAX_BRIDGE_SPAN": "3D-printing rule; no FDM process definition runs it",
    "MAX_OVERHANG_ANGLE": "3D-printing rule; no FDM process definition runs it",
    "MAX_WALL_THICKNESS": "moulding rule; machining has no upper wall limit",
    "MIN_DRAFT_ANGLE": (
        "moulding draft. The machining side asks the same question through "
        "CASTING_DRAFT_ANGLE and RIB_DRAFT_ANGLE, which the reference has"
    ),
    "MIN_WALL_THICKNESS": "moulding rule; machining asks it through THIN_WALL",
    "NO_UNDERCUTS": (
        "moulding undercut. The machining side asks it through "
        "UNDERCUT_PRESENT and BOSS_UNDERCUT, which the reference has"
    ),
    "SHARP_EXTERNAL_CORNERS": "moulding rule; no machining process runs it",
    "SHARP_INTERNAL_CORNERS": (
        "moulding rule. Not SHARP_INTERNAL_EDGE, which is the machining rule "
        "of a similar name and does pair with the reference"
    ),
}


# The moulding and 3D-printing rules of the feature-recognition engine
# (core/recognition). They run only under the moulding, FDM, resin and
# powder-bed processes, none of which the reference has.
UNPAIRED_OURS.update({
    "BED_EDGE_CHAMFER": "FDM rule; the reference has no such process",
    "BOSS_HOLE_LEAD_IN": "moulding rule; the reference has no such process",
    "HORIZONTAL_HOLE_TEARDROP": "FDM rule; the reference has no such process",
    "MAX_BED_CONTACT_AREA": "FDM rule; the reference has no such process",
    "MAX_BOSS_HEIGHT_RATIO": "moulding rule; the reference has no such process",
    "MAX_BOSS_WALL_RATIO": "moulding rule; the reference has no such process",
    "MAX_CUP_AREA": "resin rule; the reference has no such process",
    "MAX_CUSP_HEIGHT": "FDM rule; the reference has no such process",
    "MAX_FLAT_FACE_AREA": "powder-bed / resin rule; the reference has no such process",
    "MAX_HOLE_DEPTH_RATIO": "moulding rule; the reference has no such process",
    "MAX_LIVING_HINGE_WEB": "moulding rule; the reference has no such process",
    "MAX_PART_ASPECT_RATIO": "powder-bed rule; the reference has no such process",
    "MAX_PIN_SLENDERNESS": "FDM / powder-bed / resin rule; the reference has no such process",
    "MAX_PRINTED_HOLE_DEPTH": "powder-bed / resin rule; the reference has no such process",
    "MAX_RIB_HEIGHT_RATIO": "moulding rule; the reference has no such process",
    "MAX_RIB_THICKNESS_RATIO": "moulding rule; the reference has no such process",
    "MAX_SNAP_FIT_STRAIN": "moulding rule; the reference has no such process",
    "MAX_SUPPORT_MATERIAL": "FDM rule; the reference has no such process",
    "MAX_WALL_THICKNESS_VARIATION": "moulding / powder-bed rule; the reference has no such process",
    "MIN_BOSS_CORE_DEPTH": "moulding rule; the reference has no such process",
    "MIN_BOSS_DRAFT": "moulding rule; the reference has no such process",
    "MIN_BOSS_HOLE_BASE_RADIUS": "moulding rule; the reference has no such process",
    "MIN_BOSS_OD_RATIO": "moulding rule; the reference has no such process",
    "MIN_BOSS_ROOT_RADIUS": "moulding rule; the reference has no such process",
    "MIN_BOSS_SPACING": "moulding rule; the reference has no such process",
    "MIN_ESCAPE_HOLE_DIAMETER": "powder-bed / resin rule; the reference has no such process",
    "MIN_HOLE_EDGE_DISTANCE": "moulding rule; the reference has no such process",
    "MIN_INTERNAL_CORNER_RADIUS": "moulding rule; the reference has no such process",
    "MIN_MOLD_STEEL_THICKNESS": "moulding rule; the reference has no such process",
    "MIN_PIN_DIAMETER": "FDM / powder-bed / resin rule; the reference has no such process",
    "MIN_PRINTED_HOLE_DIAMETER": "FDM / powder-bed / resin rule; the reference has no such process",
    "MIN_PRINTED_TEXT_DEPTH": "FDM / powder-bed / resin rule; the reference has no such process",
    "MIN_PRINTED_TEXT_STROKE": "FDM / powder-bed / resin rule; the reference has no such process",
    "MIN_PRINT_GAP": "FDM / powder-bed / resin rule; the reference has no such process",
    "MIN_RIB_DRAFT": "moulding rule; the reference has no such process",
    "MIN_RIB_ROOT_RADIUS": "moulding rule; the reference has no such process",
    "MIN_RIB_SPACING": "moulding rule; the reference has no such process",
    "MIN_TEXT_STROKE": "moulding rule; the reference has no such process",
    "PIN_ROOT_FILLET": "FDM / powder-bed / resin rule; the reference has no such process",
    "SEALED_CAVITY": "powder-bed / resin rule; the reference has no such process",
    "STANDALONE_BOSS": "moulding rule; the reference has no such process",
})


@dataclass(frozen=True)
class AliasMap:
    """Reference rule identifiers translated into ours, and back.

    Built rather than written down: :func:`alias_map` walks the ``Rulebook``
    and lower-cases it, then lets :data:`RENAMED_RULES` override. A rule
    added to either engine therefore needs an edit here only if it was
    *renamed*, and a rule added to both under matching names needs none at
    all -- which is what keeps this table from quietly going stale, the
    failure mode that makes alias maps worse than no alias map.
    """

    to_ours: dict[str, str] = field(default_factory=dict)
    to_reference: dict[str, str] = field(default_factory=dict)

    def translate(self, reference_rule: str) -> Optional[str]:
        """Our spelling of a reference rule, or ``None`` if we have none.

        ``None`` is never a licence to drop the rule. Every caller here
        collects it into an unmapped list that the report prints; a rule
        that vanishes between two engines because a lookup missed is a whole
        class of finding disappearing with nothing to show for it.
        """
        return self.to_ours.get(reference_rule)


def alias_map(our_rules: Optional[Iterable[str]] = None) -> AliasMap:
    """Build the reference-to-ours rule alias map.

    ``our_rules`` defaults to every ``Rulebook`` member's name, which is the
    authoritative list of what this engine can emit. It is a parameter only
    so the tests can drive the derivation without standing up the enum.

    The renames are applied last and win, in both directions. That matters
    on one entry: ``MATERIAL_REMOVAL`` lower-cases to ``material_removal``,
    which is a perfectly good mechanical pairing that the reference has
    simply never used, so the forward map carries both spellings and the
    reverse map has to be told which one the reference actually emits.
    """
    if our_rules is None:
        from ..rules import Rulebook

        our_rules = [rule.name for rule in Rulebook]

    names = sorted(our_rules)
    to_ours = {name.lower(): name for name in names}
    # Reverse first from the mechanical pairing, so that every rule has an
    # answer, then let the renames overwrite the seven that need it.
    to_reference = {name: name.lower() for name in names}

    for reference_id, our_name in RENAMED_RULES.items():
        to_ours[reference_id] = our_name
        to_reference[our_name] = reference_id

    return AliasMap(to_ours=to_ours, to_reference=to_reference)


@dataclass
class AliasCoverage:
    """What the alias map does and does not account for, in both directions.

    Both unmapped lists are split in two. ``*_expected`` are the identifiers
    already understood and written down in :data:`UNPAIRED_REFERENCE` and
    :data:`UNPAIRED_OURS`; ``*_unexplained`` are the rest, and those are the
    ones worth interrupting somebody over. A rule that turns up on one side
    and not the other is either a port that has not happened or a rename
    nobody recorded, and both stay invisible for as long as the tooling is
    willing to shrug at an identifier it does not recognise.
    """

    mapped: dict[str, str] = field(default_factory=dict)
    reference_unmapped_expected: list[str] = field(default_factory=list)
    reference_unmapped_unexplained: list[str] = field(default_factory=list)
    ours_unmapped_expected: list[str] = field(default_factory=list)
    ours_unmapped_unexplained: list[str] = field(default_factory=list)
    #: Reference identifiers seen in the goldens that :data:`REFERENCE_RULE_IDS`
    #: does not list -- the reference grew a rule since the snapshot, or the
    #: snapshot was taken against a different checkout.
    unknown_to_snapshot: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing unexplained is unaccounted for on either side."""
        return not (
            self.reference_unmapped_unexplained
            or self.ours_unmapped_unexplained
            or self.unknown_to_snapshot
        )


def coverage(
    aliases: AliasMap,
    reference_ids: Iterable[str],
    our_ids: Iterable[str],
) -> AliasCoverage:
    """Account for every rule identifier on both sides.

    ``reference_ids`` and ``our_ids`` are the two vocabularies being paired
    -- ordinarily :data:`REFERENCE_RULE_IDS` against every ``Rulebook``
    member, though the CLI also passes the narrower sets it actually
    observed so that a run can say which rules the corpus exercises.

    The asymmetry is deliberate. A reference rule with no translation is
    unmapped outright. One of ours is unmapped when *no* reference
    identifier in the given set translates to it, which is not the same as
    having no lower-cased twin: it is the question that matters, because a
    rule of ours the reference cannot express is a rule nothing over there
    will ever corroborate.
    """
    reference = sorted(set(reference_ids))
    ours = sorted(set(our_ids))

    mapped: dict[str, str] = {}
    reference_unmapped: list[str] = []
    claimed: set[str] = set()
    for reference_id in reference:
        our_name = aliases.translate(reference_id)
        if our_name is None:
            reference_unmapped.append(reference_id)
            continue
        mapped[reference_id] = our_name
        claimed.add(our_name)

    ours_unmapped = [name for name in ours if name not in claimed]

    return AliasCoverage(
        mapped=mapped,
        reference_unmapped_expected=[
            r for r in reference_unmapped if r in UNPAIRED_REFERENCE
        ],
        reference_unmapped_unexplained=[
            r for r in reference_unmapped if r not in UNPAIRED_REFERENCE
        ],
        ours_unmapped_expected=[n for n in ours_unmapped if n in UNPAIRED_OURS],
        ours_unmapped_unexplained=[n for n in ours_unmapped if n not in UNPAIRED_OURS],
        unknown_to_snapshot=[r for r in reference if r not in REFERENCE_RULE_IDS],
    )


def translate_by_rule(
    aliases: AliasMap, by_rule: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """A reference ``by_rule`` map in our spelling, plus what would not translate.

    Returns ``(translated, untranslatable)``. The second is returned rather
    than logged or dropped because the caller is the only thing that knows
    whether an unknown rule with four findings on it is worth failing over,
    and because a comparison that silently discards the rules it cannot read
    reports its best numbers on its worst days.

    Two reference identifiers translating to the same rule of ours -- which
    no current entry does, but a future rename could introduce -- are summed
    rather than one overwriting the other. Losing half a rule's occurrences
    to a dictionary collision is precisely the kind of quiet wrong answer
    this module exists to prevent.
    """
    translated: dict[str, int] = {}
    untranslatable: dict[str, int] = {}
    for reference_id, count in by_rule.items():
        our_name = aliases.translate(reference_id)
        if our_name is None:
            untranslatable[reference_id] = untranslatable.get(reference_id, 0) + count
            continue
        translated[our_name] = translated.get(our_name, 0) + count
    return translated, untranslatable


# =============================================================================
# The comparison
# =============================================================================

#: The fields compared, in report order. ``pmi`` is deliberately absent; see
#: :data:`SKIPPED_FIELDS`.
COMPARED_FIELDS = ("feature_count", "severity", "by_category", "by_rule", "features")

#: Which compared fields count published rows, and are therefore subject to
#: :data:`COUNTING_CAVEAT`. ``feature_count`` and ``features`` are not: they
#: count what the recognizers found, before anything is published, and are
#: the two axes on which a magnitude here can be read at face value.
ROW_COUNTED_FIELDS = ("severity", "by_category", "by_rule")

#: The one thing this comparison cannot correct for, stated rather than
#: buried.
#:
#: The reference engine collapses findings that carry identical details into
#: a single published row, records the multiplicity in
#: ``details.occurrence_count`` and prefixes the message with ``48x:``. Its
#: ``expected/*.json`` tallies are taken *after* that pass, so they count
#: **rows**. This workbench does not aggregate at all: it publishes one
#: finding per occurrence, and ``dfm_baseline.json`` counts **occurrences**.
#:
#: So every finding-count field here compares one engine's rows against the
#: other's occurrences, and is inflated on our side by exactly the amount the
#: reference deduplicated. The effect is not small and it is not evenly
#: spread -- it lands entirely on patterned parts, where one rule firing on
#: forty-eight identical slots is forty-eight here and one there. On
#: ``turbine_compressor_disk`` that single difference accounts for 47 of the
#: part's 145, and the underlying rules agree exactly.
#:
#: Two things follow, and the report is built around both.
#:
#: **A magnitude on a rule that fires on both sides is not evidence of
#: anything** until somebody has checked whether the part is patterned. The
#: ranked tables therefore separate the parts where a rule fires on only one
#: side -- which aggregation cannot explain, because collapsing N rows into
#: one never reaches zero -- from the parts where both sides fire and only
#: the count differs.
#:
#: **``feature_count`` and ``features`` are clean.** They are recognition,
#: counted before anything is published, and nothing aggregates them.
#:
#: The figures in docs/parity-tasks.md were measured a different way: by
#: running both engines and summing ``occurrence_count`` off the reference's
#: own ``dfm.json`` output, which is where the multiplicities survive. That
#: is a better measurement and it is also one that takes a build of the
#: reference engine and several minutes. This takes a second and compares
#: two files, so it can run after every change. Neither replaces the other,
#: and a number from here should never be quoted against a number from there
#: without saying which is which.
COUNTING_CAVEAT = (
    "the reference collapses identical findings into one published row and "
    "counts rows; this engine publishes one finding per occurrence and counts "
    "occurrences. severity, by_category and by_rule magnitudes are inflated on "
    "our side by whatever the reference deduplicated, which on patterned parts "
    "is most of the difference. feature_count and features are unaffected."
)

#: What is not compared, and why. Printed by the CLI rather than left to be
#: noticed, because a field quietly missing from a parity report is
#: indistinguishable from a field in perfect agreement.
SKIPPED_FIELDS: dict[str, str] = {
    "pmi": (
        "nothing supplies annotations on this side yet -- a FreeCAD document "
        "has nowhere to keep a feature control frame, so annotations.py "
        "returns nothing for every part and every pmi block we record is {}. "
        "Forty-seven reference goldens carry one, and comparing against them "
        "would report the same known absence 47 times and drown everything "
        "else. Unskip this the day PMI extraction lands, not before"
    ),
    "part_process_type": (
        "categorical rather than counted, so it has no magnitude to rank and "
        "does not belong in the tables. Counted separately and reported on "
        "its own line, because a part the two engines send to different "
        "machines is a disagreement worth more than any single finding"
    ),
}


@dataclass(frozen=True)
class Difference:
    """One number on which the two engines disagree.

    ``key`` carries *our* spelling for ``by_rule``, so that a difference can
    be grouped with the rule it belongs to and looked up in the Rulebook
    without the reader having to remember which of the seven renames applies.
    """

    fixture: str
    field: str
    key: str
    ours: int
    reference: int

    @property
    def delta(self) -> int:
        """Ours minus the reference: positive means we report more."""
        return self.ours - self.reference

    @property
    def magnitude(self) -> int:
        return abs(self.delta)

    @property
    def one_sided(self) -> bool:
        """True when one side is silent and the other is not.

        The distinction that survives :data:`COUNTING_CAVEAT`. Collapsing N
        identical rows into one never reaches zero, so a rule firing here
        and nowhere there -- or there and nowhere here -- is a real
        difference in what the two engines found, whatever the counting
        convention. A difference where both sides fire may be nothing but
        the reference having deduplicated a patterned part.
        """
        return self.ours == 0 or self.reference == 0

    @property
    def label(self) -> str:
        return f"{self.field}.{self.key}" if self.key else self.field

    def describe(self) -> str:
        """One line, in the shape the drift report uses.

        Not the drift report's exact wording. ``check_dfm_baselines.py``
        prints "expected X, got Y" because a baseline genuinely is the
        expectation; here neither side is, and phrasing the reference as
        "expected" would quietly assert that every one of these is ours to
        close, which docs/parity-tasks.md already records as untrue in at
        least two places.
        """
        sign = "+" if self.delta >= 0 else ""
        return (
            f"{self.label}: ours {self.ours}, reference {self.reference} "
            f"({sign}{self.delta})"
        )


@dataclass
class ParityResult:
    """What one fixture's two recordings say about each other."""

    fixture: str = ""
    differences: list[Difference] = field(default_factory=list)
    #: Reference rules the alias map could not translate, with their counts.
    #: Never folded into ``differences``: an untranslatable rule is a hole in
    #: the instrument, not a measurement, and reporting it as a −4 would
    #: invite somebody to go and close it.
    untranslatable: dict[str, int] = field(default_factory=dict)
    #: Fields the reference states no opinion on for this fixture, so that a
    #: fixture compared on two axes cannot be mistaken for one compared on
    #: five.
    not_asserted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.differences and not self.untranslatable

    @property
    def magnitude(self) -> int:
        """Total occurrences of disagreement, for ranking fixtures."""
        return sum(d.magnitude for d in self.differences)


def compare(
    ours: BaselineSpec,
    reference: BaselineSpec,
    aliases: Optional[AliasMap] = None,
) -> ParityResult:
    """Compare one of our baselines against the reference's golden.

    The absent/empty/zero distinction is the whole of the subtlety here, and
    it is taken directly from ``expected_checker.cpp`` rather than invented:

    * ``feature_count``, ``severity.*`` and ``features`` are **optional**.
      Absent on the reference side means it states no opinion and the axis
      is skipped -- recorded in ``not_asserted`` so the skip is visible.
      Present and zero, or present and empty, is an assertion and is
      compared.
    * ``by_rule`` and ``by_category`` are **not** optional. The reference
      writer omits an empty map, and its checker still walks our side of it
      looking for surplus keys, so an absent map there means "this part
      published nothing" and every rule we fire against it is a difference.
      Treating absent as no-opinion would let a part where the reference is
      entirely silent and we publish nine findings pass as agreement.

    Both maps are compared in both directions for the same reason
    :func:`.spec.check_drift` does: a rule that fires on exactly one side is
    the interesting case, and a one-directional walk sees only half of them.
    """
    aliases = aliases or alias_map()
    fixture = ours.fixture or reference.fixture
    result = ParityResult(fixture=fixture)

    def note(field_name: str, key: str, mine: int, theirs: int) -> None:
        if mine == theirs:
            return
        result.differences.append(
            Difference(
                fixture=fixture,
                field=field_name,
                key=key,
                ours=mine,
                reference=theirs,
            )
        )

    if reference.feature_count is None:
        result.not_asserted.append("feature_count")
    else:
        note("feature_count", "", ours.feature_count or 0, reference.feature_count)

    for key, mine, theirs in (
        ("error", ours.severity_error, reference.severity_error),
        ("warn", ours.severity_warn, reference.severity_warn),
        ("info", ours.severity_info, reference.severity_info),
    ):
        if theirs is None:
            result.not_asserted.append(f"severity.{key}")
            continue
        note("severity", key, mine or 0, theirs)

    _compare_map(note, "by_category", ours.by_category, reference.by_category)

    translated, untranslatable = translate_by_rule(aliases, reference.by_rule)
    result.untranslatable = untranslatable
    _compare_map(note, "by_rule", ours.by_rule, translated)

    # The feature block is the one the task of porting keeps getting wrong
    # in a way no other block can see: a recognizer that calls a louver a
    # lance, or grows a pocket where the reference leaves a step, moves
    # nothing in the finding counts at all. Comparing only feature_count
    # would miss every one of those, since a miscount of one type against an
    # overcount of another nets to zero.
    if reference.features is None:
        result.not_asserted.append("features")
    else:
        _compare_map(note, "features", ours.features or {}, reference.features)

    return result


def _compare_map(
    note, field_name: str, ours: dict[str, int], reference: dict[str, int]
) -> None:
    """One map, walked from both ends, in sorted order.

    Sorted so two runs over the same corpus print identically and the
    reports can be diffed; the union rather than either map alone so that a
    key present on exactly one side -- which is what a ported rule that
    never fires, or one that fires only here, looks like -- is compared
    against the zero it deserves rather than skipped.
    """
    for key in sorted(set(ours) | set(reference)):
        note(field_name, key, ours.get(key, 0), reference.get(key, 0))
