"""
PyQt6 / pyqtgraph user interface.
"""

from __future__ import annotations

import html
import logging
import math
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .acquisition import Acquisition
from .config import CellConfig, Config
from .datalog import CsvLogger
from .samples import CellSample, Sample
from .units import DISPLAY_SCALE, RATE_TO_DISPLAY, THICKNESS_TO_DISPLAY

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]

WINDOWS = [
    ("5 min", 300),
    ("30 min", 1800),
    ("2 h", 7200),
    ("12 h", 43200),
    ("All", None),
]


class Bridge(QObject):
    """
    Carries data from the acquisition thread to the GUI thread.
    """

    sample = pyqtSignal(object)
    message = pyqtSignal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.setFormatter(
            logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.bridge.message.emit(self.format(record), record.levelno)


# ----------------------------------------------------------------------
# Time axis
# ----------------------------------------------------------------------

def format_duration(seconds: float, decimals: int = 0) -> str:
    """
    Format seconds as [-]h:mm:ss (or m:ss below one hour).
    """

    sign = "-" if seconds < 0 else ""
    seconds = round(abs(seconds), decimals)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    width = 3 + decimals if decimals else 2
    secs = f"{secs:0{width}.{decimals}f}"
    if hours:
        return f"{sign}{int(hours)}:{int(minutes):02d}:{secs}"
    return f"{sign}{int(minutes)}:{secs}"


class ElapsedAxis(pg.AxisItem):
    """
    Time axis in h:mm:ss with ticks at round time steps.
    """

    STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
             3600, 7200, 10800, 21600, 43200, 86400]

    def tickSpacing(self, minVal, maxVal, size):
        span = maxVal - minVal
        if span <= 0 or size <= 0:
            return super().tickSpacing(minVal, maxVal, size)

        # At most one labelled tick per ~90 px.
        wanted = span / max(size / 90.0, 1.0)
        major = next((s for s in self.STEPS if s >= wanted), self.STEPS[-1])
        minor = next(
            (s for s in reversed(self.STEPS) if s < major and major % s == 0
             and major / s <= 6),
            major / 5,
        )
        return [(major, 0), (minor, 0)]

    def tickStrings(self, values, scale, spacing):
        return [format_duration(v) for v in values]


# ----------------------------------------------------------------------
# History buffer
# ----------------------------------------------------------------------

class History:
    """
    Growable column store of the samples received so far, in display
    units.
    """

    FIELDS = [
        "temperature", "working_setpoint", "rate", "rate_target", "qcm_rate",
        "thickness",
    ]

    def __init__(self, n_cells: int) -> None:
        self.n_cells = n_cells
        self.size = 0
        self._capacity = 4096
        self.time = np.empty(self._capacity)
        self.data = {
            (i, f): np.empty(self._capacity)
            for i in range(n_cells)
            for f in self.FIELDS
        }

    def append(self, sample: Sample) -> None:
        if self.size == self._capacity:
            self._capacity *= 2
            self.time = np.resize(self.time, self._capacity)
            for key, array in self.data.items():
                self.data[key] = np.resize(array, self._capacity)

        self.time[self.size] = sample.since_start
        for i, cell in enumerate(sample.cells):
            for f in self.FIELDS:
                self.data[(i, f)][self.size] = (
                    getattr(cell, f) * DISPLAY_SCALE.get(f, 1.0)
                )
        self.size += 1

    def view(self, since: float | None):
        """
        Return (time since start, data) for samples newer than ``since``.
        """

        start = 0
        if since is not None:
            start = int(np.searchsorted(self.time[: self.size], since))

        sl = slice(start, self.size)
        return self.time[sl], {k: v[sl] for k, v in self.data.items()}


# ----------------------------------------------------------------------
# Per-cell panel
# ----------------------------------------------------------------------

def _fmt(value: float, fmt: str, unit: str) -> str:
    return "—" if not math.isfinite(value) else f"{value:{fmt}} {unit}"


