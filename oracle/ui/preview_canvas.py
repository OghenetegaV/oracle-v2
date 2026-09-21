"""Oracle — Drawing Preview Canvas (interface)

Purpose:
    A Tkinter canvas that draws the geometry of an oracle.application.preview.DrawingPreview and the review overlays on top of it: fit to
    window, mouse-wheel zoom about the pointer, drag to pan, and emphasis for detected views (by review state), the selected item,
    unresolved questions and engineer-approved items. A click reports which view (and observation outline) is under the pointer. It draws
    only what the preview model holds; it never reads a file and knows nothing about CAD formats.

Role in Oracle:
    The "see what Oracle is talking about" part of the architectural review screen. Drawing is level-of-detail aware (paths smaller than
    a pixel are skipped, and a very dense view is decimated) so a 24,000-entity sheet set stays usable, and it says so in the status line.

Dependencies:
    tkinter; oracle.application.preview (models only).

Consumers:
    oracle.ui.architectural_workspace, tests.

Status:
    Interface (interface phase).

Migration/Notes:
    Screen y grows downward, drawing y grows upward: the transform flips it. The view is kept as (scale, offset) in drawing units, so a
    pan or zoom never modifies anything. Text is drawn only when zoomed in enough to be legible.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from oracle.application.preview import DrawingPreview, Overlay, union_box

LINE_COLOR = "#8a94a3"
BG = "#ffffff"
STYLES = {                                  # quiet by default: the drawing is the subject, the overlays only orient
    "view_proposed": ("#c2b48c", 1, (3, 4)),
    "view_accepted": ("#7fb094", 1, ()),
    "view_rejected": ("#c98a8a", 1, (2, 4)),
    "selected": ("#2f5d9e", 3, ()),
    "unresolved": ("#c0761a", 3, ()),
    "approved": ("#2f7a50", 3, ()),
    "align_preview": ("#2f5d9e", 1, ()),        # the plan being aligned, drawn where the alignment would put it
    "split_line": ("#c0761a", 2, (6, 3)),
    "split_low": ("#2f5d9e", 2, (4, 3)),
    "split_high": ("#2f7a50", 2, (4, 3)),
    "panel": ("#2f7a50", 2, ()),                # reserved for slab panels (a later phase)
}
_POLYLINE_DASH = {"align_preview": None, "split_line": (6, 3), "split_low": (4, 3), "split_high": (4, 3), "panel": None}
STRUCTURAL, ORIGINAL = "structural", "original"
MARKER_COLORS = {"plan": "#2f5d9e", "reference": "#2f7a50"}
MAX_DRAWN = 12000


class PreviewCanvas(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, bg=BG, highlightthickness=1, highlightbackground="#c9d1d9", cursor="crosshair", **kw)
        self.preview: Optional[DrawingPreview] = None
        self.base_overlay = Overlay()          # e.g. all views by review state
        self.selection = Overlay()             # the selected item / question / issue
        # Layers, bottom to top: the drawing linework (structural review or original), the view outlines (base_overlay), a reserved STRUCTURAL
        # OVERLAY for what a later phase will interpret (slab panels, slab openings, supports, span directions: it is empty now and nothing here
        # generates it), the selection, then the point markers of a tool in use.
        self.structural = Overlay()
        self.mode = STRUCTURAL                 # "structural": furnishing and presentation clutter hidden; "original": every entity (read only)
        self.markers: list = []                # (x, y, label, role) role: plan | reference
        self.point_handler: Optional[Callable[[tuple], None]] = None      # while a tool waits for a click on the drawing
        self._hover: Optional[tuple] = None
        self.show_text = tk.BooleanVar(value=True)
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0
        self.bounds: Optional[tuple] = None
        self.on_pick: Optional[Callable[[Optional[str]], None]] = None
        self.pick_enabled = True               # "Select view" mode: a click (without dragging) reports the view under the pointer
        self.status_callback: Optional[Callable[[str], None]] = None
        self._drag = None
        self._moved = False
        self._pending = None
        self._closed = False
        self._fit_wanted: Optional[tuple] = None    # a fit asked for before the canvas had a size is done when it gets one
        self.bind("<Configure>", self._on_configure)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", lambda e: self.fit())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._clear_crosshair())

    # ---------------------------------------------------------------- model

    def set_preview(self, preview: Optional[DrawingPreview]) -> None:
        self.preview = preview
        self.bounds = self._all_bounds()
        self.fit()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._schedule()

    def set_structural_overlay(self, overlay: Overlay) -> None:
        """The reserved layer for future structural interpretation (slab panels ...). Drawn above the linework, below the selection."""
        self.structural = overlay
        self._schedule()

    def set_markers(self, markers: list) -> None:
        self.markers = list(markers)
        self._schedule()

    def begin_point_pick(self, handler: Callable[[tuple], None]) -> None:
        """A tool wants the next click on the drawing: the pointer becomes a crosshair that snaps to drawn corners and crossings."""
        self.point_handler = handler
        self.config(cursor="tcross")

    def end_point_pick(self) -> None:
        self.point_handler = None
        self._hover = None
        self.config(cursor="crosshair")
        self._clear_crosshair()

    def snapped(self, sx: float, sy: float) -> tuple:
        """The drawing point under a screen position, snapped to the nearest drawn vertex within 10 px when there is one."""
        point = self.to_drawing(sx, sy)
        if self.preview is not None:
            near = self.preview.nearest_vertex(point, 10.0 / self.scale, self.mode == STRUCTURAL)
            if near is not None:
                return near
        return point

    def _on_motion(self, event) -> None:
        if self.point_handler is None:
            return
        self._hover = self.snapped(event.x, event.y)
        self._clear_crosshair()
        sx, sy = self.to_screen(*self._hover)
        w, h = self.winfo_width(), self._h()
        color = "#2f5d9e"
        self.create_line(0, sy, w, sy, fill=color, dash=(2, 4), tags="xhair")
        self.create_line(sx, 0, sx, h, fill=color, dash=(2, 4), tags="xhair")
        self.create_oval(sx - 6, sy - 6, sx + 6, sy + 6, outline=color, width=2, tags="xhair")

    def _clear_crosshair(self) -> None:
        self.delete("xhair")

    def set_overlays(self, base: Optional[Overlay] = None, selection: Optional[Overlay] = None, *, focus: bool = False) -> None:
        if base is not None:
            self.base_overlay = base
            self.bounds = self._all_bounds()
        if selection is not None:
            self.selection = selection
        if focus and self.selection.focus:
            self.focus_on(self.selection.focus)
        else:
            self._schedule()

    def _all_bounds(self) -> Optional[tuple]:
        boxes = []
        if self.preview is not None and self.preview.bounds:
            boxes.append(self.preview.bounds)
        boxes += [r[0] for r in self.base_overlay.rects]
        return union_box(boxes)

    # ---------------------------------------------------------------- transform

    def to_screen(self, x: float, y: float) -> tuple:
        return (x - self.ox) * self.scale, self._h() - (y - self.oy) * self.scale

    def to_drawing(self, sx: float, sy: float) -> tuple:
        return sx / self.scale + self.ox, (self._h() - sy) / self.scale + self.oy

    def _h(self) -> float:
        return max(1, self.winfo_height())

    def fit(self) -> None:
        self._fit_box(self.bounds)

    def _on_configure(self, event) -> None:
        if self._fit_wanted is not None and event.width >= 50 and event.height >= 50:
            box, self._fit_wanted = self._fit_wanted, None
            self._fit_box(box)
        else:
            self._schedule()

    def _fit_box(self, box: Optional[tuple], margin: float = 0.06) -> None:
        w, h = max(1, self.winfo_width()), self._h()
        if box and (w < 50 or h < 50):
            self._fit_wanted = box                  # not laid out yet (or hidden): fit again when it is
            self._schedule()
            return
        if not box:
            self.scale, self.ox, self.oy = 1.0, 0.0, 0.0
            self._schedule()
            return
        bw, bh = max(box[2] - box[0], 1e-9), max(box[3] - box[1], 1e-9)
        self.scale = min(w / (bw * (1 + 2 * margin)), h / (bh * (1 + 2 * margin)))
        self.ox = box[0] - (w / self.scale - bw) / 2
        self.oy = box[1] - (h / self.scale - bh) / 2
        self._schedule()

    def focus_on(self, box: tuple) -> None:
        bw, bh = box[2] - box[0], box[3] - box[1]
        pad = max(bw, bh, 1.0) * 0.35
        self._fit_box((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad), margin=0.02)

    def zoom(self, factor: float, at: Optional[tuple] = None) -> None:
        sx, sy = at if at else (self.winfo_width() / 2, self._h() / 2)
        dx, dy = self.to_drawing(sx, sy)
        self.scale *= factor
        self.ox = dx - sx / self.scale
        self.oy = dy - (self._h() - sy) / self.scale
        self._schedule()

    # ---------------------------------------------------------------- events

    def _on_wheel(self, event):
        self.zoom(1.25 if event.delta > 0 else 0.8, (event.x, event.y))

    def _on_press(self, event):
        self._drag = (event.x, event.y, self.ox, self.oy)
        self._moved = False

    def _on_drag(self, event):
        if not self._drag:
            return
        x0, y0, ox, oy = self._drag
        if abs(event.x - x0) + abs(event.y - y0) > 3:
            self._moved = True
        self.ox = ox - (event.x - x0) / self.scale
        self.oy = oy + (event.y - y0) / self.scale
        self._schedule(fast=True)

    def _on_release(self, event):
        self._drag = None
        if self._moved:
            self._schedule()
            return
        if self.point_handler is not None:
            self.point_handler(self.snapped(event.x, event.y))
            return
        if self.on_pick and self.pick_enabled:
            self.on_pick(self.pick_view(*self.to_drawing(event.x, event.y)))

    def pick_view(self, x: float, y: float) -> Optional[str]:
        """The reference (view id) of the smallest overlay rectangle under a drawing point."""
        best = None
        for rect in self.base_overlay.rects:
            box, ref = rect[0], (rect[3] if len(rect) > 3 else rect[2])
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                area = (box[2] - box[0]) * (box[3] - box[1])
                if best is None or area < best[0]:
                    best = (area, ref)
        return best[1] if best else None

    # ---------------------------------------------------------------- drawing

    def cancel_pending(self) -> None:
        """Stop drawing for good (the canvas is being destroyed): cancels the queued redraw and refuses new ones."""
        self._closed = True
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None

    def _schedule(self, fast: bool = False) -> None:
        if self._closed:
            return
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(1 if fast else 15, lambda: self.redraw(fast))

    def redraw(self, fast: bool = False) -> None:
        self._pending = None
        self.delete("all")
        w, h = self.winfo_width(), self._h()
        note = ""
        if self.preview is not None and len(self.preview):
            box = (self.ox, self.oy, self.ox + w / self.scale, self.oy + h / self.scale)
            indices = self.preview.visible(box)
            drawn = skipped = hidden = 0
            structural = self.mode == STRUCTURAL
            paths = self.preview.paths
            step = 1 if len(indices) <= MAX_DRAWN or not fast else max(1, len(indices) // MAX_DRAWN)
            for n, i in enumerate(indices):
                if step > 1 and n % step:
                    continue
                if self.preview.is_hidden(i, structural):
                    hidden += 1
                    continue
                p = paths[i]
                if (p.box[2] - p.box[0]) * self.scale < 1.0 and (p.box[3] - p.box[1]) * self.scale < 1.0:
                    skipped += 1
                    continue
                coords = []
                for x, y in p.points:
                    sx, sy = self.to_screen(x, y)
                    coords += (sx, sy)
                self.create_line(*coords, fill=LINE_COLOR, width=1)
                drawn += 1
            note = (f"Structural review: {self.preview.hidden_count:,} furnishing and presentation entities hidden (the drawing is unchanged)"
                    if structural and self.preview.hidden_count else "Original drawing: every entity shown" if not structural else "")
            if self.show_text.get() and self.scale * 300 > 5:            # text only when the drawing is zoomed in enough to read it
                shown = 0
                for x, y, text, height, layer in self.preview.texts:
                    if box[0] <= x <= box[2] and box[1] <= y <= box[3] and height * self.scale >= 6 and shown < 400 and self.preview.shows_text(layer, structural):
                        sx, sy = self.to_screen(x, y)
                        self.create_text(sx, sy, text=text[:40], anchor="sw", fill="#4b5563", font=("Segoe UI", max(6, min(14, int(height * self.scale)))))
                        shown += 1
        elif self.preview is None:
            note = "No drawing linework is loaded: showing what Oracle stored."
        self._draw_overlay(self.base_overlay, thin=True)
        self._draw_overlay(self.structural)
        self._draw_overlay(self.selection)
        self._draw_markers()
        if self.status_callback:
            self.status_callback(note)

    def _draw_markers(self) -> None:
        for x, y, label, role in self.markers:
            sx, sy = self.to_screen(x, y)
            color = MARKER_COLORS.get(role, "#2f5d9e")
            self.create_oval(sx - 7, sy - 7, sx + 7, sy + 7, fill="white", outline=color, width=2)
            self.create_line(sx - 11, sy, sx + 11, sy, fill=color)
            self.create_line(sx, sy - 11, sx, sy + 11, fill=color)
            self.create_text(sx + 11, sy - 11, text=str(label), anchor="sw", fill=color, font=("Segoe UI", 10, "bold"))

    def _draw_overlay(self, overlay: Overlay, thin: bool = False) -> None:
        for rect in overlay.rects:
            box, style, label = rect[:3]
            color, width, dash = STYLES.get(style, STYLES["selected"])
            (x0, y0), (x1, y1) = self.to_screen(box[0], box[3]), self.to_screen(box[2], box[1])
            self.create_rectangle(x0, y0, x1, y1, outline=color, width=width if not thin else max(1, width - 1), dash=dash or None)
            if not thin and label:
                self.create_text(x0 + 4, y0 + 2, text=label, anchor="nw", fill=color, font=("Segoe UI", 9, "bold"))
        if self.preview is not None and overlay.entity_ids:
            color, width, _dash = STYLES.get(overlay.rects[0][1] if overlay.rects else "selected", STYLES["selected"])
            for i in self.preview.paths_for(overlay.entity_ids)[:2000]:
                coords = []
                for x, y in self.preview.paths[i].points:
                    coords += self.to_screen(x, y)
                self.create_line(*coords, fill=color, width=width + 1)
        for pts, style in overlay.polylines:
            color, width, _dash = STYLES.get(style, STYLES["selected"])
            coords = []
            for x, y in pts:
                coords += self.to_screen(x, y)
            if len(coords) >= 4:
                self.create_line(*coords, fill=color, width=width, dash=_POLYLINE_DASH.get(style, (2, 2)))
            elif len(coords) == 2:
                self.create_oval(coords[0] - 4, coords[1] - 4, coords[0] + 4, coords[1] + 4, outline=color, width=width)
