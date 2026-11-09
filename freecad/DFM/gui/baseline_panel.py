# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Run the fixture corpus, see what moved, and approve it if it should have.

The command line answers the same question and is what a commit gets gated
on. This exists because of what a drift report cannot tell you. Forty
fixtures going from three findings to two is either a rule that stopped
crying wolf or a recognizer that stopped recognizing, and the difference is
not in the numbers -- it is in the part. So the row that says what changed
also loads the part it changed on, into the 3D view, with the findings in
the ordinary results panel and the faces they name lit up. Deciding whether
a change is right has to be possible by looking at it.

Approving writes the counts as the new expected answer, which is the other
half: a drift someone has looked at and accepted stops being noise, and the
next run is clean again. That only works if approving is easy enough to do
row by row rather than in one sweep at the end, which is why it is a button
on a row and not a command-line flag over the whole corpus.

Behind a developer preference, and gated a second time by whether the
fixture recipes are installed at all. Neither of those is about safety
exactly; it is that this panel is meaningless to anyone who is not editing
the rules, and a workbench that offers meaningless things gets trusted less
for it.
"""

from pathlib import Path
from typing import Any, Optional

from PySide6 import QtCore, QtGui, QtWidgets

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore

from ..app.history import HistoryManager
from ..core.baselines.corpus import (
    corpus_available,
    load_registry,
    missing_step_fixtures,
)
from ..core.baselines.harness import (
    default_process_for,
    load_process,
    observe_shape,
    process_names,
)
from ..core.baselines.spec import BaselineSpec, check_drift, spec_from_observation
from ..core.baselines.store import BaselineStore
from ..core.models import Severity
from ..core.utils.conversion import ocp_to_freecad
from ..gui import DFM_rc  # noqa: F401
from ..gui.preferences import fixture_baselines_enabled
from ..gui.results.bridge import DFMViewProvider
from ..gui.results.models import DFMReportModel
from ..gui.results.presenter import TaskResultsPresenter
from ..gui.results.visuals import severity_color
from ..gui.task_results import TaskResults


#: The document the panel builds fixtures into. One document, reused, so a
#: developer working through a drifted run does not end up with forty of
#: them and no idea which is which.
SCRATCH_DOCUMENT = "DFM_Fixtures"

_COLUMNS = ("Fixture", "Status", "Process", "Error", "Warn", "Features", "Changed")

# Status colours, matched to the severity palette the results tree already
# uses, so red means the same thing in both places.
_OK_COLOUR = QtGui.QColor(severity_color(Severity.SUCCESS))
_DRIFT_COLOUR = QtGui.QColor(severity_color(Severity.ERROR))
_UNAPPROVED_COLOUR = QtGui.QColor(severity_color(Severity.WARNING))


def _why_it_cannot_run(name: str) -> str:
    """Why a fixture in the table has no shape behind it.

    The STEP fixtures are gitignored, so a working tree that never had them
    copied in is the common case rather than the broken one, and telling
    someone their baseline is stale when the file is merely absent sends
    them looking for a recipe that was never there.
    """
    if name in missing_step_fixtures():
        return (
            "The STEP file for this fixture is not installed, so it cannot "
            "be run here. Its baseline is still recorded."
        )
    return "No builder registered -- a stale baseline?"


class FixtureRow:
    """One fixture, and however much is known about it so far.

    A row starts knowing only whether it has a baseline. Running fills in
    the observation and the drift; approving replaces the baseline from the
    observation. Keeping the run on the row rather than in a parallel
    dictionary is what lets Approve and Inspect work on whatever was last
    run without re-running it -- recognizing features on a big fixture is
    seconds, and doing it again to look at the same answer is seconds
    wasted every time.
    """

    def __init__(self, name: str, spec: Optional[BaselineSpec]):
        self.name = name
        self.spec = spec
        self.observation: Any = None
        self.run: Any = None
        self.drift: Any = None
        self.error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.drift is not None:
            return self.drift.status
        return "approved" if self.spec is not None else "unapproved"

    @property
    def process(self) -> str:
        if self.spec is not None and self.spec.process:
            return self.spec.process
        return default_process_for(self.name)


class BaselinePanel(QtWidgets.QDialog):
    """The corpus, its approved answers, and the difference between them."""

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        baseline_path: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("DFM Fixture Baselines")
        self.setWindowIcon(QtGui.QIcon(":/icons/dfm_analysis.svg"))
        self.resize(1000, 640)

        # The path is an argument only so that the panel can be driven
        # against a scratch file during verification. Approving rewrites the
        # committed baselines, and a check that proved the button worked by
        # using it on the real ones would be its own kind of failure.
        self.store = BaselineStore(baseline_path)
        self.registry = load_registry()
        self.rows: dict[str, FixtureRow] = {
            name: FixtureRow(name, self.store.get(name))
            for name in sorted(set(self.registry) | set(self.store.names()))
        }
        self.stop_requested = False
        self.is_running = False
        self.results_presenter: Any = None

        self._build_ui()
        self._fill_tree()
        self._update_summary()

    # -- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        controls = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QComboBox()
        self.filter.addItems(["All", "Drifted", "Unapproved", "Approved", "Failed"])
        self.filter.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(QtWidgets.QLabel("Show:"))
        controls.addWidget(self.filter)

        self.process_override = QtWidgets.QComboBox()
        # The empty first entry is not a null choice; it is the ordinary one.
        # A fixture's process comes from its baseline, or from its name if it
        # has none, and overriding that is how a fixture gets moved onto a
        # different machine -- rare, deliberate, and not the default.
        self.process_override.addItem("As recorded", "")
        for name in process_names():
            self.process_override.addItem(name, name)
        controls.addSpacing(12)
        controls.addWidget(QtWidgets.QLabel("Process:"))
        controls.addWidget(self.process_override)

        controls.addStretch()

        self.run_selected_button = QtWidgets.QPushButton("Run selected")
        self.run_selected_button.clicked.connect(self._run_selected)
        controls.addWidget(self.run_selected_button)

        self.run_all_button = QtWidgets.QPushButton("Run all")
        self.run_all_button.clicked.connect(self._run_all)
        controls.addWidget(self.run_all_button)

        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.clicked.connect(self._request_stop)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(list(_COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tree.itemSelectionChanged.connect(self._show_detail)
        self.tree.itemDoubleClicked.connect(lambda *_: self._inspect())
        splitter.addWidget(self.tree)

        self.detail = QtWidgets.QTextBrowser()
        self.detail.setHtml(
            "Select a fixture to see what its analysis says and how that "
            "differs from the approved answer."
        )
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self.progress = QtWidgets.QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)

        buttons = QtWidgets.QHBoxLayout()
        self.inspect_button = QtWidgets.QPushButton("Inspect in 3D")
        self.inspect_button.setToolTip(
            "Build the selected fixture, put it in the 3D view and open the\n"
            "ordinary results panel on it, so the findings can be looked at\n"
            "rather than counted."
        )
        self.inspect_button.clicked.connect(self._inspect)
        buttons.addWidget(self.inspect_button)

        self.approve_button = QtWidgets.QPushButton("Approve")
        self.approve_button.setToolTip(
            "Record what the rules say about the selected fixtures as the new\n"
            "expected answer. Notes and must-not-fire entries are kept."
        )
        self.approve_button.clicked.connect(self._approve)
        buttons.addWidget(self.approve_button)

        self.forget_button = QtWidgets.QPushButton("Forget baseline")
        self.forget_button.clicked.connect(self._forget)
        buttons.addWidget(self.forget_button)

        buttons.addStretch()
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        if not corpus_available():
            self._disable_running(
                "The fixture recipes are not installed with this copy of the "
                "workbench, so there is nothing to run. This panel is only "
                "useful on a working tree."
            )

    def _disable_running(self, why: str) -> None:
        self.detail.setHtml(f"<p>{why}</p>")
        for widget in (
            self.run_all_button,
            self.run_selected_button,
            self.approve_button,
            self.inspect_button,
        ):
            widget.setEnabled(False)

    # -- the table --------------------------------------------------------

    def _fill_tree(self) -> None:
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for name in sorted(self.rows):
            item = QtWidgets.QTreeWidgetItem(self.tree)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, name)
            self._paint_row(item, self.rows[name])
        self.tree.setSortingEnabled(True)
        for column in range(len(_COLUMNS)):
            self.tree.resizeColumnToContents(column)

    def _paint_row(self, item: QtWidgets.QTreeWidgetItem, row: FixtureRow) -> None:
        """Everything known about one fixture, in one line of the table.

        The counts shown are the *current* ones once a fixture has been run
        and the recorded ones before that, because the question being asked
        changes at that moment: beforehand it is "what did we approve", and
        afterwards it is "what does it say now".
        """
        # Once a fixture has been run the table shows what it says now; until
        # then it shows what was approved. The question being asked changes at
        # that moment, and showing the stale half of it afterwards is how
        # someone approves a number they were not looking at.
        spec = row.spec
        if row.observation is not None:
            spec = spec_from_observation(row.observation)
        item.setText(0, row.name)
        item.setText(1, row.status)
        item.setText(2, row.process)
        item.setText(3, "" if spec is None else str(spec.severity_error or 0))
        item.setText(4, "" if spec is None else str(spec.severity_warn or 0))
        item.setText(5, "" if spec is None else str(spec.feature_count or 0))
        item.setText(
            6, str(len(row.drift.mismatches)) if row.drift and not row.drift.ok else ""
        )

        status = row.status
        if status == "ok":
            item.setIcon(0, QtGui.QIcon(":/icons/dfm_success.svg"))
            item.setForeground(1, _OK_COLOUR)
        elif status in ("drifted", "failed"):
            item.setIcon(0, QtGui.QIcon(":/icons/dfm_error.svg"))
            item.setForeground(1, _DRIFT_COLOUR)
        elif status == "unapproved":
            item.setIcon(0, QtGui.QIcon(":/icons/dfm_warning.svg"))
            item.setForeground(1, _UNAPPROVED_COLOUR)
        else:
            item.setIcon(0, QtGui.QIcon(":/icons/dfm_info.svg"))

        # A fixture with a baseline but no recipe cannot be run or looked at,
        # and saying so on the row beats letting someone click Run and get
        # nothing. There are two ways to arrive here and they want different
        # answers: an imported part whose STEP file was never copied in is
        # ordinary and fixable, while any other name is a recipe that was
        # renamed or removed without its baseline.
        if row.name not in self.registry:
            item.setToolTip(0, _why_it_cannot_run(row.name))

    def _row_for(self, item: QtWidgets.QTreeWidgetItem) -> FixtureRow:
        return self.rows[item.data(0, QtCore.Qt.ItemDataRole.UserRole)]

    def _selected_rows(self) -> list[FixtureRow]:
        return [self._row_for(item) for item in self.tree.selectedItems()]

    def _apply_filter(self) -> None:
        wanted = self.filter.currentText().lower()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            status = self._row_for(item).status
            keep = wanted == "all" or status == wanted or (
                wanted == "approved" and status in ("approved", "ok")
            )
            item.setHidden(not keep)

    def _update_summary(self) -> None:
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row.status] = counts.get(row.status, 0) + 1
        parts = [f"{len(self.rows)} fixtures"]
        for status in ("ok", "drifted", "unapproved", "failed", "approved"):
            if counts.get(status):
                parts.append(f"{counts[status]} {status}")
        self.summary.setText(
            "  --  ".join(parts)
            + f"\nBaselines: {self.store.path}"
        )

    # -- running ----------------------------------------------------------

    def _run_all(self) -> None:
        self._run([self.rows[name] for name in sorted(self.registry)])

    def _run_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self._warn("Nothing selected", "Select one or more fixtures to run.")
            return
        self._run(rows)

    def _request_stop(self) -> None:
        self.stop_requested = True

    def _run(self, rows: list[FixtureRow]) -> None:
        """Build and analyse each fixture in turn, in the interface thread.

        Two hundred parts is minutes, not seconds, so the loop pumps the
        event queue between fixtures: the table fills in as it goes and Stop
        is answered on the next one rather than at the end. Putting the work
        on a thread would be faster to watch and a good deal worse to
        reason about -- the recognizers are not written to be re-entrant,
        and the results panel this hands off to has to be on the interface
        thread anyway.
        """
        if self.is_running:
            return
        self.is_running = True
        self.stop_requested = False
        self.stop_button.setEnabled(True)
        self.run_all_button.setEnabled(False)
        self.run_selected_button.setEnabled(False)
        self.progress.setRange(0, len(rows))
        self.progress.setValue(0)
        self.progress.show()

        override = self.process_override.currentData() or None
        try:
            for index, row in enumerate(rows):
                if self.stop_requested:
                    break
                self.progress.setFormat(f"{row.name}  (%v of %m)")
                self.progress.setValue(index)
                QtWidgets.QApplication.processEvents()
                self._run_one(row, override)
                self._refresh_row(row)
        finally:
            self.progress.hide()
            self.stop_button.setEnabled(False)
            self.run_all_button.setEnabled(True)
            self.run_selected_button.setEnabled(True)
            self.is_running = False
            self._apply_filter()
            self._update_summary()
            self._show_detail()

    def _run_one(self, row: FixtureRow, override: Optional[str]) -> None:
        row.error = None
        row.observation = None
        row.run = None
        row.drift = None

        builder = self.registry.get(row.name)
        if builder is None:
            row.error = _why_it_cannot_run(row.name)
            return
        try:
            shape = builder()
            row.observation, row.run = observe_shape(
                row.name, shape, override or row.process
            )
        except Exception as exc:
            # A fixture that will not build or will not analyse is a real
            # result and belongs in the table, not in a dialog that stops
            # the other two hundred.
            row.error = f"{type(exc).__name__}: {exc}"
            return
        if row.spec is not None:
            row.drift = check_drift(row.spec, row.observation)

    def _refresh_row(self, row: FixtureRow) -> None:
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if self._row_for(item) is row:
                self._paint_row(item, row)
                return

    # -- detail -----------------------------------------------------------

    def _show_detail(self) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            self.detail.setHtml(
                f"<p>{len(rows)} fixtures selected.</p>" if rows else ""
            )
            return
        self.detail.setHtml(_describe(rows[0]))

    # -- approving --------------------------------------------------------

    def _approve(self) -> None:
        """Record the last run of each selected fixture as its baseline.

        Only fixtures that have actually been run this session can be
        approved. Approving one that has not would mean re-running it
        silently and writing whatever came back, which is the one thing a
        button called Approve must not do.
        """
        rows = [r for r in self._selected_rows() if r.observation is not None]
        if not rows:
            self._warn(
                "Nothing to approve",
                "Run the fixtures first. Approving records what the rules "
                "said on this run, so there has to have been one.",
            )
            return

        for row in rows:
            previous = row.spec
            row.spec = spec_from_observation(
                row.observation,
                notes=previous.notes if previous else "",
                must_not_fire=previous.must_not_fire if previous else None,
            )
            self.store.approve(row.spec)
            row.drift = check_drift(row.spec, row.observation)
            self._refresh_row(row)
        self.store.save()
        App.Console.PrintMessage(
            f"DFM: approved {len(rows)} fixture baseline(s) into {self.store.path}\n"
        )
        self._apply_filter()
        self._update_summary()
        self._show_detail()

    def _forget(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for row in rows:
            self.store.forget(row.name)
            row.spec = None
            row.drift = None
            self._refresh_row(row)
        self.store.save()
        self._apply_filter()
        self._update_summary()

    # -- the 3D view ------------------------------------------------------

    def _inspect(self) -> None:
        """Put the selected fixture in the viewport with its findings on it.

        The analysis is not re-run if the row already carries one, so what
        is shown is exactly the run the drift was computed from -- looking
        at a *different* run than the one the report is about would be a
        quiet way to chase the wrong thing for an afternoon.
        """
        rows = self._selected_rows()
        if len(rows) != 1:
            self._warn("Select one fixture", "Inspecting shows one part at a time.")
            return
        row = rows[0]
        if row.observation is None:
            self._run([row])
        if row.run is None:
            self._warn("Nothing to show", row.error or "This fixture did not analyse.")
            return

        try:
            target = self._place_in_document(row)
        except Exception as exc:
            self._warn("Could not build the fixture", str(exc))
            return
        self._open_results(row, target)

    def _place_in_document(self, row: FixtureRow):
        """Build the fixture into the scratch document and show only it.

        The object is replaced rather than added to, so that working down a
        list of drifted fixtures leaves one part on screen instead of forty
        overlapping ones.
        """
        document = App.getDocument(SCRATCH_DOCUMENT) if _has_document() else None
        if document is None:
            document = App.newDocument(SCRATCH_DOCUMENT)
        for existing in list(document.Objects):
            document.removeObject(existing.Name)

        shape = self.registry[row.name]()
        target = document.addObject("Part::Feature", row.name)
        target.Shape = ocp_to_freecad(shape)
        document.recompute()
        Gui.ActiveDocument = Gui.getDocument(document.Name)
        Gui.SendMsgToActiveView("ViewFit")
        return target

    def _open_results(self, row: FixtureRow, target) -> None:
        """Hand the findings to the panel a machinist would see them in.

        Everything below this line is the ordinary results path -- the same
        tree, the same double-click-to-highlight, the same feature census --
        because a fixture's findings are not a different kind of thing from
        a part's and showing them differently would make them harder to
        compare, not easier.
        """
        # Imported here rather than at the top: task_setup registers its own
        # command on import, and the workbench's init already decides when
        # that happens. Reaching for the helper at the moment it is needed
        # keeps this module out of that ordering.
        from ..gui.task_setup import _resolve_geometry_refs

        findings = list(row.run.findings)
        _resolve_geometry_refs(findings)

        process = load_process(row.run.process)
        model = DFMReportModel(
            results=findings, process=process, material=row.run.material
        )
        try:
            self.results_presenter = TaskResultsPresenter(
                view=TaskResults(),
                model=model,
                bridge=DFMViewProvider(target),
                # The ordinary history manager, because the panel's Save
                # button should keep working; what changes is the document
                # name it files under, so a fixture a developer saved is
                # recognisable as one rather than mixed in with real work.
                history_manager=HistoryManager(Path(App.getUserAppDataDir())),
                doc_name=SCRATCH_DOCUMENT,
                shape_name=row.name,
                machining_context=row.run.context,
            )
        except Exception as exc:
            App.Console.PrintError(f"DFM: could not open the results panel: {exc}\n")
            import traceback

            App.Console.PrintError(traceback.format_exc())

    # -- helpers ----------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.information(self, title, text)


def _has_document() -> bool:
    return SCRATCH_DOCUMENT in (App.listDocuments() or {})


# =============================================================================
# Formatting
# =============================================================================


def _describe(row: FixtureRow) -> str:
    """The detail pane for one fixture, as HTML.

    Written as a page rather than a diff because the numbers are only half
    of it: which process the recording was made under, and any note someone
    left on it, are what make a drift line mean something.
    """
    parts = [f"<h3>{row.name}</h3>"]

    if row.error:
        parts.append(
            f"<p style='color:{_DRIFT_COLOUR.name()}'><b>Did not run:</b> "
            f"{row.error}</p>"
        )

    if row.spec is None:
        parts.append(
            "<p>No approved baseline. Run it, look at the findings, and "
            "approve them if they are what this part should say.</p>"
        )
    else:
        parts.append(
            f"<p>Approved under <b>{row.spec.process or 'unrecorded'}</b> / "
            f"{row.spec.material or 'Default'}, classified "
            f"<b>{row.spec.part_process_type or 'UNKNOWN'}</b>.</p>"
        )
        if row.spec.notes:
            parts.append(f"<p><i>{row.spec.notes}</i></p>")
        if row.spec.must_not_fire:
            parts.append(
                "<p>Must not fire: " + ", ".join(sorted(row.spec.must_not_fire)) + "</p>"
            )

    if row.drift is not None and not row.drift.ok:
        parts.append(
            f"<p style='color:{_DRIFT_COLOUR.name()}'><b>"
            f"{len(row.drift.mismatches)} changed since approval:</b></p><ul>"
        )
        parts.extend(f"<li><tt>{line}</tt></li>" for line in row.drift.mismatches)
        parts.append("</ul>")
    elif row.drift is not None:
        parts.append(
            f"<p style='color:{_OK_COLOUR.name()}'>Matches the approved "
            "baseline exactly.</p>"
        )

    if row.observation is not None:
        current = spec_from_observation(row.observation)
        parts.append("<p><b>This run</b></p>")
        parts.append(_table(current))

    return "".join(parts)


def _table(spec: BaselineSpec) -> str:
    """The counts of one run, laid out so two of them can be read side by side."""
    rows = [
        ("features", str(spec.feature_count or 0)),
        (
            "severity",
            f"{spec.severity_error or 0} error, {spec.severity_warn or 0} warn, "
            f"{spec.severity_info or 0} info",
        ),
        ("classified", spec.part_process_type or "UNKNOWN"),
    ]
    for label, source in (("rules", spec.by_rule), ("families", spec.by_category)):
        if source:
            rows.append(
                (label, ", ".join(f"{k} {v}" for k, v in sorted(source.items())))
            )
    if spec.features:
        rows.append(
            (
                "recognized",
                ", ".join(f"{k} {v}" for k, v in sorted(spec.features.items())),
            )
        )
    body = "".join(
        f"<tr><td valign='top'><b>{label}</b>&nbsp;&nbsp;</td>"
        f"<td valign='top'>{value}</td></tr>"
        for label, value in rows
    )
    return f"<table>{body}</table>"


# =============================================================================


class BaselinePanelCommand:
    """The toolbar entry, which is only there when the preference says so."""

    _panel: Optional[BaselinePanel] = None

    def GetResources(self):
        return {
            "Pixmap": ":/icons/dfm_analysis.svg",
            "MenuText": "Fixture Baselines",
            "ToolTip": "Run the fixture corpus and compare it against the "
            "approved answers.",
        }

    def Activated(self):
        # Modeless, and kept on the class rather than made afresh: the panel
        # hands off to the results task dialog, and a dialog that closed
        # itself to do that would lose the run it took minutes to produce.
        if BaselinePanelCommand._panel is None:
            BaselinePanelCommand._panel = BaselinePanel(Gui.getMainWindow())
            BaselinePanelCommand._panel.finished.connect(_forget_panel)
        BaselinePanelCommand._panel.show()
        BaselinePanelCommand._panel.raise_()

    def IsActive(self):
        return fixture_baselines_enabled()


def _forget_panel(*_args) -> None:
    BaselinePanelCommand._panel = None


if App.GuiUp:
    Gui.addCommand("DFM_FixtureBaselines", BaselinePanelCommand())
