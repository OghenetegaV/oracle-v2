"""Oracle — Point-Pick Tools: Align Plan and Split View (interface)

Purpose:
    Two small, guided drawing tools that replace typing numbers. ALIGN PLAN: the engineer clicks a point on the plan being aligned and the same
    physical place on a reference plan (a grid crossing, column, building corner); a second pair also turns the plan. Oracle previews where the
    plan would land, and nothing is recorded until the engineer presses "Accept Alignment". SPLIT VIEW: used when Oracle grouped one region as a
    single view but it really holds several; the engineer clicks where the region divides, sees the two resulting parts, and accepts.

Role in Oracle:
    Interaction only. The geometry is computed by oracle.application (alignment.solve, session.split_preview, read-only); accepting calls the
    session, which records ONE engineer decision through the ordinary mechanism. Cancelling, picking again or closing leaves the project untouched;
    the source coordinates are never modified. The numeric forms remain in Evidence & Details for engineers who want exact values.

Dependencies:
    oracle.application (session calls, Overlay); oracle.ui.theme.

Consumers:
    oracle.ui.architectural_workspace.

Status:
    Interface (interface refinement, quick build).

Migration/Notes:
    A tool owns the canvas' point-pick mode while it is active: a crosshair that snaps to drawn corners and crossings, numbered markers for the
    picked points, and an instruction banner above the drawing.
"""

from __future__ import annotations

from typing import Optional

from oracle.application import ActionRefused, Overlay
from oracle.application.alignment import describe

AXIS_CHOICES = (("x", "A vertical line: the parts are left and right"), ("y", "A horizontal line: the parts are top and bottom"))


class PointTool:
    """Common behaviour: own the canvas pick mode, show the panel and banner, finish cleanly."""

    def __init__(self, ws, view_id: str):
        self.ws, self.s, self.view_id = ws, ws.session, view_id
        self.canvas, self.screen = ws.canvas, ws.review
        self.name = self.s.view_name(view_id)
        self.active = False

    def begin(self) -> bool:
        self.active = True
        self.canvas.begin_point_pick(self.on_point)
        self.render()
        return True

    def finish(self) -> None:
        self.active = False
        self.canvas.end_point_pick()
        self.canvas.set_markers([])
        self.canvas.set_overlays(selection=Overlay())
        self.screen.set_banner(None)

    def cancel(self) -> None:
        self.finish()
        self.ws.tool = None
        self.back_to_card()

    def back_to_card(self) -> None:
        if self.screen.current_key:
            self.ws._show_key(self.screen.current_key)

    def inside(self, point: tuple, box: tuple) -> bool:
        pad = 0.02 * max(box[2] - box[0], box[3] - box[1])
        return box[0] - pad <= point[0] <= box[2] + pad and box[1] - pad <= point[1] <= box[3] + pad

    def on_point(self, point: tuple) -> None:            # pragma: no cover - overridden
        raise NotImplementedError

    def render(self) -> None:                            # pragma: no cover - overridden
        raise NotImplementedError


class AlignTool(PointTool):
    def __init__(self, ws, view_id: str):
        super().__init__(ws, view_id)
        self.choices = self.s.plan_choices(view_id)
        self.ref_id: Optional[str] = self.choices[0][0] if self.choices else None
        self.pairs: list = []
        self.pending: Optional[tuple] = None
        self.state = "plan"                              # plan -> reference -> preview
        self.want = 1                                    # how many point pairs this alignment will use (1: move; 2: move and turn)
        self.solution = None

    def begin(self) -> bool:
        if not self.choices:
            self.ws.prompts.info("Nothing to align to", "There is no other floor plan in this drawing to align this plan to.")
            return False
        return super().begin()

    @property
    def ref_name(self) -> str:
        return dict(self.choices)[self.ref_id]

    # ---- clicks

    def on_point(self, point: tuple) -> None:
        if self.state == "plan":
            if not self.inside(point, self.s.view_box(self.view_id)):
                self.ws.flash(f"Click a point on {self.name} (highlighted).", error=True)
                return
            self.pending, self.state = point, "reference"
        elif self.state == "reference":
            if not self.inside(point, self.s.view_box(self.ref_id)):
                self.ws.flash(f"Click the matching point on {self.ref_name} (highlighted).", error=True)
                return
            trial = self.pairs + [(self.pending, point)]
            try:
                self.solution = self.s.alignment_preview(self.view_id, self.ref_id, trial)
            except ActionRefused as exc:
                self.ws.flash(str(exc), error=True)
                self.state = "plan" if len(trial) == 2 else "plan"
                self.pending = None
                self.render()
                return
            self.pairs, self.pending, self.state = trial, None, "preview"
        self.render()

    # ---- buttons

    def add_second_pair(self) -> None:
        self.want, self.state, self.pending = 2, "plan", None
        self.render()

    def pick_again(self) -> None:
        self.pairs, self.pending, self.state, self.want, self.solution = [], None, "plan", 1, None
        self.render()

    def choose_reference(self, label: str) -> None:
        for rid, name in self.choices:
            if name == label:
                self.ref_id = rid
        self.render()

    def accept(self) -> None:
        done = self.ws._act(lambda: self.s.apply_alignment(self.view_id, self.ref_id, self.pairs), "Alignment recorded: the source coordinates are unchanged.")
        if done is not None:
            self.finish()
            self.ws.tool = None
            self.back_to_card()

    # ---- what the engineer sees

    def render(self) -> None:
        pair_no = len(self.pairs) + (0 if self.state == "preview" else 1)
        step = 2 * len(self.pairs) + (1 if self.state == "plan" else 2)
        total = 2 * self.want
        if self.state == "preview":
            title, banner = "Preview alignment", f"Preview: {self.name} shown where the alignment would place it on {self.ref_name}."
            lines = [(t, "warn" if "scale" in t else "text") for t in describe(self.solution)]
            lines.append(("Nothing is recorded until you accept. Your drawing is not changed.", "muted"))
            buttons = [("Accept Alignment", "primary", self.accept)]
            if len(self.pairs) == 1:
                buttons.append(("Add second point pair (also turns the plan)", "secondary", self.add_second_pair))
            buttons += [("Pick Again", "secondary", self.pick_again), ("Cancel", "link", self.cancel)]
            self.screen.show_tool_panel("Align plan", title, lines, buttons)
        else:
            on_plan = self.state == "plan"
            banner = (f"Click a matching point (a grid intersection, column or building corner) on {self.name}." if on_plan
                      else f"Now click the same point on {self.ref_name}.")
            if on_plan and self.want == 2:
                banner = f"Second pair: click another matching point on {self.name}, far from the first."
            lines = [(f"Align:  {self.name}", "text"), (f"To:  {self.ref_name}", "text"),
                     (("Click a point on the plan being aligned." if on_plan else "Now click the corresponding point on the reference plan."), "muted"),
                     ("Snaps to drawn corners and crossings.", "muted")]
            options = None
            if on_plan and not self.pairs and len(self.choices) > 1:
                options = ("Align to", [name for _i, name in self.choices], self.ref_name, self.choose_reference)
            self.screen.show_tool_panel(f"Align plan — step {step} of {total}", f"Step {step} of {total}", lines, [("Cancel", "link", self.cancel)], options)
        self.screen.set_banner(banner)
        self._draw()

    def _draw(self) -> None:
        markers = []
        for n, (p, q) in enumerate(self.pairs, 1):
            markers += [(p[0], p[1], n, "plan"), (q[0], q[1], n, "reference")]
        if self.pending is not None:
            markers.append((self.pending[0], self.pending[1], len(self.pairs) + 1, "plan"))
        overlay = Overlay()
        if self.state == "preview" and self.solution is not None:
            box = self.s.view_box(self.ref_id)
            overlay.rects.append((box, "selected", self.ref_name, self.ref_id))
            overlay.polylines = [(line, "align_preview") for line in self.solution.moved]
            overlay.focus = box
            markers = [(q[0], q[1], n, "reference") for n, (_p, q) in enumerate(self.pairs, 1)]
        else:
            target = self.view_id if self.state == "plan" else self.ref_id
            overlay = self.s.overlay_for_object(target)
        self.canvas.set_markers(markers)
        self.canvas.set_overlays(selection=overlay, focus=True)