class CellPanel(QGroupBox):
    setpoint_requested = pyqtSignal(float)
    feedback_requested = pyqtSignal(bool, float)

    def __init__(self, cell: CellConfig, color: str, rate_window: float) -> None:
        super().__init__(cell.name)
        self.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; border: 2px solid {color};"
            " border-radius: 4px; margin-top: 1.2em; padding: 6px; }"
            " QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )

        big = QFont()
        big.setPointSize(15)
        big.setBold(True)

        def value(row: int, col: int, caption: str) -> QLabel:
            label = QLabel(caption)
            label.setStyleSheet("color: gray; font-weight: normal;")
            number = QLabel("—")
            number.setFont(big)
            values.addWidget(label, 2 * row, col)
            values.addWidget(number, 2 * row + 1, col)
            return number

        values = QGridLayout()
        values.setHorizontalSpacing(16)
        self.temperature = value(0, 0, "Temperature")
        self.setpoint = value(0, 1, "Setpoint")
        self.working_setpoint = value(0, 2, "Working setpoint")
        self.output = value(0, 3, "Output")
        self.rate = value(1, 0, f"Rate ({rate_window:g} s fit)")
        self.qcm_rate = value(1, 1, "Rate (QCM)")
        self.thickness = value(1, 2, "Thickness")
        self.frequency = value(1, 3, "Crystal frequency")
        for col in range(4):
            values.setColumnStretch(col, 1)

        # Setpoint control
        self.setpoint_input = QDoubleSpinBox()
        self.setpoint_input.setRange(cell.min_setpoint, cell.max_setpoint)
        self.setpoint_input.setDecimals(1)
        self.setpoint_input.setSuffix(" °C")
        self.setpoint_input.setKeyboardTracking(False)
        set_button = QPushButton("Set")
        set_button.clicked.connect(
            lambda: self.setpoint_requested.emit(self.setpoint_input.value())
        )

        # Rate feedback control
        self.rate_input = QDoubleSpinBox()
        self.rate_input.setRange(0.01, 60000.0)
        self.rate_input.setDecimals(2)
        self.rate_input.setValue(6.0)
        self.rate_input.setSuffix(" Å/min")
        self.rate_input.setKeyboardTracking(False)
        self.rate_input.valueChanged.connect(self._rate_target_changed)

        self.feedback_button = QPushButton("Rate feedback: OFF")
        self.feedback_button.setCheckable(True)
        self.feedback_button.clicked.connect(self._feedback_clicked)

        controls = QGridLayout()
        controls.addWidget(QLabel("Temperature setpoint"), 0, 0)
        controls.addWidget(self.setpoint_input, 0, 1)
        controls.addWidget(set_button, 0, 2)
        controls.addWidget(QLabel("Target rate"), 1, 0)
        controls.addWidget(self.rate_input, 1, 1)
        controls.addWidget(self.feedback_button, 1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(values)
        layout.addSpacing(6)
        layout.addLayout(controls)

    def _feedback_clicked(self, checked: bool) -> None:
        self.feedback_requested.emit(
            checked, self.rate_input.value() / RATE_TO_DISPLAY
        )

    def _rate_target_changed(self, value: float) -> None:
        if self.feedback_button.isChecked():
            self.feedback_requested.emit(True, value / RATE_TO_DISPLAY)

    def update_values(self, cell: CellSample) -> None:
        self.temperature.setText(_fmt(cell.temperature, ".1f", "°C"))
        self.rate.setText(_fmt(cell.rate * RATE_TO_DISPLAY, ".2f", "Å/min"))
        self.setpoint.setText(_fmt(cell.target_setpoint, ".1f", "°C"))
        self.working_setpoint.setText(_fmt(cell.working_setpoint, ".1f", "°C"))
        self.output.setText(_fmt(cell.output, ".1f", "%"))
        self.qcm_rate.setText(
            _fmt(cell.qcm_rate * RATE_TO_DISPLAY, ".1f", "Å/min")
        )
        self.thickness.setText(
            _fmt(cell.thickness * THICKNESS_TO_DISPLAY, ".0f", "Å")
        )
        self.frequency.setText(_fmt(cell.frequency / 1e6, ".6f", "MHz"))

        # Show the controller's setpoint unless the user is editing it.
        if (
            math.isfinite(cell.target_setpoint)
            and not self.setpoint_input.hasFocus()
        ):
            self.setpoint_input.setValue(cell.target_setpoint)

        # Feedback can be switched off by the acquisition thread
        # (e.g. communication error); keep the button in sync.
        self.feedback_button.blockSignals(True)
        self.feedback_button.setChecked(cell.feedback)
        self.feedback_button.blockSignals(False)
        self.feedback_button.setText(
            f"Rate feedback: {'ON' if cell.feedback else 'OFF'}"
        )
        self.feedback_button.setStyleSheet(
            "background-color: #2e7d32; color: white;" if cell.feedback else ""
        )


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(
        self,
        config: Config,
        acquisition: Acquisition,
        data_logger: CsvLogger,
        bridge: Bridge,
        title: str = "Deposition control",
    ) -> None:
        super().__init__()
        self.config = config
        self.acquisition = acquisition
        self.data_logger = data_logger
        self.history = History(len(config.cells))
        self.window_seconds: float | None = WINDOWS[0][1]
        self._last_redraw = 0.0
        # Time since start of the last reset (or the start): x = 0.
        self._reference = 0.0
        self._latest = 0.0
        self._reset_lines: list[tuple[float, pg.InfiniteLine]] = []

        self.setWindowTitle(title)
        self.resize(1300, 1000)

        self._build_toolbar()

        # Cell panels
        self.panels = []
        panels = QHBoxLayout()
        for i, cell in enumerate(config.cells):
            panel = CellPanel(cell, COLORS[i % len(COLORS)], config.rate.window)
            panel.setpoint_requested.connect(
                lambda v, i=i: self.acquisition.set_setpoint(i, v)
            )
            panel.feedback_requested.connect(
                lambda on, rate, i=i: self._feedback_requested(i, on, rate)
            )
            panels.addWidget(panel)
            self.panels.append(panel)

        # Plots
        pg.setConfigOptions(antialias=True)
        plots = pg.GraphicsLayoutWidget()
        plots.setBackground("w")

        self.temperature_plot = plots.addPlot(
            row=0, col=0, axisItems={"bottom": ElapsedAxis("bottom")}
        )
        self.rate_plot = plots.addPlot(
            row=1, col=0, axisItems={"bottom": ElapsedAxis("bottom")}
        )
        self.thickness_plot = plots.addPlot(
            row=2, col=0, axisItems={"bottom": ElapsedAxis("bottom")}
        )
        self.rate_plot.setXLink(self.temperature_plot)
        self.thickness_plot.setXLink(self.temperature_plot)

        self.temperature_plot.setLabel("left", "Temperature", units="°C")
        self.rate_plot.setLabel("left", "Rate (Å/min)")
        self.rate_plot.getAxis("left").enableAutoSIPrefix(False)
        self.thickness_plot.setLabel("left", "Thickness (Å)")
        self.thickness_plot.setLabel("bottom", "Time since reset (h:mm:ss)")
        self.thickness_plot.getAxis("left").enableAutoSIPrefix(False)

        self.curves = {}
        for plot, fields in [
            (self.temperature_plot, ("temperature", "working_setpoint")),
            (self.rate_plot, ("rate", "rate_target")),
            (self.thickness_plot, ("thickness", None)),
        ]:
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setClipToView(True)
            plot.setDownsampling(auto=True, mode="peak")
            plot.getViewBox().setAutoVisible(y=True)
            legend = plot.addLegend(offset=(10, 10))
            legend.setLabelTextColor("k")

            for i, cell in enumerate(config.cells):
                color = COLORS[i % len(COLORS)]
                solid, dashed = fields
                self.curves[(i, solid)] = plot.plot(
                    pen=pg.mkPen(color, width=2), name=cell.name
                )
                if dashed is None:
                    continue
                self.curves[(i, dashed)] = plot.plot(
                    pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                    name=f"{cell.name} {'setpoint' if plot is self.temperature_plot else 'target'}",
                )

            if plot is self.rate_plot:
                for i, cell in enumerate(config.cells):
                    faint = pg.mkColor(COLORS[i % len(COLORS)])
                    faint.setAlpha(70)
                    curve = plot.plot(
                        pen=pg.mkPen(faint, width=1),
                        name=f"{cell.name} QCM rate",
                    )
                    curve.setZValue(-1)
                    self.curves[(i, "qcm_rate")] = curve

        # Event log
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(2000)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(plots)
        splitter.addWidget(self.events)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(panels)
        layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        bridge.sample.connect(self.on_sample)
        bridge.message.connect(self.on_message)

        self._update_logging_state()

        # Keep the initial focus away from the setpoint boxes, which do
        # not follow the controller while focused.
        self.events.setFocus()

    # ------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.logging_action = QAction("Start logging", self)
        self.logging_action.setCheckable(True)
        self.logging_action.triggered.connect(self._toggle_logging)
        toolbar.addAction(self.logging_action)

        self.log_label = QLabel()
        self.log_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        toolbar.addWidget(self.log_label)

        toolbar.addSeparator()
        reset_button = QPushButton("Reset thickness && time")
        reset_button.setToolTip(
            "Zero the QCM thickness readings and deposition timer"
        )
        reset_button.clicked.connect(self._reset_clicked)
        toolbar.addWidget(reset_button)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(spacer)

        caption = QLabel("Time since reset: ")
        caption.setStyleSheet("color: gray;")
        toolbar.addWidget(caption)
        self.elapsed_label = QLabel("—")
        font = QFont()
        font.setPointSize(15)
        font.setBold(True)
        self.elapsed_label.setFont(font)
        self.elapsed_label.setMinimumWidth(130)
        toolbar.addWidget(self.elapsed_label)
        toolbar.addSeparator()

        toolbar.addWidget(QLabel("Plot window: "))
        self.window_box = QComboBox()
        for label, _ in WINDOWS:
            self.window_box.addItem(label)
        self.window_box.currentIndexChanged.connect(self._window_changed)
        toolbar.addWidget(self.window_box)

    def _toggle_logging(self, checked: bool) -> None:
        if checked:
            path = self.data_logger.start()
            logging.getLogger(__name__).info("Logging to %s", path)
        else:
            self.data_logger.stop()
            logging.getLogger(__name__).info("Logging stopped.")
        self._update_logging_state()

    def _update_logging_state(self) -> None:
        active = self.data_logger.active
        self.logging_action.setChecked(active)
        self.logging_action.setText("Stop logging" if active else "Start logging")
        self.log_label.setText(
            f"  Logging to {self.data_logger.path.name}" if active
            else "  Not logging"
        )
        self.log_label.setToolTip(
            str(self.data_logger.path) if active else ""
        )
        self.log_label.setStyleSheet(
            "color: #2e7d32; font-weight: bold;" if active else "color: #b71c1c;"
        )

    def _reset_clicked(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset",
            "Zero the thickness readings and the deposition timer of the "
            "QCM?\n\nThe rate is unavailable for half a fit window "
            "afterwards; rate feedback holds the setpoint meanwhile.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.acquisition.reset_measurement()

    def _window_changed(self, index: int) -> None:
        self.window_seconds = WINDOWS[index][1]
        self._redraw()

    def _feedback_requested(self, index: int, on: bool, rate: float) -> None:
        if on:
            self.acquisition.enable_feedback(index, rate)
        else:
            self.acquisition.disable_feedback(index)

    # ------------------------------------------------------------------

    def on_sample(self, sample: Sample) -> None:
        self.history.append(sample)
        self._latest = sample.since_start
        self._reference = sample.since_start - sample.elapsed

        if sample.reset:
            for plot in (self.temperature_plot, self.rate_plot, self.thickness_plot):
                line = pg.InfiniteLine(
                    angle=90,
                    pen=pg.mkPen("#757575", width=1, style=Qt.PenStyle.DotLine),
                    label="reset" if plot is self.temperature_plot else None,
                    labelOpts={"position": 0.95, "color": "#757575"},
                )
                plot.addItem(line)
                self._reset_lines.append((self._reference, line))

        # Samples may arrive fast in an accelerated simulation.
        now = time.monotonic()
        if now - self._last_redraw < 0.2:
            return
        self._last_redraw = now

        self.elapsed_label.setText(format_duration(sample.elapsed))
        for panel, cell in zip(self.panels, sample.cells):
            panel.update_values(cell)
        self._redraw()

    def on_message(self, text: str, level: int) -> None:
        self.statusBar().showMessage(text, 10000)

        escaped = html.escape(text)
        if level >= logging.ERROR:
            escaped = f'<span style="color:#b71c1c">{escaped}</span>'
        elif level >= logging.WARNING:
            escaped = f'<span style="color:#e65100">{escaped}</span>'
        self.events.appendHtml(escaped)

    def _redraw(self) -> None:
        if self.history.size == 0:
            return

        since = None
        if self.window_seconds is not None:
            since = self._latest - self.window_seconds

        # x = time relative to the last reset (earlier data is negative).
        t, data = self.history.view(since)
        x = t - self._reference
        for key, curve in self.curves.items():
            curve.setData(x, data[key], connect="finite")

        for position, line in self._reset_lines:
            line.setPos(position - self._reference)

        if self.window_seconds is None:
            self.temperature_plot.enableAutoRange(x=True)
        else:
            end = self._latest - self._reference
            self.temperature_plot.setXRange(
                end - self.window_seconds, end, padding=0
            )
        for plot in (self.temperature_plot, self.rate_plot, self.thickness_plot):
            plot.enableAutoRange(y=True)

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        answer = QMessageBox.question(
            self,
            "Quit",
            "Stop acquisition and quit?\n\n"
            "The Eurotherm controllers keep their current setpoints.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self.acquisition.stop()
        self.data_logger.stop()
        event.accept()
