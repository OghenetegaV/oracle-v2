"""Oracle — Evidence & Details Window (interface)

Purpose:
    The technical view behind the calm review screen, opened on request ("Evidence & Details" or "Review evidence" on an item): what Oracle
    read from the drawing, every view, level, observation and question with its evidence and confidence, the issues, the approved
    architecture, the engineer's own input and the full decision history. It is where an engineer investigates WHY Oracle concluded something
    and where the less common operations (rename, align, merge, split, edit a level) live.

Role in Oracle:
    Progressive disclosure: none of this is needed to finish a review, all of it stays available. The window holds the existing ReviewPanels
    and asks the workspace (its host) for everything; it owns no data. Closing it changes nothing.

Dependencies:
    tkinter; oracle.ui.review_panels, theme.

Consumers:
    oracle.ui.architectural_workspace.

Status:
    Interface (interface refinement phase).

Migration/Notes:
    The window is modeless so the drawing preview stays usable beside it; selecting a row in it highlights the item on the preview.
"""

from __future__ import annotations

import tkinter as tk

from . import theme
from .review_panels import ReviewPanels


class DetailsWindow(tk.Toplevel):
    def __init__(self, workspace):
        super().__init__(workspace)
        self.ws = workspace
        self.title("Evidence & Details")
        self.configure(bg=theme.BG)
        self.geometry("980x700")
        self.minsize(760, 520)
        head = tk.Frame(self, bg=theme.PANEL, highlightthickness=1, highlightbackground=theme.LINE)
        head.pack(fill="x")
        tk.Label(head, text="Evidence & Details", font=theme.H2, bg=theme.PANEL, fg=theme.INK).pack(side="left", padx=16, pady=(10, 2))
        tk.Label(head, text="Technical detail behind Oracle's conclusions: evidence, confidence, identifiers, and the full decision history.",
                 font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED).pack(side="left", padx=4, pady=(14, 2))
        self.panels = ReviewPanels(self, workspace)
        self.panels.pack(fill="both", expand=True, padx=10, pady=10)
        self.protocol("WM_DELETE_WINDOW", workspace.close_details)
