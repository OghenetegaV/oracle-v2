"""Oracle — Wizard (Tkinter GUI)

Purpose:
    Eight-step guided workflow from drawing to detail drawing: drawing selection, checks,
    questions, layout, analysis, design and drawing output, with notes and an Ask-Claude chat.

Role in Oracle:
    The current application entry point. It calls every legacy module and holds the whole
    project state in one self.data dict (no oracle.core objects yet).

Dependencies:
    tkinter, config, oracle_log, dxf_parser, ga_dxf_parser, claude_ga_generator, ga_sketch,
    ml_sketch, staad_v8i_integration, staad_mock, design_module, dwg_detail_generator,
    lisp_detail_generator; anthropic (chat).

Consumers:
    Started by 'Launch Oracle.bat' (pythonw); not imported by other modules.

Status:
    Legacy / Transitional (the working application).

Migration:
    Retained unchanged. Per docs/PHASE_1_ARCHITECTURE.md it will later populate an OracleProject
    alongside self.data, and only afterwards be reworked.

Details (original module notes, retained):
    Oracle Wizard — a friendly, no-terminal, step-by-step guide from an
    architectural drawing to a finished structural detail drawing.

    Launch by double-clicking "Launch Oracle.bat", or running:
        pythonw oracle_wizard.py
    No command-line knowledge needed beyond that.
"""

import os
import sys

# Running via pythonw.exe (no console window -- the friendliest launch method)
# leaves sys.stdout/sys.stderr as None. Every pipeline module below prints
# progress messages (some with unicode symbols); redirect those safely
# instead of touching every print() call across the codebase.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

import json
import queue
import shutil
import subprocess
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from config import (
    INPUT_DIR,
    ODA_CONVERTER_PATH,
    OUTPUT_JSON_DIR,
    PROJECT_ROOT,
    get_api_key,
    save_api_key,
)

STANDARDS_PATH = PROJECT_ROOT / "company_standards.json"

OCCUPANCY_TABLE = {
    "Homes / residential rooms": 1.5,
    "Offices (general use)": 2.5,
    "Classrooms / institutional": 3.0,
    "Shops / retail floors": 4.0,
    "Assembly areas (fixed seating)": 4.0,
    "Storage areas (light)": 5.0,
}

# BS 6399-3 roof-access categories, not room-use -- offered only for the top level of
# a multi-floor import, instead of applying an office/residential load to a roof.
ROOF_OCCUPANCY_TABLE = {
    "Roof - no access (maintenance only)": 0.75,
    "Roof - occasional access": 1.5,
}

MATERIAL_OPTIONS = ["Concrete (BS 8110)", "Steel (BS 5950)"]
STEEL_GRADE_OPTIONS = ["S275", "S355"]
CONCRETE_GRADE_OPTIONS = ["C25/30 (fcu=25 N/mm2)", "C28/35 (fcu=28 N/mm2)",
                          "C32/40 (fcu=32 N/mm2)", "C35/45 (fcu=35 N/mm2)", "C40/50 (fcu=40 N/mm2)"]
EXPOSURE_OPTIONS = ["Mild", "Moderate", "Severe"]


def _material_key(label):
    return "steel" if label.startswith("Steel") else "concrete"

STEP_TITLES = [
    "Welcome", "Select drawing", "Check what we found", "A few quick questions",
    "Structural layout", "Structural analysis", "Element design", "Detail drawing",
]

FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_SUBTITLE = ("Segoe UI", 11)
FONT_BODY = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
COLOR_BG = "#f4f6f8"
COLOR_ACCENT = "#1f6feb"
COLOR_MUTED = "#6b7280"


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Oracle — Structural Design Assistant")
        self.geometry("820x600")
        self.minsize(760, 560)
        self.configure(bg=COLOR_BG)

        self.data = {
            "dxf_filename": None,
            "geometry": None,
            "storey_height_m": 3.0,
            "occupancy": "Offices (general use)",
            "slab_thickness_mm": 175,
            "column_material": "concrete",
            "beam_material": "concrete",
            "steel_grade": "S275",
            "concrete_grade": CONCRETE_GRADE_OPTIONS[0],
            "exposure_class": "Mild",
            "engineer_notes": "",
            "element_notes": {},
            "chat_instructions": [],
            "ga": None,
            "forces": None,
            "design": None,
            "used_real_staad": False,
        }
        self.step_index = 0
        self.result_queue = queue.Queue()
        self.chat_messages = []  # persists across opens/closes of the chat window, for the session
        self.chat_window = None
        self.notes_window = None
        # Separate from result_queue: the chat can be used while a wizard step's own
        # async work (GA/design generation, analysis) is in flight, and sharing one
        # queue would let the two consume each other's results.
        self.chat_queue = queue.Queue()

        self._build_chrome()
        self.after(100, self._maybe_ask_for_api_key)
        self.show_step(0)

    # ---------- chrome (header/footer shared across every screen) ----------

    def _build_chrome(self):
        header = tk.Frame(self, bg="white", height=70)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Oracle", font=("Segoe UI", 16, "bold"),
                 bg="white", fg=COLOR_ACCENT).pack(side="left", padx=20, pady=15)
        self.step_label = tk.Label(header, text="", font=FONT_SMALL, bg="white", fg=COLOR_MUTED)
        self.step_label.pack(side="right", padx=20)

        # Always available, from any step -- not part of the linear flow.
        self.chat_btn = tk.Button(header, text="\U0001F4AC Ask Claude", command=self._open_chat_window,
                                   font=FONT_SMALL, relief="flat", padx=10, pady=5, cursor="hand2")
        self.chat_btn.pack(side="right", padx=6)
        self.notes_btn = tk.Button(header, text="\U0001F4DD Layout & Notes", command=self._open_notes_window,
                                    font=FONT_SMALL, relief="flat", padx=10, pady=5, cursor="hand2",
                                    state="disabled")
        self.notes_btn.pack(side="right", padx=6)

        # Every step's content goes in self.content, same as before -- but that now
        # lives inside a scrollable canvas rather than being packed straight into the
        # window. Without this, a step whose content is taller than the window (a big
        # sketch image, a long warning list) had nowhere to go: the content frame had
        # no size cap, so the whole window grew to fit it, and on a screen too short
        # to show the grown window, the footer (with Next) was pushed off-screen
        # entirely -- unreachable, not just hidden. A fixed-size scrollable area
        # caps the window's height and makes the overflow a scrollbar instead.
        content_area = tk.Frame(self, bg=COLOR_BG)
        content_area.pack(side="top", fill="both", expand=True)
        self.content_canvas = tk.Canvas(content_area, bg=COLOR_BG, highlightthickness=0)
        content_scrollbar = ttk.Scrollbar(content_area, orient="vertical", command=self.content_canvas.yview)
        self.content = tk.Frame(self.content_canvas, bg=COLOR_BG)
        content_window = self.content_canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", lambda e: self.content_canvas.configure(
            scrollregion=self.content_canvas.bbox("all")))
        self.content_canvas.bind("<Configure>", lambda e: self.content_canvas.itemconfig(
            content_window, width=e.width))
        self.content_canvas.configure(yscrollcommand=content_scrollbar.set)

        def _on_mousewheel(event):
            self.content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        # Bound only while the cursor is actually over the content area (not bind_all
        # for the app's lifetime) so scrolling a dialog or the chat window doesn't
        # instead scroll this hidden-behind-it canvas.
        self.content_canvas.bind("<Enter>", lambda e: self.content_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.content_canvas.bind("<Leave>", lambda e: self.content_canvas.unbind_all("<MouseWheel>"))

        self.content_canvas.pack(side="left", fill="both", expand=True, padx=(30, 6), pady=20)
        content_scrollbar.pack(side="right", fill="y", pady=20)

        footer = tk.Frame(self, bg="white", height=60)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.status_label = tk.Label(footer, text="", font=FONT_SMALL, bg="white", fg=COLOR_MUTED)
        self.status_label.pack(side="left", padx=20)
        self.next_btn = tk.Button(footer, text="Next →", command=self._on_next,
                                   bg=COLOR_ACCENT, fg="white", font=FONT_BODY,
                                   relief="flat", padx=16, pady=6, cursor="hand2")
        self.next_btn.pack(side="right", padx=20, pady=12)
        self.back_btn = tk.Button(footer, text="← Back", command=self._on_back,
                                   font=FONT_BODY, relief="flat", padx=16, pady=6, cursor="hand2")
        self.back_btn.pack(side="right", padx=4, pady=12)

    def clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def _show_out_of_order_message(self):
        tk.Label(self.content, text="Please complete the previous step first.",
                 font=FONT_SUBTITLE, bg=COLOR_BG, fg="#b91c1c").pack(anchor="w", pady=20)
        self.back_btn.config(state="normal")

    # ================= Layout & Notes (available from any step) =================
    # Per-element instructions ("thicken this slab", "check this column again")
    # attached by clicking an element on the plan, independent of which wizard
    # step you're currently on. Stored in self.data["element_notes"] and folded
    # into the next layout/design regeneration's prompt.

    def _open_notes_window(self):
        if self.data["ga"] is None:
            messagebox.showinfo("Not yet available",
                                 "The layout doesn't exist yet -- this becomes available after "
                                 "Step 4 generates one.")
            return
        if self.notes_window is not None and self.notes_window.winfo_exists():
            self.notes_window.deiconify()
            self.notes_window.lift()
            self._redraw_notes_canvas()
            return

        win = tk.Toplevel(self)
        win.title("Layout & Notes")
        win.geometry("780x680")
        self.notes_window = win

        tk.Label(win, text="Click a column, beam, or slab to add a note for it",
                 font=FONT_SUBTITLE, bg=COLOR_BG).pack(anchor="w", padx=15, pady=(15, 5))
        tk.Label(win, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG, wraplength=740, justify="left",
                 text="Elements with a note already on them are marked with an orange dot. Notes "
                      "are applied the next time you regenerate the layout (Step 4) or the design "
                      "(Step 6) -- add them any time, from any step.").pack(anchor="w", padx=15)

        self.notes_canvas = tk.Canvas(win, bg="white", highlightthickness=1,
                                       highlightbackground="#d1d5db")
        self.notes_canvas.pack(fill="both", expand=True, padx=15, pady=15)
        self.notes_canvas.bind("<Button-1>", self._on_notes_canvas_click)
        self.notes_canvas.bind("<Configure>", lambda e: self._redraw_notes_canvas())

        list_frame = tk.Frame(win, bg=COLOR_BG)
        list_frame.pack(fill="x", padx=15, pady=(0, 15))
        tk.Label(list_frame, text="Current notes:", font=FONT_BODY, bg=COLOR_BG).pack(anchor="w")
        self.notes_list_label = tk.Label(list_frame, font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED,
                                          justify="left", wraplength=740)
        self.notes_list_label.pack(anchor="w")
        self._refresh_notes_list_label()

    def _element_geometry(self):
        """Screen-space layout of every column/beam/slab, computed fresh each
        draw so the canvas can be resized freely."""
        ga = self.data["ga"]
        cols = ga.get("columns", [])
        if not cols:
            return None
        xs = [c["x"] for c in cols]
        ys = [c["y"] for c in cols]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        span_x = max(x_max - x_min, 1)
        span_y = max(y_max - y_min, 1)

        canvas_w = max(self.notes_canvas.winfo_width(), 200)
        canvas_h = max(self.notes_canvas.winfo_height(), 200)
        margin = 60
        scale = min((canvas_w - 2 * margin) / span_x, (canvas_h - 2 * margin) / span_y)

        def to_px(x, y):
            px = margin + (x - x_min) * scale
            py = canvas_h - (margin + (y - y_min) * scale)  # flip Y (screen Y grows downward)
            return px, py

        return ga, cols, to_px

    def _redraw_notes_canvas(self):
        canvas = self.notes_canvas
        canvas.delete("all")
        geom = self._element_geometry()
        if geom is None:
            return
        ga, cols, to_px = geom
        col_positions = {c["name"]: (c["x"], c["y"]) for c in cols}
        notes = self.data["element_notes"]

        for slab in ga.get("slabs", []):
            verts = slab.get("vertices")
            if not verts:
                continue
            pts = [to_px(*v) for v in verts]
            flat = [coord for pt in pts for coord in pt]
            tag = f"elem_slab_{slab['name']}"
            canvas.create_polygon(flat, fill="#dbeafe", outline="#93c5fd", tags=("element", tag))
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            canvas.create_text(cx, cy, text=slab["name"], font=("Segoe UI", 8), fill="#1e3a8a",
                                tags=("element", tag))
            if notes.get(slab["name"]):
                canvas.create_oval(cx - 4, cy - 10, cx + 4, cy - 2, fill="#f97316", outline="",
                                    tags=("element", tag))

        for beam in ga.get("beams", []):
            p1 = col_positions.get(beam.get("start_col"))
            p2 = col_positions.get(beam.get("end_col"))
            if not p1 or not p2:
                continue
            x1, y1 = to_px(*p1)
            x2, y2 = to_px(*p2)
            tag = f"elem_beam_{beam['name']}"
            canvas.create_line(x1, y1, x2, y2, width=4, fill="#1f6feb", capstyle="round",
                                tags=("element", tag))
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            canvas.create_text(mx, my - 10, text=beam["name"], font=("Segoe UI", 8), fill="#1f6feb",
                                tags=("element", tag))
            if notes.get(beam["name"]):
                canvas.create_oval(mx - 4, my - 22, mx + 4, my - 14, fill="#f97316", outline="",
                                    tags=("element", tag))

        for col in cols:
            x, y = to_px(col["x"], col["y"])
            tag = f"elem_column_{col['name']}"
            box = 10
            canvas.create_rectangle(x - box, y - box, x + box, y + box, fill="#111827",
                                     outline="", tags=("element", tag))
            canvas.create_text(x, y - box - 10, text=col["name"], font=("Segoe UI", 8, "bold"),
                                fill="#111827", tags=("element", tag))
            if notes.get(col["name"]):
                canvas.create_oval(x + box - 2, y - box - 2, x + box + 8, y - box + 8,
                                    fill="#f97316", outline="white", width=1, tags=("element", tag))

    def _on_notes_canvas_click(self, event):
        canvas = self.notes_canvas
        clicked = canvas.find_closest(event.x, event.y)
        if not clicked:
            return
        tags = canvas.gettags(clicked[0])
        elem_tag = next((t for t in tags if t.startswith("elem_")), None)
        if not elem_tag:
            return
        _, kind, name = elem_tag.split("_", 2)
        self._prompt_element_note(kind, name)

    def _prompt_element_note(self, kind, name):
        dialog = tk.Toplevel(self)
        dialog.title(f"Note for {name}")
        dialog.geometry("420x260")
        dialog.transient(self.notes_window or self)
        dialog.grab_set()
        tk.Label(dialog, text=f"{kind.capitalize()} {name}", font=FONT_SUBTITLE).pack(pady=(15, 5))
        tk.Label(dialog, font=FONT_SMALL, fg=COLOR_MUTED, wraplength=380,
                 text="This is folded into the prompt the next time the layout or design for "
                      "this element is (re)generated.").pack(padx=15)
        text = tk.Text(dialog, width=45, height=6, font=FONT_BODY, wrap="word")
        text.insert("1.0", self.data["element_notes"].get(name, ""))
        text.pack(padx=15, pady=10)
        text.focus_set()

        def save():
            value = text.get("1.0", "end").strip()
            if value:
                self.data["element_notes"][name] = value
            else:
                self.data["element_notes"].pop(name, None)
            dialog.destroy()
            self._redraw_notes_canvas()
            self._refresh_notes_list_label()

        def clear():
            self.data["element_notes"].pop(name, None)
            dialog.destroy()
            self._redraw_notes_canvas()
            self._refresh_notes_list_label()

        btns = tk.Frame(dialog)
        btns.pack(pady=5)
        tk.Button(btns, text="Save note", command=save, bg=COLOR_ACCENT, fg="white",
                  relief="flat", padx=12, pady=5).pack(side="left", padx=5)
        tk.Button(btns, text="Clear note", command=clear, relief="flat", padx=12, pady=5).pack(
            side="left", padx=5)

    def _refresh_notes_list_label(self):
        notes = self.data["element_notes"]
        if not notes:
            self.notes_list_label.config(text="(none yet)")
        else:
            self.notes_list_label.config(
                text="\n".join(f"• {name}: {note}" for name, note in notes.items()))

    # ================= Ask Claude (persistent chat, available from any step) =================

    def _open_chat_window(self):
        if self.chat_window is not None and self.chat_window.winfo_exists():
            self.chat_window.deiconify()
            self.chat_window.lift()
            return
        if not get_api_key():
            self._maybe_ask_for_api_key()

        win = tk.Toplevel(self)
        win.title("Ask Claude")
        win.geometry("500x640")
        win.configure(bg=COLOR_BG)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)  # hide, don't destroy -- keeps history alive
        self.chat_window = win

        header = tk.Frame(win, bg=COLOR_BG)
        header.pack(fill="x", padx=15, pady=(15, 0))
        tk.Label(header, text="Ask Claude anything about this project", font=FONT_SUBTITLE,
                 bg=COLOR_BG).pack(side="left")
        tk.Button(header, text="View log file", command=self._open_log_file, font=FONT_SMALL,
                  relief="flat", padx=8, pady=2, cursor="hand2").pack(side="right")
        tk.Label(win, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG, wraplength=460, justify="left",
                 text="Claude can see what's been generated so far, plus recent errors and log "
                      "entries -- ask it to help troubleshoot something that went wrong. Anything "
                      "you say here also carries forward into later layout/design steps.").pack(
            anchor="w", padx=15, pady=(4, 10))

        self.chat_history_text = tk.Text(win, state="disabled", wrap="word", font=FONT_BODY,
                                          bg="white", relief="solid", borderwidth=1, padx=4, pady=4,
                                          cursor="arrow")
        self.chat_history_text.pack(fill="both", expand=True, padx=15, pady=(0, 10))
        # Bubble-style paragraphs: a colored background block per speaker, not just
        # colored text, so a message's origin is obvious at a glance.
        self.chat_history_text.tag_configure(
            "user_bubble", background="#e0e7ff", lmargin1=8, lmargin2=8, rmargin=70,
            spacing1=6, spacing3=10, wrap="word")
        self.chat_history_text.tag_configure(
            "claude_bubble", background="#dbeafe", lmargin1=70, lmargin2=70, rmargin=8,
            spacing1=6, spacing3=10, wrap="word")
        self.chat_history_text.tag_configure(
            "user_speaker", foreground="#3730a3", font=("Segoe UI", 9, "bold"))
        self.chat_history_text.tag_configure(
            "claude_speaker", foreground="#1e40af", font=("Segoe UI", 9, "bold"))
        self.chat_history_text.tag_configure("pending", foreground=COLOR_MUTED, font=("Segoe UI", 9, "italic"))

        input_row = tk.Frame(win, bg=COLOR_BG)
        input_row.pack(fill="x", padx=15, pady=(0, 15))
        self.chat_input = tk.Text(input_row, height=3, font=FONT_BODY, wrap="word")
        self.chat_input.pack(side="left", fill="x", expand=True)
        self.chat_input.bind("<Return>", self._on_chat_send_keypress)
        self.chat_send_btn = tk.Button(input_row, text="Send", command=self._send_chat_message,
                                        bg=COLOR_ACCENT, fg="white", relief="flat", padx=14)
        self.chat_send_btn.pack(side="left", padx=(8, 0), fill="y")

        self._render_chat_history()

    def _open_log_file(self):
        try:
            from oracle_log import LOG_PATH
            if not LOG_PATH.exists():
                LOG_PATH.write_text("(no log entries yet)\n", encoding="utf-8")
            os.startfile(LOG_PATH)
        except Exception as e:
            messagebox.showerror("Couldn't open log", str(e))

    def _on_chat_send_keypress(self, event):
        if not (event.state & 0x0001):  # plain Enter sends; Shift+Enter inserts a newline
            self._send_chat_message()
            return "break"

    def _render_chat_history(self):
        t = self.chat_history_text
        t.config(state="normal")
        t.delete("1.0", "end")
        for msg in self.chat_messages:
            is_user = msg["role"] == "user"
            bubble = "user_bubble" if is_user else "claude_bubble"
            speaker_tag = "user_speaker" if is_user else "claude_speaker"
            speaker = "You" if is_user else "Claude"
            t.insert("end", f"{speaker}\n", (speaker_tag, bubble))
            t.insert("end", f"{msg['content']}\n", (bubble,))
            t.insert("end", "\n")
        t.config(state="disabled")
        t.see("end")

    def _project_context_summary(self):
        lines = []
        if self.data["geometry"]:
            g = self.data["geometry"]
            lines.append(f"Architectural drawing parsed: {len(g['walls'])} walls, "
                         f"{len(g['columns'])} columns, {len(g['gridlines'])} gridlines.")
        if self.data["ga"]:
            ga = self.data["ga"]
            lines.append(f"Structural layout: {len(ga.get('columns', []))} columns, "
                         f"{len(ga.get('beams', []))} beams, {len(ga.get('slabs', []))} slabs. "
                         f"Summary: {ga.get('summary', 'N/A')}")
        if self.data["forces"]:
            lines.append(f"Analysis basis: {self.data['forces'].get('loading_basis', {})}")
        if self.data["design"]:
            d = self.data["design"]
            lines.append(f"Design: {len(d.get('columns', []))} columns and "
                         f"{len(d.get('beams', []))} beams sized. Summary: {d.get('summary', 'N/A')}")
        if self.data["element_notes"]:
            lines.append("Existing per-element notes: " +
                         "; ".join(f"{k}: {v}" for k, v in self.data["element_notes"].items()))
        summary = "\n".join(lines) if lines else "Nothing generated yet -- the engineer is still at the start of the wizard."

        from oracle_log import read_recent_log
        recent_log = read_recent_log(max_chars=3000)
        if recent_log:
            summary += ("\n\nRECENT LOG ENTRIES (Claude API retries, JSON parse failures, and errors "
                        "shown to the engineer -- use these to help troubleshoot if asked "
                        "\"why did that fail\" or similar):\n" + recent_log)
        return summary

    def _combined_engineer_notes(self):
        """The Step 3 free-text box plus anything typed into the persistent chat --
        both count as "instructions Claude should accept", so both flow into the
        next generation prompt, not just the Step 3 box."""
        parts = []
        if self.data.get("engineer_notes"):
            parts.append(self.data["engineer_notes"])
        if self.data.get("chat_instructions"):
            parts.append("From ongoing chat with Claude:\n" +
                         "\n".join(f"- {m}" for m in self.data["chat_instructions"]))
        return "\n\n".join(parts) if parts else None

    def _send_chat_message(self):
        message = self.chat_input.get("1.0", "end").strip()
        if not message:
            return
        if not get_api_key():
            self._maybe_ask_for_api_key()
            return

        self.chat_messages.append({"role": "user", "content": message})
        self.data["chat_instructions"].append(message)
        self._render_chat_history()
        self.chat_input.delete("1.0", "end")
        self.chat_send_btn.config(state="disabled")
        t = self.chat_history_text
        t.config(state="normal")
        t.insert("end", "Claude is thinking...\n", "pending")
        t.config(state="disabled")
        t.see("end")

        def work():
            import anthropic
            client = anthropic.Anthropic(api_key=get_api_key(), timeout=120.0)
            system = ("You are helping an engineer use Oracle, a structural design tool, "
                       "part-way through their project. Answer questions and accept instructions "
                       "about the project below; be concise.\n\n" + self._project_context_summary())
            history = [{"role": m["role"], "content": m["content"]} for m in self.chat_messages]
            resp = client.messages.create(model="claude-sonnet-5", max_tokens=1000,
                                           system=system, messages=history)
            for block in resp.content:
                if hasattr(block, "text") and block.text:
                    return block.text
            raise RuntimeError("Claude's reply had no usable text.")

        def on_done():
            try:
                kind, payload = self.chat_queue.get_nowait()
            except queue.Empty:
                self.after(150, on_done)
                return
            self.chat_send_btn.config(state="normal")
            if kind == "ok":
                self.chat_messages.append({"role": "assistant", "content": payload})
            else:
                exc, _tb = payload
                self.chat_messages.append({"role": "assistant", "content": f"(Couldn't reach Claude: {exc})"})
            self._render_chat_history()

        def worker():
            try:
                result = work()
                self.chat_queue.put(("ok", result))
            except Exception as e:
                self.chat_queue.put(("error", (e, traceback.format_exc())))

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, on_done)

    def set_status(self, text, error=False):
        self.status_label.config(text=text, fg="#b91c1c" if error else COLOR_MUTED)

    def _on_next(self):
        handler = getattr(self, f"advance_step_{self.step_index}", None)
        if handler:
            handler()

    def _on_back(self):
        if self.step_index > 0:
            self.show_step(self.step_index - 1)

    # ---------- first-run API key setup ----------

    def _maybe_ask_for_api_key(self):
        if get_api_key():
            return
        win = tk.Toplevel(self)
        win.title("One-time setup")
        win.geometry("480x300")
        win.transient(self)
        win.grab_set()
        tk.Label(win, text="Before we start", font=FONT_TITLE).pack(pady=(20, 10))
        tk.Label(
            win, font=FONT_BODY, wraplength=420, justify="left",
            text="Oracle uses Claude (an AI assistant) to generate the structural "
                 "layout and element design. It needs an API key, which you only "
                 "have to enter once — it will be remembered on this computer.\n\n"
                 "Get one at console.anthropic.com, then paste it below."
        ).pack(padx=20, pady=(0, 15))
        entry = tk.Entry(win, width=48, show="•")
        entry.pack(pady=5)
        entry.focus_set()

        def save_and_close():
            key = entry.get().strip()
            if not key:
                messagebox.showwarning("Needed", "Please paste your API key, or close this "
                                                  "window and set it up later.", parent=win)
                return
            save_api_key(key)
            win.destroy()

        tk.Button(win, text="Save and continue", command=save_and_close,
                  bg=COLOR_ACCENT, fg="white", relief="flat", padx=16, pady=6).pack(pady=15)
        tk.Label(win, text="You can also skip this and set it up before Step 5.",
                 font=FONT_SMALL, fg=COLOR_MUTED).pack()

    # ---------- background work helper ----------

    def run_async(self, work_fn, on_success, on_error=None, busy_message="Working..."):
        self.set_status(busy_message)
        self.next_btn.config(state="disabled")

        def worker():
            try:
                result = work_fn()
                self.result_queue.put(("ok", result))
            except Exception as e:
                self.result_queue.put(("error", (e, traceback.format_exc())))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_queue(on_success, on_error)

    def _poll_queue(self, on_success, on_error):
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_queue(on_success, on_error))
            return
        self.next_btn.config(state="normal")
        if kind == "ok":
            self.set_status("")
            on_success(payload)
        else:
            exc, tb = payload
            self.set_status("Something went wrong -- see details below.", error=True)
            (on_error or self.default_error_handler)(exc, tb)

    def default_error_handler(self, exc, tb):
        friendly = str(exc) or exc.__class__.__name__
        try:
            from oracle_log import log_event
            log_event("wizard.error_shown_to_user", f"{friendly}\n{tb}")
        except Exception:
            pass  # logging must never be why an error dialog fails to show
        box = tk.Toplevel(self)
        box.title("We hit a problem")
        box.geometry("560x360")
        tk.Label(box, text="Something didn't go as expected", font=FONT_SUBTITLE,
                 fg="#b91c1c").pack(pady=(15, 5))
        tk.Label(box, text=friendly, font=FONT_BODY, wraplength=500, justify="left").pack(padx=20, pady=5)
        details = tk.Text(box, height=10, width=68, font=("Consolas", 8))
        details.insert("1.0", tb)
        details.config(state="disabled")
        details.pack(padx=20, pady=10)
        tk.Button(box, text="Close", command=box.destroy).pack(pady=5)

    # ================= STEP 0: Welcome =================

    def show_step_0(self):
        self.clear_content()
        tk.Label(self.content, text="Structural Design Assistant", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(
            self.content, bg=COLOR_BG, font=FONT_SUBTITLE, justify="left", wraplength=700,
            text="This walks you from an architectural drawing to a finished, "
                 "detailed structural drawing -- columns, beams, reinforcement, "
                 "and a bar bending schedule -- with a few plain questions along "
                 "the way. No commands to type, no code to read."
        ).pack(anchor="w", pady=(0, 20))
        steps_frame = tk.Frame(self.content, bg=COLOR_BG)
        steps_frame.pack(anchor="w", fill="x")
        for i, title in enumerate(STEP_TITLES[1:], start=1):
            tk.Label(steps_frame, text=f"{i}.  {title}", font=FONT_BODY,
                     bg=COLOR_BG, anchor="w").pack(anchor="w", pady=3)
        self.next_btn.config(text="Get started →")
        self.back_btn.config(state="disabled")

    def advance_step_0(self):
        self.show_step(1)

    # ================= STEP 1: Select drawing =================

    def show_step_1(self):
        self.clear_content()
        self.back_btn.config(state="normal")
        self.next_btn.config(text="Next →", state="disabled")
        tk.Label(self.content, text="Select your architectural drawing", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="Two kinds of file work here: a blank architectural plan (walls, columns "
                      "and gridlines on layers named Walls / Columns / Gridlines, for Oracle to "
                      "design a layout for), or an already-designed multi-floor structural GA "
                      "(real beam/column layers per floor, e.g. \"F.F BEAMS\" / \"COLUMN G-1\") "
                      "-- Oracle detects which one you've given it automatically.").pack(
            anchor="w", pady=(0, 20))

        self.selected_file_label = tk.Label(self.content, text="No file selected yet",
                                             font=FONT_BODY, bg=COLOR_BG, fg=COLOR_MUTED)
        self.selected_file_label.pack(anchor="w", pady=10)

        tk.Button(self.content, text="Browse for a drawing...", command=self._browse_file,
                  bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=8,
                  cursor="hand2").pack(anchor="w")

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select architectural drawing",
            filetypes=[("CAD drawings", "*.dxf *.dwg"), ("All files", "*.*")],
        )
        if not path:
            return
        self.selected_file_label.config(text=f"Selected: {Path(path).name}", fg="black")
        self._pending_source_path = Path(path)
        self.next_btn.config(state="normal")

    def advance_step_1(self):
        src = getattr(self, "_pending_source_path", None)
        if not src:
            return

        def work():
            dxf_name = self._ensure_dxf_in_input_dir(src)
            dxf_path = INPUT_DIR / dxf_name

            # An already-designed multi-floor GA (real "COLUMN <a>-<b>" / "<level> BEAMS"
            # layers) is a fundamentally different kind of file from a blank architectural
            # plan (just Walls/Columns/Gridlines) -- detect which one this is before
            # picking a parser, rather than forcing every file through the simple path.
            import re
            import ezdxf
            import ga_dxf_parser as gp
            doc = ezdxf.readfile(str(dxf_path))
            has_multilevel_pattern = any(
                re.match(r"^COLUMN\s+\S+-\S+$", l.dxf.name, re.IGNORECASE) for l in doc.layers
            )
            if has_multilevel_pattern:
                levels, beam_layer_by_level, column_layer_by_boundary, issues = gp.detect_levels(doc)
                return "multilevel_ga", dxf_name, {
                    "levels": levels, "beam_layer_by_level": beam_layer_by_level,
                    "column_layer_by_boundary": column_layer_by_boundary, "issues": issues,
                }

            from dxf_parser import DXFParser
            parser = DXFParser(dxf_name)
            geometry = parser.parse_all()
            if geometry is None:
                raise RuntimeError(f"Couldn't open {dxf_name} -- is it a valid DXF/DWG file?")
            parser.to_json()
            return "architectural", dxf_name, geometry

        def on_success(payload):
            kind, dxf_name, detail = payload
            self.data["dxf_filename"] = dxf_name
            self.data["dxf_kind"] = kind
            if kind == "multilevel_ga":
                self.data["ml_detect"] = detail
            else:
                self.data["geometry"] = detail
            self.show_step(2)

        self.run_async(work, on_success, busy_message="Reading your drawing...")

    def _ensure_dxf_in_input_dir(self, src: Path) -> str:
        if src.suffix.lower() == ".dxf":
            dest = INPUT_DIR / src.name
            if src.resolve() != dest.resolve():
                shutil.copy(src, dest)
            return src.name

        if src.suffix.lower() == ".dwg":
            if not ODA_CONVERTER_PATH:
                raise RuntimeError(
                    "This is a DWG file, but the ODA File Converter (needed to read DWG) "
                    "isn't installed. Please save your drawing as DXF instead, or install "
                    "the free ODA File Converter."
                )
            tmp_in = INPUT_DIR / "_wizard_dwg_in"
            tmp_in.mkdir(exist_ok=True)
            shutil.copy(src, tmp_in / src.name)
            args = [ODA_CONVERTER_PATH, str(tmp_in), str(INPUT_DIR), "ACAD2018", "DXF", "0", "0", "*.dwg"]
            subprocess.run(args, capture_output=True, text=True, timeout=60)
            shutil.rmtree(tmp_in, ignore_errors=True)
            dxf_name = src.stem + ".dxf"
            if not (INPUT_DIR / dxf_name).exists():
                raise RuntimeError("Couldn't convert that DWG file to DXF. Please try saving it as DXF instead.")
            return dxf_name

        raise RuntimeError("Please choose a .dxf or .dwg file.")

    # ================= STEP 2: Confirm what we found =================

    def show_step_2(self):
        self.clear_content()
        self.next_btn.config(text="Looks good, continue →", state="normal")

        if self.data.get("dxf_kind") == "multilevel_ga":
            self._show_step_2_multilevel()
            return

        geometry = self.data["geometry"]
        if geometry is None:
            tk.Label(self.content, text="Please select a drawing first.", font=FONT_SUBTITLE,
                     bg=COLOR_BG, fg="#b91c1c").pack(anchor="w", pady=20)
            self.next_btn.config(state="disabled")
            self.back_btn.config(state="normal")
            return
        tk.Label(self.content, text="Here's what we found in your drawing", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 15))

        counts = [
            ("Wall segments", len(geometry["walls"])),
            ("Columns", len(geometry["columns"])),
            ("Gridlines", len(geometry["gridlines"])),
        ]
        for label, count in counts:
            row = tk.Frame(self.content, bg=COLOR_BG)
            row.pack(anchor="w", pady=4, fill="x")
            tk.Label(row, text=f"{count}", font=("Segoe UI", 14, "bold"),
                     bg=COLOR_BG, fg=COLOR_ACCENT, width=4, anchor="e").pack(side="left")
            tk.Label(row, text=label, font=FONT_BODY, bg=COLOR_BG).pack(side="left", padx=10)

        if len(geometry["columns"]) == 0 or len(geometry["gridlines"]) == 0:
            tk.Label(
                self.content, bg="#fff7ed", fg="#9a3412", font=FONT_BODY, wraplength=680,
                justify="left", padx=12, pady=10,
                text="⚠ We didn't find any columns or gridlines. Double-check that they're "
                     "on layers named exactly 'Columns' and 'Gridlines' in your drawing, then "
                     "go back and re-select the file."
            ).pack(anchor="w", pady=15, fill="x")

        tk.Label(
            self.content, bg=COLOR_BG, font=FONT_SMALL, fg=COLOR_MUTED, wraplength=680,
            justify="left",
            text="If these numbers look wrong, go back and pick a different file, or check "
                 "the drawing's layers."
        ).pack(anchor="w", pady=(20, 0))

    def advance_step_2(self):
        self.show_step(3)

    def _show_step_2_multilevel(self):
        # advance_step_2 above handles both branches (Next just moves to Step 3 either
        # way); this method only builds Step 2's multi-level content.
        detect = self.data["ml_detect"]
        tk.Label(self.content, text="Here's the floor sequence we found", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 15))

        blocking = [i for i in detect["issues"] if i.kind == "blocking"]
        warnings = [i for i in detect["issues"] if i.kind != "blocking"]

        if detect["levels"]:
            tk.Label(self.content, font=("Segoe UI", 14, "bold"), bg=COLOR_BG, fg=COLOR_ACCENT,
                     text=" → ".join(detect["levels"])).pack(anchor="w", pady=(0, 10))
            for level, layer in detect["beam_layer_by_level"].items():
                tk.Label(self.content, font=FONT_BODY, bg=COLOR_BG,
                         text=f"  Floor \"{level}\" beams  →  layer \"{layer}\"").pack(anchor="w")
            for (lo, hi), layer in detect["column_layer_by_boundary"].items():
                tk.Label(self.content, font=FONT_BODY, bg=COLOR_BG,
                         text=f"  Columns {lo}→{hi}  →  layer \"{layer}\"").pack(anchor="w")

        if blocking:
            msg = "\n".join(f"⚠ {i.message}" for i in blocking)
            tk.Label(self.content, bg="#fef2f2", fg="#991b1b", font=FONT_BODY, wraplength=680,
                     justify="left", padx=12, pady=10, text=msg).pack(anchor="w", pady=15, fill="x")
            self.next_btn.config(state="disabled")
        elif warnings:
            msg = "\n".join(f"⚠ {i.message}" for i in warnings)
            tk.Label(self.content, bg="#fff7ed", fg="#9a3412", font=FONT_BODY, wraplength=680,
                     justify="left", padx=12, pady=10, text=msg).pack(anchor="w", pady=15, fill="x")

        tk.Label(self.content, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG, wraplength=680,
                 justify="left",
                 text="This is an already-designed layout, so the next step asks for each "
                      "floor's storey height, then goes straight to structural analysis -- "
                      "no AI layout design needed for this kind of file.").pack(anchor="w", pady=(15, 0))

    # ================= STEP 3: Questions =================

    def show_step_3(self):
        self.clear_content()
        self.next_btn.config(text="Next →", state="normal")

        if self.data.get("dxf_kind") == "multilevel_ga":
            self._show_step_3_multilevel()
            return

        tk.Label(self.content, text="A few quick questions", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="These decide how the layout, loads, and materials are worked out. "
                      "Defaults are filled in -- change anything that doesn't fit.").pack(
            anchor="w", pady=(0, 15))

        # self.content itself scrolls (see _build_chrome) -- this form just packs
        # straight into it like every other step's content.
        form = tk.Frame(self.content, bg=COLOR_BG)
        form.pack(anchor="w", fill="x")

        def row(label_text, widget_builder, hint=None, r=[0]):
            i = r[0]
            tk.Label(form, text=label_text, font=FONT_BODY, bg=COLOR_BG).grid(
                row=i, column=0, sticky="w", pady=7)
            widget_builder().grid(row=i, column=1, sticky="w", padx=10)
            if hint:
                tk.Label(form, text=hint, font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED).grid(
                    row=i, column=2, sticky="w")
            r[0] += 1

        self.storey_var = tk.DoubleVar(value=self.data["storey_height_m"])
        row("Storey (floor-to-floor) height, in metres:",
            lambda: tk.Spinbox(form, from_=2.4, to=6.0, increment=0.1, textvariable=self.storey_var,
                                width=8, font=FONT_BODY))

        self.occupancy_var = tk.StringVar(value=self.data["occupancy"])
        row("How will this floor mainly be used?",
            lambda: ttk.Combobox(form, textvariable=self.occupancy_var, width=28, font=FONT_BODY,
                                  values=list(OCCUPANCY_TABLE.keys()), state="readonly"),
            hint="(sets the live/imposed load per BS 6399-1)")

        self.slab_var = tk.IntVar(value=self.data["slab_thickness_mm"])
        row("Preferred slab thickness, in mm (a starting guide):",
            lambda: tk.Spinbox(form, from_=125, to=300, increment=25, textvariable=self.slab_var,
                                width=8, font=FONT_BODY))

        col_mat_default = "Steel (BS 5950)" if self.data["column_material"] == "steel" else "Concrete (BS 8110)"
        self.column_material_var = tk.StringVar(value=col_mat_default)
        row("Column material:",
            lambda: ttk.Combobox(form, textvariable=self.column_material_var, width=20, font=FONT_BODY,
                                  values=MATERIAL_OPTIONS, state="readonly"))

        beam_mat_default = "Steel (BS 5950)" if self.data["beam_material"] == "steel" else "Concrete (BS 8110)"
        self.beam_material_var = tk.StringVar(value=beam_mat_default)
        row("Beam material:",
            lambda: ttk.Combobox(form, textvariable=self.beam_material_var, width=20, font=FONT_BODY,
                                  values=MATERIAL_OPTIONS, state="readonly"),
            hint="(steel beams on concrete columns is a common mix)")

        self.steel_grade_var = tk.StringVar(value=self.data["steel_grade"])
        row("Steel grade (used only if steel is chosen above):",
            lambda: ttk.Combobox(form, textvariable=self.steel_grade_var, width=10, font=FONT_BODY,
                                  values=STEEL_GRADE_OPTIONS, state="readonly"))

        self.concrete_grade_var = tk.StringVar(value=self.data["concrete_grade"])
        row("Concrete grade (used only if concrete is chosen above):",
            lambda: ttk.Combobox(form, textvariable=self.concrete_grade_var, width=26, font=FONT_BODY,
                                  values=CONCRETE_GRADE_OPTIONS, state="readonly"))

        self.exposure_var = tk.StringVar(value=self.data["exposure_class"])
        row("Exposure (how sheltered is the concrete once built)?",
            lambda: ttk.Combobox(form, textvariable=self.exposure_var, width=15, font=FONT_BODY,
                                  values=EXPOSURE_OPTIONS, state="readonly"),
            hint="(adjusts concrete cover for durability)")

        notes_row = len(form.grid_slaves()) // 3
        tk.Label(form, text="Anything else the engineer should know? (optional)", font=FONT_BODY,
                 bg=COLOR_BG).grid(row=notes_row, column=0, columnspan=3, sticky="w", pady=(15, 4))
        self.notes_text = tk.Text(form, width=70, height=4, font=FONT_BODY, wrap="word",
                                   relief="solid", borderwidth=1)
        self.notes_text.insert("1.0", self.data.get("engineer_notes", ""))
        self.notes_text.grid(row=notes_row + 1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        tk.Label(form, font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED, wraplength=680, justify="left",
                 text="E.g. \"corner columns must stay within the wall line\", \"use 200mm slabs "
                      "everywhere\", \"this is a warehouse extension, expect racking loads\". "
                      "Free text -- write it the way you'd tell a colleague.").grid(
            row=notes_row + 2, column=0, columnspan=3, sticky="w")

    def advance_step_3(self):
        if self.data.get("dxf_kind") == "multilevel_ga":
            self._advance_step_3_multilevel()
            return
        self.data["storey_height_m"] = round(self.storey_var.get(), 2)
        self.data["occupancy"] = self.occupancy_var.get()
        self.data["slab_thickness_mm"] = int(self.slab_var.get())
        self.data["column_material"] = _material_key(self.column_material_var.get())
        self.data["beam_material"] = _material_key(self.beam_material_var.get())
        self.data["steel_grade"] = self.steel_grade_var.get()
        self.data["concrete_grade"] = self.concrete_grade_var.get()
        self.data["exposure_class"] = self.exposure_var.get()
        self.data["engineer_notes"] = self.notes_text.get("1.0", "end").strip()
        self.show_step(4)

    def _show_step_3_multilevel(self):
        detect = self.data["ml_detect"]
        levels = detect["levels"]
        tk.Label(self.content, text="Storey heights & floor loading", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="A plan drawing carries no elevation or load information, so both have to "
                      "come from you -- one row per floor.").pack(anchor="w", pady=(0, 15))

        # self.content itself scrolls (see _build_chrome) -- this form just packs
        # straight into it like every other step's content.
        form = tk.Frame(self.content, bg=COLOR_BG)
        form.pack(anchor="w", fill="x")

        headers = ["Floor", "Height (m)", "Use (sets imposed load)", "Slab thickness (mm)"]
        for c, h in enumerate(headers):
            tk.Label(form, text=h, font=("Segoe UI", 9, "bold"), bg=COLOR_BG, fg=COLOR_MUTED).grid(
                row=0, column=c, sticky="w", padx=(0, 14), pady=(0, 6))

        tk.Label(form, text=f"Ground ({levels[0]})", font=FONT_BODY, bg=COLOR_BG).grid(
            row=1, column=0, sticky="w", pady=6)
        tk.Label(form, text="0.0 (base)", font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED).grid(
            row=1, column=1, sticky="w")

        self.ml_storey_vars = {}
        self.ml_occupancy_vars = {}
        self.ml_slab_vars = {}
        saved_occ = self.data.get("ml_per_level_loading", {})
        saved_heights = self.data.get("ml_storey_heights", {})
        for i, level in enumerate(levels[1:], start=2):
            is_roof = level == levels[-1]
            table = ROOF_OCCUPANCY_TABLE if is_roof else OCCUPANCY_TABLE
            default_occ = "Roof - occasional access" if is_roof else "Offices (general use)"

            tk.Label(form, text=f"{levels[i - 2]} → {level}", font=FONT_BODY, bg=COLOR_BG).grid(
                row=i, column=0, sticky="w", pady=6)

            h_var = tk.DoubleVar(value=saved_heights.get(level, 3.0))
            tk.Spinbox(form, from_=2.4, to=12.0, increment=0.1, textvariable=h_var, width=7,
                       font=FONT_BODY).grid(row=i, column=1, sticky="w", padx=(0, 14))
            self.ml_storey_vars[level] = h_var

            occ_var = tk.StringVar(value=saved_occ.get(level, {}).get("occupancy", default_occ))
            ttk.Combobox(form, textvariable=occ_var, width=26, font=FONT_BODY,
                         values=list(table.keys()), state="readonly").grid(
                row=i, column=2, sticky="w", padx=(0, 14))
            self.ml_occupancy_vars[level] = (occ_var, table)

            slab_var = tk.IntVar(value=saved_occ.get(level, {}).get("slab_thickness_mm", 150))
            tk.Spinbox(form, from_=100, to=300, increment=25, textvariable=slab_var, width=7,
                       font=FONT_BODY).grid(row=i, column=3, sticky="w")
            self.ml_slab_vars[level] = slab_var

        tk.Label(self.content, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG, wraplength=680,
                 justify="left",
                 text="Dead load = self-weight (from each slab's own thickness) + a standard "
                      "1.5 kN/m² finishes allowance. Combined per BS 8110 cl 2.4.3 (1.4 dead + "
                      "1.6 imposed). Wind/lateral load is not modeled.").pack(anchor="w", pady=(15, 0))

    def _advance_step_3_multilevel(self):
        detect = self.data["ml_detect"]
        levels = detect["levels"]
        cumulative = {levels[0]: 0.0}
        running = 0.0
        per_level_loading = {}
        for level in levels[1:]:
            running += round(self.ml_storey_vars[level].get(), 2)
            cumulative[level] = running
            occ_var, table = self.ml_occupancy_vars[level]
            occupancy = occ_var.get()
            per_level_loading[level] = {
                "occupancy": occupancy,
                "imposed_kn_m2": table[occupancy],
                "slab_thickness_mm": int(self.ml_slab_vars[level].get()),
            }
        self.data["ml_storey_heights"] = {lv: self.ml_storey_vars[lv].get() for lv in levels[1:]}
        self.data["ml_cumulative_heights"] = cumulative
        self.data["ml_per_level_loading"] = per_level_loading
        self.show_step(4)

    # ================= STEP 4: Generate GA =================

    def show_step_4(self):
        self.clear_content()
        self.next_btn.config(text="Next →", state="disabled")

        if self.data.get("dxf_kind") == "multilevel_ga":
            self._show_step_4_multilevel()
            return

        if self.data["geometry"] is None:
            self._show_out_of_order_message()
            return
        tk.Label(self.content, text="Creating your structural layout", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="We're placing columns, beams, and slabs onto your building's grid. "
                      "This usually takes 10-20 seconds.").pack(anchor="w", pady=(0, 20))
        self.ga_result_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.ga_result_frame.pack(anchor="w", fill="both", expand=True)
        self._generate_ga()

    def _generate_ga(self):
        if not get_api_key():
            self._maybe_ask_for_api_key()

        def work():
            from claude_ga_generator import generate_ga_prompt, call_claude, save_ga
            prompt = generate_ga_prompt(
                self.data["geometry"],
                storey_height_m=self.data["storey_height_m"],
                slab_thickness_hint_mm=(self.data["slab_thickness_mm"] - 25, self.data["slab_thickness_mm"] + 25),
                engineer_notes=self._combined_engineer_notes(),
                element_notes=self.data.get("element_notes") or None,
            )
            response = call_claude(prompt)
            if not response:
                raise RuntimeError("Claude didn't return a layout. Please try again.")
            ga_data = save_ga(response)
            if ga_data is None:
                raise RuntimeError("Claude's reply couldn't be understood as a layout. Please try again.")

            sketch_path = None
            try:
                from ga_sketch import render_ga_sketch
                sketch_path = render_ga_sketch(ga_data, str(OUTPUT_JSON_DIR / "_ga_sketch.png"))
            except Exception:
                pass  # the sketch is a nice-to-have; a failure here shouldn't block the layout
            return ga_data, sketch_path

        def on_success(payload):
            ga_data, sketch_path = payload
            self.data["ga"] = ga_data
            self.notes_btn.config(state="normal")
            for w in self.ga_result_frame.winfo_children():
                w.destroy()

            left = tk.Frame(self.ga_result_frame, bg=COLOR_BG)
            left.pack(side="left", anchor="n", fill="y")
            for label, count in [("Columns", len(ga_data.get("columns", []))),
                                  ("Beams", len(ga_data.get("beams", []))),
                                  ("Slabs", len(ga_data.get("slabs", [])))]:
                row = tk.Frame(left, bg=COLOR_BG)
                row.pack(anchor="w", pady=3)
                tk.Label(row, text=f"{count}", font=("Segoe UI", 13, "bold"), bg=COLOR_BG,
                         fg=COLOR_ACCENT, width=4, anchor="e").pack(side="left")
                tk.Label(row, text=label, font=FONT_BODY, bg=COLOR_BG).pack(side="left", padx=8)
            if ga_data.get("summary"):
                tk.Label(left, text=ga_data["summary"], font=FONT_BODY, bg=COLOR_BG,
                         wraplength=320, justify="left").pack(anchor="w", pady=(10, 0))
            tk.Button(left, text="Regenerate layout", command=self._generate_ga,
                      font=FONT_SMALL, relief="flat", padx=10, pady=4).pack(anchor="w", pady=15)

            if sketch_path:
                try:
                    self._ga_sketch_photo = tk.PhotoImage(file=sketch_path)
                    tk.Label(self.ga_result_frame, image=self._ga_sketch_photo, bg=COLOR_BG).pack(
                        side="left", anchor="n", padx=(20, 0))
                except Exception:
                    pass

            self.next_btn.config(state="normal")

        def on_error(exc, tb):
            self.default_error_handler(exc, tb)
            retry = tk.Button(self.ga_result_frame, text="Try again", command=self._generate_ga,
                               bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=6)
            retry.pack(anchor="w", pady=10)

        self.run_async(work, on_success, on_error, busy_message="Talking to Claude...")

    def advance_step_4(self):
        self.show_step(5)

    def _show_step_4_multilevel(self):
        tk.Label(self.content, text="Building your structural model", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="Extracting joints and members from your drawing's geometry and finding "
                      "the slab panels on each floor. This is deterministic (no AI involved) "
                      "since your layout is already designed.").pack(anchor="w", pady=(0, 20))
        self.ml_build_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.ml_build_frame.pack(anchor="w", fill="both", expand=True)
        self._build_multilevel_model()

    def _build_multilevel_model(self):
        def work():
            import ga_dxf_parser as gp
            dxf_path = INPUT_DIR / self.data["dxf_filename"]
            result = gp.parse_multilevel_ga(
                str(dxf_path), storey_heights_m=self.data["ml_cumulative_heights"],
                per_level_loading=self.data.get("ml_per_level_loading"),
            )
            sketch_path = None
            if "model" in result:
                try:
                    from ml_sketch import render_multilevel_sketch
                    sketch_path = render_multilevel_sketch(
                        result["levels"], result["model"], self.data["ml_cumulative_heights"],
                        result.get("void_centroids", {}), str(OUTPUT_JSON_DIR / "_ml_sketch.png"),
                    )
                except Exception:
                    pass  # the sketch is a nice-to-have; a failure here shouldn't block the model
            return result, sketch_path

        def on_success(payload):
            result, sketch_path = payload
            self.data["ml_result"] = result
            for w in self.ml_build_frame.winfo_children():
                w.destroy()

            blocking = [i for i in result["issues"] if i.kind == "blocking"]
            warnings = [i for i in result["issues"] if i.kind != "blocking"]

            if blocking or "model" not in result:
                msg = "\n".join(f"⚠ {i.message}" for i in blocking) or "The model couldn't be built."
                tk.Label(self.ml_build_frame, bg="#fef2f2", fg="#991b1b", font=FONT_BODY,
                         wraplength=680, justify="left", padx=12, pady=10, text=msg).pack(
                    anchor="w", fill="x")
                tk.Button(self.ml_build_frame, text="Back to storey heights",
                          command=lambda: self.show_step(3), relief="flat", padx=12, pady=6).pack(
                    anchor="w", pady=10)
                return

            model = result["model"]
            row = tk.Frame(self.ml_build_frame, bg=COLOR_BG)
            row.pack(anchor="w", pady=6)
            for label, count in [("Joints", len(model["joints"].coordinates())),
                                  ("Members", len(model["members"])),
                                  ("Slab panels", sum(len(p) for p in model["slab_panels"].values()))]:
                col = tk.Frame(row, bg=COLOR_BG)
                col.pack(side="left", padx=(0, 30))
                tk.Label(col, text=f"{count}", font=("Segoe UI", 16, "bold"), bg=COLOR_BG,
                         fg=COLOR_ACCENT).pack()
                tk.Label(col, text=label, font=FONT_BODY, bg=COLOR_BG).pack()

            if warnings:
                msg = "\n".join(f"⚠ {i.message}" for i in warnings)
                tk.Label(self.ml_build_frame, bg="#fff7ed", fg="#9a3412", font=FONT_BODY,
                         wraplength=680, justify="left", padx=12, pady=10, text=msg).pack(
                    anchor="w", pady=15, fill="x")

            if sketch_path:
                tk.Label(self.ml_build_frame, text="Detected panels (shaded) and any voids (crossed "
                                                     "out) -- check these against the real drawing:",
                         font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED).pack(anchor="w", pady=(10, 4))
                try:
                    self._ml_sketch_photo = tk.PhotoImage(file=sketch_path)
                    tk.Label(self.ml_build_frame, image=self._ml_sketch_photo, bg=COLOR_BG).pack(anchor="w")
                except Exception:
                    pass

            tk.Label(self.ml_build_frame, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG,
                     wraplength=680, justify="left",
                     text="Standard initial member sizes were used (225x225 columns, 225x450 "
                          "beams, 225x300 roof beams, C25/30 concrete). Loading is real, from your "
                          "Step 3 answers: dead = self-weight + finishes, imposed = per-floor "
                          "occupancy, combined per BS 8110 cl 2.4.3 -- these sizes are a starting "
                          "point for analysis, not a final design.").pack(anchor="w", pady=(10, 0))
            self.next_btn.config(state="normal")

        def on_error(exc, tb):
            self.default_error_handler(exc, tb)
            tk.Button(self.ml_build_frame, text="Try again", command=self._build_multilevel_model,
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=6).pack(anchor="w", pady=10)

        self.run_async(work, on_success, on_error, busy_message="Building the model...")

    # ================= STEP 5: Structural analysis =================

    def show_step_5(self):
        self.clear_content()
        self.next_btn.config(text="Next →", state="disabled")

        if self.data.get("dxf_kind") == "multilevel_ga":
            self._show_step_5_multilevel()
            return

        tk.Label(self.content, text="Structural analysis", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(
            self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
            text="Now we work out the real loads on every column and beam. "
                 "If STAAD.Pro is open on this computer, we can run a full analysis "
                 "in it directly. Otherwise, we'll use a quick, code-based estimate instead."
        ).pack(anchor="w", pady=(0, 20))

        btns = tk.Frame(self.content, bg=COLOR_BG)
        btns.pack(anchor="w")
        tk.Button(btns, text="Use STAAD.Pro (more accurate)", command=self._run_real_analysis,
                  bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=10,
                  cursor="hand2").pack(side="left", padx=(0, 10))
        tk.Button(btns, text="Use the quick estimate instead", command=self._run_mock_analysis,
                  relief="flat", padx=14, pady=10, cursor="hand2").pack(side="left")

        self.analysis_result_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.analysis_result_frame.pack(anchor="w", fill="both", expand=True, pady=20)

    def _update_project_loading(self):
        standards = json.loads(STANDARDS_PATH.read_text())
        standards["loading"]["imposed_load_kn_m2"] = OCCUPANCY_TABLE[self.data["occupancy"]]
        standards["loading"]["imposed_load_basis"] = (
            f"BS 6399-1:1996 Table 1 -- chosen by the engineer during setup as "
            f"'{self.data['occupancy']}'."
        )
        STANDARDS_PATH.write_text(json.dumps(standards, indent=2))
        return standards

    def _run_real_analysis(self):
        def work():
            self._update_project_loading()
            import staad_v8i_integration as sv
            return sv.run()

        def on_success(results):
            self.data["forces"] = results
            self.data["used_real_staad"] = True
            self._show_analysis_result(results, real=True)

        def on_error(exc, tb):
            self.set_status("")
            for w in self.analysis_result_frame.winfo_children():
                w.destroy()
            tk.Label(
                self.analysis_result_frame, bg="#fff7ed", fg="#9a3412", font=FONT_BODY,
                wraplength=680, justify="left", padx=12, pady=10,
                text=f"Couldn't complete the STAAD.Pro analysis ({exc}).\n"
                     "Falling back to the quick estimate instead so you can keep moving -- "
                     "you can retry STAAD.Pro any time."
            ).pack(anchor="w", fill="x")
            self._run_mock_analysis()

        self.run_async(work, on_success, on_error,
                        busy_message="Running the analysis in STAAD.Pro (this can take a couple of minutes)...")

    def _run_mock_analysis(self):
        def work():
            standards = self._update_project_loading()
            from staad_mock import generate_mock_forces
            return generate_mock_forces(self.data["ga"], standards)

        def on_success(results):
            self.data["forces"] = results
            self.data["used_real_staad"] = False
            self._show_analysis_result(results, real=False)

        self.run_async(work, on_success, busy_message="Estimating loads...")

    def _show_analysis_result(self, results, real):
        for w in self.analysis_result_frame.winfo_children():
            w.destroy()
        source = "Real STAAD.Pro analysis" if real else "Quick built-in estimate"
        tk.Label(self.analysis_result_frame, text=f"✓ {source} complete", font=FONT_SUBTITLE,
                 bg=COLOR_BG, fg="#166534").pack(anchor="w")
        loads = sorted(((nf["column_name"], abs(nf["fy"])) for nf in results["node_forces"].values()),
                        key=lambda x: -x[1])
        preview = ", ".join(f"{name} {load:.0f} kN" for name, load in loads[:5])
        tk.Label(self.analysis_result_frame, text=f"Column loads: {preview}{'...' if len(loads) > 5 else ''}",
                 font=FONT_BODY, bg=COLOR_BG, wraplength=680, justify="left").pack(anchor="w", pady=6)
        self.next_btn.config(state="normal")

    def advance_step_5(self):
        self.show_step(6)

    def _show_step_5_multilevel(self):
        tk.Label(self.content, text="Structural analysis", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        if "model" not in self.data.get("ml_result", {}):
            self._show_out_of_order_message()
            return
        tk.Label(
            self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
            text="This model needs STAAD.Pro -- it's a real multi-floor building with plate "
                 "slab elements, which the quick built-in estimate doesn't support. Make sure "
                 "STAAD.Pro V8i SS6 is open, then run the analysis."
        ).pack(anchor="w", pady=(0, 20))

        tk.Button(self.content, text="Run analysis in STAAD.Pro", command=self._run_multilevel_analysis,
                  bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=10,
                  cursor="hand2").pack(anchor="w")

        self.ml_analysis_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.ml_analysis_frame.pack(anchor="w", fill="both", expand=True, pady=20)

    def _run_multilevel_analysis(self):
        def work():
            import staad_v8i_integration as sv
            sv.STD_PATH.write_text(self.data["ml_result"]["std_text"])
            # The wizard process itself may be 64-bit; STAAD's OpenSTAAD COM automation
            # is 32-bit only, so this has to run in the dedicated 32-bit subprocess (same
            # mechanism sv.run() uses for the single-floor path), not in-process here.
            sv.run_subprocess_step("analyze")
            return sv.ANL_PATH

        def on_success(anl_path):
            for w in self.ml_analysis_frame.winfo_children():
                w.destroy()
            tk.Label(self.ml_analysis_frame, text="✓ Analysis complete", font=FONT_SUBTITLE,
                     bg=COLOR_BG, fg="#166534").pack(anchor="w")
            tk.Label(self.ml_analysis_frame,
                     text=f"Model file: {sys.modules['staad_v8i_integration'].STD_PATH}\n"
                          f"Full results: {anl_path}",
                     font=FONT_BODY, bg=COLOR_BG, wraplength=680, justify="left").pack(anchor="w", pady=8)
            tk.Label(
                self.ml_analysis_frame, font=FONT_SMALL, fg=COLOR_MUTED, bg=COLOR_BG,
                wraplength=680, justify="left",
                text="Automated design and detail-drawing generation for multi-floor buildings "
                     "isn't wired up yet -- open the results file above to review reactions and "
                     "member forces directly, or ask Claude about them."
            ).pack(anchor="w", pady=(5, 15))
            tk.Button(self.ml_analysis_frame, text="Open results folder",
                      command=lambda: os.startfile(anl_path.parent),
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=8,
                      cursor="hand2").pack(anchor="w")
            self.next_btn.config(state="normal", text="Start a new project", command=self._restart)

        def on_error(exc, tb):
            self.default_error_handler(exc, tb)
            tk.Button(self.ml_analysis_frame, text="Try again", command=self._run_multilevel_analysis,
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=6).pack(anchor="w", pady=10)

        self.run_async(work, on_success, on_error,
                        busy_message="Running the analysis in STAAD.Pro (this can take a couple of minutes)...")

    # ================= STEP 6: Element design =================

    def show_step_6(self):
        self.clear_content()
        self.next_btn.config(text="Next →", state="disabled")
        if self.data["ga"] is None or self.data["forces"] is None:
            self._show_out_of_order_message()
            return
        tk.Label(self.content, text="Designing columns and beams", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="Sizing every column and beam and choosing reinforcement, "
                      "to BS 8110-1:1997.").pack(anchor="w", pady=(0, 20))
        self.design_result_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.design_result_frame.pack(anchor="w", fill="both", expand=True)
        self._generate_design()

    def _generate_design(self):
        def work():
            from design_module import generate_design_prompt, call_claude_design, save_design
            prompt = generate_design_prompt(
                self.data["ga"], self.data["forces"],
                column_material=self.data["column_material"],
                beam_material=self.data["beam_material"],
                concrete_grade=self.data["concrete_grade"],
                steel_grade=self.data["steel_grade"],
                exposure_class=self.data["exposure_class"],
                engineer_notes=self._combined_engineer_notes(),
                element_notes=self.data.get("element_notes") or None,
            )
            response = call_claude_design(prompt)
            if not response:
                raise RuntimeError("Claude didn't return a design. Please try again.")
            design_data = save_design(response)
            if design_data is None:
                raise RuntimeError("Claude's reply couldn't be understood as a design. Please try again.")
            return design_data

        def on_success(design_data):
            self.data["design"] = design_data
            for w in self.design_result_frame.winfo_children():
                w.destroy()
            cols = design_data.get("columns", [])
            beams = design_data.get("beams", [])
            tk.Label(self.design_result_frame,
                     text=f"✓ {len(cols)} columns and {len(beams)} beams designed",
                     font=FONT_SUBTITLE, bg=COLOR_BG, fg="#166534").pack(anchor="w", pady=(0, 10))
            for col in cols[:4]:
                tk.Label(self.design_result_frame,
                         text=f"  {col['name']}: {col['section']} mm, {col.get('bars', 'N/A')}",
                         font=FONT_BODY, bg=COLOR_BG).pack(anchor="w")
            if len(cols) > 4:
                tk.Label(self.design_result_frame, text=f"  ...and {len(cols) - 4} more",
                         font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED).pack(anchor="w")
            tk.Button(self.design_result_frame, text="Redesign", command=self._generate_design,
                      font=FONT_SMALL, relief="flat", padx=10, pady=4).pack(anchor="w", pady=15)
            self.next_btn.config(state="normal")

        def on_error(exc, tb):
            self.default_error_handler(exc, tb)
            tk.Button(self.design_result_frame, text="Try again", command=self._generate_design,
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=6).pack(anchor="w", pady=10)

        self.run_async(work, on_success, on_error, busy_message="Talking to Claude...")

    def advance_step_6(self):
        self.show_step(7)

    # ================= STEP 7: Final detail drawing =================

    def show_step_7(self):
        self.clear_content()
        self.next_btn.config(text="Finish", state="disabled")
        if self.data["design"] is None:
            self._show_out_of_order_message()
            return
        tk.Label(self.content, text="Building your detail drawing", font=FONT_TITLE,
                 bg=COLOR_BG).pack(anchor="w", pady=(10, 5))
        tk.Label(self.content, bg=COLOR_BG, font=FONT_SUBTITLE, wraplength=700, justify="left",
                 text="Putting together the plan, bar marks, and bar bending schedule "
                      "into one drawing file.").pack(anchor="w", pady=(0, 20))
        self.final_frame = tk.Frame(self.content, bg=COLOR_BG)
        self.final_frame.pack(anchor="w", fill="both", expand=True)
        self._generate_drawing()

    def _generate_drawing(self):
        def work():
            import dwg_detail_generator as dg
            bbs = dg.build_drawing()
            dwg_path, dxf_path, converted = None, dg.DXF_PATH, False
            try:
                dwg_path = dg.convert_to_dwg()
                converted = True
            except Exception:
                pass
            try:
                import lisp_detail_generator as ld
                ld.main()
            except Exception:
                pass
            return bbs, dxf_path, dwg_path, converted

        def on_success(payload):
            bbs, dxf_path, dwg_path, converted = payload
            for w in self.final_frame.winfo_children():
                w.destroy()
            tk.Label(self.final_frame, text="✓ Your detail drawing is ready!",
                     font=("Segoe UI", 14, "bold"), bg=COLOR_BG, fg="#166534").pack(anchor="w", pady=(0, 10))
            tk.Label(self.final_frame, text=f"Total steel weight: {bbs['grand_total_steel_weight_kg']:.1f} kg",
                     font=FONT_BODY, bg=COLOR_BG).pack(anchor="w", pady=3)
            final_path = dwg_path if converted else dxf_path
            tk.Label(self.final_frame, text=f"Saved to: {final_path}", font=FONT_BODY,
                     bg=COLOR_BG, wraplength=680, justify="left").pack(anchor="w", pady=3)
            if not converted:
                tk.Label(self.final_frame,
                         text="(Saved as DXF -- open it in AutoCAD and use Save As to get a DWG.)",
                         font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_MUTED).pack(anchor="w")
            tk.Button(self.final_frame, text="Open the folder", command=lambda: os.startfile(final_path.parent),
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=8,
                      cursor="hand2").pack(anchor="w", pady=15)
            self.next_btn.config(state="normal", text="Start a new project", command=self._restart)

        def on_error(exc, tb):
            self.default_error_handler(exc, tb)
            tk.Button(self.final_frame, text="Try again", command=self._generate_drawing,
                      bg=COLOR_ACCENT, fg="white", relief="flat", padx=14, pady=6).pack(anchor="w", pady=10)

        self.run_async(work, on_success, on_error, busy_message="Drawing...")

    def _restart(self):
        self.next_btn.config(command=self._on_next)
        for key in self.data:
            self.data[key] = None
        self.data.update({
            "storey_height_m": 3.0, "occupancy": "Offices (general use)",
            "slab_thickness_mm": 175, "used_real_staad": False,
            "column_material": "concrete", "beam_material": "concrete",
            "steel_grade": "S275", "concrete_grade": CONCRETE_GRADE_OPTIONS[0],
            "exposure_class": "Mild", "engineer_notes": "", "element_notes": {},
            "chat_instructions": [],
        })
        self.chat_messages = []
        self.notes_btn.config(state="disabled")
        self.show_step(0)

    # ---------- navigation ----------

    def show_step(self, index):
        self.step_index = index
        self.step_label.config(text=f"Step {index} of {len(STEP_TITLES) - 1}: {STEP_TITLES[index]}"
                                if index > 0 else "Welcome")
        self.set_status("")
        getattr(self, f"show_step_{index}")()


if __name__ == "__main__":
    app = Wizard()
    app.mainloop()