class SplitTool(PointTool):
    def __init__(self, ws, view_id: str):
        super().__init__(ws, view_id)
        self.axis = "x"
        self.point: Optional[tuple] = None
        self.preview: Optional[dict] = None

    def on_point(self, point: tuple) -> None:
        if not self.inside(point, self.s.view_box(self.view_id)):
            self.ws.flash(f"Click inside {self.name} (highlighted), where it should be divided.", error=True)
            return
        coordinate = point[0] if self.axis == "x" else point[1]
        try:
            self.preview = self.s.split_preview(self.view_id, self.axis, coordinate)
        except ActionRefused as exc:
            self.ws.flash(str(exc), error=True)
            return
        self.point, self.coordinate = point, coordinate
        self.render()

    def choose_axis(self, label: str) -> None:
        self.axis = next(a for a, text in AXIS_CHOICES if text == label)
        self.point = self.preview = None
        self.render()

    def pick_again(self) -> None:
        self.point = self.preview = None
        self.render()

    def accept(self) -> None:
        done = self.ws._act(lambda: self.s.split_view(self.view_id, self.axis, self.coordinate, "Divided where the engineer clicked."),
                            "View split into two: the source drawing is unchanged.")
        if done is not None:
            self.finish()
            self.ws.tool = None
            self.back_to_card()

    def render(self) -> None:
        explain = ("Use Split View when Oracle has treated one region of the drawing as a single view, but you know it holds more than one — "
                   "for example a sheet with a ground floor plan and a first floor plan side by side.")
        if self.preview is None:
            options = ("Divide with", [text for _a, text in AXIS_CHOICES], dict(AXIS_CHOICES)[self.axis], self.choose_axis)
            lines = [(explain, "muted"), (f"View:  {self.name}", "text"), ("Click the place where the two views should be divided.", "text")]
            self.screen.show_tool_panel("Split view — step 1 of 2", "Where should it be divided?", lines, [("Cancel", "link", self.cancel)], options)
            self.screen.set_banner(f"Click where {self.name} should be divided.")
            overlay = self.s.overlay_for_object(self.view_id)
        else:
            a, b = self.preview["counts"]
            lines = [(f"Part 1: {a} drawing items", "text"), (f"Part 2: {b} drawing items", "text"),
                     ("The parts will be two separate views; the original view stays on record as split. The drawing itself is not changed.", "muted")]
            self.screen.show_tool_panel("Split view — step 2 of 2", "Preview the split", lines,
                                        [("Accept Split", "primary", self.accept), ("Pick Again", "secondary", self.pick_again), ("Cancel", "link", self.cancel)])
            self.screen.set_banner("Preview: the dividing line and the two resulting views.")
            overlay = Overlay()
            overlay.rects = [(self.preview["low"], "split_low", "Part 1", ""), (self.preview["high"], "split_high", "Part 2", "")]
            overlay.polylines = [(self.preview["line"], "split_line")]
            overlay.focus = self.s.view_box(self.view_id)
        self.canvas.set_markers([(self.point[0], self.point[1], "✕", "plan")] if self.point else [])
        self.canvas.set_overlays(selection=overlay, focus=True)
