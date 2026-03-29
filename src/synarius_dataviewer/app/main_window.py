"""Main window: Studio-like chrome, channel sidebar, MDI data viewers."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMdiArea, QMdiSubWindow, QMessageBox, QSplitter

from synarius_dataviewer._version import __version__
from synarius_dataviewer.app import theme
from synarius_dataviewer.io.timeseries_io import TimeSeriesBundle
from synarius_dataviewer.widgets.channel_sidebar import ChannelSidebar
from synarius_dataviewer.widgets.data_viewer import DataViewerShell


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Synarius Dataviewer {__version__}")
        self.resize(1280, 720)
        self._bundle: TimeSeriesBundle | None = None

        self._mdi = QMdiArea()
        self._mdi.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._mdi.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._mdi.setActivationOrder(QMdiArea.WindowOrder.CreationOrder)

        self._sidebar = ChannelSidebar(self)
        self._sidebar.plot_selected_requested.connect(self._plot_selected)
        self._sidebar.bundle_changed.connect(self._on_bundle_changed)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._mdi)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1000])
        self.setCentralWidget(splitter)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()

    def _create_actions(self) -> None:
        self._act_open = QAction("Open…", self)
        self._act_open.triggered.connect(self._sidebar._open_file)
        self._act_new_view = QAction("New data viewer", self)
        self._act_new_view.triggered.connect(self._new_viewer)
        self._act_tile = QAction("Tile windows", self)
        self._act_tile.triggered.connect(self._mdi.tileSubWindows)
        self._act_cascade = QAction("Cascade windows", self)
        self._act_cascade.triggered.connect(self._mdi.cascadeSubWindows)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self._act_open)
        file_menu.addSeparator()
        file_menu.addAction("Save metadata…", self._sidebar._export_meta)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close)

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self._act_new_view)
        view_menu.addSeparator()
        view_menu.addAction(self._act_tile)
        view_menu.addAction(self._act_cascade)

    def _create_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setStyleSheet(theme.studio_toolbar_stylesheet())
        tb.addAction(self._act_open)
        tb.addAction(self._act_new_view)

    def _on_bundle_changed(self, bundle: object) -> None:
        self._bundle = bundle if isinstance(bundle, TimeSeriesBundle) else None

    def _resolve_series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        if self._bundle is None:
            raise KeyError(name)
        return self._bundle.get_series(name)

    def _resolve_channel_unit(self, name: str) -> str:
        if self._bundle is None:
            return ""
        return self._bundle.channel_unit(name)

    def _active_shell(self) -> DataViewerShell | None:
        sub = self._mdi.activeSubWindow()
        if sub is None:
            return None
        w = sub.widget()
        return w if isinstance(w, DataViewerShell) else None

    def _new_viewer(self) -> None:
        shell = DataViewerShell(
            self._resolve_series,
            self,
            enable_walking_axis=False,
            resolve_channel_unit=self._resolve_channel_unit,
        )
        shell.viewer.channel_drop_requested.connect(self._on_channel_drop_unknown)
        sub = QMdiSubWindow(self._mdi)
        sub.setWidget(shell)
        idx = len(self._mdi.subWindowList()) + 1
        sub.setWindowTitle(f"Data viewer {idx}")
        sub.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._mdi.addSubWindow(sub)
        sub.show()
        self._mdi.setActiveSubWindow(sub)

    def _on_channel_drop_unknown(self, name: str) -> None:
        QMessageBox.warning(self, "Channel", f"Unknown channel: {name!r}. Load a matching file first.")

    def _plot_selected(self, names: list[str]) -> None:
        shell = self._active_shell()
        if shell is None:
            self._new_viewer()
            shell = self._active_shell()
        if shell is None:
            return
        v = shell.viewer
        for n in names:
            try:
                v.add_channel(n)
            except KeyError as exc:
                QMessageBox.warning(self, "Channel", f"Unknown channel: {exc}")

    def active_viewer(self) -> DataViewerShell | None:
        """Return front MDI viewer shell for API use (e.g. Studio integration)."""
        return self._active_shell()
