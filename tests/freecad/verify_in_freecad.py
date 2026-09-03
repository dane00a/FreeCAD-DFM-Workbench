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
    check("the deep hole is reported", "HOLE_DEPTH_RATIO" in fired)
    check(
        "nothing else fires on an otherwise ordinary part",
        len(findings) == 1,
        " (%d findings)" % len(findings),
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
