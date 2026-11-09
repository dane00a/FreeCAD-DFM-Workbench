# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Drive the fixture-baseline panel and check what it does.

The comparison underneath is covered by the unit suite, on hand-written
counts. What is not coverable there is everything this panel is actually
for: that running fills the table in, that a drifted fixture says what
moved, that Approve writes a baseline the next run agrees with, and that
Inspect puts the part in the viewport with its findings painted on it.

None of that is provable headless. ``freecadcmd`` has no GUI -- a
``ViewObject`` is None, ``Gui.Control`` refuses a dialog, and a QDialog has
no application to live in -- so every one of those paths would be skipped
rather than exercised, and a panel that raises on the first click would
pass a headless run untouched. So this file runs under the real thing, like
``verify_ui.py`` next to it and for the same reasons.

Run with the real GUI, not freecadcmd::

    "C:/Program Files/FreeCAD 1.1/bin/freecad.exe" tests/freecad/verify_baseline_panel.py

Writes ``verify_baseline_panel.out`` beside this file, then closes FreeCAD.
The committed baselines are never touched: the panel is pointed at a
scratch file in a temporary directory, which is removed at the end, and the
developer preference it is gated by is put back the way it was found.
"""

import os
import shutil
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(_HERE, "verify_baseline_panel.out")

log = open(REPORT, "w", encoding="utf-8")
failures = []
scratch = None

# Two fixtures, picked to be quick and to be different from each other: a
# plain milled block with a hole in it, and a sheet part that goes through
# the brake rules instead. Running the whole corpus here would take minutes
# and prove nothing this pair does not.
FIXTURES = ("deep_hole", "sm_bend_angle_135")


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
    """Run something that touches the UI and report a raise as a failure."""
    try:
        result = fn()
        check(description, True)
        return result
    except Exception as exc:
        check(description, False, ": %s: %s" % (type(exc).__name__, exc))
        say("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
        return None


def pump():
    from PySide6 import QtWidgets

    QtWidgets.QApplication.processEvents()


# ---------------------------------------------------------------------------


def rows_by_name(panel):
    return {
        panel.tree.topLevelItem(i).text(0): panel.tree.topLevelItem(i)
        for i in range(panel.tree.topLevelItemCount())
    }


def select(panel, names):
    panel.tree.clearSelection()
    table = rows_by_name(panel)
    for name in names:
        table[name].setSelected(True)
    pump()


def run():
    global scratch

    import FreeCAD
    import FreeCADGui

    say("FreeCAD " + FreeCAD.Version()[0] + "." + FreeCAD.Version()[1] + "." + FreeCAD.Version()[2])
    check("the GUI is up", FreeCAD.GuiUp)
    if not FreeCAD.GuiUp:
        return

    scratch = tempfile.mkdtemp(prefix="dfm_baselines_")
    say("  scratch: " + scratch)
    baseline_path = os.path.join(scratch, "dfm_baseline.json")

    verify_preference()
    panel = verify_construction(baseline_path)
    if panel is None:
        return
    verify_running(panel)
    verify_approval(panel, baseline_path)
    verify_drift_is_seen(panel, baseline_path)
    verify_filter(panel)
    verify_inspect(panel)
    verify_teardown(panel)


def verify_preference():
    """The gate, both halves of it."""
    import FreeCAD

    from freecad.DFM.gui.preferences import (
        PREF_FIXTURE_BASELINES,
        fixture_baselines_enabled,
    )
    from freecad.DFM.gui.baseline_panel import BaselinePanelCommand

    section("The developer preference")
    params = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Mod/DFM")
    was = params.GetBool(PREF_FIXTURE_BASELINES, False)

    params.RemBool(PREF_FIXTURE_BASELINES)
    check("it is off unless somebody turns it on", not fixture_baselines_enabled())
    check("and the command is inactive while it is", not BaselinePanelCommand().IsActive())

    params.SetBool(PREF_FIXTURE_BASELINES, True)
    check("turning it on enables the command", BaselinePanelCommand().IsActive())

    # The other half of the gate: the workbench builds its toolbar once, at
    # activation, and reads the preference then. Activating it here with the
    # preference on is the only way to see whether the command actually
    # reaches the toolbar rather than merely being registered.
    import FreeCADGui

    check(
        "the command is registered whether or not it is offered",
        "DFM_FixtureBaselines" in FreeCADGui.listCommands(),
    )
    guard(
        "the DFM workbench activates",
        lambda: FreeCADGui.activateWorkbench("DFMWorkbench"),
    )
    pump()
    offered = getattr(FreeCADGui.getWorkbench("DFMWorkbench"), "list", [])
    check(
        "and offers the baseline command while the preference is on",
        "DFM_FixtureBaselines" in offered,
        " (%s)" % offered,
    )

    params.SetBool(PREF_FIXTURE_BASELINES, was)
    say("  (restored to %r)" % was)

    from freecad.DFM.gui.preferences import DFMPreferencesGeneral

    page = DFMPreferencesGeneral()
    page.loadSettings()
    check(
        "the preference page carries a checkbox bound to the same key",
        page.fixture_baselines.property("prefEntry") == PREF_FIXTURE_BASELINES,
    )


def verify_construction(baseline_path):
    from freecad.DFM.core.baselines.corpus import corpus_available
    from freecad.DFM.gui.baseline_panel import BaselinePanel

    section("Opening the panel")
    check("the fixture recipes are reachable from the addon", corpus_available())

    panel = guard("the panel builds", lambda: BaselinePanel(None, baseline_path))
    if panel is None:
        return None
    panel.show()
    pump()

    table = rows_by_name(panel)
    check("every registered fixture has a row", len(table) > 200, " (%d)" % len(table))
    for name in FIXTURES:
        check("  %s is one of them" % name, name in table)
    check(
        "nothing is approved yet against a scratch baseline file",
        all(panel.rows[n].status == "unapproved" for n in table),
    )
    return panel


def verify_running(panel):
    section("Running a fixture")
    select(panel, FIXTURES)
    guard("Run selected completes", panel._run_selected)

    table = rows_by_name(panel)
    for name in FIXTURES:
        row = panel.rows[name]
        check("  %s ran" % name, row.error is None, " (%s)" % row.error)
        check("  %s produced an observation" % name, row.observation is not None)
        if row.observation is None:
            continue
        say(
            "      %d findings, %d features, classified %s, under %s"
            % (
                len(row.observation.findings),
                len(row.observation.features),
                row.observation.part_process_type,
                row.observation.process,
            )
        )
        item = table[name]
        check(
            "  %s shows its counts in the table" % name,
            item.text(5) == str(len(row.observation.features)),
            " (features column %r)" % item.text(5),
        )

    sheet = panel.rows[FIXTURES[1]]
    check(
        "a sheet fixture is run under the brake rules, not the mill",
        sheet.observation is not None
        and sheet.observation.process == "Sheet Metal Fabrication",
    )


def verify_approval(panel, baseline_path):
    import json

    from freecad.DFM.core.baselines.store import BaselineStore

    section("Approving")
    select(panel, FIXTURES)
    guard("Approve writes the baselines", panel._approve)

    check("the file was written", os.path.exists(baseline_path))
    if not os.path.exists(baseline_path):
        return
    with open(baseline_path, encoding="utf-8") as handle:
        written = json.load(handle)
    check("both fixtures are in it", sorted(written) == sorted(FIXTURES), " (%s)" % sorted(written))

    for name in FIXTURES:
        row = panel.rows[name]
        check("  %s now reads as matching" % name, row.status == "ok", " (%s)" % row.status)

    stored = BaselineStore(baseline_path)
    spec = stored.get(FIXTURES[0])
    check(
        "the baseline records which process it was taken under",
        spec is not None and spec.process != "",
        " (%r)" % (spec.process if spec else None),
    )
    check(
        "it tracks PMI and feature classification even when both are empty",
        spec is not None and spec.pmi is not None and spec.features is not None,
    )
    blob = json.dumps(written)
    check(
        "and stores no dimension or face id anywhere in it",
        "_mm" not in blob and "face_id" not in blob,
    )


def verify_drift_is_seen(panel, baseline_path):
    """Move a baseline by hand, re-run, and see whether the panel notices.

    Editing the approved answer rather than the rules is the only way to
    produce drift on demand, and it exercises exactly the path a changed
    recognizer would: the counts no longer agree, and the panel has to say
    which ones and by how much.
    """
    section("Seeing drift")
    name = FIXTURES[0]
    spec = panel.rows[name].spec
    before = spec.severity_warn or 0
    spec.severity_warn = before + 3
    spec.by_rule["A_RULE_THAT_NEVER_FIRES"] = 1
    panel.store.approve(spec)
    panel.store.save()

    select(panel, [name])
    guard("re-running against the moved baseline", panel._run_selected)

    row = panel.rows[name]
    check("the fixture reads as drifted", row.status == "drifted", " (%s)" % row.status)
    lines = row.drift.mismatches if row.drift else []
    for line in lines:
        say("      " + line)
    check(
        "the severity change is named with its delta",
        any(line.startswith("severity.warn: expected %d" % (before + 3)) for line in lines),
    )
    check(
        "so is the rule that was supposed to fire and did not",
        any("by_rule.A_RULE_THAT_NEVER_FIRES" in line for line in lines),
    )

    table = rows_by_name(panel)
    check(
        "the table's Changed column counts them",
        table[name].text(6) == str(len(lines)),
        " (%r for %d)" % (table[name].text(6), len(lines)),
    )
    check(
        "and the detail pane spells them out",
        all(line in panel.detail.toHtml() for line in lines),
    )

    guard("approving the drift clears it", panel._approve)
    check("the fixture matches again", panel.rows[name].status == "ok")


def verify_filter(panel):
    section("Filtering")
    panel.filter.setCurrentText("Approved")
    pump()
    table = rows_by_name(panel)
    shown = [n for n, item in table.items() if not item.isHidden()]
    check(
        "showing approved hides everything that has never been run",
        set(shown) == set(FIXTURES),
        " (%d shown)" % len(shown),
    )
    panel.filter.setCurrentText("All")
    pump()
    hidden = [n for n, item in rows_by_name(panel).items() if item.isHidden()]
    check("showing all puts them back", not hidden)


def verify_inspect(panel):
    """The part in the viewport, with the findings on it.

    This is the reason the panel exists rather than only the command line,
    so it gets checked all the way down to a face actually changing colour.
    """
    import FreeCAD
    import FreeCADGui

    from freecad.DFM.core.models import Severity
    from freecad.DFM.gui.baseline_panel import SCRATCH_DOCUMENT

    section("Inspecting in 3D")
    name = FIXTURES[0]
    select(panel, [name])
    guard("Inspect opens the results panel", panel._inspect)
    pump()

    check("a scratch document was made", SCRATCH_DOCUMENT in FreeCAD.listDocuments())
    document = FreeCAD.getDocument(SCRATCH_DOCUMENT)
    # The highlight overlay is a Part::Feature too, and the results panel may
    # already have made one by the time this runs. The fixture is whatever is
    # left once the workbench's own scaffolding is set aside.
    parts = [
        o
        for o in document.Objects
        if o.isDerivedFrom("Part::Feature") and not o.Name.startswith("DFM_")
    ]
    check("the fixture is in it as a solid", len(parts) == 1 and parts[0].Shape.isValid(),
          " (%s)" % [o.Name for o in document.Objects])
    if parts:
        check(
            "  named after the fixture",
            parts[0].Label.startswith(name),
            " (%r)" % parts[0].Label,
        )
        check("  and the shape survived the round trip through BRep", parts[0].Shape.Solids)

    presenter = panel.results_presenter
    check("a results presenter was created", presenter is not None)
    if presenter is None:
        return

    view = presenter.view
    check(
        "the panel names the fixture as the part under analysis",
        view.form.leTarget.text() == parts[0].Label,
        " (%r)" % view.form.leTarget.text(),
    )
    check(
        "and the process the baseline recorded",
        view.form.leProcess.text() == panel.rows[name].run.process,
        " (%r)" % view.form.leProcess.text(),
    )
    check("the results tree filled in", view.model.rowCount() > 0)

    # The census drives the viewport the same way it does after an ordinary
    # analysis: pick a feature, and its faces light up.
    context = panel.rows[name].run.context
    features = list(context.recognition.features) if context else []
    check("the feature census has something in it", bool(features))
    if features:
        guard(
            "selecting a feature highlights its faces",
            lambda: presenter.handle_feature_faces(list(features[0].faces)),
        )
        pump()
        overlay = document.getObject("DFM_Highlight_Overlay")
        check("an overlay appeared in the document", overlay is not None)

    guard("the results panel closes", FreeCADGui.Control.closeDialog)
    pump()


def verify_teardown(panel):
    import FreeCAD

    from freecad.DFM.gui.baseline_panel import SCRATCH_DOCUMENT

    section("Teardown")
    panel.close()
    pump()
    check("the panel closes without raising", not panel.isVisible())

    if SCRATCH_DOCUMENT in FreeCAD.listDocuments():
        FreeCAD.closeDocument(SCRATCH_DOCUMENT)
    check("the scratch document is gone", SCRATCH_DOCUMENT not in FreeCAD.listDocuments())

    from freecad.DFM.core.baselines.store import DEFAULT_BASELINE_PATH

    check(
        "the committed baseline file was never opened by this run",
        panel.store.path != DEFAULT_BASELINE_PATH,
    )


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
