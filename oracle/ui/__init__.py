"""Oracle — User Interface Package

Purpose:
    The Tkinter interface pieces that are not the legacy wizard itself: the Architectural Drawing workspace (choose a DWG/DXF, watch Oracle
    interpret it, review and decide), its review tabs, drawing preview canvas, dialogs and theme.

Role in Oracle:
    Presentation only. Everything the interface shows or does goes through oracle.application.ArchitecturalSession; this package imports no CAD
    library and holds no model of its own. Importing the package does not import tkinter, so headless code can still import oracle.

Dependencies:
    None at import time (modules import tkinter and oracle.application when used).

Consumers:
    oracle_wizard (lazily, when the engineer chooses the Architectural Drawing workflow), tests.

Status:
    Interface (interface phase).

Migration/Notes:
    oracle.ui may depend on oracle.application; oracle.application and below must never depend on oracle.ui.
"""
