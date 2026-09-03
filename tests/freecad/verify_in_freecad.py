# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""End-to-end verification of the machining analysis inside FreeCAD.

The rest of the test suite runs headlessly against OpenCascade, which proves
the geometry and the rules but never touches FreeCAD. This script covers the
gap: it builds a document the way a user would, from primitives and booleans,
and runs the real analysis path over the resulting ``Part::Feature`` shape.

That distinction is not academic. FreeCAD's solids store face orientations the
opposite way round from OpenCascade's own modelling, with surface
parameterisations that differ to match. Every unit test passed while the hole
rules were silently dead on real documents, and only running here found it.

Run with::

    "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe" tests/freecad/verify_in_freecad.py

freecadcmd discards stdout, so the report is written to
``verify_in_freecad.out`` beside this file. Exit is silent; read the report.

Requires ``cadquery-ocp`` in FreeCAD's Python and this repository visible in
FreeCAD's Mod directory. See docs/cnc-machining-port.md, Phase 6.
"""

import os
import traceback

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_in_freecad.out")
log = open(REPORT, "w", encoding="utf-8")
failures = []


def say(text=""):
    log.write(str(text) + "\n")
    log.flush()


def check(description, condition, detail=""):
    say("  [%s] %s%s" % ("PASS" if condition else "FAIL", description, detail))
    if not condition:
        failures.append(description)


try:
    import FreeCAD

    say("FreeCAD " + ".".join(FreeCAD.Version()[:3]))
    say("")

    import OCP  # noqa: F401
    import Part  # noqa: F401
    import freecad.DFM  # noqa: F401

    from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
    from freecad.DFM.core.machining import AagBuilder, SurfaceType
    from freecad.DFM.core.machining.recognizers import HoleRecognizer
    from freecad.DFM.core.processes.process import RuleFeedback
    from freecad.DFM.core.registries import ProcessRegistry, get_check_class
    from freecad.DFM.core.utils.conversion import freecad_to_ocp
    from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex

    say("Environment")
    check("addon importable from FreeCAD", True, " (%s)" % freecad.DFM.__file__)

    # -- a document built the way a user builds one ------------------------
    doc = FreeCAD.newDocument("dfm_verify")

    body = doc.addObject("Part::Box", "Body")
    body.Length, body.Width, body.Height = 120.0, 80.0, 25.0

    pocket_tool = doc.addObject("Part::Box", "PocketTool")
    pocket_tool.Length, pocket_tool.Width, pocket_tool.Height = 80.0, 40.0, 20.0
    pocket_tool.Placement.Base = FreeCAD.Vector(20, 20, 10)
    stage = doc.addObject("Part::Cut", "Stage")
    stage.Base, stage.Tool = body, pocket_tool

    # 4mm through 25mm of plate is 6.25 times its diameter: deep enough to
    # report, shallow enough to be ordinary.
    drill = doc.addObject("Part::Cylinder", "DrillTool")
    drill.Radius, drill.Height = 2.0, 60.0
    drill.Placement.Base = FreeCAD.Vector(116, 70, -5)
    part = doc.addObject("Part::Cut", "Part")
    part.Base, part.Tool = stage, drill
    doc.recompute()

    shape = part.Shape
    say("")
    say("Geometry")
    check("document shape is valid", shape.isValid())

    occ = freecad_to_ocp(shape)
    face_index, edge_index = FaceIndex(occ), EdgeIndex(occ)
    check(
        "conversion preserves face count",
        len(face_index) == len(shape.Faces),
        " (%d document, %d converted)" % (len(shape.Faces), len(face_index)),
    )

    # -- the orientation convention ----------------------------------------
    say("")
    say("Face orientation")
    graph = AagBuilder(occ, face_index).build()
    bores = [
        node
        for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        if node.is_internal
    ]
    check(
        "a bore in a FreeCAD solid reads as internal",
        len(bores) == 1,
        " (found %d)" % len(bores),
    )
    if bores:
        check(
            "the raw orientation flag alone would have said otherwise",
            not bores[0].is_reversed,
            " (FreeCAD stores it forward, OpenCascade reversed)",
        )

    # -- recognition --------------------------------------------------------
    data = MachiningAnalyzer().execute(occ, face_index, edge_index, prefs={})
    context = list(data.values())[0]
    counts = context.recognition.counts()
    say("")
    say("Recognition")
    say("  process: " + context.process_type.value)
    say("  features: " + str(counts))
    check("part classifies as milled", context.process_type.value == "MILLED")
    check("the pocket is recognized", counts.get("POCKET", 0) == 1)
    check("the through hole is recognized", counts.get("THROUGH_HOLE", 0) == 1)

    # -- rules --------------------------------------------------------------
    registry = ProcessRegistry.get_instance()
    process = registry.get_process_by_name("CNC Milling")
    default = process.materials["Default"]
    material = process.materials["Aluminum (Soft Wrought Alloy)"]

    say("")
    say("Rules (%s / %s)" % (process.name, material.name))
    findings = []
    for rule in process.active_rules:
        check_class = get_check_class(rule)
        limits = material.rule_limits.get(rule) or default.rule_limits.get(rule)
        if check_class is None or limits is None:
            continue
        feedback = process.rule_feedback.get(rule) or RuleFeedback()
        for result in check_class().run_check(data, limits, rule, feedback=feedback):
            findings.append((rule, result))
            say(
                "    [%-7s] %-22s %s"
                % (result.severity.name, rule.label, result.overview)
            )

    fired = {rule.name for rule, _ in findings}
    # The part is deliberately built so these two apply: a hole a little
    # deeper than six diameters, and a pocket with square corners.
    check("the deep hole is reported", "HOLE_DEPTH_RATIO" in fired)
    check("the square-cornered pocket is reported", "POCKET_CORNER_RADIUS" in fired)
    # Not an exact-set assertion any more. With fifty-odd rules active the
    # useful question is not which fired but whether the quiet ones stayed
    # quiet: a plain milled plate must not attract turning, sheet or thread
    # findings, and those are the families that would indicate the process
    # gates had come undone.
    wrong_family = {
        name
        for name in fired
        if name.startswith(("THREAD_", "SHEET_", "RIB_", "BOSS_", "GDT_"))
        or name in ("TURNED_PROFILE_RADIUS", "GROOVE_SQUARE_CORNER")
    }
    check(
        "no rule from an inapplicable family fires",
        not wrong_family,
        "" if not wrong_family else " (fired: %s)" % sorted(wrong_family),
    )
    say("  fired: %s" % ", ".join(sorted(fired)))
    check(
        "a square corner warns rather than errors",
        all(
            result.severity.name == "WARNING"
            for rule, result in findings
            if rule.name == "POCKET_CORNER_RADIUS"
        ),
    )

    bad = [
        index
        for _, result in findings
        for kind, index in result.failing_geometry
        if kind == "Face" and not (1 <= index <= len(shape.Faces))
    ]
    check(
        "every face a finding names exists on the document shape",
        not bad,
        "" if not bad else " (out of range: %s)" % bad,
    )
    check(
        "findings carry readable messages",
        all(len(result.message) > 40 and "{" not in result.message for _, result in findings),
    )

    # -- the feature census -------------------------------------------------
    say("")
    say("Feature census")
    from freecad.DFM.core.machining.census import CENSUS_COLUMNS, census_rows

    rows = census_rows(context.recognition.features)
    check("the census has a header and a row per feature",
          len(rows) == len(context.recognition.features) + 1)
    check("every census row has every column",
          all(len(row) == len(CENSUS_COLUMNS) for row in rows))

    # -- a turned part, built in FreeCAD ------------------------------------
    say("")
    say("A turned part")
    shaft = doc.addObject("Part::Cylinder", "Shaft")
    shaft.Radius, shaft.Height = 12.0, 90.0
    doc.recompute()
    turned_occ = freecad_to_ocp(shaft.Shape)
    turned_data = MachiningAnalyzer().execute(
        turned_occ, FaceIndex(turned_occ), EdgeIndex(turned_occ), prefs={}
    )
    turned = list(turned_data.values())[0]
    say("  process: " + turned.process_type.value)
    check("a plain shaft classifies as turned",
          turned.process_type.value == "TURNED")
    check("its outside diameter is not read as a bore",
          not any(
              node.is_internal
              for node in turned.graph.nodes_by_surface_type(SurfaceType.CYLINDER)
          ))

    # -- a threaded PartDesign::Hole ----------------------------------------
    #
    # The one thing no headless test can cover. A tapped hole is modelled as
    # a plain bore, so the only reason to call this one tapped is that the
    # Hole feature says so, and that statement exists nowhere but in a live
    # PartDesign document. Reading it back out means finding the Hole,
    # working out where its profile put the bore, and matching that against
    # the bore the recognizer found on the finished shape.
    say("")
    say("A threaded PartDesign::Hole")
    from freecad.DFM.core.machining.thread_sources import (
        NATIVE_DECLARATION,
        thread_evidence_for,
    )

    tapped_body = doc.addObject("PartDesign::Body", "TappedBody")
    pad_sketch = doc.addObject("Sketcher::SketchObject", "PadSketch")
    tapped_body.addObject(pad_sketch)
    pad_sketch.addGeometry(Part.LineSegment(
        FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(40, 0, 0)), False)
    pad_sketch.addGeometry(Part.LineSegment(
        FreeCAD.Vector(40, 0, 0), FreeCAD.Vector(40, 40, 0)), False)
    pad_sketch.addGeometry(Part.LineSegment(
        FreeCAD.Vector(40, 40, 0), FreeCAD.Vector(0, 40, 0)), False)
    pad_sketch.addGeometry(Part.LineSegment(
        FreeCAD.Vector(0, 40, 0), FreeCAD.Vector(0, 0, 0)), False)
    pad = doc.addObject("PartDesign::Pad", "Pad")
    tapped_body.addObject(pad)
    pad.Profile = pad_sketch
    pad.Length = 20.0
    doc.recompute()

    # The profile circle is drawn on the top face's plane, which is where a
    # user would put it: the Hole feature drills down from there.
    hole_sketch = doc.addObject("Sketcher::SketchObject", "HoleSketch")
    tapped_body.addObject(hole_sketch)
    hole_sketch.Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, 20), FreeCAD.Rotation(0, 0, 0, 1)
    )
    hole_sketch.addGeometry(
        Part.Circle(FreeCAD.Vector(20, 20, 0), FreeCAD.Vector(0, 0, 1), 2.5), False
    )
    hole = doc.addObject("PartDesign::Hole", "TappedHole")
    tapped_body.addObject(hole)
    hole.Profile = hole_sketch
    hole.Threaded = True
    hole.ThreadType = "ISOMetricProfile"
    # Spelled the way FreeCAD's own enumeration spells it, which happens to
    # be the way the thread table spells it too.
    hole.ThreadSize = "M6x1.0"
    hole.ThreadDepthType = "Hole Depth"
    hole.DepthType = "Dimension"
    hole.Depth = 12.0
    hole.ModelThread = False
    doc.recompute()

    check("the body recomputes with a threaded hole in it", hole.Shape.isValid())
    check(
        "the hole is not modelled as a thread",
        not hole.ModelThread,
        " (a plain bore is what a tapped hole really looks like)",
    )

    evidence = thread_evidence_for(tapped_body)
    check(
        "the declaration is read off the document",
        len(evidence.declarations) == 1,
        " (found %d)" % len(evidence.declarations),
    )
    if evidence.declarations:
        declared = evidence.declarations[0]
        say("  declared: %s, bore window %.2f to %.2f, at %s"
            % (declared.designation, declared.bore_window[0],
               declared.bore_window[1], declared.positions))
        check("the thread is named", declared.designation == "M6x1.0",
              " (%s)" % declared.designation)
        check(
            "the profile circle is placed in the shape's own coordinates",
            any(
                abs(p[0] - 20.0) < 0.01 and abs(p[1] - 20.0) < 0.01
                for p in declared.positions
            ),
            " (%s)" % (declared.positions,),
        )

    tapped_occ = freecad_to_ocp(tapped_body.Shape)
    tapped_data = MachiningAnalyzer().execute(
        tapped_occ,
        FaceIndex(tapped_occ),
        EdgeIndex(tapped_occ),
        prefs={},
        target_object=tapped_body,
    )
    tapped_context = list(tapped_data.values())[0]
    tapped = [
        f
        for f in tapped_context.recognition.features
        if f.param("thread_designation")
    ]
    say("  features: " + str(tapped_context.recognition.counts()))
    check(
        "the bore comes back as a tapped hole with nothing asked",
        len(tapped) == 1,
        " (found %d)" % len(tapped),
    )
    if tapped:
        check("it carries the declared thread",
              tapped[0].param("thread_designation") == "M6x1.0",
              " (%s)" % tapped[0].param("thread_designation"))
        check("the evidence names the document as the source",
              tapped[0].param("thread_evidence") == NATIVE_DECLARATION,
              " (%s)" % tapped[0].param("thread_evidence"))
        check("the pitch comes with it",
              abs((tapped[0].number("thread_pitch_mm") or 0.0) - 1.0) < 1e-6)

    # The same shape with no object behind it must stay a plain bore. This is
    # what keeps a 5 mm hole on an imported part from being called an M6.
    bare = MachiningAnalyzer().execute(
        tapped_occ, FaceIndex(tapped_occ), EdgeIndex(tapped_occ), prefs={}
    )
    bare_context = list(bare.values())[0]
    check(
        "the same shape with no document behind it is not tapped",
        not any(
            f.param("thread_designation")
            for f in bare_context.recognition.features
        ),
    )

    # -- the same hole, drilled many times ----------------------------------
    #
    # A tapped hole on a bolt circle is drawn once and repeated, and the
    # copies exist only in the finished solid with nothing on them to say
    # where they came from. Placing them means reading the pattern's own
    # numbers and resolving whatever its direction happens to point at, and
    # neither of those exists outside a live document. The unit tests cover
    # the arithmetic against stand-ins; this is the only place the reading
    # itself gets proved.
    say("")
    say("A tapped hole repeated")
    from freecad.DFM.core.machining.features import BORE_TYPES

    def tapped_plate(label, hole_at, half=40.0, thick=20.0):
        """A plate with one threaded hole in it, ready to be repeated."""
        plate = doc.addObject("PartDesign::Body", label)
        outline = doc.addObject("Sketcher::SketchObject", label + "Outline")
        plate.addObject(outline)
        corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
        for index in range(4):
            here, there = corners[index], corners[(index + 1) % 4]
            outline.addGeometry(
                Part.LineSegment(
                    FreeCAD.Vector(here[0], here[1], 0),
                    FreeCAD.Vector(there[0], there[1], 0),
                ),
                False,
            )
        slab = doc.addObject("PartDesign::Pad", label + "Pad")
        plate.addObject(slab)
        slab.Profile = outline
        slab.Length = thick
        doc.recompute()

        profile = doc.addObject("Sketcher::SketchObject", label + "Profile")
        plate.addObject(profile)
        profile.Placement = FreeCAD.Placement(
            FreeCAD.Vector(0, 0, thick), FreeCAD.Rotation(0, 0, 0, 1)
        )
        profile.addGeometry(
            Part.Circle(
                FreeCAD.Vector(hole_at[0], hole_at[1], 0),
                FreeCAD.Vector(0, 0, 1),
                2.5,
            ),
            False,
        )
        tapped = doc.addObject("PartDesign::Hole", label + "Hole")
        plate.addObject(tapped)
        tapped.Profile = profile
        tapped.Threaded = True
        tapped.ThreadType = "ISOMetricProfile"
        tapped.ThreadSize = "M6x1.0"
        tapped.ThreadDepthType = "Hole Depth"
        tapped.DepthType = "Dimension"
        tapped.Depth = 12.0
        tapped.ModelThread = False
        doc.recompute()
        return plate, outline, tapped

    def check_every_copy(label, plate, expected):
        """Every hole the pattern cut, tapped, pitched, and nothing asked."""
        doc.recompute()
        repeated_occ = freecad_to_ocp(plate.Shape)
        repeated = list(
            MachiningAnalyzer()
            .execute(
                repeated_occ,
                FaceIndex(repeated_occ),
                EdgeIndex(repeated_occ),
                prefs={},
                target_object=plate,
            )
            .values()
        )[0]
        drilled = [f for f in repeated.recognition.features if f.type in BORE_TYPES]
        tapped = [f for f in drilled if f.param("thread_designation")]
        say("  %s: %d bores in the solid, %d declared tapped"
            % (label, len(drilled), len(tapped)))
        check(
            "%s: the pattern really cut %d holes" % (label, expected),
            len(drilled) == expected,
            " (found %d)" % len(drilled),
        )
        check(
            "%s: every one of them comes back tapped" % label,
            len(tapped) == expected,
            " (%d of %d)" % (len(tapped), len(drilled)),
        )
        check(
            "%s: each carries the thread the one Hole declared" % label,
            bool(tapped)
            and all(f.param("thread_designation") == "M6x1.0" for f in tapped),
        )
        check(
            "%s: and the pitch with it" % label,
            bool(tapped)
            and all(
                abs((f.number("thread_pitch_mm") or 0.0) - 1.0) < 1e-6 for f in tapped
            ),
        )
        check(
            "%s: with nothing put to the user" % label,
            not candidates_for(
                repeated.recognition.features,
                repeated.graph,
                thread_evidence_for(plate),
            ),
        )

    from freecad.DFM.core.machining.thread_sources import candidates_for

    # A row of four. The direction is a link to the pad sketch's own
    # horizontal axis, which is how the interface writes it when a user picks
    # the axis rather than an edge.
    row_plate, row_outline, row_hole = tapped_plate("Row", (-30.0, 0.0))
    row = doc.addObject("PartDesign::LinearPattern", "Row")
    row_plate.addObject(row)
    row.Originals = [row_hole]
    row.BaseFeature = row_hole
    row_plate.Tip = row
    row.Direction = (row_outline, ["H_Axis"])
    row.Mode = "Extent"
    row.Length = 60.0
    row.Occurrences = 4
    doc.recompute()
    check("the row recomputes", row_plate.Shape.isValid())
    row_evidence = thread_evidence_for(row_plate)
    check(
        "the row's declaration reaches all four holes",
        sum(len(d.positions) for d in row_evidence.declarations) == 4,
        " (%s)"
        % [tuple(round(v, 2) for v in p)
           for d in row_evidence.declarations for p in d.positions],
    )
    check_every_copy("row of four", row_plate, 4)

    # A bolt circle of six about the body's Z axis, which is an origin
    # feature rather than anything the user drew.
    circle_plate, circle_outline, circle_hole = tapped_plate("Circle", (25.0, 0.0))
    z_axis = [
        o for o in circle_plate.Origin.OutList if getattr(o, "Role", "") == "Z_Axis"
    ][0]
    circle = doc.addObject("PartDesign::PolarPattern", "Circle")
    circle_plate.addObject(circle)
    circle.Originals = [circle_hole]
    circle.BaseFeature = circle_hole
    circle_plate.Tip = circle
    circle.Axis = (z_axis, [""])
    circle.Mode = "Extent"
    circle.Angle = 360.0
    circle.Occurrences = 6
    doc.recompute()
    check("the bolt circle recomputes", circle_plate.Shape.isValid())
    circle_evidence = thread_evidence_for(circle_plate)
    check(
        "the bolt circle's declaration reaches all six holes",
        sum(len(d.positions) for d in circle_evidence.declarations) == 6,
        " (%s)"
        % [tuple(round(v, 2) for v in p)
           for d in circle_evidence.declarations for p in d.positions],
    )
    check_every_copy("bolt circle of six", circle_plate, 6)

    # A row of two turned three ways. Stacking transforms is where the
    # arithmetic is easiest to get wrong: the stages multiply rather than
    # add, so this is six holes and not five.
    stack_plate, stack_outline, stack_hole = tapped_plate("Stack", (12.0, 0.0))
    stack_row = doc.addObject("PartDesign::LinearPattern", "StackRow")
    stack_row.Direction = (stack_outline, ["H_Axis"])
    stack_row.Mode = "Extent"
    stack_row.Length = 14.0
    stack_row.Occurrences = 2
    stack_circle = doc.addObject("PartDesign::PolarPattern", "StackCircle")
    stack_circle.Axis = (
        [o for o in stack_plate.Origin.OutList if getattr(o, "Role", "") == "Z_Axis"][0],
        [""],
    )
    stack_circle.Mode = "Extent"
    stack_circle.Angle = 360.0
    stack_circle.Occurrences = 3
    stack = doc.addObject("PartDesign::MultiTransform", "Stack")
    stack_plate.addObject(stack)
    stack.Originals = [stack_hole]
    stack.BaseFeature = stack_hole
    stack_plate.Tip = stack
    stack.Transformations = [stack_row, stack_circle]
    doc.recompute()
    check("the stacked transform recomputes", stack_plate.Shape.isValid())
    check_every_copy("row of two turned three ways", stack_plate, 6)

    # -- and what happens when the copies cannot be placed ------------------
    #
    # Set to repeat the whole shape, a pattern fuses copies of the solid
    # rather than cutting copies of the hole, so a hole in one lands in metal
    # in the next and is filled in by it. There is no honest reading of that
    # from the properties, and half a bolt circle reported as tapped looks
    # like an answer. The declaration is dropped instead.
    say("")
    say("A repeat that cannot be placed")
    whole_plate, whole_outline, whole_hole = tapped_plate("Whole", (20.0, 20.0))
    whole = doc.addObject("PartDesign::LinearPattern", "Whole")
    whole_plate.addObject(whole)
    whole.BaseFeature = whole_hole
    whole_plate.Tip = whole
    whole.TransformMode = "Whole shape"
    whole.Direction = (whole_outline, ["H_Axis"])
    whole.Mode = "Extent"
    whole.Length = 20.0
    whole.Occurrences = 2
    doc.recompute()
    whole_evidence = thread_evidence_for(whole_plate)
    say("  bores left after the fuse: %d"
        % sum(1 for f in whole_plate.Shape.Faces
              if f.Surface.__class__.__name__ == "Cylinder"))
    check(
        "a repeat of the whole shape declares nothing at all",
        not whole_evidence.declarations,
        " (declared %d)" % len(whole_evidence.declarations),
    )

    # -- confirming a thread on a part that cannot say ----------------------
    #
    # The same body with the declaration taken off it, which is what an
    # imported STEP of the same part amounts to: a bore at the M6 tap drill
    # and nothing anywhere saying what it is for.
    say("")
    say("Confirming a thread by hand")
    from freecad.DFM.core.machining.thread_sources import (
        USER_CONFIRMED,
        ConfirmationStore,
        candidates_for,
        load_confirmations,
        record_answers,
        save_confirmations,
    )

    hole.Threaded = False
    # Clearing the thread hands the diameter back to the user, and FreeCAD
    # puts its own default in. Drilling it at the M6 tap drill again is what
    # makes this the same hole as before, minus anybody saying so.
    hole.Diameter = 5.0
    doc.recompute()
    plain_occ = freecad_to_ocp(tapped_body.Shape)

    def analyse_plain():
        data = MachiningAnalyzer().execute(
            plain_occ,
            FaceIndex(plain_occ),
            EdgeIndex(plain_occ),
            prefs={},
            target_object=tapped_body,
        )
        return list(data.values())[0]

    unstated = analyse_plain()
    check(
        "with the declaration gone the bore is a plain hole again",
        not any(
            f.param("thread_designation") for f in unstated.recognition.features
        ),
    )

    offered = candidates_for(
        unstated.recognition.features,
        unstated.graph,
        thread_evidence_for(tapped_body),
    )
    check(
        "the tap-drill-sized bore is offered for confirmation",
        len(offered) == 1,
        " (offered %d)" % len(offered),
    )
    if offered:
        say("  candidate: %s, likely %s"
            % (offered[0].describe(), offered[0].designation))
        store = ConfirmationStore()
        record_answers(store, offered, {offered[0].key.encode(): True})
        check("the answer saves onto the document", save_confirmations(doc, store))
        reloaded = load_confirmations(doc)
        verdict = reloaded.verdict_for(offered[0].key)
        check("and comes back off it afterwards",
              verdict is not None and verdict.accepted)

        # The record has to be inert: no shape, nothing to recompute, and
        # nothing that turns up in the analysis as part of the part.
        holder = [o for o in doc.Objects if hasattr(o, "DFMThreadRecords")]
        check("the answers live on one hidden document object",
              len(holder) == 1, " (found %d)" % len(holder))
        check("the record object carries no shape of its own",
              not holder or not getattr(holder[0], "Shape", None))

        confirmed_context = analyse_plain()
        confirmed = [
            f
            for f in confirmed_context.recognition.features
            if f.param("thread_evidence") == USER_CONFIRMED
        ]
        check(
            "a confirmed bore is thereafter treated as tapped",
            len(confirmed) == 1,
            " (found %d)" % len(confirmed),
        )
        if confirmed:
            check("and named from the tap drill it was cut at",
                  confirmed[0].param("thread_designation") == "M6x1.0",
                  " (%s)" % confirmed[0].param("thread_designation"))
        check(
            "and is never put up for confirmation again",
            not candidates_for(
                confirmed_context.recognition.features,
                confirmed_context.graph,
                thread_evidence_for(tapped_body),
            ),
        )

        # -- and it has to survive the model moving underneath it -----------
        #
        # The reason the answers are filed under a centreline and a diameter
        # rather than a face index. Cutting another hole in the plate
        # renumbers the faces after it, so an answer filed under "Face 7"
        # would silently slide onto a different hole. This one must not.
        spare_sketch = doc.addObject("Sketcher::SketchObject", "SpareSketch")
        tapped_body.addObject(spare_sketch)
        spare_sketch.Placement = FreeCAD.Placement(
            FreeCAD.Vector(0, 0, 20), FreeCAD.Rotation(0, 0, 0, 1)
        )
        spare_sketch.addGeometry(
            Part.Circle(FreeCAD.Vector(8, 8, 0), FreeCAD.Vector(0, 0, 1), 6.0), False
        )
        spare = doc.addObject("PartDesign::Hole", "SpareHole")
        tapped_body.addObject(spare)
        spare.Profile = spare_sketch
        spare.Diameter = 12.0
        spare.DepthType = "ThroughAll"
        doc.recompute()

        rebuilt_occ = freecad_to_ocp(tapped_body.Shape)
        rebuilt = list(
            MachiningAnalyzer()
            .execute(
                rebuilt_occ,
                FaceIndex(rebuilt_occ),
                EdgeIndex(rebuilt_occ),
                prefs={},
                target_object=tapped_body,
            )
            .values()
        )[0]
        say("  after the rebuild: %s" % rebuilt.recognition.counts())
        still_tapped = [
            f
            for f in rebuilt.recognition.features
            if f.param("thread_evidence") == USER_CONFIRMED
        ]
        check(
            "the answer still lands on the same bore after a rebuild",
            len(still_tapped) == 1
            and abs((still_tapped[0].number("diameter_mm") or 0.0) - 5.0) < 0.01,
            " (found %d)" % len(still_tapped),
        )
        check(
            "and does not spread to the hole that was added",
            sum(
                1
                for f in rebuilt.recognition.features
                if f.param("thread_designation")
            )
            == 1,
        )
        doc.removeObject(spare.Name)
        doc.removeObject(spare_sketch.Name)
        doc.recompute()

        # A rejection has to stick just as hard, or the shop learns to click
        # past the question without reading it.
        rejected_store = ConfirmationStore()
        record_answers(rejected_store, offered, {offered[0].key.encode(): False})
        save_confirmations(doc, rejected_store)
        rejected_context = analyse_plain()
        check(
            "a rejected bore stays a plain hole",
            not any(
                f.param("thread_designation")
                for f in rejected_context.recognition.features
            ),
        )
        check(
            "and is not asked about again either",
            not candidates_for(
                rejected_context.recognition.features,
                rejected_context.graph,
                thread_evidence_for(tapped_body),
            ),
        )


    # -- the machining preferences page -------------------------------------
    say("")
    say("Preferences")
    params = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Mod/DFM")
    params.SetString("MachiningMachineMode", "5axis")
    prefs = {"MachiningMachineMode": params.GetString("MachiningMachineMode", "3axis")}
    from freecad.DFM.core.machining.config import MachiningConfig

    check("a preference reaches the config",
          MachiningConfig.from_preferences(prefs).machine_mode == "5axis")
    five_axis = MachiningAnalyzer().execute(
        occ, face_index, edge_index, prefs=prefs
    )
    undercut_rule = next(
        (r for r in process.active_rules if r.name == "UNDERCUT_PRESENT"), None
    )
    if undercut_rule is not None:
        limits = default.rule_limits.get(undercut_rule)
        found = get_check_class(undercut_rule)().run_check(
            five_axis, limits, undercut_rule, feedback=RuleFeedback()
        )
        check("five-axis mode stands the undercut rule down", not found)
    params.SetString("MachiningMachineMode", "3axis")

    # -- the rest of the workbench still works ------------------------------
    say("")
    say("Existing processes")
    plastic = registry.get_process_by_name("Plastic Injection Molding")
    check("injection moulding still loads", plastic is not None)
    check(
        "its rules still resolve",
        plastic is not None
        and all(get_check_class(rule) is not None for rule in plastic.active_rules),
    )

    FreeCAD.closeDocument("dfm_verify")

    say("")
    if failures:
        say("FAILED: " + ", ".join(failures))
    else:
        say("ALL CHECKS PASSED")

except Exception:
    say("")
    say("ERRORED")
    say(traceback.format_exc())
finally:
    log.close()
