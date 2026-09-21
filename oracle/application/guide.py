"""Oracle — Review Guide (application layer)

Purpose:
    Reads an OracleProject into the SMALL, plain-language picture an engineer needs to decide what to do next: an overview ("Drawing
    interpreted; 12 views found; 3 items need your review"), a review queue (what needs the engineer, what is completed), the views as a
    short list with three states (Needs review / Accepted / Rejected), rejected views with their reason, and one action card for the
    selected item: a question in ordinary words, Oracle's suggestion (never shown as fact), the actions that make sense for it, and
    what the engineer has already decided or said. Internal ids, provenance, hashes, coordinates and confidence numbers are not part of
    these models; the technical detail stays in the Details view (oracle.application.review_models) and the project.

Role in Oracle:
    The read side of the calmer review interface. It is derived data: it never stores anything, never decides anything, and never changes
    readiness: whether an item "needs review" is read from what the project itself says is open (interpretation sets, unreviewed views,
    unestablished levels, open error issues). Wording keeps three voices apart: ORACLE SUGGESTS (a proposal), ENGINEER DECISION (an accepted
    or rejected choice) and ENGINEER INPUT (the engineer's own words).

Dependencies:
    oracle.core; oracle.application.review_models (affected objects, effects in words); oracle.interpretation.naming (level names).

Consumers:
    oracle.application.session, oracle.ui, tests.

Status:
    Application layer (interface refinement phase).

Migration/Notes:
    Confidence is shown only as High / Medium / Low, never as a number. An item leaves the queue when the project no longer treats it as open;
    it then appears under Completed. A rejected view's questions are closed by the rejection itself (see ArchitecturalSession.reject_views).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from oracle.core import EffectKind, IssueSeverity, OracleProject, ReviewStatus, SetStatus, Target, TargetScope, ViewType
from oracle.interpretation.naming import level_display_name  # noqa: F401  (re-exported for the session's wording)

from . import review_models as rm

# ---------------------------------------------------------------- words

REJECT_REASONS = (
    ("duplicate", "Duplicate"),
    ("irrelevant", "Irrelevant to this project"),
    ("incorrectly_detected", "Incorrectly detected"),
    ("outside_scope", "Outside current scope"),
    ("other", "Other"),
)
REJECT_REASON_LABEL = dict(REJECT_REASONS)

_TYPE_WORDS = {"floor_plan": "floor plan", "section": "section", "elevation": "elevation", "detail": "detail", "schedule": "schedule",
               "legend": "legend", "notes": "notes sheet", "title_block": "title block", "unknown": "drawing"}
_GROUPS = (("Floor plans", ("floor_plan",)), ("Sections", ("section",)), ("Elevations", ("elevation",)), ("Details", ("detail",)),
           ("Unclear", ("unknown",)), ("Sheet furniture", ("schedule", "legend", "notes", "title_block")))
_GROUP_OF = {kind: title for title, kinds in _GROUPS for kind in kinds}
VIEW_TYPE_CHOICES = (("floor_plan", "Plan"), ("section", "Section"), ("elevation", "Elevation"), ("detail", "Detail"), ("unknown", "Other"))
_TYPE_LABEL = {"floor_plan": "Plan", "section": "Section", "elevation": "Elevation", "detail": "Detail", "unknown": "Other", "schedule": "Schedule",
               "legend": "Legend", "notes": "Notes", "title_block": "Title block"}


def type_label(view_type) -> str:
    """The engineer-facing word for a view type (the domain's own vocabulary; 'unknown' is shown as Other)."""
    return _TYPE_LABEL.get(getattr(view_type, "value", view_type), str(view_type).replace("_", " ").capitalize())


STATE_WORDS = {"needs_review": "Needs review", "accepted": "Accepted", "rejected": "Rejected"}
STATE_ICON = {"needs_review": "⚠", "accepted": "✓", "rejected": "✕"}
_MEANING_WORDS = {
    "footprint min corner": "Match the lower-left corners of the two plans", "footprint centre": "Match the centres of the two plans",
    "footprint max corner": "Match the upper-right corners of the two plans", "floor plans": "The levels shown on the floor plans",
    "sections": "The levels shown on the sections", "elevations": "The levels shown on the elevations",
    "2 distinct levels": "Two separate levels", "one level with a conflicting elevation": "One level; one of the elevations is wrong",
    "mm": "Millimetres", "cm": "Centimetres", "m": "Metres", "inch": "Inches", "foot": "Feet",
}
_LEVEL_KEY = re.compile(r"\b(GROUND|ROOF|DATUM|MEZZANINE|FLOOR:\d+|BASEMENT:\d+|NAMED:[A-Z0-9_]+)\b")
_ID_VIEW = re.compile(r"\bVIEW-\d+\b")
_ID_SRC = re.compile(r"\bSRC-\d+\b")


def state_of(review) -> str:
    value = getattr(review, "value", review)
    return {"accepted": "accepted", "rejected": "rejected"}.get(value, "needs_review")


def confidence_words(confidence: float) -> str:
    return "High" if confidence >= 0.75 else "Medium" if confidence >= 0.45 else "Low"


def when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def _sentence(text: str, limit: int = 170) -> str:
    text = " ".join((text or "").split())
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- names

def view_names(project: OracleProject, source_id: str) -> dict:
    """A readable, unique name for every view: its title (in normal capitals) or 'Untitled floor plan 2'."""
    arch = project.architecture_of(source_id)
    names, seen = {}, {}
    for v in arch.views:
        if v.title:
            base = v.title.strip().title() if v.title.isupper() else v.title.strip()
        else:
            base = f"Untitled {_TYPE_WORDS.get(v.view_type.value, 'drawing')}"
        names[v.id] = base
        seen.setdefault(base, []).append(v.id)
    for base, ids in seen.items():
        if len(ids) > 1:
            for n, vid in enumerate(sorted(ids), 1):
                names[vid] = f"{base} ({n})"
    return names


def friendly(project: OracleProject, text: str, names: dict, source_label: str) -> str:
    """Replace internal ids and level keys in a sentence with the names an engineer knows."""
    text = _ID_VIEW.sub(lambda m: f"“{names[m.group(0)]}”" if m.group(0) in names else m.group(0), text or "")
    text = _ID_SRC.sub(source_label, text)
    text = _LEVEL_KEY.sub(lambda m: level_display_name(m.group(0)), text)
    text = re.sub(r"\s*\(“[^”]+”\)", "", text)                       # "the plan (“x”)": the name adds nothing
    text = re.sub(r"(“[^”]+”)\s*\(([^)]*)\)", lambda m: m.group(1) if _repeats(m.group(1), m.group(2)) else m.group(0), text)
    text = re.sub(r"\s*\([^)]*(?:_|\d\.\d\d)[^)]*\)", "", text)          # "(best: footprint_min_corner, 0.50)" belongs in the Details view
    return _drop_repeated_names(text).replace("_", " ")


def _drop_repeated_names(text: str) -> str:
    """'the First Floor plan “First Floor Plan” cannot ...': a quoted name that only repeats the words just before it adds nothing."""
    out, last = [], 0
    for m in re.finditer(r"\s*“([^”]+)”", text):
        before = text[last:m.start()] if not out else "".join(out) + text[last:m.start()]
        if before.lower().rstrip().endswith(m.group(1).lower()):
            out.append(text[last:m.start()])
            last = m.end()
    out.append(text[last:])
    return "".join(out)


def _repeats(quoted: str, inner: str) -> bool:
    """`“Name” (NAME)` or `“Untitled …” (untitled)`: the parenthesis only repeats the name."""
    name, inner = quoted.strip("“”").strip().lower(), inner.strip().lower()
    return inner == name or inner == "untitled" or name.startswith("untitled")


def _drawing_label(project: OracleProject, source_id: str) -> str:
    d = project.architecture_of(source_id).drawing
    return d.file


# ---------------------------------------------------------------- views

@dataclass
class ViewEntry:
    id: str
    name: str
    kind: str                 # "Floor plan", "Section" ...
    group: str
    state: str                # needs_review | accepted | rejected
    note: str                 # a few words: "question open", "level not named" ...

    @property
    def label(self) -> str:
        suffix = f"  — {self.note}" if self.note else ""
        return f"{STATE_ICON[self.state]}  {self.name}{suffix}"


def build_view_entries(project: OracleProject, source_id: str) -> list:
    arch = project.architecture_of(source_id)
    names = view_names(project, source_id)
    open_by_view = {}
    for s in project.interpretation_sets:
        if s.status == SetStatus.OPEN:
            for oid in rm.affected_ids(project, s):
                open_by_view[oid] = open_by_view.get(oid, 0) + 1
    entries = []
    for v in arch.views:
        if v.review == ReviewStatus.SUPERSEDED:
            continue
        state = state_of(v.review)
        note = ""
        if state == "needs_review":
            if open_by_view.get(v.id):
                note = "question open"
            elif v.view_type == ViewType.FLOOR_PLAN and v.variant is None and v.level_key is None:
                note = "level not named"
            elif v.view_type == ViewType.UNKNOWN:
                note = "unclear"
            else:
                note = "needs review"
        entries.append(ViewEntry(v.id, names[v.id], _TYPE_WORDS.get(v.view_type.value, "drawing").capitalize(),
                                 _GROUP_OF.get(v.view_type.value, "Sheet furniture"), state, note))
    order = {title: i for i, (title, _k) in enumerate(_GROUPS)}
    entries.sort(key=lambda e: (order.get(e.group, 99), e.name.lower()))
    return entries


@dataclass
class RejectedView:
    id: str
    name: str
    reason: str
    explanation: str
    engineer: str
    when: str


def build_rejected(project: OracleProject, source_id: str) -> list:
    arch = project.architecture_of(source_id)
    names = view_names(project, source_id)
    rows = []
    for v in arch.views:
        if v.review != ReviewStatus.REJECTED:
            continue
        decision = _latest_review_decision(project, v.id)
        reason = REJECT_REASON_LABEL.get(decision.reason_code, "") if decision and decision.reason_code else ""
        rows.append(RejectedView(v.id, names[v.id], reason or "No reason category recorded", (decision.reason or "") if decision else "",
                                 decision.author if decision else "", when(decision.created_at) if decision else ""))
    return rows


def _latest_review_decision(project: OracleProject, view_id: str):
    record = project.value_status_of(Target.architectural(view_id), "review")
    if record is not None and record.decision_id:
        return project.get_decision(record.decision_id)
    return None


# ---------------------------------------------------------------- questions in words

@dataclass
class Suggestion:
    id: str
    label: str                 # what Oracle proposes, in words
    confidence: str            # High / Medium / Low
    consequence: str           # what accepting it would do, in words
    leading: bool


_FIELD_WORDS = {"units": "unit", "view_type": "kind of view", "level_key": "floor", "title": "title", "name": "name", "elevation_mm": "elevation",
                "structural_elevation_mm": "structural elevation"}
_UNIT_WORDS = {"mm": "millimetres", "cm": "centimetres", "m": "metres", "inch": "inches", "foot": "feet"}


def plain_effect(effect, names: dict, source_label: str) -> str:
    """What accepting a suggestion WOULD do, in an engineer's words (the machine-readable effect stays in the project)."""
    p, kind = effect.params, effect.kind
    if kind == EffectKind.SET_VALUE:
        field_name, value = p["field"], p["value"]
        if field_name == "units" and isinstance(value, dict):
            return f"Treat the drawing as being in {_UNIT_WORDS.get(value.get('unit'), value.get('unit'))}"
        target = names.get(effect.target.id, None)
        what = _FIELD_WORDS.get(field_name, field_name.replace("_", " "))
        shown = level_display_name(value) if field_name == "level_key" and isinstance(value, str) else str(value).replace("_", " ")
        return f"Set the {what} of \u201c{target}\u201d to {shown}" if target else f"Set the {what} to {shown}"
    if kind == EffectKind.ACCEPT_HEIGHT:
        return f"Use {p['height_mm']:g} mm as the height from {level_display_name(p['from_level'])} to {level_display_name(p['to_level'])}"
    if kind == EffectKind.ALIGN_VIEW:
        return f"Place \u201c{names.get(p['view_id'], 'the plan')}\u201d on the building this way"
    if kind == EffectKind.MERGE_VIEWS:
        return "Combine " + ", ".join(f"\u201c{names.get(v, v)}\u201d" for v in p["view_ids"]) + " into one view"
    if kind == EffectKind.SPLIT_VIEW:
        return f"Split \u201c{names.get(p['view_id'], 'the view')}\u201d into separate views"
    return p.get("note") or "Record this answer; nothing else changes"


def _alt_label(meaning: str) -> str:
    text = meaning.replace("_", " ")
    return _MEANING_WORDS.get(text) or (text[:1].upper() + text[1:] if text else text)


def _question_title(card, project, names, source_label) -> str:
    q = friendly(project, card.question, names, source_label)
    return q


def _question_detail(card, project, names, source_label) -> str:
    unit = ""
    if card.kind == "units":
        for arch in project.architectures:
            if arch.has(card.affected[0]) if card.affected else False:
                d = arch.drawing
                if d.declared_unit and d.declared_unit != d.units.unit:
                    return f"The file says {d.declared_unit}, but the drawing itself looks like {d.units.unit}."
    source = card.issues[0].message if card.issues else (card.observed[0] if card.observed else "")
    return _sentence(friendly(project, source, names, source_label)) or unit


@dataclass
class QueueItem:
    key: str
    kind: str
    title: str
    detail: str
    done: bool = False
    view_ids: list = field(default_factory=list)
    set_id: Optional[str] = None
    short: str = ""

    @property
    def label(self) -> str:
        """The one-line form shown in the queue list (the full wording is on the action card)."""
        text = self.short or self.title
        return text if len(text) <= 64 else text[:63].rstrip() + "…"


def _short_title(card, title: str, names: dict) -> str:
    views = [a for a in card.affected if a in names]
    if card.kind == "units":
        return "Unit of the drawing"
    if card.kind == "view type":
        return f"What is “{names[views[0]]}”?" if views else "An unclear view"
    if card.kind == "alignment":
        return f"Placing “{names[views[0]]}”" if views else "Plan alignment"
    if card.kind == "storey height":
        return "Height " + title.replace("What is the height ", "").replace("?", "") if title.startswith("What is the height") else title
    if card.kind == "levels":
        m = re.search(r"level name (.+?) appears", title)
        return f"{m.group(1)} has two elevations" if m else "Which levels the building has"
    return title


_ORDER = {"units": 0, "view type": 1, "views": 2, "level of a plan": 3, "levels": 4, "storey height": 5, "alignment": 6, "view level": 7,
          "establish": 8, "interpretation": 9, "issue": 10}


def _has_established_levels(project: OracleProject) -> bool:
    return bool(project.building and project.building.levels)


def _plan_levels_detected(project: OracleProject, source_id: str) -> bool:
    return any(v.view_type == ViewType.FLOOR_PLAN and v.level_key for v in project.architecture_of(source_id).views
               if v.review not in (ReviewStatus.SUPERSEDED, ReviewStatus.REJECTED))


def _unnamed_plans(project: OracleProject, source_id: str) -> list:
    return [v for v in project.architecture_of(source_id).views
            if v.view_type == ViewType.FLOOR_PLAN and v.variant is None and v.level_key is None
            and v.review not in (ReviewStatus.SUPERSEDED, ReviewStatus.REJECTED)]


def _unreviewed_views(project: OracleProject, source_id: str) -> list:
    return [v for v in project.architecture_of(source_id).views
            if v.review == ReviewStatus.PROPOSED and v.view_type in (ViewType.FLOOR_PLAN, ViewType.SECTION, ViewType.ELEVATION)]


def build_queue(project: OracleProject, source_id: str) -> tuple:
    """(items that need the engineer, in the order to work through them; completed items)."""
    names = view_names(project, source_id)
    label = _drawing_label(project, source_id)
    arch = project.architecture_of(source_id)
    todo, done = [], []
    for card in rm.build_questions(project, source_id):
        title = _question_title(card, project, names, label)
        if card.status == "open":
            todo.append(QueueItem(f"set:{card.id}", card.kind, title, _question_detail(card, project, names, label), False,
                                  [a for a in card.affected if a in names], card.id, _short_title(card, title, names)))
        else:
            done.append(QueueItem(f"set:{card.id}", card.kind, title, _decision_line(project, card), True, [], card.id, _short_title(card, title, names)))
    unreviewed = _unreviewed_views(project, source_id)
    if unreviewed:
        kinds = {}
        for v in unreviewed:
            kinds[v.view_type.value] = kinds.get(v.view_type.value, 0) + 1
        parts = [f"{n} {_TYPE_WORDS[k]}{'s' if n != 1 else ''}" for k, n in kinds.items()]
        todo.append(QueueItem("views:confirm", "views", f"Confirm the {len(unreviewed)} drawing view{'s' if len(unreviewed) != 1 else ''} Oracle found",
                              "Check " + ", ".join(parts) + " against the drawing.", False, [v.id for v in unreviewed]))
    for v in _unnamed_plans(project, source_id):
        todo.append(QueueItem(f"view_level:{v.id}", "view level", f"Which floor is “{names[v.id]}”?",
                              "This plan does not say which level it belongs to.", False, [v.id]))
    if not _has_established_levels(project) and _plan_levels_detected(project, source_id):
        todo.append(QueueItem("levels:establish", "establish", "Confirm the building levels",
                              "Oracle read level names from the drawing. You decide the levels and their elevations.", False))
    linked = {c.id for c in rm.build_questions(project, source_id)}
    unnamed_ids = {v.id for v in _unnamed_plans(project, source_id)}
    for row in rm.build_issue_rows(project, source_id, include_closed=False):
        if row.set_id in linked or row.severity not in ("blocking", "error") or any(a in unnamed_ids for a in row.affected):
            continue
        todo.append(QueueItem(f"issue:{row.id}", "issue", _sentence(friendly(project, row.message, names, label), 110),
                              "Oracle cannot settle this without you.", False, [a for a in row.affected if a in names]))
    todo.sort(key=lambda i: (_ORDER.get(i.kind, 9), i.key))
    accepted = [v for v in arch.views if v.review == ReviewStatus.ACCEPTED]
    rejected = [v for v in arch.views if v.review == ReviewStatus.REJECTED]
    if accepted:
        done.append(QueueItem("views:accepted", "views", f"{len(accepted)} view{'s' if len(accepted) != 1 else ''} accepted",
                              "Confirmed by the engineer.", True, [v.id for v in accepted]))
    if rejected:
        done.append(QueueItem("views:rejected", "views", f"{len(rejected)} view{'s' if len(rejected) != 1 else ''} rejected",
                              "Excluded by the engineer; the evidence and your reason are kept.", True, [v.id for v in rejected]))
    if _has_established_levels(project):
        n = len(project.building.levels)
        done.append(QueueItem("levels:done", "establish", f"{n} building level{'s' if n != 1 else ''} established", "Confirmed by the engineer.", True))
    return todo, done


def _decision_line(project: OracleProject, card) -> str:
    s = project.get_interpretation_set(card.id)
    alt = s.accepted
    if alt is None:
        return "No suggestion applied: the engineer rejected all of them."
    if alt.origin == "engineer":
        return "Engineer input: " + _sentence(alt.meaning, 120)
    return "Engineer decision: " + _alt_label(alt.meaning)


# ---------------------------------------------------------------- overview

@dataclass
class Overview:
    drawing: str
    revision: Optional[str]
    lines: list                # (icon, text, tone) tone: ok | attention | note
    needs: int
    complete: bool
    needs_lines: list          # "1 unit question", "2 views need confirmation"
    resolved_lines: list
    guidance_count: int


_KIND_NOUN = {"units": ("unit question", "unit questions"), "view type": ("view to identify", "views to identify"),
              "alignment": ("plan alignment", "plan alignments"), "levels": ("level question", "level questions"),
              "storey height": ("storey height", "storey heights"), "level of a plan": ("plan level", "plan levels"),
              "view level": ("plan without a level", "plans without a level"), "establish": ("set of levels to confirm", "sets of levels to confirm"),
              "issue": ("other item", "other items"), "interpretation": ("open question", "open questions")}


def build_overview(project: OracleProject, source_id: str) -> Overview:
    arch = project.architecture_of(source_id)
    todo, done = build_queue(project, source_id)
    views = [v for v in arch.views if v.review != ReviewStatus.SUPERSEDED and v.view_type != ViewType.TITLE_BLOCK]
    lines = [("✓", "Drawing interpreted", "ok"), ("✓", f"{len(views)} views found", "ok")]
    if todo:
        lines.append(("⚠", f"{len(todo)} item{'s' if len(todo) != 1 else ''} need{'' if len(todo) != 1 else 's'} your review", "attention"))
    else:
        lines.append(("✓", "Architectural review complete", "ok"))
    needs, counts = [], {}
    for item in todo:
        if item.kind == "views":
            n = len(item.view_ids)
            needs.append(f"{n} view{'s' if n != 1 else ''} need confirmation")
        else:
            counts[item.kind] = counts.get(item.kind, 0) + 1
    for kind, n in counts.items():
        one, many = _KIND_NOUN.get(kind, ("item", "items"))
        needs.append(f"{n} {one if n == 1 else many}")
    resolved = [i.title for i in done if i.key.startswith(("views:", "levels:"))]
    answered = sum(1 for i in done if i.key.startswith("set:"))
    if answered:
        resolved.append(f"{answered} question{'s' if answered != 1 else ''} answered")
    d = arch.drawing
    return Overview(d.file, d.revision, lines, len(todo), not todo, needs, resolved, len(project.clarifications))


# ---------------------------------------------------------------- the action card

@dataclass
class ActionCard:
    key: str
    kind: str                                   # question | answered | confirm_views | view_level | establish | issue | view | complete
    title: str
    body: str
    facts: list = field(default_factory=list)   # short plain sentences
    suggestions: list = field(default_factory=list)
    actions: list = field(default_factory=list)  # action ids, the primary one first
    engineer_status: str = ""                   # "Engineer decision: ..." for something already decided
    engineer_notes: list = field(default_factory=list)   # (who, when, statement, "kept as guidance for the next stage")
    state: str = ""                             # for a view: needs_review | accepted | rejected
    set_id: Optional[str] = None
    view_ids: list = field(default_factory=list)
    rejection: Optional[RejectedView] = None
    ask_target: Optional[str] = None            # the object the engineer's words attach to
    type_label: str = ""                        # for a view: "Plan", "Section" ...
    menu: list = field(default_factory=list)    # for a view: the contextual "View Actions" entries


def _notes_for(project: OracleProject, ids: list) -> list:
    out = []
    wanted = set(ids)
    for c in project.clarifications:
        if c.target.id in wanted or (c.target.scope == TargetScope.PROJECT and not wanted):
            note = "Applied to the model." if c.disposition == "applied" else "Kept as guidance for the next stage; not yet applied to the model."
            out.append((c.author, when(c.created_at), c.statement + (f"\n{c.notes}" if c.notes else ""), note))
    return out


def _default_ask_target(project: OracleProject, source_id: str, affected: list) -> str:
    return affected[0] if len(affected) == 1 else source_id


def build_card(project: OracleProject, source_id: str, key: str) -> ActionCard:
    names = view_names(project, source_id)
    label = _drawing_label(project, source_id)
    if key.startswith("set:"):
        card = next(c for c in rm.build_questions(project, source_id) if c.id == key[4:])
        title = _question_title(card, project, names, label)
        body = _question_detail(card, project, names, label)
        ask = _default_ask_target(project, source_id, [a for a in card.affected if a in names] or [source_id])
        notes = _notes_for(project, [ask])
        if card.status != "open":
            return ActionCard(key, "answered", title, body, engineer_status=_decision_line(project, card), engineer_notes=notes,
                              actions=["review_evidence"], set_id=card.id, ask_target=ask)
        stored = {a.id: a for a in project.get_interpretation_set(card.id).alternatives}
        suggestions = [Suggestion(a.id, _alt_label(a.meaning), confidence_words(a.confidence),
                                  "; ".join(plain_effect(e, names, label) for e in stored[a.id].effects) or "Record this answer; nothing else changes", a.is_leading)
                       for a in card.alternatives if a.status == "proposed"]
        actions = ["accept_suggestion"] + (["choose_another"] if len(suggestions) > 1 else []) + (["enter_value"] if card.kind == "storey height" else []) \
            + ["ask_engineer", "review_evidence"]
        facts = [_sentence(friendly(project, o, names, label), 200) for o in card.observed[1:3]] if len(card.observed) > 1 else []
        return ActionCard(key, "question", title, body, facts, suggestions, actions, engineer_notes=notes, set_id=card.id, ask_target=ask,
                          view_ids=[a for a in card.affected if a in names])
    if key == "views:confirm":
        unreviewed = _unreviewed_views(project, source_id)
        return ActionCard(key, "confirm_views", "Confirm the drawing views",
                          "Oracle found these views and proposes what each one is. Check them against the drawing. Nothing is accepted until you accept it.",
                          [names[v.id] + f"  ({_TYPE_WORDS[v.view_type.value]})" for v in unreviewed], [], ["confirm_all", "review_views"],
                          view_ids=[v.id for v in unreviewed], ask_target=source_id, engineer_notes=_notes_for(project, [source_id]))
    if key.startswith("view_level:"):
        vid = key.split(":", 1)[1]
        return ActionCard(key, "view_level", f"Which floor is “{names[vid]}”?",
                          "Oracle could not tell which level this plan belongs to. Say which floor it is, or ask Oracle to treat it differently.",
                          actions=["set_view_level", "ask_engineer", "review_evidence"], view_ids=[vid], ask_target=vid,
                          engineer_notes=_notes_for(project, [vid]))
    if key == "levels:establish":
        return ActionCard(key, "establish", "Confirm the building levels",
                          "Oracle read level names (and sometimes elevations) from the drawing. Levels become part of the model only when you confirm them.",
                          actions=["set_levels", "ask_engineer", "review_evidence"], ask_target=source_id, engineer_notes=_notes_for(project, [source_id]))
    if key.startswith("issue:"):
        row = next(r for r in rm.build_issue_rows(project, source_id) if r.id == key[6:])
        target = [a for a in row.affected if a in names]
        return ActionCard(key, "issue", _sentence(friendly(project, row.message, names, label), 110), "Oracle cannot settle this without you.",
                          actions=["acknowledge_issue", "ask_engineer", "review_evidence"], view_ids=target, ask_target=(target[0] if target else source_id),
                          engineer_notes=_notes_for(project, target or [source_id]))
    if key.startswith("view:"):
        return build_view_card(project, source_id, key[5:])
    return build_complete_card(project, source_id)


def build_view_card(project: OracleProject, source_id: str, view_id: str) -> ActionCard:
    arch = project.architecture_of(source_id)
    names = view_names(project, source_id)
    v = arch.get(view_id)
    state = state_of(v.review)
    kind = _TYPE_WORDS.get(v.view_type.value, "drawing")
    facts = []
    if v.view_type == ViewType.FLOOR_PLAN and v.level_key:
        facts.append(f"Level: {level_display_name(v.level_key)}")
    history = project.decision_history(Target.architectural(view_id), "view_type")
    if history:
        original = history[0].previous_value if history[0].previous_value is not None else v.view_type.value
        facts.append(f"Oracle read this view as: {type_label(original)}. You corrected it to: {type_label(v.view_type)}.")
    if v.view_type == ViewType.FLOOR_PLAN and v.variant is None and v.alignment_frame_id is None and state != "rejected":
        facts.append("Alignment required: this plan is not yet placed on the building.")
    open_sets = [s for s in project.interpretation_sets if s.status == SetStatus.OPEN and view_id in rm.affected_ids(project, s)]
    if open_sets:
        facts.append(f"Oracle has {len(open_sets)} open question{'s' if len(open_sets) != 1 else ''} about this view.")
    if state == "needs_review":
        actions = ["accept_view", "view_actions"]
        body = "Oracle proposes this reading of the view. It is a suggestion until you accept it."
        status = ""
    elif state == "accepted":
        actions = ["reconsider", "view_actions"]
        body, status = "You accepted this view.", "Accepted"
    else:
        actions = ["reconsider", "review_evidence"]
        body, status = "This view is excluded from the architectural interpretation. The drawing evidence and your decision are kept.", "Rejected"
    rejection = next((r for r in build_rejected(project, source_id) if r.id == view_id), None)
    is_plan = v.view_type == ViewType.FLOOR_PLAN
    menu = ["change_type", *(["align_plan"] if is_plan and state != "rejected" else []), "split_view", *(["reject_view"] if state != "rejected" else []),
            "ask_engineer", "review_evidence"]
    return ActionCard(f"view:{view_id}", "view", names[view_id], body, facts, [], actions, engineer_status=status, type_label=type_label(v.view_type),
                      menu=menu,
                      engineer_notes=_notes_for(project, [view_id]), state=state, view_ids=[view_id], rejection=rejection, ask_target=view_id,
                      set_id=open_sets[0].id if open_sets else None)


def build_complete_card(project: OracleProject, source_id: str) -> ActionCard:
    guidance = len(project.clarifications)
    body = "Every item Oracle was unsure about has been decided."
    if guidance:
        body += f" {guidance} note{'s' if guidance != 1 else ''} you wrote {'are' if guidance != 1 else 'is'} kept as guidance for the next stage."
    return ActionCard("complete", "complete", "Architectural review complete", body, actions=["save_project", "review_evidence"],
                      engineer_notes=_notes_for(project, [source_id]))


# ---------------------------------------------------------------- drawing sources in words

def source_choices(project: OracleProject) -> list:
    """[(source id, label)] for the source picker: the drawing's name and revision, never its internal id or hash."""
    rows, seen = [], {}
    for a in project.architectures:
        d = a.drawing
        stem = d.file.rsplit(".", 1)[0] if "." in d.file else d.file
        label = stem + (f" \u2014 Revision {d.revision}" if d.revision else "")
        seen[label] = seen.get(label, 0) + 1
        rows.append([d.id, label])
    for row in rows:
        if seen[row[1]] > 1:
            n = sum(1 for r in rows[:rows.index(row)] if r[1] == row[1]) + 1
            row[1] += f" ({n})"
    return [tuple(r) for r in rows]
