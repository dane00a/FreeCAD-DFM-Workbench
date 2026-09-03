# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about what the drawing asks for, rather than what the model is.

Every other machining rule reads the solid. These read the callouts on top of
it -- feature control frames, datum letters, finish notes -- and ask whether
the shop can hold them. A tolerance tighter than the machine's own error
budget is not a defect in the geometry; it is a first article that will fail
and a second, tighter process nobody quoted.

All of them are dormant. Callouts reach them through
:func:`~...machining.annotations.annotations_for`, and nothing supplies any
today, so on a real part every rule here returns nothing -- because there is
genuinely nothing to judge, not because the rule is a stub. That module's
docstring says what would have to be wired up to change that.
"""

from __future__ import annotations

from typing import Optional

from ...machining.aag import SurfaceType
from ...machining.annotations import (
    AnnotationSet,
    FeatureControlFrame,
    ToleranceCategory,
    ToleranceType,
    annotations_for,
    parse_ra_um,
)
from ...machining.config import MachiningConfig
from ...machining.context import MachiningContext
from ...machining.features import BORE_TYPES, FeatureInstance, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# How the shop says the machine mode out loud. The stored values are terse
# because they are written to preferences; a finding should read like speech.
_MACHINE_MODE_LABEL = {
    "3axis": "3-axis",
    "3plus2": "3+2 indexed",
    "5axis": "5-axis",
}

# Feature types carrying an intrinsic size dimension -- a diameter, a width, a
# thickness -- that a position tolerance can legitimately locate, at MMC or
# LMC or neither (ASME Y14.5). Anything else has no size to depart from.
#
#   BORE_TYPES     -- holes of every kind, plus the partial bore. A sub-180
#                     degree cradle is strictly not a feature of size, but a
#                     line-bore is routinely located by position and flagging
#                     that would be noise.
#   EXTERNAL_THREAD - located on its pitch diameter.
#   PATTERN        -- inherits its member holes' size ("4X dia 6 ...").
#   BOSS           -- an external pad, located by its own size.
#   SLOT / CHANNEL / BROACHED_SLOT / THROUGH_CAVITY -- the width is the size.
#                     CHANNEL is this workbench's name for a slot that runs
#                     off both ends of the part; its width is a size like any
#                     other slot's.
#   RIB            -- a standing wall whose thickness is a width-type feature
#                     of size, so position locating its centre plane is valid.
#
# What is left -- a fillet, a chamfer, a plain pocket or planar face, a
# spherical pocket -- has nothing for a position zone to locate.
_FEATURE_OF_SIZE_TYPES = BORE_TYPES | {
    FeatureType.EXTERNAL_THREAD,
    FeatureType.PATTERN,
    FeatureType.BOSS,
    FeatureType.SLOT,
    FeatureType.CHANNEL,
    FeatureType.BROACHED_SLOT,
    FeatureType.THROUGH_CAVITY,
    FeatureType.RIB,
}

# Which tolerance families imply a surface, and so a finish. A location
# tolerance says where the feature sits and nothing about how it is finished.
_FINISH_BEARING_CATEGORIES = frozenset(
    {ToleranceCategory.FORM, ToleranceCategory.PROFILE}
)


# =============================================================================
# Shared reading of the callouts
# =============================================================================


def _position_limit(config: MachiningConfig) -> float:
    """The tightest position tolerance this machine mode holds, in mm.

    An error budget, not a spindle spec: the fixture, the tool offsets and
    the repeatability of every re-clamp all land in it, which is why a mode
    that reaches five faces in one setup promises more than one that indexes.
    """
    thresholds = config.thresholds
    if config.machine_mode == "5axis":
        return thresholds.gdt_position_achievable_5axis_mm
    if config.machine_mode == "3plus2":
        return thresholds.gdt_position_achievable_3plus2_mm
    return thresholds.gdt_position_achievable_3axis_mm


def _form_limit(config: MachiningConfig) -> float:
    """The tightest form tolerance this machine mode holds, in mm."""
    thresholds = config.thresholds
    if config.machine_mode == "5axis":
        return thresholds.gdt_form_achievable_5axis_mm
    if config.machine_mode == "3plus2":
        return thresholds.gdt_form_achievable_3plus2_mm
    return thresholds.gdt_form_achievable_3axis_mm


def _mode_label(config: MachiningConfig) -> str:
    return _MACHINE_MODE_LABEL.get(config.machine_mode, config.machine_mode)


def _ra_verdict(ra_um: float, thresholds) -> tuple[Severity, str]:
    """Grade a finish callout and say what process it drives.

    The bands are the shop's, not the drawing's: what separates them is which
    machine the part has to visit after the mill, and that is the cost the
    reader is being warned about.
    """
    if ra_um <= thresholds.ra_lapping_um:
        return (
            Severity.ERROR,
            "No milling cutter leaves a surface that fine. This finish is "
            "lapping or super-finishing work, on a machine the mill cannot "
            "stand in for, so the part has to leave the cell to get it.",
        )
    if ra_um <= thresholds.ra_grinding_um:
        return (
            Severity.WARNING,
            "That is grinding territory, or at best a dedicated finish pass "
            "with a fresh tool and a light cut. Confirm the post-mill "
            "operation is quoted -- it is not part of a normal milling cycle.",
        )
    return (
        Severity.INFO,
        f"That sits under the roughly Ra {thresholds.ra_standard_mill_um:g} um "
        "a normal milling cycle leaves, so it needs a controlled finish pass: "
        "a sharp tool, a light stepover and a slower feed on the last cut.",
    )


def _frame_faces(frame: FeatureControlFrame, feature: Optional[FeatureInstance]):
    """Geometry to highlight for a frame: its own faces, else its feature's."""
    if frame.face_ids:
        return sorted(frame.face_ids)
    return sorted(feature.faces) if feature is not None else []


