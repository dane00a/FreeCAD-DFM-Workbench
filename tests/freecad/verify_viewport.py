# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Proof that a machining finding actually paints on the model.

The rest of the verification runs under ``freecadcmd``, which has no GUI at
all: a headless ``ViewObject`` is None, so every highlighting path is skipped
and the checks that pass there prove only that a finding names a face that
exists. Whether the face it names is the one the machinist sees lit up is a
different question, and it is exactly the kind that stays invisible until
somebody looks at the screen.

Face numbering is where it goes wrong. The recognizers count from one, as
OpenCascade does and as FreeCAD's own ``Face1`` labels do. The view bridge
counts from zero and adds one back on. A finding is converted on its way out
of the analysis; anything reading features directly has to convert for
itself, and an off-by-one there highlights the neighbouring face -- which
looks plausible, which is what makes it dangerous.

Run with the real GUI, not freecadcmd::

    "C:/Program Files/FreeCAD 1.1/bin/freecad.exe" tests/freecad/verify_viewport.py

Writes ``verify_viewport.out`` beside this file, and a screenshot of the
highlighted part next to it, then closes FreeCAD.
"""

import os
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(_HERE, "verify_viewport.out")
SHOT = os.path.join(_HERE, "verify_viewport.png")

log = open(REPORT, "w", encoding="utf-8")
failures = []


def say(text=""):
    log.write(str(text) + "\n")
    log.flush()


def check(description, condition, detail=""):
    say("  [%s] %s%s" % ("PASS" if condition else "FAIL", description, detail))
    if not condition:
        failures.append(description)


def run():
    import FreeCAD
    import FreeCADGui

    say("FreeCAD " + ".".join(FreeCAD.Version()[:3]))
    check("the GUI is up", FreeCAD.GuiUp)

    import freecad.DFM  # noqa: F401
    from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
    from freecad.DFM.core.processes.process import RuleFeedback
    from freecad.DFM.core.registries import ProcessRegistry, get_check_class
    from freecad.DFM.core.utils.conversion import freecad_to_ocp
    from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex
    from freecad.DFM.gui.results.bridge import DFMViewProvider
    from freecad.DFM.gui.results.visuals import severity_color
    from freecad.DFM.gui.task_setup import _resolve_geometry_refs

    # -- a part with one pocket and one deep hole ---------------------------
    doc = FreeCAD.newDocument("dfm_viewport")
    body = doc.addObject("Part::Box", "Body")
    body.Length, body.Width, body.Height = 120.0, 80.0, 25.0
    tool = doc.addObject("Part::Box", "PocketTool")
    tool.Length, tool.Width, tool.Height = 80.0, 40.0, 20.0
    tool.Placement.Base = FreeCAD.Vector(20, 20, 10)
    stage = doc.addObject("Part::Cut", "Stage")
    stage.Base, stage.Tool = body, tool
    drill = doc.addObject("Part::Cylinder", "DrillTool")
    drill.Radius, drill.Height = 2.0, 60.0
    drill.Placement.Base = FreeCAD.Vector(116, 70, -5)
    part = doc.addObject("Part::Cut", "Part")
    part.Base, part.Tool = stage, drill
    doc.recompute()
    FreeCADGui.ActiveDocument.ActiveView.viewAxonometric()
    FreeCADGui.SendMsgToActiveView("ViewFit")

    shape = part.Shape
    occ = freecad_to_ocp(shape)
    data = MachiningAnalyzer().execute(occ, FaceIndex(occ), EdgeIndex(occ), prefs={})
    context = list(data.values())[0]
    say("")
    say("Analysis")
    say("  features: " + str(context.recognition.counts()))

    process = ProcessRegistry.get_instance().get_process_by_name("CNC Milling")
    default = process.materials["Default"]
    findings = []
    for rule in process.active_rules:
        check_class = get_check_class(rule)
        limits = default.rule_limits.get(rule)
        if check_class is None or limits is None:
            continue
        for result in check_class().run_check(
            data, limits, rule, feedback=RuleFeedback()
        ):
            findings.append((rule, result))
    check("the analysis produced findings to render", bool(findings),
          " (%d)" % len(findings))
    if not findings:
        return

    # The indices a finding carries, before and after the conversion the
    # results panel does on its way to the viewport.
    raw = sorted({i for _, r in findings for k, i in r.failing_geometry if k == "Face"})
    _resolve_geometry_refs([r for _, r in findings])
    resolved = sorted(
        {ref.index for _, r in findings for ref in r.refs if ref.type == "Face"}
    )
    say("")
    say("Face numbering")
    say("  finding face ids (as recognized): %s" % raw[:8])
    say("  after conversion for the viewport: %s" % resolved[:8])
    check("the recognized ids are one-based",
          bool(raw) and min(raw) >= 1 and max(raw) <= len(shape.Faces))
    check("the viewport ids are zero-based",
          resolved == [i - 1 for i in raw])

    # -- the real rendering path -------------------------------------------
    bridge = DFMViewProvider(part)
    pairs = [
        (ref.index, severity_color(result.severity))
        for _, result in findings
        for ref in result.refs
        if ref.type == "Face"
    ]
    bridge.highlight_faces_and_edges_by_index(pairs, [])
    doc.recompute()
    FreeCADGui.updateGui()

    say("")
    say("Viewport")
    overlay = doc.getObject("DFM_Highlight_Overlay")
    check("an overlay object was created", overlay is not None)
    if overlay is None:
        return
    check("the overlay carries the part's shape",
          len(overlay.Shape.Faces) == len(shape.Faces),
          " (%d vs %d)" % (len(overlay.Shape.Faces), len(shape.Faces)))
    check("the original is hidden behind it", not part.ViewObject.Visibility)
    check("the overlay is visible", overlay.ViewObject.Visibility)

    colours = list(overlay.ViewObject.DiffuseColor)
    check("a colour is set for every face", len(colours) == len(shape.Faces),
          " (%d colours, %d faces)" % (len(colours), len(shape.Faces)))

    inactive = DFMViewProvider.OVERLAY_INACTIVE_COLOR
    def is_inactive(c):
        return all(abs(a - b) < 0.02 for a, b in zip(c[:3], inactive[:3]))

    lit = [i for i, c in enumerate(colours) if not is_inactive(c)]
    say("  faces painted a finding colour: %s" % lit)
    check("exactly the faces the findings name are painted",
          sorted(lit) == resolved,
          "" if sorted(lit) == resolved else " (painted %s, expected %s)"
          % (sorted(lit), resolved))

    # -- the feature census drives the same path ---------------------------
    say("")
    say("Feature census highlighting")
    feature = next(
        (f for f in context.recognition.features if f.faces), None
    )
    if feature is not None:
        # This is the conversion the census has to do for itself.
        census_pairs = [(fid - 1, severity_color(findings[0][1].severity))
                        for fid in feature.faces]
        bridge.highlight_faces_and_edges_by_index(census_pairs, [])
        doc.recompute()
        FreeCADGui.updateGui()
        colours = list(doc.getObject("DFM_Highlight_Overlay").ViewObject.DiffuseColor)
        lit = sorted(i for i, c in enumerate(colours) if not is_inactive(c))
        want = sorted(fid - 1 for fid in feature.faces)
        say("  %s owns faces %s; painted %s" % (feature.type, feature.faces, lit))
        check("a feature's own faces light up", lit == want,
              "" if lit == want else " (painted %s, expected %s)" % (lit, want))
        check("every painted index is in range",
              all(0 <= i < len(shape.Faces) for i in lit))

    try:
        FreeCADGui.ActiveDocument.ActiveView.saveImage(SHOT, 900, 650, "Current")
        check("a screenshot of the highlighted part was written",
              os.path.exists(SHOT))
    except Exception as exc:
        say("  [note] screenshot unavailable: %s" % exc)

    FreeCAD.closeDocument("dfm_viewport")


try:
    run()
    say("")
    say("FAILED: " + ", ".join(failures) if failures else "ALL CHECKS PASSED")
except Exception:
    say("")
    say("ERRORED")
    say(traceback.format_exc())
finally:
    log.close()
    try:
        import FreeCADGui

        FreeCADGui.getMainWindow().close()
    except Exception:
        pass
