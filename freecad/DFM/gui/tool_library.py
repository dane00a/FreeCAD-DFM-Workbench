# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The Tooling page: what is actually on the shelf.

The thresholds say how tight a corner is too tight. The shelf says what the
shop can do about it. A shop that owns a 1 mm end mill gets a different
answer on the same part from a shop whose smallest is 6 mm, and the rules
have no other way to find that out -- there is nothing in a model that says
which cutters were bought.

Three things live here, and they are read by different rules:

* The **tool library**. Size-matching rules ask whether a diameter or a
  corner radius is one the shop already owns; reach rules ask whether a flute
  is long enough to get to the bottom of a pocket.
* The **metric drill index** and the **imperial drill index**. A hole is a
  standard size if a drill in the index cuts it. Which index gets consulted
  follows the unit system on the Machining page, so a metric-only shop is
  never told its 5.1 mm hole is a quarter-twenty.

Both are lists rather than single numbers, so neither fits FreeCAD's
preference widgets and neither gets FreeCAD's native reset. They store as
text under one key each and carry their own restore buttons, which is also
why the shelf is only written out when it differs from the default -- an
untouched shelf leaves no key behind to go stale.
"""

from PySide6 import QtGui, QtWidgets

import FreeCAD as App  # type: ignore

from ..core.machining.config import (
    IMPERIAL_DRILL_PREF_KEY,
    IMPERIAL_DRILL_SIZES_MM,
    METRIC_DRILL_PREF_KEY,
    METRIC_DRILL_SIZES_MM,
    TOOL_LIBRARY_PREF_KEY,
    TOOL_TYPES,
    ToolEntry,
    decode_size_list,
    decode_tool_library,
    default_tool_library,
    encode_size_list,
    encode_tool_library,
)

_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/DFM"

# Column order matches the stored line, so a row and its text read the same
# way round.
_COLUMNS = (
    ("Type", "What the tool is. Only the type a rule asks for is consulted."),
    ("Min ⌀", "Smallest diameter this entry covers, in mm."),
    ("Max ⌀", "Largest diameter. Equal to the minimum for one fixed size."),
    (
        "Corner r",
        "Radius left in an inside corner by the flute itself.\n"
        "Zero is a sharp end mill; half the diameter marks a ball nose.",
    ),
    ("Flute", "Usable cutting length, in mm."),
    ("Reach", "Holder plus flute: how deep the tool can get, in mm."),
    (
        "Unit",
        "Which index this size belongs to. Leave it blank for a\n"
        "unit-agnostic tool such as a boring bar or a turning insert.",
    ),
)

_UNIT_CHOICES = (("", "any"), ("metric", "metric"), ("imperial", "imperial"))


class MachiningTooling:
    """The Tooling page in Edit -> Preferences -> DFM."""

    def __init__(self):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Machining Tooling")
        self.form.setWindowIcon(QtGui.QIcon(":/icons/dfm_analysis.svg"))

        layout = QtWidgets.QVBoxLayout(self.form)
        layout.addWidget(self._intro())

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_library_tab(), "Tool library")
        tabs.addTab(self._build_drill_tab(), "Drill index")
        layout.addWidget(tabs)

    # -- construction -------------------------------------------------------

    @staticmethod
    def _intro() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(
            "What the shop owns. The limits on the Machining Limits page say "
            "when a feature is a problem; the shelf here says whether there "
            "is a tool that solves it."
        )
        label.setWordWrap(True)
        return label

    def _build_library_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        self.table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([name for name, _ in _COLUMNS])
        for column, (_, tip) in enumerate(_COLUMNS):
            self.table.horizontalHeaderItem(column).setToolTip(tip)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._refresh_summary)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)

        add = QtWidgets.QPushButton("Add tool")
        add.clicked.connect(self._add_blank_tool)
        remove = QtWidgets.QPushButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        restore = QtWidgets.QPushButton("Restore the default shelf")
        restore.setToolTip(
            "A general-purpose job-shop library: metric and imperial, mill "
            "and lathe."
        )
        restore.clicked.connect(lambda: self._show_tools(default_tool_library()))

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        buttons.addWidget(restore)

        layout.addWidget(self.table)
        layout.addLayout(buttons)
        layout.addWidget(self.summary)
        return page

    def _build_drill_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        note = QtWidgets.QLabel(
            "Sizes in millimetres, separated by commas. Order does not "
            "matter. An empty index falls back to the catalogue -- to work "
            "in one system only, say so under Tooling measured in on the "
            "Machining page rather than clearing an index here."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.metric_drills, metric_box = self._size_editor(
            "Metric drills",
            "Jobber sizes, including the tap drills a shop actually stocks: "
            "3.3 for M4, 4.2 for M5, 6.8 for M8.",
            METRIC_DRILL_SIZES_MM,
        )
        self.imperial_drills, imperial_box = self._size_editor(
            "Imperial drills",
            "Fractional, number and letter drills, converted to millimetres.",
            IMPERIAL_DRILL_SIZES_MM,
        )

        layout.addWidget(metric_box)
        layout.addWidget(imperial_box)
        return page

    def _size_editor(self, title: str, blurb: str, catalogue) -> tuple:
        box = QtWidgets.QGroupBox(title)
        box_layout = QtWidgets.QVBoxLayout(box)

        label = QtWidgets.QLabel(blurb)
        label.setWordWrap(True)
        font = label.font()
        font.setItalic(True)
        label.setFont(font)

        editor = QtWidgets.QPlainTextEdit()
        editor.setTabChangesFocus(True)
        editor.setMinimumHeight(70)

        restore = QtWidgets.QPushButton("Restore the catalogue")
        restore.clicked.connect(
            lambda: editor.setPlainText(encode_size_list(catalogue))
        )

        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        row.addWidget(restore)

        box_layout.addWidget(label)
        box_layout.addWidget(editor)
        box_layout.addLayout(row)
        return editor, box

    # -- the table ----------------------------------------------------------

    def _show_tools(self, tools) -> None:
        """Redraw the whole table. Cheap enough: the shelf is a hundred rows."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for tool in tools:
            self._append_row(tool)
        self.table.blockSignals(False)
        self._refresh_summary()

    def _append_row(self, tool: ToolEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        types = QtWidgets.QComboBox()
        types.setEditable(True)  # a shop may stock something not in the list
        types.addItems(TOOL_TYPES)
        types.setCurrentText(tool.type)
        types.currentTextChanged.connect(self._refresh_summary)
        self.table.setCellWidget(row, 0, types)

        numbers = (
            tool.min_diameter_mm,
            tool.max_diameter_mm,
            tool.corner_radius_mm,
            tool.max_flute_length_mm,
            tool.max_reach_mm,
        )
        for offset, value in enumerate(numbers):
            item = QtWidgets.QTableWidgetItem(f"{value:.6g}")
            item.setTextAlignment(QtGui.Qt.AlignRight | QtGui.Qt.AlignVCenter)
            self.table.setItem(row, offset + 1, item)

        units = QtWidgets.QComboBox()
        for value, label in _UNIT_CHOICES:
            units.addItem(label, value)
        index = units.findData(tool.unit)
        units.setCurrentIndex(index if index >= 0 else 0)
        units.currentIndexChanged.connect(self._refresh_summary)
        self.table.setCellWidget(row, len(_COLUMNS) - 1, units)

    def _add_blank_tool(self) -> None:
        """A plain 6 mm end mill: the entry most likely to be edited into."""
        self._append_row(ToolEntry(type="end_mill", min_diameter_mm=6.0, max_diameter_mm=6.0))
        self.table.scrollToBottom()
        self._refresh_summary()

    def _remove_selected(self) -> None:
        for row in sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        ):
            self.table.removeRow(row)
        self._refresh_summary()

    def _row_tool(self, row: int):
        """One row as a tool, or None if it is not usable.

        Rows go through the same parser the stored text does, so a row that
        will not survive being saved is a row that reads as unusable here.
        """
        types = self.table.cellWidget(row, 0)
        units = self.table.cellWidget(row, len(_COLUMNS) - 1)
        if types is None or units is None:
            return None

        cells = []
        for column in range(1, len(_COLUMNS) - 1):
            item = self.table.item(row, column)
            cells.append((item.text() if item else "").strip() or "0")

        return ToolEntry.from_spec(
            ",".join([types.currentText().strip(), *cells, units.currentData() or ""])
        )

    def _current_tools(self) -> list[ToolEntry]:
        tools = []
        for row in range(self.table.rowCount()):
            tool = self._row_tool(row)
            if tool is not None:
                tools.append(tool)
        return tools

    def _refresh_summary(self) -> None:
        """Say what the rules will actually read off this shelf.

        The two numbers shown are the ones that change the most verdicts, and
        neither is obvious from the rows: the tightest inside corner is half
        the smallest end-mill *diameter*, not the smallest corner-radius
        column, because a sharp end mill still leaves its own radius.
        """
        tools = self._current_tools()
        unusable = self.table.rowCount() - len(tools)

        diameters = [
            tool.min_diameter_mm for tool in tools if tool.type == "end_mill"
        ]
        parts = [f"{len(tools)} tools."]
        if diameters:
            smallest = min(diameters)
            parts.append(
                f"Smallest end mill {smallest:.4g} mm, so the tightest inside "
                f"corner is {smallest / 2.0:.4g} mm."
            )
        else:
            parts.append("No end mill on the shelf: the corner-radius rules stand down.")
        if unusable:
            parts.append(f"{unusable} row(s) will be dropped as unreadable.")

        self.summary.setText(" ".join(parts))

    # -- persistence --------------------------------------------------------

    def loadSettings(self) -> None:
        params = App.ParamGet(_PARAM_PATH)

        stored = decode_tool_library(params.GetString(TOOL_LIBRARY_PREF_KEY, ""))
        self._show_tools(stored or default_tool_library())

        self.metric_drills.setPlainText(
            params.GetString(METRIC_DRILL_PREF_KEY, "")
            or encode_size_list(METRIC_DRILL_SIZES_MM)
        )
        self.imperial_drills.setPlainText(
            params.GetString(IMPERIAL_DRILL_PREF_KEY, "")
            or encode_size_list(IMPERIAL_DRILL_SIZES_MM)
        )

    def saveSettings(self) -> None:
        params = App.ParamGet(_PARAM_PATH)
        params.SetString(TOOL_LIBRARY_PREF_KEY, self._library_to_store())
        params.SetString(
            METRIC_DRILL_PREF_KEY,
            self._sizes_to_store(self.metric_drills, METRIC_DRILL_SIZES_MM),
        )
        params.SetString(
            IMPERIAL_DRILL_PREF_KEY,
            self._sizes_to_store(self.imperial_drills, IMPERIAL_DRILL_SIZES_MM),
        )

    def _library_to_store(self) -> str:
        """The shelf as text, or nothing at all if it is still the default.

        Storing three kilobytes of unchanged catalogue would mean a shop that
        never opened this page carries a copy of the defaults that cannot
        follow them when they change.
        """
        tools = self._current_tools()
        if not tools:
            return ""
        text = encode_tool_library(tools)
        return "" if text == encode_tool_library(default_tool_library()) else text

    @staticmethod
    def _sizes_to_store(editor: QtWidgets.QPlainTextEdit, catalogue) -> str:
        sizes = decode_size_list(editor.toPlainText())
        if not sizes or sizes == decode_size_list(encode_size_list(catalogue)):
            return ""
        return encode_size_list(sizes)
