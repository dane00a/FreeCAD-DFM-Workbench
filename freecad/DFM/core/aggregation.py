# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Collapse findings that say the same thing about the same part.

A rule fires once per feature, which is right, and a turbine disk has
forty-eight identical broached slots, which is also right. Together they
produce a results tree that says the identical paragraph forty-eight times
and makes the machinist click forty-eight rows to see forty-eight slots
that are all the same slot. The information the reader actually wants --
"this is wrong, and it is wrong in forty-eight places" -- is the one thing
that presentation does not state.

So after every rule has run and before anything is shown or recorded,
findings that are indistinguishable except for the geometry they name are
merged into one, which keeps all the geometry and gains a count. This is
the reference engine's own pass, ported. It is not one for one, because
the reference keys on a free-form ``details`` dictionary that this model
does not have; :func:`aggregation_key` is where that difference is
argued out, field by field.

Nothing here looks at a shape, a document or a widget. It runs in the GUI
after :class:`~..app.analysis_runner.AnalysisRunner` and headlessly after
:func:`~.baselines.harness.run_rules`, and it has to be the same code in
both or the panel and the baselines would disagree about what the engine
found -- a disagreement that would be harder to live with than the noise
this pass exists to remove.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .models import CheckResult

#: Decimal places every measurement in the aggregation key is rounded to
#: before it is compared. The reference rounds its whole details blob the
#: same way and for the same reason: two runs of a boolean over the same
#: nominal geometry come back as 1.000000200000028 and 1.000000200000029,
#: and an exact comparison files those as two different problems. Four
#: places is far below anything a DFM limit is written to and matches what
#: the panel prints, so nothing meaningful is lost by ignoring the rest.
ROUNDING = 4


