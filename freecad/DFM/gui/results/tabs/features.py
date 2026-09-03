# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""What the workbench found on the part, whether or not it complained.

The results tree shows problems. This shows the reading the problems were
derived from: how the part was classified, and every feature recognized on
it. Two quite different people want that.

A machinist wants to know whether the workbench understood the part before
trusting what it says about it. A silent analysis on a part with twenty
holes means one thing if twenty holes were found and something else entirely
if none were -- and the results tree looks identical either way.

An estimator wants the census itself. The feature list is the closest thing
to an operation list the model can produce, and exporting it is often more
useful than the findings.

Selecting a row highlights the faces, so the census is also the fastest way
to see what the workbench thinks a given lump of geometry is.
"""

from typing import Optional

from PySide6 import QtCore, QtWidgets

import FreeCAD as App  # type: ignore

from ....core.machining.census import census_summary, write_census
from ....core.machining.features import FeatureInstance


# Parameters worth putting in the summary column, in the order a machinist
# would say them. Anything else is available on the row itself.
_HEADLINE_PARAMETERS = (
    ("diameter_mm", "dia", "mm"),
    ("major_diameter_mm", "dia", "mm"),
    ("width_mm", "w", "mm"),
    ("min_width_mm", "w", "mm"),
    ("length_mm", "len", "mm"),
    ("depth_mm", "deep", "mm"),
    ("height_mm", "high", "mm"),
    ("thickness_mm", "thk", "mm"),
    ("radius_mm", "r", "mm"),
    ("corner_radius_mm", "corner r", "mm"),
    ("thread_pitch_mm", "pitch", "mm"),
    ("draft_angle_deg", "draft", "deg"),
    ("chamfer_angle_deg", "angle", "deg"),
)

_COLUMNS = ("Feature", "Size", "Faces", "Detail")


class FeaturesTab(QtWidgets.QWidget):
    """The feature census for one analysed part."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._features: list[FeatureInstance] = []
        self._process_line = ""
        self.on_faces_selected = None

        layout = QtWidgets.QVBoxLayout(self)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(list(_COLUMNS))
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tree.itemSelectionChanged.connect(self._handle_selection)
        layout.addWidget(self.tree)

        buttons = QtWidgets.QHBoxLayout()
        self.export_button = QtWidgets.QPushButton("Export census...")
        self.export_button.setToolTip(
            "Write the feature list to CSV -- the closest thing to an\n"
            "operation list the model can produce."
        )
        self.export_button.clicked.connect(self._handle_export)
        buttons.addStretch()
        buttons.addWidget(self.export_button)
        layout.addLayout(buttons)

    # -- population ---------------------------------------------------------

    def set_context(self, context) -> None:
        """Fill the tab from a machining analysis, or empty it.

        Called with None for a part that was not analysed by the machining
        rules at all -- a sheet or a 3D-printed part -- in which case saying
        so plainly is better than showing an empty tree.
        """
        self.tree.clear()
        if context is None:
            self._features = []
            self._process_line = ""
            self.summary.setText(
                "No machining analysis was run on this part, so there is no "
                "feature census to show."
            )
            self.export_button.setEnabled(False)
            return

        self._features = list(context.recognition.features)
        self._process_line = _describe_process(context)
        self.summary.setText(self._summary_text())
        self.export_button.setEnabled(bool(self._features))
        self._build_tree()

    def _summary_text(self) -> str:
        if not self._features:
            return (
                f"{self._process_line} No features were recognized on this "
                "part. If that is a surprise, the geometry may not be a "
                "single solid, or it may be shaped in a way the recognizers "
                "do not read -- the findings will be thin either way."
            )
        return (
            f"{self._process_line} {len(self._features)} features recognized. "
            "Selecting a row highlights it on the model."
        )

    def _build_tree(self) -> None:
        by_type: dict[str, list[FeatureInstance]] = {}
        for feature in self._features:
            by_type.setdefault(feature.type, []).append(feature)

        # Commonest kinds first, so the part's character is visible at a
        # glance rather than in alphabetical order.
        ordered = sorted(by_type.items(), key=lambda item: (-len(item[1]), item[0]))

        for feature_type, features in ordered:
            parent = QtWidgets.QTreeWidgetItem(self.tree)
            parent.setText(0, f"{_readable(feature_type)}  ({len(features)})")
            parent.setFirstColumnSpanned(False)
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            parent.setData(0, QtCore.Qt.ItemDataRole.UserRole, _faces_of(features))

            for feature in features:
                child = QtWidgets.QTreeWidgetItem(parent)
                child.setText(0, feature.instance_id)
                child.setText(1, _headline(feature))
                child.setText(2, str(len(feature.faces)))
                child.setText(3, _detail(feature))
                child.setData(
                    0, QtCore.Qt.ItemDataRole.UserRole, list(feature.faces)
                )

        self.tree.expandAll()
        for column in range(len(_COLUMNS)):
            self.tree.resizeColumnToContents(column)

    # -- interaction --------------------------------------------------------

    def _handle_selection(self) -> None:
        if self.on_faces_selected is None:
            return
        faces: list[int] = []
        for item in self.tree.selectedItems():
            stored = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if stored:
                faces.extend(stored)
        self.on_faces_selected(sorted(set(faces)))

    def _handle_export(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export feature census", "features.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            write_census(path, self._features)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self, "Export failed", f"Could not write {path}:\n{exc}"
            )
            return
        App.Console.PrintMessage(f"DFM: feature census written to {path}\n")


# =============================================================================
# Formatting
# =============================================================================


def _readable(feature_type: str) -> str:
    return feature_type.replace("_", " ").title()


def _faces_of(features) -> list[int]:
    faces: set[int] = set()
    for feature in features:
        faces.update(feature.faces)
    return sorted(faces)


def _headline(feature: FeatureInstance) -> str:
    """The one or two dimensions that identify a feature at a glance."""
    parts = []
    seen: set[str] = set()
    for key, label, unit in _HEADLINE_PARAMETERS:
        value = feature.number(key)
        if value is None or value <= 0.0 or label in seen:
            continue
        seen.add(label)
        parts.append(f"{label} {value:.2f} {unit}")
        if len(parts) == 2:
            break
    return "  ".join(parts)


def _detail(feature: FeatureInstance) -> str:
    """The qualitative facts about a feature, as short phrases."""
    parts = []
    designation = feature.param("thread_designation")
    if designation:
        parts.append(str(designation))
    if feature.param("is_through"):
        parts.append("through")
    if feature.param("flat_bottom"):
        parts.append("flat bottom")
    if feature.param("is_internal"):
        parts.append("internal")
    if feature.param("merged_across_void"):
        parts.append("crosses a void")
    if feature.param("terminates_in_cavity"):
        parts.append("runs into a cavity")
    shape = feature.param("gland_shape")
    if shape:
        parts.append(str(shape))
    evidence = feature.param("thread_evidence")
    if evidence:
        parts.append(str(evidence).replace("_", " "))
    return ", ".join(parts)


def _describe_process(context) -> str:
    process = context.part_process
    name = process.type.value.replace("_", "-").lower()
    if process.type.name == "SHEET_METAL" and process.sheet_thickness_mm:
        return f"Read as sheet metal, {process.sheet_thickness_mm:.2f} mm gauge."
    if process.blank:
        return f"Read as a {name} part from {process.blank.replace('_', ' ')}."
    return f"Read as a {name} part."