def _feature_index(context: MachiningContext) -> dict[str, FeatureInstance]:
    return {feature.instance_id: feature for feature in context.recognition.features}


def _describe(frame: FeatureControlFrame, feature: Optional[FeatureInstance]) -> str:
    """How to name what a frame is attached to, in a machinist's terms."""
    if feature is not None:
        return f"{feature.type.lower().replace('_', ' ')} {feature.instance_id}"
    if frame.face_ids:
        count = len(frame.face_ids)
        return f"{count} face{'' if count == 1 else 's'}"
    return "the part"


def _callouts(context: MachiningContext) -> AnnotationSet:
    return annotations_for(context)


# =============================================================================
# Tolerance capability
# =============================================================================


@register_check(Rulebook.GDT_TOLERANCE_ACHIEVABLE)
class GdtToleranceAchievableCheck(MachiningCheck):
    """A tolerance the machine cannot hold is a first article that fails.

    Location tolerances are judged against the machine's positioning budget
    and everything else against its form budget, because those two errors
    come from different places: one from the fixture and the re-clamps, the
    other from the spindle and the way the tool deflects.
    """

    @property
    def name(self) -> str:
        return "GD&T Tolerance Achievable Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.frames:
            return []

        # One editor field cannot express the position/form split, so a shop
        # that fills it in is stating a single floor for both. Left blank,
        # each family is judged against its own machine-mode threshold.
        override = self.safe_float(rule_config.limit)
        position_limit = override if override is not None else _position_limit(context.config)
        form_limit = override if override is not None else _form_limit(context.config)
        mode = _mode_label(context.config)

        features = _feature_index(context)
        results: list[CheckResult] = []

        for frame in callouts.sorted_frames():
            tolerance = float(frame.tolerance_value_mm)
            if tolerance <= 0.0:
                continue  # no zone stated, nothing to compare

            is_location = frame.category == ToleranceCategory.LOCATION
            limit = position_limit if is_location else form_limit
            if tolerance >= limit:
                continue

            feature = features.get(frame.feature_id)
            subject = _describe(frame, feature)
            budget = (
                "once the fixture, the tool offsets and every re-clamp are "
                "counted"
                if is_location
                else "once spindle runout and tool deflection are counted"
            )
            severity = Severity.WARNING

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{tolerance:.3f} mm vs {limit:.3f} mm",
                    self.render(
                        feedback,
                        severity,
                        tolerance,
                        limit,
                        limit,
                        "mm",
                        f"The {frame.type.replace('_', ' ')} callout on {subject} "
                        f"asks for {tolerance:.3f} mm. A {mode} machine holds about "
                        f"{limit:.3f} mm on this kind of control {budget}, so as "
                        "drawn the part cannot be shown capable off this machine -- "
                        "expect it back from first article. Opening the zone to "
                        f"{limit:.3f} mm, moving the job to a machine with a "
                        "tighter budget, or adding a finishing operation held to "
                        "the drawing would each close the gap.",
                    ),
                    faces=_frame_faces(frame, feature),
                    value=tolerance,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