def _number(value: Any) -> Any:
    """A measurement as it enters the key: rounded, or left alone if it is not one.

    ``value`` and ``limit`` are typed as floats and are usually floats, but
    a check that had nothing to measure is allowed to leave them ``None``,
    and rounding that would raise in the middle of an analysis that had
    otherwise succeeded.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return round(float(value), ROUNDING)


def aggregation_key(result: CheckResult) -> tuple:
    """What makes two findings the same finding.

    The reference builds its key from ``rule_id | severity | category |
    rounded(details)``. Two of those four map across unchanged; the other
    two need explaining.

    *Category* is the finding's own when it has one. The reference keeps it
    in the key because a rule that fires on both a POCKET and a SLOT stamps
    a different category on each, and merging across them once filed slot
    findings under the Pocket badge in its UI. Most of our findings take
    their category from the rule's :class:`~.rules.RuleFamily` and carry an
    empty one here; a rule that files per feature (pocket_square_corner on a
    slot) sets it, and it then keeps the two apart as the reference does.

    ``overview`` stands in for the rest as far as anything can, which is why it
    is here and the reference's equivalent -- the finding's title -- is not
    in the reference's key. It is the nearest thing we have to a
    per-finding label: it is written by the rule at the point it decided,
    not by the rule as a whole, and it is what the results tree prints
    beside the row. Two findings titled differently merging into one titled
    by whichever arrived first is the same class of mistake the reference's
    category rule exists to prevent, and it would show up in the same
    place -- as a row labelled something the reader cannot reconcile with
    what lit up in the viewport.

    *Details are stood in for by the rendered message*, plus the measured
    numbers around it. We have no details dict; the message is what the
    details were rendered into, so two findings whose message is character
    for character identical are saying the same sentence about the same
    part, and two that differ in a single dimension will not merge. That
    errs towards leaving findings apart, which is the harmless direction:
    the worst case is the noise this pass was written to remove.

    ``refs`` and ``failing_geometry`` are deliberately absent. They are
    precisely what differs between the duplicates worth merging, and a key
    that included them would never merge anything.

    ``ignore`` is in the key so that a finding the machinist has muted
    never absorbs one they have not, or the other way round. Nothing sets
    it at emission time today -- it is toggled later, from the results tree
    -- so in practice it is constant and costs a merge nothing. It is here
    for the day the toggle is applied before this pass rather than after,
    at which point leaving it out would silently mute findings the user
    never touched.
    """
    return (
        result.rule_id,
        result.severity,
        getattr(result, "category", ""),
        _number(result.value),
        _number(result.limit),
        result.unit,
        result.comparison,
        result.overview,
        result.message,
        result.ignore,
    )


def _extend_unique(existing: list, incoming: Iterable, identity) -> None:
    """Append what is not already there, in the order it arrived.

    Order matters more than it looks. The faces of a merged finding are
    what the viewport highlights and what the CSV lists, and a set would
    hand back a different order on every run -- turning an exported report
    into something that cannot be diffed against the one before it.
    """
    seen = {identity(item) for item in existing}
    for item in incoming:
        token = identity(item)
        if token in seen:
            continue
        seen.add(token)
        existing.append(item)


def _geometry_identity(item: Any) -> Any:
    """What makes two pieces of failing geometry the same piece.

    Everything that reaches here today is a ``(kind, index)`` tuple, which
    is hashable and compares the way one would hope. Anything else is
    identified by the text of its repr rather than by its identity, because
    two equal-but-distinct objects naming the same face should collapse,
    and an unhashable one must not take the whole analysis down with it.
    """
    if isinstance(item, tuple):
        return item
    try:
        hash(item)
    except TypeError:
        return repr(item)
    return item


def _ref_identity(ref: Any) -> Any:
    """A geometry reference by what it points at, not by object identity.

    :class:`~.models.GeometryRef` is a plain dataclass and so unhashable;
    two of them built from the same face by two different rules are equal
    in every field that matters and must not both survive the merge.
    """
    return (
        getattr(ref, "type", None),
        getattr(ref, "index", None),
        getattr(ref, "label", None),
    )


#: Two findings name the same gap only if they agree on its size this closely.
_SAME_GAP_MM = 0.05


def _faces_of(result: CheckResult) -> set[int]:
    faces: set[int] = set()
    for item in result.failing_geometry:
        if isinstance(item, (tuple, list)) and len(item) == 2 and item[0] == "Face":
            faces.add(int(item[1]))
    for ref in result.refs:
        if getattr(ref, "type", None) == "Face":
            faces.add(int(ref.index) + 1)  # refs are 0-based
    return faces


def defer_edge_distance_to_thin_wall(results: Sequence[CheckResult]) -> list[CheckResult]:
    """Drop each hole_edge_distance finding that a thin_wall finding already states.

    A hole too close to a face and a thin wall between that hole and that
    face are one gap measured from opposite sides; both used to report it.
    Thin wall survives because it grades the gap more severely (an error
    where edge distance warns) and also speaks for interior pocket walls and
    webs edge distance cannot see. Sharing a face is not proof of the same
    gap, since a thin-wall finding carries the hole's own bore and so
    matches every edge-distance finding on that hole: the two must also
    agree on the number. Per finding, before aggregation, so occurrences
    thin wall does not cover still report. The reference does this in its
    rules engine just before its own aggregation pass.
    """
    from .rules import Rulebook

    thin_walls = [
        (_faces_of(r), r.value)
        for r in results
        if r.rule_id == Rulebook.THIN_WALL and isinstance(r.value, (int, float))
    ]
    if not thin_walls:
        return list(results)

    def redundant(result: CheckResult) -> bool:
        if result.rule_id != Rulebook.HOLE_EDGE_DISTANCE:
            return False
        if not isinstance(result.value, (int, float)):
            return False
        faces = _faces_of(result)
        return any(
            abs(wall - result.value) <= _SAME_GAP_MM and faces & wall_faces
            for wall_faces, wall in thin_walls
        )

    return [r for r in results if not redundant(r)]


def aggregate_findings(results: Sequence[CheckResult]) -> list[CheckResult]:
    """One finding per distinct thing the engine has to say, in the order first said.

    The first finding of each group survives and is mutated in place, the
    way the reference moves its first match into the aggregate. Every field
    the key compares is equal across the group by construction, so the only
    thing that choice actually decides is which of two measurements
    differing below the fourth decimal gets kept -- and at that scale the
    two are the same number reported twice by a boolean operation.

    Groups of one come back exactly as they went in: no count on them, no
    prefix on their message, nothing for a reader to wonder about. That
    also makes the pass safe to run twice, which is worth having in a
    codebase where the GUI and the corpus reach it by different routes.
    """
    # Keyed by group rather than by the surviving finding's identity: two
    # calls that construct equal findings are common, and `id()` would be
    # one lookup away from crediting a merge to the wrong row.
    results = defer_edge_distance_to_thin_wall(results)

    position: dict[tuple, int] = {}
    order: list[CheckResult] = []
    counts: list[int] = []

    for result in results:
        key = aggregation_key(result)
        index = position.get(key)
        if index is None:
            position[key] = len(order)
            order.append(result)
            counts.append(1)
            continue

        first = order[index]
        counts[index] += 1
        _extend_unique(first.failing_geometry, result.failing_geometry, _geometry_identity)
        _extend_unique(first.refs, result.refs, _ref_identity)

    for result, count in zip(order, counts):
        if count > 1:
            result.occurrence_count = count
            result.message = f"{count}x: {result.message}"

    return order
