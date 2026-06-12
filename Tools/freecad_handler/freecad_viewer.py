#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Open a completed Xenon FreeCAD model without blocking the Xenon worker."""

from __future__ import annotations

import sys
from pathlib import Path


def _model_path(arguments):
    for value in reversed(arguments):
        path = Path(str(value).strip().strip('"'))
        if path.suffix.lower() == ".fcstd" and path.is_file():
            return path.resolve()
    raise FileNotFoundError("No existing FCStd model path was provided")


def _hide_noisy_panels(Gui, QtCore, QtWidgets):
    def hide():
        main_window = Gui.getMainWindow()
        for dock in main_window.findChildren(QtWidgets.QDockWidget):
            if dock.objectName() in {"Report view", "Python console"}:
                dock.hide()

    hide()
    QtCore.QTimer.singleShot(250, hide)
    QtCore.QTimer.singleShot(1000, hide)


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

    source = _model_path(sys.argv[1:])
    if App.activeDocument() is None or Path(App.activeDocument().FileName or "").resolve() != source:
        App.openDocument(str(source))
    Gui.activeDocument().activeView().viewAxonometric()
    Gui.activeDocument().activeView().fitAll()
    _hide_noisy_panels(Gui, QtCore, QtWidgets)
    Gui.getMainWindow().statusBar().showMessage(
        "Xenon drawing completed. This viewer is independent; you can continue chatting with Xenon."
    )


if __name__ == "__main__":
    main()