# =============================================================================
# Datums
# =============================================================================


@register_check(Rulebook.GDT_DATUM_VALID)
class GdtDatumValidCheck(MachiningCheck):
    """A reference frame needs real surfaces to sit on.

    Counted rather than matched by letter: nothing in the model carries datum
    labels, so the question this can honestly answer is whether the part even
    has enough substantial flats to build the frame the drawing describes.
    Three datums called up on a part with one usable flat is a fixture
    problem before it is an inspection problem.
    """

    @property
    def name(self) -> str:
        return "GD&T Datum Valid Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.frames:
            return []

        minimum_area = context.config.thresholds.datum_face_min_area_mm2
        candidates = [
            node.face_id
            for node in context.graph.nodes
            if node.surface_type is SurfaceType.PLANE and node.area >= minimum_area
        ]
        available = len(candidates)

        features = _feature_index(context)
        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for frame in callouts.sorted_frames():
            refs = [ref for ref in frame.datum_refs if ref]
            if not refs or available >= len(refs):
                continue

            feature = features.get(frame.feature_id)
            listed = ", ".join(refs)
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{len(refs)} datums, {available} usable flats",
                    self.render(
                        feedback,
                        severity,
                        float(available),
                        float(len(refs)),
                        float(len(refs)),
                        "",
                        f"The {frame.type.replace('_', ' ')} callout on "
                        f"{_describe(frame, feature)} is measured from {len(refs)} "
                        f"datums ({listed}), but the part carries only {available} "
                        f"flat face{'' if available == 1 else 's'} of at least "
                        f"{minimum_area:.0f} mm2 to establish them on. There is not "
                        "enough surface to build the reference frame, so the "
                        "fixture cannot repeat and the inspection cannot be set up "
                        "as drawn. Either the datums belong on different features, "
                        "or the part needs machined pads to locate from.",
                    ),
                    faces=sorted(candidates),
                    value=float(available),
                    limit=float(len(refs)),
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.GDT_DATUM_UNRESOLVED)
class GdtDatumUnresolvedCheck(MachiningCheck):
    """A datum letter that points at nothing.

    The letter parses, the geometry link does not -- the datum is a symbol on
    the drawing with no face behind it. Everything measured from it is then
    uninspectable, which is why the finding names the tolerances left adrift
    rather than just the letter.

    Datum targets are exempt: a target is anchored by its own placement
    point, so carrying no face is correct for one.
    """

    @property
    def name(self) -> str:
        return "GD&T Datum Unresolved Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.datums:
            return []

        # A datum nothing cites locates nothing, so its failing to resolve
        # costs nobody anything. Flagging it would be noise.
        cited = callouts.cited_datum_labels()
        if not cited:
            return []

        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for datum in callouts.sorted_datums():
            if datum.is_target or datum.face_ids:
                continue
            if not datum.label or datum.label not in cited:
                continue

            orphaned = sorted(
                frame.label
                for frame in callouts.frames
                if datum.label in frame.datum_refs
            )
            count = len(orphaned)
            listed = ", ".join(orphaned)

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"datum {datum.label} unattached",
                    self.render(
                        feedback,
                        severity,
                        float(count),
                        0.0,
                        0.0,
                        "",
                        f"Datum {datum.label} is referenced by {count} "
                        f"tolerance{'' if count == 1 else 's'} ({listed}) but is not "
                        "attached to any face in the model. The reference frame "
                        "cannot be established, so none of those tolerances can be "
                        "inspected as drawn -- there is nothing to set the part up "
                        "against. Check that the datum in CAD is associated with "
                        "geometry rather than being a drawing symbol only, and "
                        "re-export.",
                    ),
                    value=float(count),
                    comparison=">",
                )
            )
        return results


# =============================================================================
# Finish implied by a tolerance
# =============================================================================


