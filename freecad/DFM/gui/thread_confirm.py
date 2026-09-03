# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Asking the user which bores are tapped, on a part that cannot say.

An imported STEP has no feature tree. The thread was on the drawing and the
drawing did not come with the file, so the only thing left in the model is a
bore at the tap drill size -- indistinguishable from a dowel hole, a reamed
hole or a pilot hole, all of which are drilled at the same sizes for the same
reason. Guessing would be worse than useless, so the workbench asks.

The question is put once per bore and the answer is kept, both ways round. A
shop that has told the workbench a hole is a dowel hole has told it, and being
asked again next Tuesday would only teach the operator to click past the
dialog without reading it.

Nothing decided here. Which bores are worth raising, how they are described,
and how an answer is filed all live in ``core.machining.thread_sources``,
which has no Qt in it and can be tested without an event loop. This is the
window that displays them.
"""

from __future__ import annotations

from typing import Sequence

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore

from PySide6 import QtCore, QtWidgets

from ..core.machining.thread_sources import ThreadCandidate


# The three answers a row can carry. "Ask again" is the default because a
# dialog full of pre-set verdicts is a dialog that answers itself, and an
# unread rejection is worse than an unanswered question.
_UNANSWERED = "Ask again"
_VERDICTS = ((_UNANSWERED, None), ("Tapped", True), ("Not tapped", False))

_COLUMNS = ("Bore", "Likely thread", "Verdict")


class ThreadConfirmDialog(QtWidgets.QDialog):
    """One row per bore that looks like a tap drill, with a verdict each."""

    def __init__(self, candidates: Sequence[ThreadCandidate], target_object=None, parent=None):
        super().__init__(parent)
        self.candidates = list(candidates)
        self.target_object = target_object

        self.setWindowTitle("Tapped Holes")
        self.setMinimumWidth(520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(self._preamble()))

        self.table = QtWidgets.QTableWidget(len(self.candidates), len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        self.combos: list[QtWidgets.QComboBox] = []
        for row, candidate in enumerate(self.candidates):
            self.table.setItem(row, 0, self._cell(candidate.describe()))
            self.table.setItem(row, 1, self._cell(candidate.designation))
            combo = QtWidgets.QComboBox(self.table)
            for label, value in _VERDICTS:
                combo.addItem(label, value)
            self.table.setCellWidget(row, 2, combo)
            self.combos.append(combo)

        # Selecting a row lights up the hole in the viewport. Nobody can
        # answer this question from a diameter alone -- the answer is in
        # where the hole is and what it is for -- so the part has to be
        # visible while it is asked.
        self.table.currentCellChanged.connect(self._on_row_changed)
        layout.addWidget(self.table)

        bulk = QtWidgets.QHBoxLayout()
        all_tapped = QtWidgets.QPushButton("All tapped", self)
        none_tapped = QtWidgets.QPushButton("None tapped", self)
        all_tapped.setToolTip("A plate of identical tapped holes is the common case")
        all_tapped.clicked.connect(lambda: self._set_all(True))
        none_tapped.clicked.connect(lambda: self._set_all(False))
        bulk.addWidget(all_tapped)
        bulk.addWidget(none_tapped)
        bulk.addStretch(1)
        layout.addLayout(bulk)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- rows ----------------------------------------------------------------

    def _preamble(self) -> str:
        count = len(self.candidates)
        holes = "bore" if count == 1 else "bores"
        return (
            f"{count} {holes} on this part are drilled at a standard tap drill "
            "size. This model carries no thread data, so the workbench cannot "
            "tell a tapped hole from a dowel or clearance hole.\n"
            "Whatever is answered here is remembered with the document and not "
            "asked again."
        )

    @staticmethod
    def _cell(text: str) -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem(text)
        item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
        return item

    def _set_all(self, tapped: bool) -> None:
        for combo in self.combos:
            combo.setCurrentIndex(1 if tapped else 2)

    def _on_row_changed(self, row: int, _column: int, _prev_row: int, _prev_col: int) -> None:
        if self.target_object is None or not 0 <= row < len(self.candidates):
            return
        try:
            Gui.Selection.clearSelection()
            document = self.target_object.Document.Name
            name = self.target_object.Name
            for face_id in self.candidates[row].faces:
                Gui.Selection.addSelection(document, name, f"Face{face_id}")
        except Exception:
            # A selection that will not take is a cosmetic loss. The question
            # is still answerable and losing the dialog over it would not be.
            pass

    # -- the result ----------------------------------------------------------

    def answers(self) -> dict[str, bool]:
        """The verdicts given, keyed the way the store keys them.

        Rows left at "Ask again" are absent rather than false. An unanswered
        question has to stay unanswered, or the next run would treat a
        dialog somebody skimmed as a shop decision.
        """
        given: dict[str, bool] = {}
        for candidate, combo in zip(self.candidates, self.combos):
            verdict = combo.currentData()
            if verdict is None:
                continue
            given[candidate.key.encode()] = bool(verdict)
        return given


def ask(
    candidates: Sequence[ThreadCandidate], target_object=None, parent=None
) -> dict[str, bool]:
    """Put the candidates to the user and return the verdicts given.

    Empty when there is nothing to ask, when the dialog is cancelled, or when
    no GUI is running -- a batch analysis must not stop to ask a question
    nobody is there to answer.
    """
    if not candidates:
        return {}
    if not getattr(App, "GuiUp", False):
        return {}
    dialog = ThreadConfirmDialog(candidates, target_object, parent)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return {}
    return dialog.answers()
