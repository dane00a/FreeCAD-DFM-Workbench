# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Drive the whole workbench through its own interface and check what it shows.

The unit suite proves the rules are right. This proves the machinist can
actually get at them: that the tree fills in, that clicking a finding paints
the face it names, that ignoring one changes the verdict, that the export
button writes a file with the columns the checkboxes asked for.

None of that is provable headless. ``freecadcmd`` has no GUI at all -- a
``ViewObject`` is None, ``Gui.Control`` refuses a dialog, ``QIcon`` renders
nothing -- so every one of these paths is skipped rather than exercised, and
a panel that raises on the first click passes a headless suite untouched.
The whole file therefore runs under the real application.

What it is looking for is the class of fault that only appears once a human
is in the loop: an index converted in one place and not another, a handler
wired to a signal that no longer exists, a widget read before it is filled,
a dialog that leaves the model hidden behind an overlay after it closes.

Run with the real GUI, not freecadcmd::

    "C:/Program Files/FreeCAD 1.1/bin/freecad.exe" tests/freecad/verify_ui.py

Writes ``verify_ui.out`` beside this file, then closes FreeCAD. Nothing is
written outside the scratch directory it makes and removes: the history
manager is pointed at a temporary directory rather than the user's own, and
every preference it writes is put back.
"""

import os
import shutil
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(_HERE, "verify_ui.out")

log = open(REPORT, "w", encoding="utf-8")
failures = []
scratch = None


def say(text=""):
    log.write(str(text) + "\n")
    log.flush()


def section(title):
    say("")
    say(title)


def check(description, condition, detail=""):
    say("  [%s] %s%s" % ("PASS" if condition else "FAIL", description, detail))
    if not condition:
        failures.append(description)


def guard(description, fn):
    """Run something that touches the UI and report a raise as a failure.

    A panel that throws is the fault this file exists to catch, and one
    exception should not take the remaining sections down with it.
    """
    try:
        fn()
        check(description, True)
        return True
    except Exception as exc:
        check(description, False, ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
        return False


def pump():
    """Let Qt deliver whatever the last call queued."""
    from PySide6 import QtWidgets

    QtWidgets.QApplication.processEvents()


# ---------------------------------------------------------------------------
# The part, and the analysis of it
# ---------------------------------------------------------------------------


def build_milled_part(doc):
    """A plate carrying enough trouble to fill the panel.

    Every level of the results tree needs something in it before the code
    that draws it is actually run. So: a rule that fires once, a rule that
    fires seven times over a bolt circle, a finding at error severity and
    findings at warning, and a good many rules that stay silent -- without
    those last the Passed group never appears.
    """
    import math

    import FreeCAD
    import Part

    solid = Part.makeBox(120.0, 80.0, 25.0)

    # A deep square pocket: sharp corners no cutter can leave.
    solid = solid.cut(Part.makeBox(70, 40, 21, FreeCAD.Vector(15, 20, 4)))

    # A bolt circle of six deep holes -- one rule, six identical findings,
    # which is what a rule row with several children has to cope with.
    for index in range(6):
        angle = index * math.pi / 3.0
        solid = solid.cut(
            Part.makeCylinder(
                1.6,
                40,
                FreeCAD.Vector(
                    100 + 8 * math.cos(angle), 40 + 8 * math.sin(angle), -5
                ),
            )
        )

    # One hole far past the tool's reach, so at least one finding is an
    # error rather than a warning and the verdict has to say FAILED.
    solid = solid.cut(Part.makeCylinder(0.8, 40, FreeCAD.Vector(30, 70, -5)))

    # A narrow deep slot and a flat-bottomed blind hole: two more rules.
    solid = solid.cut(Part.makeBox(3, 50, 18, FreeCAD.Vector(6, 15, 7)))
    solid = solid.cut(Part.makeCylinder(4, 15, FreeCAD.Vector(60, 68, 10)))

    part = doc.addObject("Part::Feature", "MilledPart")
    part.Shape = solid
    doc.recompute()
    return part


def build_moulded_part(doc):
    """An open-topped box, for the rules that point at edges.

    No machining rule names an edge -- they all speak about faces -- so the
    edge half of the viewport bridge is only reachable through a moulding
    analysis, where the sharp-corner rules report the corner itself. A
    thin-walled cup has one at every wall-to-floor junction.
    """
    import FreeCAD
    import Part

    part = doc.addObject("Part::Feature", "MouldedPart")
    part.Shape = Part.makeBox(60, 40, 20).cut(
        Part.makeBox(54, 34, 18, FreeCAD.Vector(3, 3, 2))
    )
    doc.recompute()
    return part


def analyse(part):
    """The findings, resolved the way the panel gets them."""
    from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
    from freecad.DFM.core.processes.process import RuleFeedback
    from freecad.DFM.core.registries import ProcessRegistry, get_check_class
    from freecad.DFM.core.utils.conversion import freecad_to_ocp
    from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex
    from freecad.DFM.gui.task_setup import _resolve_geometry_refs

    occ = freecad_to_ocp(part.Shape)
    data = MachiningAnalyzer().execute(occ, FaceIndex(occ), EdgeIndex(occ), prefs={})
    context = list(data.values())[0]

    process = ProcessRegistry.get_instance().get_process_by_name("CNC Milling")
    material = process.materials["Default"]
    results = []
    for rule in process.active_rules:
        check_class = get_check_class(rule)
        limits = material.rule_limits.get(rule)
        if check_class is None or limits is None:
            continue
        results.extend(
            check_class().run_check(data, limits, rule, feedback=RuleFeedback())
        )
    _resolve_geometry_refs(results)
    return process, context, results


def analyse_moulding(part):
    """The same, for the process whose rules name edges.

    Written out rather than shared with `analyse` because this one has to
    resolve its analyzers by id and hand them a pull direction -- which is
    the requirement plumbing the setup panel exists to collect, and worth
    exercising here rather than assuming.
    """
    from OCP.gp import gp_Dir

    from freecad.DFM.core.processes.process import RuleFeedback
    from freecad.DFM.core.registries import (
        ProcessRegistry,
        get_analyzer_class,
        get_check_class,
    )
    from freecad.DFM.core.utils.conversion import freecad_to_ocp
    from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex
    from freecad.DFM.gui.task_setup import _resolve_geometry_refs

    occ = freecad_to_ocp(part.Shape)
    faces, edges = FaceIndex(occ), EdgeIndex(occ)

    process = ProcessRegistry.get_instance().get_process_by_name(
        "Plastic Injection Molding"
    )
    material = process.materials.get("Default") or list(process.materials.values())[0]

    cache = {}
    results = []
    for rule in process.active_rules:
        check_class = get_check_class(rule)
        limits = material.rule_limits.get(rule)
        if check_class is None or limits is None:
            continue
        instance = check_class()
        analyzer_id = instance.required_analyzer_id
        if analyzer_id not in cache:
            cache[analyzer_id] = get_analyzer_class(analyzer_id)().execute(
                occ, faces, edges, prefs={}, PULL_DIRECTION=gp_Dir(0, 0, 1)
            )
        results.extend(
            instance.run_check(cache[analyzer_id], limits, rule, feedback=RuleFeedback())
        )
    _resolve_geometry_refs(results)
    return process, results


# ---------------------------------------------------------------------------
# Reading the viewport back
# ---------------------------------------------------------------------------


def painted_faces(doc, face_count):
    """Which face indices are currently lit, read off the overlay itself.

    Read from `DiffuseColor` rather than from what we asked for, because the
    whole point is to catch the case where the two disagree.
    """
    from freecad.DFM.gui.results.bridge import DFMViewProvider

    overlay = doc.getObject("DFM_Highlight_Overlay")
    if overlay is None:
        # Clearing the highlight removes the overlay outright and gives the
        # part back, rather than repainting every face inactive. Nothing
        # painted is the honest reading of that, not "cannot tell".
        return []
    inactive = DFMViewProvider.OVERLAY_INACTIVE_COLOR
    colours = list(overlay.ViewObject.DiffuseColor)
    if len(colours) != face_count:
        return None
    return sorted(
        index
        for index, colour in enumerate(colours)
        if not all(abs(a - b) < 0.02 for a, b in zip(colour[:3], inactive[:3]))
    )


def face_refs_of(findings):
    """The zero-based face indices a set of findings names, ignored ones out."""
    return sorted(
        {
            ref.index
            for finding in findings
            if not finding.ignore
            for ref in finding.refs
            if ref.type == "Face"
        }
    )


def painted_edges(doc, edge_count):
    """Which edge indices are currently drawn in a finding colour."""
    from freecad.DFM.gui.results.bridge import DFMViewProvider

    overlay = doc.getObject("DFM_Highlight_Overlay")
    if overlay is None:
        return []
    inactive = DFMViewProvider.OVERLAY_INACTIVE_COLOR
    colours = list(overlay.ViewObject.LineColorArray)
    if len(colours) != edge_count:
        return None
    return sorted(
        index
        for index, colour in enumerate(colours)
        if not all(abs(a - b) < 0.02 for a, b in zip(colour[:3], inactive[:3]))
    )


def edge_refs_of(findings):
    """The zero-based edge indices a set of findings names."""
    return sorted(
        {
            ref.index
            for finding in findings
            if not finding.ignore
            for ref in finding.refs
            if ref.type == "Edge"
        }
    )


def walk(model, item=None):
    """Every item in the tree, depth first."""
    root = item if item is not None else model.invisibleRootItem()
    for row in range(root.rowCount()):
        child = root.child(row)
        yield child
        for deeper in walk(model, child):
            yield deeper


def kind_of(item):
    from PySide6 import QtCore

    return item.data(QtCore.Qt.ItemDataRole.UserRole + 1)


def label_of(item):
    from PySide6 import QtCore

    return item.data(QtCore.Qt.ItemDataRole.UserRole + 2)


def payload_of(item):
    from PySide6 import QtCore

    return item.data(QtCore.Qt.ItemDataRole.UserRole)


def find_item(model, kind, label=None):
    for item in walk(model):
        if kind_of(item) == kind and (label is None or label_of(item) == label):
            return item
    return None


def select(view, item):
    """Click a row, the way the user does, and let the signal land."""
    view.form.tvResults.setCurrentIndex(view.model.indexFromItem(item))
    pump()


# ---------------------------------------------------------------------------
# The results panel
# ---------------------------------------------------------------------------


def verify_tree(view, model_obj, results):
    """The shape of what the machinist is presented with."""
    section("Results tree")
    model = view.model

    all_item = find_item(model, "all")
    check("there is a top row covering every finding", all_item is not None)
    if all_item is None:
        return
    check("it is labelled for a reader, not for the code",
          label_of(all_item) == "Recommendations",
          " (%r)" % label_of(all_item))
    check("it carries every finding, ignored ones included",
          len(payload_of(all_item)) == len(results),
          " (%d of %d)" % (len(payload_of(all_item) or []), len(results)))

    crit_items = [i for i in walk(model) if kind_of(i) == "criticality"]
    rule_items = [i for i in walk(model) if kind_of(i) == "rule"]
    finding_items = [i for i in walk(model) if kind_of(i) == "finding"]
    say("  %d criticality groups, %d rule rows, %d finding rows"
        % (len(crit_items), len(rule_items), len(finding_items)))

    grouped = model_obj.get_grouped_results()
    check("every rule that fired has a row",
          len({label_of(i) for i in rule_items}) >= len(grouped),
          " (%d rows for %d firing rules)" % (len(rule_items), len(grouped)))
    check("every finding has a row of its own",
          len(finding_items) == len(results),
          " (%d rows for %d findings)" % (len(finding_items), len(results)))

    # The Passed group is what tells a machinist the analysis actually ran
    # the rest of the rules rather than stopping early.
    passed = next((i for i in walk(model) if label_of(i) == "Passed"), None)
    check("rules that passed are listed as having passed", passed is not None)
    if passed is not None:
        silent = [r for r in model_obj.process.active_rules if r not in grouped]
        check("every silent rule is named there",
              passed.rowCount() == len(silent),
              " (%d rows for %d silent rules)" % (passed.rowCount(), len(silent)))
        check("a passed rule carries no findings",
              all(payload_of(passed.child(r)) == []
                  for r in range(passed.rowCount())))

    # Severity is the ordering the panel promises: worst first.
    ordered = True
    for rule_item in rule_items:
        findings = payload_of(rule_item) or []
        active = [f for f in findings if not f.ignore]
        severities = [f.severity.value for f in active]
        if severities != sorted(severities, reverse=True):
            ordered = False
    check("findings under a rule run worst-first", ordered)

    check("every finding row names the geometry it is about",
          all(label_of(i) for i in finding_items))


def verify_selection(view, presenter, doc, part, results):
    """Clicking a row has to light up exactly what the row is about."""
    section("Selection lights the right geometry")
    model = view.model
    face_count = len(part.Shape.Faces)

    finding_items = [i for i in walk(model) if kind_of(i) == "finding"]
    with_faces = [
        i for i in finding_items
        if any(ref.type == "Face" for ref in payload_of(i).refs)
    ]
    check("at least one finding points at a face", bool(with_faces))

    if with_faces:
        item = with_faces[0]
        finding = payload_of(item)
        select(view, item)
        doc.recompute()
        want = face_refs_of([finding])
        got = painted_faces(doc, face_count)
        check("one finding paints exactly its own faces", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))
        check("the details pane says what the finding is",
              finding.overview[:20] in view.form.tbDetails.toPlainText(),
              " (pane reads %r)" % view.form.tbDetails.toPlainText()[:60])

    # A rule row stands for all of its findings at once.
    rule_items = [
        i for i in walk(model)
        if kind_of(i) == "rule" and payload_of(i)
    ]
    if rule_items:
        item = max(rule_items, key=lambda i: len(payload_of(i)))
        findings = payload_of(item)
        select(view, item)
        doc.recompute()
        want = face_refs_of(findings)
        got = painted_faces(doc, face_count)
        check("a rule row paints every face its findings name", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))
        check("the details pane names the rule",
              item and label_of(item)[:12] in view.form.tbDetails.toPlainText(),
              " (pane reads %r)" % view.form.tbDetails.toPlainText()[:60])

    # And the top row stands for the whole part.
    all_item = find_item(model, "all")
    if all_item is not None:
        select(view, all_item)
        doc.recompute()
        want = face_refs_of(payload_of(all_item))
        got = painted_faces(doc, face_count)
        check("the top row paints every face the analysis named", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))

    # A rule that passed has nothing to show, and must say so rather than
    # leaving the last selection's faces lit.
    passed_rule = next(
        (i for i in walk(model) if kind_of(i) == "rule" and payload_of(i) == []),
        None,
    )
    if passed_rule is not None:
        select(view, passed_rule)
        doc.recompute()
        got = painted_faces(doc, face_count)
        check("selecting a passed rule clears the highlight", got == [],
              "" if got == [] else " (still painted %s)" % got)
        check("and says the rule passed",
              "No issues" in view.form.tbDetails.toPlainText(),
              " (pane reads %r)" % view.form.tbDetails.toPlainText()[:60])

    check("no painted index is ever out of range",
          all(0 <= i < face_count for i in (painted_faces(doc, face_count) or [])))


def verify_ignore(view, presenter, model_obj, doc, part):
    """Ignoring a finding has to change the verdict, the tree and the paint."""
    section("Ignoring findings")
    face_count = len(part.Shape.Faces)

    def active_count():
        return len(model_obj.active_results)

    before_verdict = view.form.leVerdict.text()
    before_active = active_count()

    finding_items = [i for i in walk(view.model) if kind_of(i) == "finding"]
    target = next(
        (payload_of(i) for i in finding_items
         if not payload_of(i).ignore
         and any(ref.type == "Face" for ref in payload_of(i).refs)),
        None,
    )
    check("there is an active finding to ignore", target is not None)
    if target is None:
        return

    presenter.handle_ignore(target)
    pump()
    check("the finding is marked ignored", target.ignore)
    check("it drops out of the active count",
          active_count() == before_active - 1,
          " (%d, was %d)" % (active_count(), before_active))

    # The row must still be there -- ignoring is not deleting -- and the
    # tree must be rebuilt rather than left stale.
    rebuilt = [i for i in walk(view.model) if kind_of(i) == "finding"]
    check("the row stays in the tree after being ignored",
          len(rebuilt) == len(finding_items),
          " (%d rows, was %d)" % (len(rebuilt), len(finding_items)))
    check("and is flagged as ignored on its row",
          any(payload_of(i).ignore for i in rebuilt))

    # An ignored finding must not keep painting the model.
    rule_item = next(
        (i for i in walk(view.model)
         if kind_of(i) == "rule" and target in (payload_of(i) or [])),
        None,
    )
    if rule_item is not None:
        select(view, rule_item)
        doc.recompute()
        got = painted_faces(doc, face_count)
        want = face_refs_of(payload_of(rule_item))
        check("an ignored finding stops painting its faces", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))

    presenter.handle_ignore(target)
    pump()
    check("restoring puts it back in the count",
          active_count() == before_active,
          " (%d, wanted %d)" % (active_count(), before_active))
    check("and the verdict returns to what it was",
          view.form.leVerdict.text() == before_verdict,
          " (%r, was %r)" % (view.form.leVerdict.text(), before_verdict))

    # Ignore-all over a whole rule, which is the context-menu action.
    busiest = max(
        (i for i in walk(view.model) if kind_of(i) == "rule" and payload_of(i)),
        key=lambda i: len(payload_of(i)),
        default=None,
    )
    if busiest is not None:
        findings = list(payload_of(busiest))
        presenter.handle_ignore_all(findings)
        pump()
        check("ignoring a whole rule ignores all of its findings",
              all(f.ignore for f in findings))
        check("and takes them all out of the count",
              active_count() == before_active - len(findings),
              " (%d, wanted %d)"
              % (active_count(), before_active - len(findings)))
        presenter.handle_ignore_all(findings)
        pump()
        check("restoring the rule restores every one",
              not any(f.ignore for f in findings))
        check("and the verdict is back where it started",
              view.form.leVerdict.text() == before_verdict,
              " (%r)" % view.form.leVerdict.text())


def verify_zoom(view, presenter, doc, part):
    """Zoom must move the camera and must not disturb the highlight."""
    section("Zoom")
    import FreeCADGui

    face_count = len(part.Shape.Faces)
    view3d = FreeCADGui.ActiveDocument.ActiveView
    finding = next(
        (payload_of(i) for i in walk(view.model)
         if kind_of(i) == "finding"
         and any(ref.type == "Face" for ref in payload_of(i).refs)),
        None,
    )
    if finding is None:
        check("a finding with a face to zoom to", False)
        return

    def camera():
        return tuple(view3d.getCameraNode().position.getValue().getValue())

    view3d.viewAxonometric()
    FreeCADGui.SendMsgToActiveView("ViewFit")
    pump()
    before = camera()
    guard("double-clicking a finding zooms without raising",
          lambda: presenter.handle_zoom(finding))
    pump()
    moved = any(abs(a - b) > 1e-6 for a, b in zip(before, camera()))
    check("the camera actually moved", moved,
          "" if moved else " (still at %s)" % (camera(),))
    got = painted_faces(doc, face_count)
    want = face_refs_of([finding])
    check("and the finding is still the thing lit up", got == want,
          "" if got == want else " (painted %s, wanted %s)" % (got, want))

    busiest = max(
        (payload_of(i) for i in walk(view.model)
         if kind_of(i) == "rule" and payload_of(i)),
        key=len,
        default=None,
    )
    if busiest:
        guard("zoom-to-rule runs over every finding at once",
              lambda: presenter.handle_zoom_to_rule(busiest))
        pump()
        got = painted_faces(doc, face_count)
        want = face_refs_of(busiest)
        check("and lights all of them", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))


def verify_export(view, model_obj, part):
    """The export button has to write the file the checkboxes describe."""
    section("CSV export")
    import csv as _csv

    from freecad.DFM.gui.results.utils import CSVResultExporter

    path = os.path.join(scratch, "findings.csv")

    # Every column on, ignored rows in: the widest shape the panel offers.
    view.form.cbColCriticality.setChecked(True)
    view.form.cbColFeedback.setChecked(True)
    view.form.cbColMetadata.setChecked(True)
    view.form.cbColUnit.setChecked(True)
    view.form.cbRowErrors.setChecked(True)
    view.form.cbRowWarnings.setChecked(True)
    view.form.cbRowIgnored.setChecked(True)
    view.form.cbDelimiter.setCurrentIndex(0)
    config = view.get_export_config()
    check("the panel's checkboxes reach the export config",
          config.include_criticality and config.include_unit
          and config.include_metadata and config.include_ignored)
    check("the delimiter combo picks a real delimiter",
          config.delimiter == ",", " (%r)" % config.delimiter)

    written = CSVResultExporter.export(
        path, part.Label, model_obj, config, model_obj.process.get_criticality
    )
    check("the exporter reports success", written)
    check("a file appeared on disk", os.path.exists(path))
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(_csv.reader(handle))
    flat = [cell for row in rows for cell in row]
    check("the part it describes is named in the file", part.Label in flat)
    check("so is the process", model_obj.process.name in flat)
    header = next((r for r in rows if r and r[0] == "Status"), None)
    check("there is a header row", header is not None)
    if header is not None:
        for column in ("Rule", "Criticality", "Face", "Value", "Limit", "Unit"):
            check("the %s column is present" % column.lower(),
                  column in header)
    body = [r for r in rows[rows.index(header) + 1:] if r] if header else []
    findings = [r for r in body if r[0] != "PASS"]
    passed_rows = [r for r in body if r[0] == "PASS"]
    check("every finding got a row",
          len(findings) == len(model_obj.results),
          " (%d rows for %d findings)"
          % (len(findings), len(model_obj.results)))
    # An estimator reading the file needs to see what was checked and came
    # back clean, not just what failed.
    silent = [
        r for r in model_obj.process.active_rules
        if r not in model_obj.get_grouped_results()
    ]
    if config.include_passed:
        check("and every rule that passed is listed as having passed",
              len(passed_rows) == len(silent),
              " (%d rows for %d silent rules)" % (len(passed_rows), len(silent)))
    else:
        check("passed rules stay out when the box is unchecked",
              not passed_rows, " (%d rows)" % len(passed_rows))

    # Turning columns off has to actually narrow the file.
    view.form.cbColCriticality.setChecked(False)
    view.form.cbColUnit.setChecked(False)
    view.form.cbColMetadata.setChecked(False)
    narrow_path = os.path.join(scratch, "narrow.csv")
    CSVResultExporter.export(
        narrow_path, part.Label, model_obj,
        view.get_export_config(), model_obj.process.get_criticality,
    )
    with open(narrow_path, encoding="utf-8", newline="") as handle:
        narrow = list(_csv.reader(handle))
    narrow_header = next((r for r in narrow if r and r[0] == "Status"), None)
    check("switching off criticality removes the column",
          narrow_header is not None and "Criticality" not in narrow_header)
    check("switching off units removes the column",
          narrow_header is not None and "Unit" not in narrow_header)
    check("switching off metadata removes the preamble",
          bool(narrow) and bool(narrow[0]) and narrow[0][0] == "Status",
          " (first row %r)" % (narrow[0] if narrow else None))

    # A semicolon shop is a real shop.
    view.form.cbDelimiter.setCurrentIndex(1)
    check("choosing semicolon changes the delimiter",
          view.get_export_config().delimiter == ";",
          " (%r)" % view.get_export_config().delimiter)
    view.form.cbDelimiter.setCurrentIndex(0)


def verify_features_tab(view, presenter, context, doc, part):
    """The census tab, and the face numbering it has to convert for itself.

    The recognizers count faces from one; the view bridge counts from zero.
    A finding is converted on its way out of the analysis, but the census
    reads the features directly and has to convert here -- and getting it
    wrong lights the neighbouring face, which looks plausible enough to
    ship.
    """
    section("Feature census tab")

    tab = getattr(view, "features_tab", None)
    check("the panel has a features tab", tab is not None)
    if tab is None:
        return

    check("the census is rendered as a tree", tab.tree is not None)
    features = [f for f in context.recognition.features if f.faces]
    check("the analysis recognized something to show", bool(features),
          " (%s)" % context.recognition.counts())

    shown = tab.tree.topLevelItemCount()
    say("  %d top-level census rows for %d feature types"
        % (shown, len(context.recognition.counts())))
    check("one row per feature type recognized",
          shown == len(context.recognition.counts()),
          " (%d rows, %d types)" % (shown, len(context.recognition.counts())))
    total_rows = sum(tab.tree.topLevelItem(i).childCount() for i in range(shown))
    check("and one child row per instance of it",
          total_rows == len(context.recognition.features),
          " (%d children, %d features)"
          % (total_rows, len(context.recognition.features)))
    check("the columns are headed",
          tab.tree.columnCount() == 4 and
          tab.tree.headerItem().text(0) == "Feature",
          " (%d columns, first %r)"
          % (tab.tree.columnCount(), tab.tree.headerItem().text(0)))

    check("the tab says how the part was classified",
          bool(tab.summary.text()),
          " (%r)" % tab.summary.text()[:70])

    # The conversion, driven through the presenter's own callback.
    if features:
        feature = features[0]
        face_count = len(part.Shape.Faces)
        presenter.handle_feature_faces(sorted(feature.faces))
        doc.recompute()
        got = painted_faces(doc, face_count)
        want = sorted(fid - 1 for fid in feature.faces)
        check("selecting a feature lights that feature's faces", got == want,
              "" if got == want
              else " (painted %s, wanted %s)" % (got, want))
        check("the census converts from one-based to zero-based",
              got != sorted(feature.faces),
              " (painted the raw one-based ids)")
        check("no painted index runs off the end of the shape",
              all(0 <= i < face_count for i in (got or [])))

    # The census export is a separate writer from the findings CSV.
    census_path = os.path.join(scratch, "census.csv")
    from freecad.DFM.core.machining.census import write_census

    guard("the census writes itself out",
          lambda: write_census(census_path, context.recognition.features))
    check("a census file appeared", os.path.exists(census_path))
    if os.path.exists(census_path):
        text = open(census_path, encoding="utf-8").read()
        check("it names the features it found",
              any(t in text for t in context.recognition.counts()),
              " (%r)" % text[:80])


def verify_overlay_lifecycle(view, presenter, doc, part):
    """The overlay is a decoration, and it has to come off cleanly."""
    section("Overlay lifecycle")

    overlay = doc.getObject("DFM_Highlight_Overlay")
    check("an overlay stands in for the part while the panel is open",
          overlay is not None)
    if overlay is not None:
        check("it carries the same face count as the part",
              len(overlay.Shape.Faces) == len(part.Shape.Faces),
              " (%d vs %d)"
              % (len(overlay.Shape.Faces), len(part.Shape.Faces)))
        check("the part itself is hidden behind it",
              not part.ViewObject.Visibility)
        check("and the overlay is what is being shown",
              overlay.ViewObject.Visibility)
        check("every face has a colour, not just the lit ones",
              len(list(overlay.ViewObject.DiffuseColor))
              == len(part.Shape.Faces))

    presenter.handle_cleanup()
    doc.recompute()
    pump()
    check("closing the panel takes the overlay away",
          doc.getObject("DFM_Highlight_Overlay") is None)
    check("and gives the machinist their part back",
          part.ViewObject.Visibility)


def verify_setup_panel(doc, part):
    """The panel the analysis is started from."""
    section("Analysis setup panel")
    import FreeCADGui
    from PySide6 import QtWidgets

    from freecad.DFM.core.machining import blank_declaration
    from freecad.DFM.gui.task_setup import TaskSetup

    FreeCADGui.Selection.clearSelection()
    FreeCADGui.Selection.addSelection(part)

    panel = None
    try:
        panel = TaskSetup()
    except Exception as exc:
        check("the setup panel opens", False,
              ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
        return
    check("the setup panel opens", True)

    form = panel.form
    check("it picked up the selected object by itself",
          panel.target_object is part,
          " (%r)" % getattr(panel.target_object, "Label", None))
    check("and shows its name", form.leSelectModel.text() == part.Label,
          " (%r)" % form.leSelectModel.text())

    check("the category list is populated", form.cbManCategory.count() > 0)
    check("the process list waits for a category",
          not form.cbManProcess.isEnabled())
    check("the material list waits for a process",
          not form.cbMaterial.isEnabled())

    # Walk it the way a user does: category, then process, then material.
    milling_category = None
    for index in range(form.cbManCategory.count()):
        form.cbManCategory.setCurrentIndex(index)
        pump()
        for p in range(form.cbManProcess.count()):
            if form.cbManProcess.itemText(p) == "CNC Milling":
                milling_category = (index, p)
                break
        if milling_category:
            break
    check("CNC Milling is reachable from the category list",
          milling_category is not None)
    if milling_category is None:
        panel.reject()
        return

    form.cbManCategory.setCurrentIndex(milling_category[0])
    pump()
    check("choosing a category enables the process list",
          form.cbManProcess.isEnabled())
    form.cbManProcess.setCurrentIndex(milling_category[1])
    pump()
    check("choosing a process enables the material list",
          form.cbMaterial.isEnabled())
    check("and the process is loaded", panel.process is not None,
          " (%r)" % getattr(panel.process, "name", None))
    check("the material list has entries", form.cbMaterial.count() > 0)

    # The blank row: machining only, saved on the part, read back.
    check("the blank control is shown for a machining process",
          form.cbBlankForm.isVisible() or form.cbBlankForm.isEnabled())
    check("it offers the shop default first",
          form.cbBlankForm.itemData(0) == "",
          " (%r)" % form.cbBlankForm.itemData(0))
    values = [form.cbBlankForm.itemData(i)
              for i in range(form.cbBlankForm.count())]
    check("and the three stock forms", set(values) ==
          {"", "billet", "as_cast", "profile_extrusion"},
          " (%s)" % values)

    billet = form.cbBlankForm.findData("billet")
    form.cbBlankForm.setCurrentIndex(billet)
    panel._save_blank_declaration()
    check("choosing a blank records it on the part",
          blank_declaration.declared_blank(part) == "billet",
          " (%r)" % blank_declaration.declared_blank(part))

    panel.target_object = None
    panel.target_object = part
    panel._load_blank_declaration()
    check("and re-opening the panel shows it again",
          form.cbBlankForm.currentData() == "billet",
          " (%r)" % form.cbBlankForm.currentData())

    form.cbBlankForm.setCurrentIndex(0)
    panel._save_blank_declaration()
    check("retracting it clears the declaration",
          blank_declaration.declared_blank(part) is None)

    form.cbMaterial.setCurrentIndex(0)
    pump()
    check("with a model, a process and a material the run button is live",
          form.pbRunAnalysis.isEnabled())

    panel.reject()
    pump()


def verify_edge_findings(doc):
    """A finding that names an edge has to light that edge, not its faces.

    The whole edge half of the bridge is unreachable from a machining
    analysis -- every machining rule speaks about faces. A moulding
    analysis is the only thing in the workbench that reports a corner as a
    corner, so it is the only thing that proves `LineColorArray` is written
    at all, that the same one-based-to-zero-based conversion happens on the
    edge path, and that a rule which names both faces and edges paints
    both.
    """
    section("Findings that point at edges")
    from pathlib import Path

    from freecad.DFM.app.history import HistoryManager
    from freecad.DFM.gui.results.bridge import DFMViewProvider
    from freecad.DFM.gui.results.models import DFMReportModel
    from freecad.DFM.gui.results.presenter import TaskResultsPresenter
    from freecad.DFM.gui.task_results import TaskResults

    part = build_moulded_part(doc)
    process, results = analyse_moulding(part)
    edge_count = len(part.Shape.Edges)
    face_count = len(part.Shape.Faces)
    say("  %d faces, %d edges, %d findings"
        % (face_count, edge_count, len(results)))

    named_edges = [r for r in results if any(ref.type == "Edge" for ref in r.refs)]
    check("the moulding rules report corners as corners", bool(named_edges),
          " (%d of %d findings name an edge)" % (len(named_edges), len(results)))
    if not named_edges:
        return

    check("every edge index is in range",
          all(0 <= ref.index < edge_count
              for r in results for ref in r.refs if ref.type == "Edge"))
    check("and every face index too",
          all(0 <= ref.index < face_count
              for r in results for ref in r.refs if ref.type == "Face"))

    view = TaskResults()
    model_obj = DFMReportModel(results, process, "Default")
    bridge = DFMViewProvider(part)
    presenter = TaskResultsPresenter(
        view, model_obj, bridge, HistoryManager(Path(scratch)),
        doc_name=doc.Name, shape_name=part.Label, machining_context=None,
    )
    pump()

    check("the panel opens on a process with no machining context",
          view.form.leProcess.text() == process.name,
          " (%r)" % view.form.leProcess.text())
    check("and the features tab stays quiet rather than raising",
          view.features_tab is not None)

    finding = named_edges[0]
    item = next(
        (i for i in walk(view.model)
         if kind_of(i) == "finding" and payload_of(i) is finding),
        None,
    )
    check("the finding has a row", item is not None)
    if item is not None:
        select(view, item)
        doc.recompute()
        got = painted_edges(doc, edge_count)
        want = edge_refs_of([finding])
        check("selecting it lights exactly the edges it names", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))
        faces_lit = painted_faces(doc, face_count)
        face_want = face_refs_of([finding])
        check("and exactly the faces, which may be none",
              faces_lit == face_want,
              "" if faces_lit == face_want
              else " (painted %s, wanted %s)" % (faces_lit, face_want))

    # A rule row covering many edge findings at once.
    busiest = max(
        (i for i in walk(view.model)
         if kind_of(i) == "rule" and payload_of(i)
         and any(ref.type == "Edge"
                 for f in payload_of(i) for ref in f.refs)),
        key=lambda i: len(payload_of(i)),
        default=None,
    )
    if busiest is not None:
        findings = payload_of(busiest)
        select(view, busiest)
        doc.recompute()
        got = painted_edges(doc, edge_count)
        want = edge_refs_of(findings)
        say("  %r covers %d findings on %d edges"
            % (label_of(busiest), len(findings), len(want)))
        check("a rule row lights every edge its findings name", got == want,
              "" if got == want else " (painted %s, wanted %s)" % (got, want))

        overlay = doc.getObject("DFM_Highlight_Overlay")
        check("the overlay draws its edges wide enough to see",
              overlay is not None and overlay.ViewObject.LineWidth > 0,
              " (width %s)"
              % (overlay.ViewObject.LineWidth if overlay else None))

    # Both severities are present here, and they must not be painted alike.
    from freecad.DFM.gui.results.visuals import severity_color
    from freecad.DFM.core.models import Severity

    check("an error and a warning are told apart by colour",
          severity_color(Severity.ERROR) != severity_color(Severity.WARNING))

    presenter.handle_cleanup()
    doc.recompute()
    pump()
    check("closing gives the moulded part back too",
          part.ViewObject.Visibility
          and doc.getObject("DFM_Highlight_Overlay") is None)
    try:
        import FreeCADGui

        FreeCADGui.Control.closeDialog()
    except Exception:
        pass
    pump()
    doc.removeObject(part.Name)
    doc.recompute()


# ---------------------------------------------------------------------------
# The preference pages
# ---------------------------------------------------------------------------


def verify_preference_pages():
    """Every page has to open, save what it was shown, and read it back.

    A preference page that loses a setting is worse than one that has none:
    the machinist sets the smallest end mill on the shelf, closes the
    dialog, and the analysis quietly keeps using the default.
    """
    section("Preference pages")
    import FreeCAD

    params = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Mod/DFM")
    saved = {key: value for _, key, value in (params.GetContents() or ())}

    def restore():
        for _, key, _value in list(params.GetContents() or ()):
            if key not in saved:
                params.RemContents(key) if hasattr(params, "RemContents") else None
        for key, value in saved.items():
            if isinstance(value, bool):
                params.SetBool(key, value)
            elif isinstance(value, int):
                params.SetInt(key, value)
            elif isinstance(value, float):
                params.SetFloat(key, value)
            else:
                params.SetString(key, str(value))

    try:
        from freecad.DFM.gui.machining_preferences import MachiningPreferences

        page = MachiningPreferences()
        check("the machining page builds", page.form is not None)
        page.loadSettings()
        check("the axes list is populated", page.machine_mode.count() == 3,
              " (%d entries)" % page.machine_mode.count())
        check("the stock list offers a not-declared state",
              page.blank_form.itemData(0) == "",
              " (%r)" % page.blank_form.itemData(0))

        page.machine_mode.setCurrentIndex(page.machine_mode.findData("5axis"))
        page.blank_form.setCurrentIndex(page.blank_form.findData("as_cast"))
        page.precision_mode.setChecked(True)
        page.saveSettings()

        again = MachiningPreferences()
        again.loadSettings()
        check("the machine mode survives a save and reload",
              again.machine_mode.currentData() == "5axis",
              " (%r)" % again.machine_mode.currentData())
        check("so does the blank form",
              again.blank_form.currentData() == "as_cast",
              " (%r)" % again.blank_form.currentData())
        check("and the precision switch",
              again.precision_mode.isChecked())

        # And the settings have to actually reach the analysis config.
        from freecad.DFM.core.machining.config import MachiningConfig

        prefs = {key: value for _, key, value in (params.GetContents() or ())}
        config = MachiningConfig.from_preferences(prefs)
        check("five-axis reaches the analysis config",
              config.machine_mode == "5axis", " (%r)" % config.machine_mode)
        check("so does the declared blank",
              config.blank_form == "as_cast", " (%r)" % config.blank_form)
    except Exception as exc:
        check("the machining preference page works", False,
              ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())

    try:
        from freecad.DFM.gui.machining_thresholds import MachiningThresholds

        page = MachiningThresholds()
        check("the limits page builds", page.form is not None)
        page.loadSettings()
        pages = page.stack.count()
        from PySide6 import QtWidgets

        count = sum(
            len(page.stack.widget(i).findChildren(QtWidgets.QDoubleSpinBox))
            + len(page.stack.widget(i).findChildren(QtWidgets.QSpinBox))
            for i in range(pages)
        )
        say("  %d threshold editors across %d pages" % (count, pages))
        check("the limits are grouped into pages the chooser offers",
              page.chooser.count() == pages and pages > 1,
              " (%d chooser entries, %d pages)" % (page.chooser.count(), pages))
        check("it exposes a substantial number of limits", count >= 100,
              " (%d)" % count)
        check("switching pages does not raise",
              all(page.stack.setCurrentIndex(i) is None for i in range(pages)))
        guard("the limits page saves without raising", page.saveSettings)
    except Exception as exc:
        check("the limits preference page works", False,
              ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())

    try:
        from freecad.DFM.gui.tool_library import MachiningTooling

        page = MachiningTooling()
        check("the tooling page builds", page.form is not None)
        page.loadSettings()
        guard("the tooling page saves without raising", page.saveSettings)
    except Exception as exc:
        check("the tooling preference page works", False,
              ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())

    restore()
    say("  preferences put back as they were")


def verify_thread_confirmation(doc, part, context):
    """The dialog that asks which tap-drill-sized bores are actually tapped."""
    section("Thread confirmation dialog")

    try:
        from freecad.DFM.core.machining import thread_sources
    except Exception as exc:
        check("the thread source module imports", False,
              ": %s: %s" % (type(exc).__name__, exc))
        return

    candidates = thread_sources.candidates_for(
        context.recognition.features,
        context.graph,
        unit_system=context.config.unit_system,
    )
    say("  %d bore(s) put up for confirmation" % len(candidates))
    check("asking a part for its candidates does not raise", True)

    store = thread_sources.load_confirmations(doc)
    check("a document with no answers reads as empty", len(store) == 0,
          " (%d records)" % len(store))

    if candidates:
        from freecad.DFM.gui.thread_confirm import ThreadConfirmDialog

        dialog = None
        try:
            dialog = ThreadConfirmDialog(candidates, target_object=part)
            check("the confirmation dialog builds", True)
        except Exception as exc:
            check("the confirmation dialog builds", False,
                  ": %s: %s" % (type(exc).__name__, exc))
            say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
        if dialog is not None:
            check("it shows one row per candidate bore",
                  dialog.table.rowCount() == len(candidates),
                  " (%d rows, %d candidates)"
                  % (dialog.table.rowCount(), len(candidates)))
            check("the columns are headed",
                  dialog.table.columnCount() >= 3,
                  " (%d columns)" % dialog.table.columnCount())
            guard("it can be closed again", dialog.reject)
    else:
        say("  (this part has no tap-drill-sized bores; dialog not exercised)")


def verify_process_isolation(part):
    """One analysis answers one question, and no other.

    The workbench holds four process rule sets, and a part is a part: the
    moulding rules would happily report every wall of a machined plate as
    undrafted, and the machining rules would report every corner of a
    moulded cup as needing a cutter radius. Both readings would be nonsense
    and each would bury the other.

    What stops that is that the machinist says which question they are
    asking -- the setup panel makes them choose a process before the Run
    button lights up -- and the runner then runs that process's rules and
    nothing else. This checks it holds, from both directions, on the two
    parts most likely to expose a leak.
    """
    section("One process at a time")
    from freecad.DFM.app.analysis_runner import AnalysisRunner
    from freecad.DFM.core.registries import ProcessRegistry

    registry = ProcessRegistry.get_instance()
    milling = registry.get_process_by_name("CNC Milling")
    moulding = registry.get_process_by_name("Plastic Injection Molding")
    check("both process definitions are registered",
          milling is not None and moulding is not None)
    if milling is None or moulding is None:
        return

    milling_rules = set(milling.active_rules)
    moulding_rules = set(moulding.active_rules)
    overlap = milling_rules & moulding_rules
    check("the machining and moulding rule sets do not overlap at all",
          not overlap, " (%s)" % sorted(r.label for r in overlap))

    # And the runner honours the choice rather than running everything it
    # has a check class for.
    results = AnalysisRunner().run_analysis(
        process_name="CNC Milling",
        material_name="Default",
        shape=part.Shape,
        target_object=part,
    )
    fired = {r.rule_id for r in results}
    check("a milling run reports only milling rules",
          fired <= milling_rules,
          " (%s)" % sorted(r.label for r in fired - milling_rules))
    check("and no moulding rule creeps in",
          not (fired & moulding_rules),
          " (%s)" % sorted(r.label for r in fired & moulding_rules))

    moulded = AnalysisRunner().run_analysis(
        process_name="Plastic Injection Molding",
        material_name=(list(moulding.materials)[0] if moulding.materials else "Default"),
        shape=part.Shape,
        target_object=part,
    )
    moulded_fired = {r.rule_id for r in moulded}
    check("a moulding run reports only moulding rules",
          moulded_fired <= moulding_rules,
          " (%s)" % sorted(r.label for r in moulded_fired - moulding_rules))
    check("and no machining rule creeps in",
          not (moulded_fired & milling_rules),
          " (%s)" % sorted(r.label for r in moulded_fired & milling_rules))
    say("  milling %d findings, moulding %d, on the same solid"
        % (len(results), len(moulded)))

    # The sheet and turning sets share only rules that are about a part
    # rather than about a process -- how small a detail is, how long and
    # thin the part is, whether it is marked. Anything else appearing in
    # more than one set is a rule filed under the wrong process.
    sheet = registry.get_process_by_name("Sheet Metal Fabrication")
    if sheet is not None:
        from freecad.DFM.core.rules import Rulebook

        shared = milling_rules & set(sheet.active_rules)
        general = {
            Rulebook.MINIMUM_FEATURE_SIZE,
            Rulebook.PART_ASPECT_RATIO,
            Rulebook.PART_MARKING,
        }
        check("milling and sheet share only part-level rules",
              shared <= general,
              " (%s)" % sorted(r.label for r in shared - general))


# ---------------------------------------------------------------------------
# The whole thing, end to end
# ---------------------------------------------------------------------------


def verify_runner(part):
    """The path a click on Run Analysis actually takes.

    Everything above drives the panel with findings computed by hand. This
    goes through AnalysisRunner itself, which is what reads the preferences,
    resolves the analyzers and caches the machining context the features tab
    then reads.
    """
    section("Analysis runner")
    from freecad.DFM.app.analysis_runner import AnalysisRunner
    from freecad.DFM.core.machining import blank_declaration

    runner = AnalysisRunner()
    steps = []

    results = runner.run_analysis(
        process_name="CNC Milling",
        material_name="Default",
        shape=part.Shape,
        progress_cb=lambda done, total, message="": steps.append(done),
        target_object=part,
    )
    check("the runner returns findings", bool(results),
          " (%d)" % len(results))
    check("it reported progress along the way", bool(steps),
          " (%d callbacks)" % len(steps))
    check("it left the machining context cached for the features tab",
          "MACHINING_ANALYZER" in runner.analyzer_cache)

    check("every finding names the rule it came from",
          all(r.rule_id is not None for r in results))
    check("and carries a severity",
          all(r.severity is not None for r in results))

    # A blank declared on the part has to change what the runner analyses.
    blank_declaration.declare_blank(part, "billet")
    declared = AnalysisRunner()
    declared.run_analysis(
        process_name="CNC Milling",
        material_name="Default",
        shape=part.Shape,
        target_object=part,
    )
    context = list(declared.analyzer_cache["MACHINING_ANALYZER"].values())[0]
    check("a blank declared on the part reaches the analysis",
          context.config.blank_form == "billet",
          " (%r)" % context.config.blank_form)
    blank_declaration.declare_blank(part, "")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run():
    global scratch

    import FreeCAD
    import FreeCADGui

    say("FreeCAD " + ".".join(FreeCAD.Version()[:3]))
    check("the GUI is up", FreeCAD.GuiUp)
    if not FreeCAD.GuiUp:
        say("  (run this under freecad.exe, not freecadcmd)")
        return

    import freecad.DFM  # noqa: F401
    from pathlib import Path

    from freecad.DFM.app.history import HistoryManager
    from freecad.DFM.gui.results.bridge import DFMViewProvider
    from freecad.DFM.gui.results.models import DFMReportModel
    from freecad.DFM.gui.results.presenter import TaskResultsPresenter
    from freecad.DFM.gui.task_results import TaskResults

    scratch = tempfile.mkdtemp(prefix="dfm_ui_")
    say("  scratch: " + scratch)

    doc = FreeCAD.newDocument("dfm_ui")
    part = build_milled_part(doc)
    FreeCADGui.ActiveDocument.ActiveView.viewAxonometric()
    FreeCADGui.SendMsgToActiveView("ViewFit")
    pump()

    section("Analysis")
    process, context, results = analyse(part)
    say("  %d faces, %d findings" % (len(part.Shape.Faces), len(results)))
    say("  features: %s" % context.recognition.counts())
    check("the part analysed to findings worth showing", len(results) >= 2,
          " (%d)" % len(results))
    check("every finding resolved to geometry or said it could not",
          all(hasattr(r, "refs") for r in results))

    # The panel, wired the way task_setup wires it -- with the history
    # manager pointed somewhere disposable rather than the user's own.
    view = TaskResults()
    model_obj = DFMReportModel(results, process, "Default")
    bridge = DFMViewProvider(part)
    presenter = TaskResultsPresenter(
        view,
        model_obj,
        bridge,
        HistoryManager(Path(scratch)),
        doc_name=doc.Name,
        shape_name=part.Label,
        machining_context=context,
    )
    pump()

    section("Panel header")
    check("the panel names the part being analysed",
          view.form.leTarget.text() == part.Label,
          " (%r)" % view.form.leTarget.text())
    check("and the process it was judged against",
          view.form.leProcess.text() == process.name,
          " (%r)" % view.form.leProcess.text())
    check("and the material", view.form.leMaterial.text() == "Default",
          " (%r)" % view.form.leMaterial.text())
    verdict = view.form.leVerdict.text()
    check("and gives a verdict", bool(verdict), " (%r)" % verdict)
    errors = sum(1 for r in results if not r.ignore and r.severity.name == "ERROR")
    warnings = sum(1 for r in results if not r.ignore and r.severity.name == "WARNING")
    expected = "FAILED" if errors else "WARNING" if warnings else "SUCCESS"
    check("the verdict matches what the findings say",
          expected.lower()[:4] in verdict.lower(),
          " (%r for %d errors, %d warnings)" % (verdict, errors, warnings))

    verify_tree(view, model_obj, results)
    verify_selection(view, presenter, doc, part, results)
    verify_ignore(view, presenter, model_obj, doc, part)
    verify_zoom(view, presenter, doc, part)
    verify_export(view, model_obj, part)
    verify_features_tab(view, presenter, context, doc, part)
    verify_overlay_lifecycle(view, presenter, doc, part)

    # The panel is done with; close it before opening the setup one.
    try:
        FreeCADGui.Control.closeDialog()
    except Exception:
        pass
    pump()

    verify_edge_findings(doc)
    verify_setup_panel(doc, part)
    verify_thread_confirmation(doc, part, context)
    verify_preference_pages()
    verify_process_isolation(part)
    verify_runner(part)

    section("Teardown")
    check("the part is left visible and unmodified",
          part.ViewObject.Visibility and part.Shape.isValid())
    check("no overlay is left behind in the document",
          doc.getObject("DFM_Highlight_Overlay") is None)
    leftovers = [o.Name for o in doc.Objects if o.Name.startswith("DFM_")]
    check("and no annotation objects either", not leftovers,
          " (%s)" % leftovers)

    FreeCAD.closeDocument("dfm_ui")


try:
    run()
    say("")
    say("FAILED: " + ", ".join(failures) if failures else "ALL CHECKS PASSED")
except Exception:
    say("")
    say("ERRORED")
    say(traceback.format_exc())
finally:
    if scratch and os.path.isdir(scratch):
        shutil.rmtree(scratch, ignore_errors=True)
    log.close()
    try:
        import FreeCADGui

        FreeCADGui.getMainWindow().close()
    except Exception:
        pass