@register_check(Rulebook.GDT_SURFACE_FINISH_CONFLICT)
class GdtSurfaceFinishConflictCheck(MachiningCheck):
    """A form tolerance tight enough to demand a second machine.

    Form and profile control the surface itself, so a very tight one carries
    a finish requirement whether or not anybody wrote an Ra on the drawing.
    Below the grinding threshold a milled surface will not hold the form;
    below the lapping one nothing but lapping will.

    The severity comes from which process the tolerance drives rather than
    from the rule's configured one -- an ERROR and a WARNING here mean two
    different machines, not two strengths of opinion.
    """

    @property
    def name(self) -> str:
        return "GD&T Surface Finish Conflict Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.frames:
            return []

        thresholds = context.config.thresholds
        grinding = thresholds.tol_grinding_max_mm
        lapping = thresholds.tol_lapping_max_mm

        features = _feature_index(context)
        results: list[CheckResult] = []

        for frame in callouts.sorted_frames():
            tolerance = float(frame.tolerance_value_mm)
            if tolerance <= 0.0:
                continue
            if frame.category not in _FINISH_BEARING_CATEGORIES:
                continue
            if tolerance >= grinding:
                continue

            feature = features.get(frame.feature_id)
            subject = _describe(frame, feature)
            control = frame.type.replace("_", " ")

            if tolerance < lapping:
                severity = Severity.ERROR
                limit = lapping
                advice = (
                    f"The {control} callout on {subject} holds the surface to "
                    f"{tolerance:.4f} mm. A milled face does not stay that flat "
                    "once it comes off the cutter and relaxes, and no finish pass "
                    "recovers it -- this is lapping or super-finishing work on a "
                    "separate machine. Confirm the operation is quoted, or open "
                    "the tolerance."
                )
            else:
                severity = Severity.WARNING
                limit = grinding
                advice = (
                    f"The {control} callout on {subject} holds the surface to "
                    f"{tolerance:.4f} mm. That is below what milling reliably "
                    "leaves, so plan on grinding the face after machining. Leave "
                    "grind stock on it, and expect the extra operation in the "
                    "price and the lead time."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{tolerance:.4f} mm {control}",
                    self.render(feedback, severity, tolerance, limit, limit, "mm", advice),
                    faces=_frame_faces(frame, feature),
                    value=tolerance,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


# =============================================================================
# Tolerance on the wrong kind of feature
# =============================================================================


@register_check(Rulebook.GDT_FEATURE_TOLERANCE_MISMATCH)
class GdtFeatureToleranceMismatchCheck(MachiningCheck):
    """True position on something that has no size.

    Position locates a feature of size -- a hole, a slot, a boss, a tab. On a
    fillet or a plain face there is no axis or centre plane for the zone to
    surround, so the callout is either mis-authored or, more often, the sign
    that the recognizer read the wrong feature.

    A material modifier sharpens that considerably. MMC trades size departure
    for position, which needs a size to depart from, so it cannot legally sit
    on a non-feature-of-size at all: seeing one says the geometry was almost
    certainly mis-recognized rather than that the drawing is unusual.
    """

    @property
    def name(self) -> str:
        return "GD&T Feature Tolerance Mismatch Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.frames:
            return []

        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for feature in sorted(
            context.recognition.features, key=lambda f: f.instance_id
        ):
            if feature.type in _FEATURE_OF_SIZE_TYPES:
                continue

            kind = feature.type.lower().replace("_", " ")
            for frame in callouts.frames_for(feature.instance_id):
                if frame.type != ToleranceType.POSITION:
                    continue

                if frame.has_material_modifier:
                    modifier = frame.material_condition
                    advice = (
                        f"The position callout on {feature.instance_id} carries a "
                        f"{modifier} modifier, but the feature under it was "
                        f"recognized as a {kind}. {modifier} trades size departure "
                        "for position, so it only means anything on a feature of "
                        "size -- a hole, a slot, a boss, a tab. A "
                        f"{kind} is not one. Far more likely a feature of size was "
                        "read wrongly here -- an obround slot taken for a fillet, "
                        "say -- than that the drawing toleranced a "
                        f"{kind} at {modifier}. Check the geometry before quoting."
                    )
                    overview = f"{frame.material_condition} on a {kind}"
                else:
                    advice = (
                        f"A position tolerance is applied to {feature.instance_id}, "
                        f"recognized as a {kind}. True position locates a feature "
                        "of size -- a hole, a slot, a boss, a tab -- by surrounding "
                        f"its axis or centre plane. A {kind} has neither, so there "
                        "is nothing for the zone to be about. Confirm this is what "
                        "the drawing intends; profile of a surface is usually the "
                        "control that was wanted."
                    )
                    overview = f"position on a {kind}"

                results.append(
                    self.finding(
                        rule,
                        severity,
                        overview,
                        self.render(feedback, severity, 0.0, 0.0, 0.0, "", advice),
                        faces=_frame_faces(frame, feature),
                    )
                )
        return results


# =============================================================================
# Surface finish called out directly
# =============================================================================


@register_check(Rulebook.NOTE_SURFACE_FINISH_DEMANDING)
class NoteSurfaceFinishDemandingCheck(MachiningCheck):
    """An Ra buried in the general notes still has to be cut.

    The usual place a demanding finish hides: "ALL SURFACES Ra 0.4 UNLESS
    OTHERWISE SPECIFIED" in the title block, applying to the whole part and
    never appearing on any one feature. Quoted off the geometry alone it is
    invisible, and it is often the largest single cost on the job.
    """

    @property
    def name(self) -> str:
        return "Demanding Surface Finish Note Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.notes:
            return []

        thresholds = context.config.thresholds
        ceiling = thresholds.ra_standard_mill_um
        results: list[CheckResult] = []

        for note in callouts.sorted_notes():
            ra_um = parse_ra_um(note.text)
            if ra_um <= 0.0 or ra_um >= ceiling:
                continue

            severity, advice = _ra_verdict(ra_um, thresholds)
            if note.face_ids:
                count = len(note.face_ids)
                scope = f"on {count} face{'' if count == 1 else 's'}"
            else:
                scope = "as a general, part-wide note"

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"Ra {ra_um:.2f} um note",
                    self.render(
                        feedback,
                        severity,
                        ra_um,
                        ceiling,
                        ceiling,
                        "um",
                        f'A drawing note reading "{note.text.strip()}" calls for '
                        f"Ra {ra_um:.2f} um {scope}. {advice}",
                    ),
                    faces=sorted(note.face_ids),
                    value=ra_um,
                    limit=ceiling,
                    comparison="<",
                    unit="um",
                )
            )
        return results


