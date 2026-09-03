# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The shop's own settings, which the machining rules are judged against.

Almost every machining rule is really a question about the shop rather than
about the part. Whether a corner radius is too tight depends on the smallest
end mill on the shelf. Whether an undercut matters depends on whether the
machine can tilt. Whether a bore is a standard size depends on which drill
index gets opened. None of that is in the model, and none of it is the same
from one shop to the next.

So these are declarations, not measurements. Nothing here is inferred from
geometry, because none of it reliably can be -- a machined casting and a
machined billet are identical once the flash is off, and no analysis can
recover which one it was looking at.

The thresholds themselves live in the material and process editors, where
they belong: they are policy, and policy varies by material. What is here is
the equipment and the stock.
"""

from PySide6 import QtGui, QtWidgets

import FreeCAD as App  # type: ignore

from ..core.machining.config import BLANK_FORMS, MACHINE_MODES, UNIT_SYSTEMS

_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/DFM"


# Each entry is the stored value, the label a machinist would recognize, and
# what choosing it actually changes.
_MACHINE_MODE_CHOICES = (
    (
        "3axis",
        "3-axis",
        "The part is fixtured once and the tool comes straight down. "
        "Anything the tool cannot see from one of the six directions is "
        "reported as an undercut.",
    ),
    (
        "3plus2",
        "3+2 (indexed)",
        "The part can be indexed between operations but the tool does not "
        "move while cutting. Angled features become extra setups rather "
        "than undercuts.",
    ),
    (
        "5axis",
        "5-axis",
        "The tool can tilt while cutting, so most undercuts stop being "
        "undercuts and the access rules stand down.",
    ),
)

_UNIT_SYSTEM_CHOICES = (
    ("both", "Both", "Match hole and slot sizes against metric and imperial tooling."),
    (
        "metric",
        "Metric only",
        "A shop with no imperial taps should not be told its 5.1 mm hole is "
        "a quarter-twenty.",
    ),
    ("imperial", "Imperial only", "Match against fractional and numbered tooling only."),
)

_BLANK_FORM_CHOICES = (
    ("", "Not declared", "Rules that depend on the stock form stay quiet."),
    (
        "billet",
        "Solid billet",
        "Everything is cut away from solid, so the material-removal rule "
        "measures against the bounding box.",
    ),
    (
        "as_cast",
        "As-cast",
        "The part arrives near net shape. Walls are then expected to carry "
        "draft, and a model with none is a contradiction worth reporting.",
    ),
    (
        "profile_extrusion",
        "Profile extrusion",
        "The cross section comes from the mill. Only what varies along the "
        "length is actually machined.",
    ),
)


class MachiningPreferences:
    """The Machining page in Edit -> Preferences -> DFM."""

    def __init__(self):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Machining")
        self.form.setWindowIcon(QtGui.QIcon(":/icons/dfm_analysis.svg"))

        layout = QtWidgets.QVBoxLayout(self.form)
        layout.addWidget(self._intro())

        self.machine_mode = self._combo(_MACHINE_MODE_CHOICES)
        self.unit_system = self._combo(_UNIT_SYSTEM_CHOICES)
        self.blank_form = self._combo(_BLANK_FORM_CHOICES)

        self.precision_mode = QtWidgets.QCheckBox(
            "Precision work: tighten the limits across the board"
        )
        self.precision_mode.setToolTip(
            "For shops whose ordinary work is closer than general machining.\n"
            "Tightens the thresholds rather than changing which rules run."
        )

        self.confirm_threads = QtWidgets.QCheckBox(
            "Ask before treating an inferred thread as real"
        )
        self.confirm_threads.setToolTip(
            "A thread is only asserted when it is actually modelled as a\n"
            "helix. With this on, anything less certain is raised as a\n"
            "question rather than reported as a fact."
        )

        layout.addWidget(
            self._group(
                "Machine",
                [
                    ("Axes available", self.machine_mode),
                    ("Tooling measured in", self.unit_system),
                ],
            )
        )
        layout.addWidget(
            self._group("Stock", [("Blank arrives as", self.blank_form)])
        )

        policy = QtWidgets.QGroupBox("Policy")
        policy_layout = QtWidgets.QVBoxLayout(policy)
        policy_layout.addWidget(self.precision_mode)
        policy_layout.addWidget(self.confirm_threads)
        layout.addWidget(policy)

        layout.addWidget(self._footnote())
        layout.addStretch()

        self.machine_mode.currentIndexChanged.connect(self._refresh_tooltips)
        self.unit_system.currentIndexChanged.connect(self._refresh_tooltips)
        self.blank_form.currentIndexChanged.connect(self._refresh_tooltips)

    # -- construction -------------------------------------------------------

    @staticmethod
    def _intro() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(
            "What the machining rules are judged against. These describe the "
            "shop, not the part -- whether a corner is too tight depends on "
            "the smallest cutter on the shelf, and nothing in the model can "
            "say what that is."
        )
        label.setWordWrap(True)
        return label

    @staticmethod
    def _footnote() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(
            "Limits and tolerances live in the material and process editors, "
            "where they can vary by material. Tool sizes come from the tool "
            "library."
        )
        label.setWordWrap(True)
        font = label.font()
        font.setItalic(True)
        label.setFont(font)
        return label

    @staticmethod
    def _combo(choices) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        for value, label, explanation in choices:
            combo.addItem(label, value)
            combo.setItemData(
                combo.count() - 1, explanation, QtGui.Qt.ToolTipRole
            )
        return combo

    @staticmethod
    def _group(title: str, rows) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        grid = QtWidgets.QGridLayout(box)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        for row, (label, widget) in enumerate(rows):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        return box

    def _refresh_tooltips(self) -> None:
        """Show the current choice's explanation on the control itself.

        The consequence of each setting is the part worth reading, and it is
        no use buried in a dropdown the user has already closed.
        """
        for combo in (self.machine_mode, self.unit_system, self.blank_form):
            combo.setToolTip(
                combo.itemData(combo.currentIndex(), QtGui.Qt.ToolTipRole) or ""
            )

    # -- persistence --------------------------------------------------------

    def loadSettings(self) -> None:
        params = App.ParamGet(_PARAM_PATH)
        self._select(self.machine_mode, params.GetString("MachiningMachineMode", "3axis"))
        self._select(self.unit_system, params.GetString("MachiningUnitSystem", "both"))
        self._select(self.blank_form, params.GetString("MachiningBlankForm", ""))
        self.precision_mode.setChecked(params.GetBool("MachiningPrecisionMode", False))
        self.confirm_threads.setChecked(
            params.GetBool("MachiningConfirmInferredThreads", True)
        )
        self._refresh_tooltips()

    def saveSettings(self) -> None:
        params = App.ParamGet(_PARAM_PATH)
        params.SetString("MachiningMachineMode", self._value(self.machine_mode, MACHINE_MODES))
        params.SetString("MachiningUnitSystem", self._value(self.unit_system, UNIT_SYSTEMS))
        params.SetString("MachiningBlankForm", self._value(self.blank_form, BLANK_FORMS))
        params.SetBool("MachiningPrecisionMode", self.precision_mode.isChecked())
        params.SetBool(
            "MachiningConfirmInferredThreads", self.confirm_threads.isChecked()
        )

    @staticmethod
    def _select(combo: QtWidgets.QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _value(combo: QtWidgets.QComboBox, allowed) -> str:
        """The selected value, checked against what the config accepts.

        A stale preference from an older version would otherwise be written
        straight back out and silently ignored by every rule that reads it.
        """
        value = combo.currentData()
        return value if value in allowed else (allowed[0] if allowed else "")