@register_check(Rulebook.SURFACE_FINISH_PER_FACE_DEMANDING)
class SurfaceFinishPerFaceDemandingCheck(MachiningCheck):
    """A finish requirement pinned to particular faces.

    The same bands as the note rule, reported against the feature that owns
    the faces so the highlight lands on the geometry the finish applies to.
    A requirement whose faces belong to no recognized feature still gets said
    once, at part level -- a finish on a plain wall is as expensive as a
    finish on a pocket.
    """

    @property
    def name(self) -> str:
        return "Per-Face Surface Finish Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        callouts = _callouts(context)
        if not callouts.surface_finishes:
            return []

        thresholds = context.config.thresholds
        ceiling = thresholds.ra_standard_mill_um
        results: list[CheckResult] = []

        for finish in callouts.sorted_surface_finishes():
            ra_um = float(finish.ra_um)
            if ra_um <= 0.0 or ra_um >= ceiling:
                continue

            severity, advice = _ra_verdict(ra_um, thresholds)
            faces = sorted(finish.face_ids)
            owners = self._owners(context, set(faces))

            def emit(subject: str, highlight: list[int]) -> CheckResult:
                return self.finding(
                    rule,
                    severity,
                    f"Ra {ra_um:.2f} um",
                    self.render(
                        feedback,
                        severity,
                        ra_um,
                        ceiling,
                        ceiling,
                        "um",
                        f"A finish requirement of Ra {ra_um:.2f} um applies to "
                        f"{subject}. {advice}",
                    ),
                    faces=highlight,
                    value=ra_um,
                    limit=ceiling,
                    comparison="<",
                    unit="um",
                )

            if not faces:
                # The requirement is real but the source could not say which
                # faces it lands on. Still worth one part-level finding: the
                # cost is the same whether or not the linkage resolved.
                results.append(emit("this part, with no face linkage resolved", []))
                continue

            if not owners:
                count = len(faces)
                results.append(
                    emit(f"{count} face{'' if count == 1 else 's'} of the part", faces)
                )
                continue

            for feature in owners:
                shared = sorted(set(feature.faces) & set(faces))
                subject = (
                    f"{feature.type.lower().replace('_', ' ')} {feature.instance_id}"
                )
                results.append(emit(subject, shared))
        return results

    @staticmethod
    def _owners(context, faces: set[int]) -> list[FeatureInstance]:
        """Features sharing any face with the requirement, in a stable order."""
        return sorted(
            (
                feature
                for feature in context.recognition.features
                if faces & set(feature.faces)
            ),
            key=lambda f: f.instance_id,
        )
