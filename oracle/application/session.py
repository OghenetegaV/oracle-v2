"""Oracle — Architectural Session (application layer)

Purpose:
    The one object an interface talks to for the architectural-drawing workflow. It owns the OracleProject being worked on (the ONLY
    state: there is no second, interface-side copy), runs the interpretation of a chosen drawing through the real pipeline in honest
    stages, reads the project into review rows (oracle.application.review_models), resolves what a selection means on the drawing
    (oracle.application.preview), and turns each engineer action into the ordinary domain call: an accepted ENGINEER decision applied
    through set_value, accept_interpretation, review_views, establish_levels, align_view, merge_views and so on. It opens and saves
    projects, and keeps a geometry cache beside a saved project so the drawing preview does not need the interpretation to be rerun.

Role in Oracle:
    The thin application layer between an interface and the backend:  UI -> ArchitecturalSession -> interpretation -> core. The interface
    never touches a CAD library, the pipeline internals or the project's JSON. Oracle only PROPOSES; every consequential change made
    here is an engineer decision recorded in the project, and a refused action changes nothing and says why. Failures during
    interpretation become a WorkflowError with a plain explanation and the file named; the technical traceback goes to the injected
    logger (the wizard passes Oracle's own event log), never to the user.

Dependencies:
    oracle.core, oracle.ingestion, oracle.interpretation, oracle.application.files, .preview, .review_models; standard library.

Consumers:
    oracle.ui.architectural_workspace, the wizard integration, tests.

Status:
    Application layer (interface phase).

Migration/Notes:
    Several sources are supported: an ambiguous request (approved architecture with two sources and none chosen) raises
    SourceChoiceRequired so the interface asks the engineer; nothing silently takes the first source. Revision carry-forward is not
    implemented. The geometry cache (<project>.geometry.json.gz) is a preview convenience, not part of the project schema.
"""

from __future__ import annotations

import getpass
import gzip
import json
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, Effect, EffectKind, EngineeringDecision, InterpretationStatus, OracleProject, ReviewStatus, SchemaVersionError,
    SetStatus, Target, TargetScope, ValidationError, ViewType,
)
from oracle.ingestion import DrawingDocument, IngestionError, read_drawing
from oracle.interpretation import align_view, establish_levels, interpret_document, interpret_into, suggest_elevations
from oracle.interpretation.layers import block_meaning
from oracle.interpretation.pipeline import STAGES as PIPELINE_STAGES

from . import alignment, guide
from . import review_models as rm
from .files import DrawingFileInfo, inspect_drawing_file
from .preview import DrawingPreview, Overlay, union_box

# ---------------------------------------------------------------- stages the engineer sees

STAGE_LABELS = {
    "validate": "Validating the file",
    "read": "Reading source geometry",
    "units": "Detecting drawing units",
    "layers": "Classifying layers",
    "regions": "Detecting drawing regions and identifying views",
    "views": "Building views and coordinate frames",
    "analysis": "Detecting levels and analysing architectural observations",
    "alignment": "Aligning plans",
    "reconcile": "Detecting ambiguities and conflicts",
    "project": "Building the project model",
    "review": "Preparing engineer review",
}
WORKFLOW_STAGES = ("validate", "read", *PIPELINE_STAGES, "review")


@dataclass(frozen=True)
class StageEvent:
    key: str
    label: str
    state: str                  # "start", "done" or "failed"
    index: int                  # 1-based position in WORKFLOW_STAGES
    total: int


class WorkflowError(Exception):
    """Something went wrong in a way the engineer needs to know about, in words an engineer can act on."""

    def __init__(self, title: str, message: str, *, file: Optional[str] = None, stage: Optional[str] = None,
                 logged: bool = False):
        super().__init__(message)
        self.title, self.message, self.file, self.stage, self.logged = title, message, file, stage, logged


class ActionRefused(Exception):
    """An engineer action the domain model refused. Nothing was changed."""


class SourceChoiceRequired(Exception):
    """More than one drawing source and none chosen: the engineer must say which one. Oracle does not guess."""

    def __init__(self, sources: list):
        super().__init__("This project has several drawing sources; choose which one to work with.")
        self.sources = sources


def _stage_event(key: str, state: str) -> StageEvent:
    return StageEvent(key, STAGE_LABELS[key], state, WORKFLOW_STAGES.index(key) + 1, len(WORKFLOW_STAGES))


def cache_path_for(project_path) -> Path:
    p = Path(project_path)
    stem = p.name[:-5] if p.name.endswith(".json") else p.name
    return p.with_name(stem + ".geometry.json.gz")


class ArchitecturalSession:
    def __init__(self, engineer: Optional[str] = None, logger: Optional[Callable[[str, str], None]] = None):
        self.engineer = (engineer or "").strip() or _default_engineer()
        self._logger = logger
        self.project: Optional[OracleProject] = None
        self.project_path: Optional[Path] = None
        self.dirty = False
        self.active_source_id: Optional[str] = None
        self.documents: dict = {}            # source id -> DrawingDocument (the geometry the interpreter used)
        self.source_paths: dict = {}         # source id -> where the drawing was read from
        self._previews: dict = {}
        self.geometry_note: dict = {}        # source id -> why the preview has no linework, if it has none

    # ------------------------------------------------------------ small helpers

    def log(self, kind: str, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(kind, message)
            except Exception:
                pass                          # logging must never be why an action fails

    @property
    def has_project(self) -> bool:
        return self.project is not None and bool(self.project.architectures)

    def inspect_file(self, path) -> DrawingFileInfo:
        return inspect_drawing_file(path)

    def sources(self) -> list:
        return rm.source_infos(self.project) if self.project else []

    def set_active_source(self, source_id: str) -> None:
        self.project.architecture_of(source_id)
        self.active_source_id = source_id

    def source_id(self, source_id: Optional[str] = None) -> str:
        """The source a request is about: the one named, else the active one, else the only one. With several and no choice this raises
        SourceChoiceRequired instead of taking the first."""
        if self.project is None or not self.project.architectures:
            raise ActionRefused("There is no architectural interpretation in this project yet.")
        if source_id:
            self.project.architecture_of(source_id)
            return source_id
        if self.active_source_id and any(a.drawing.id == self.active_source_id for a in self.project.architectures):
            return self.active_source_id
        if len(self.project.architectures) == 1:
            return self.project.architectures[0].drawing.id
        raise SourceChoiceRequired(self.sources())

    # ------------------------------------------------------------ interpretation

    def interpret(self, path, *, engineer: Optional[str] = None, project_name: Optional[str] = None, add_source: bool = False,
                  revision: Optional[str] = None, progress: Optional[Callable[[StageEvent], None]] = None) -> str:
        """Validate, read and interpret a drawing through the real pipeline; the project becomes the result. Returns the new source id.
        `progress` hears about each stage as it starts and only says "done" when that stage has really finished."""
        if engineer is not None and engineer.strip():
            self.engineer = engineer.strip()
        emit = progress or (lambda event: None)
        current = ["validate"]
        p = Path(str(path))

        def run(key: str):
            current[0] = key
            emit(_stage_event(key, "start"))

        def done(key: str):
            emit(_stage_event(key, "done"))

        try:
            run("validate")
            info = self.inspect_file(p)
            if not info.ok:
                raise WorkflowError("This file cannot be used", info.problem or "The file cannot be read.", file=str(p), stage="validate")
            done("validate")
            run("read")
            document = read_drawing(p)
            done("read")

            def on_pipeline(stage, event):
                current[0] = stage
                emit(_stage_event(stage, event))

            base = OracleProject.from_dict(self.project.to_dict()) if (add_source and self.project is not None) else None
            if base is not None:
                target = interpret_into(base, document, revision=revision, progress=on_pipeline)
            else:
                target = interpret_document(document, project_name=project_name or p.stem, engineer=self.engineer, revision=revision,
                                            progress=on_pipeline)
            run("review")
            new_source = target.architectures[-1].drawing.id
            # exercise every read model once, so a problem in showing the result surfaces here as a stage failure, not later
            rm.build_summary(target, new_source)
            rm.build_view_rows(target, new_source)
            rm.build_levels(target, new_source)
            rm.build_observation_groups(target, new_source)
            rm.build_questions(target, new_source)
            rm.build_readiness(target)
            done("review")
        except WorkflowError as exc:
            emit(_stage_event(current[0], "failed"))
            exc.file = exc.file or str(p)
            raise
        except Exception as exc:                                        # noqa: BLE001 - reported, logged, never swallowed
            emit(_stage_event(current[0], "failed"))
            raise self._workflow_error(exc, p, current[0]) from None

        was_add = add_source and self.project is not None
        self.project = target
        self.documents[new_source] = document
        self.source_paths[new_source] = str(p.resolve())
        self._previews.pop(new_source, None)
        self.geometry_note.pop(new_source, None)
        self.active_source_id = new_source
        if not was_add:
            self.project_path = None
        self.dirty = True
        return new_source

    def _workflow_error(self, exc: BaseException, path: Path, stage: str) -> WorkflowError:
        technical = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log("architectural.interpretation_failed", f"file: {path}\nstage: {stage}\n{technical}")
        name = path.name
        if isinstance(exc, IngestionError):
            return WorkflowError("Oracle could not read this drawing", f"{exc}", file=str(path), stage=stage, logged=True)
        if isinstance(exc, MemoryError):
            return WorkflowError("Not enough memory", f"{name} is too large to interpret with the memory available. Close other programs and "
                                 "try again, or split the drawing.", file=str(path), stage=stage, logged=True)
        if isinstance(exc, (ValidationError, SchemaVersionError)):
            return WorkflowError("The interpretation could not be assembled", f"Oracle read {name} but could not build a consistent project "
                                 f"from it ({exc}). The details were recorded in Oracle's log.", file=str(path), stage=stage, logged=True)
        return WorkflowError("Oracle could not interpret this drawing", f"Something unexpected happened while Oracle was working on "
                             f"{name} (during: {STAGE_LABELS.get(stage, stage).lower()}). Nothing was changed. The technical details were "
                             "recorded in Oracle's log; you can try again or choose another drawing.", file=str(path), stage=stage, logged=True)

    # ------------------------------------------------------------ opening and saving

    def open_project(self, path) -> None:
        p = Path(str(path))
        try:
            project = OracleProject.load(p)
        except FileNotFoundError:
            raise WorkflowError("Project not found", f"There is no project file at {p}.", file=str(p)) from None
        except (ValidationError, SchemaVersionError) as exc:
            self.log("architectural.open_project_failed", f"file: {p}\n{traceback.format_exc()}")
            raise WorkflowError("This project cannot be opened", f"{p.name} is not a project this version of Oracle can open: {exc}",
                                file=str(p), logged=True) from None
        except Exception as exc:                                        # noqa: BLE001
            self.log("architectural.open_project_failed", f"file: {p}\n{traceback.format_exc()}")
            raise WorkflowError("This project cannot be opened", f"{p.name} could not be read ({exc.__class__.__name__}). The details were "
                                "recorded in Oracle's log.", file=str(p), logged=True) from None
        self.project = project
        self.project_path = p
        self.dirty = False
        self.documents, self.source_paths, self._previews, self.geometry_note = {}, {}, {}, {}
        self.active_source_id = project.architectures[0].drawing.id if len(project.architectures) == 1 else None
        self._load_geometry_cache(p)

    def _load_geometry_cache(self, project_path: Path) -> None:
        cache = cache_path_for(project_path)
        blob = {}
        if cache.exists():
            try:
                with gzip.open(cache, "rt", encoding="utf-8") as handle:
                    blob = json.load(handle)
            except Exception:                                           # noqa: BLE001 - a bad cache only costs the linework
                self.log("architectural.geometry_cache_unreadable", f"{cache}\n{traceback.format_exc()}")
                blob = {}
        for arch in self.project.architectures:
            sid, sha = arch.drawing.id, arch.drawing.sha256
            entry = (blob.get("sources") or {}).get(sid)
            if entry and entry.get("sha256") == sha:
                self.documents[sid] = DrawingDocument.from_dict(entry["document"])
                self.source_paths[sid] = entry.get("source_path") or ""
            else:
                self.source_paths[sid] = (entry or {}).get("source_path") or ""
                self.geometry_note[sid] = ("The drawing linework was not saved with this project, so the preview shows only what Oracle "
                                           "stored (views and observation outlines). Use 'Reload linework' to read it again from the drawing "
                                           "file; the interpretation is not rerun.")

    def save_project(self, path=None) -> Path:
        if self.project is None:
            raise ActionRefused("There is nothing to save yet.")
        target = Path(str(path)) if path else self.project_path
        if target is None:
            raise ActionRefused("Choose a file name to save the project.")
        try:
            self.project.validate()
        except ValidationError as exc:
            self.log("architectural.save_refused", traceback.format_exc())
            raise ActionRefused(f"The project is not consistent and was not saved: {exc}") from None
        self.project.save(target)
        if self.documents:
            blob = {"format": 1, "sources": {sid: {"sha256": doc.sha256, "source_path": self.source_paths.get(sid, ""), "document": doc.to_dict()}
                                              for sid, doc in self.documents.items()}}
            cache = cache_path_for(target)
            tmp = cache.with_name(cache.name + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                json.dump(blob, handle)
            os.replace(tmp, cache)
        self.project_path = target
        self.dirty = False
        return target

    def reload_geometry(self, source_id: Optional[str] = None, path=None, progress=None) -> None:
        """Read the drawing file again ONLY to draw it (no interpretation). The file must be the one the project was made from."""
        sid = self.source_id(source_id)
        arch = self.project.architecture_of(sid)
        p = Path(str(path or self.source_paths.get(sid) or ""))
        if not p.is_file():
            raise ActionRefused(f"Choose the drawing file this source was made from ({arch.drawing.file}).")
        try:
            doc = read_drawing(p)
        except Exception as exc:                                        # noqa: BLE001
            self.log("architectural.reload_geometry_failed", traceback.format_exc())
            raise WorkflowError("Oracle could not read this drawing", str(exc), file=str(p), logged=True) from None
        if doc.sha256 != arch.drawing.sha256:
            raise ActionRefused(f"{p.name} is not the drawing this source was made from (its content differs), so it was not used.")
        self.documents[sid] = doc
        self.source_paths[sid] = str(p.resolve())
        self._previews.pop(sid, None)
        self.geometry_note.pop(sid, None)

    # ------------------------------------------------------------ read models

    def summary(self, source_id=None):
        return rm.build_summary(self.project, self.source_id(source_id))

    def view_rows(self, source_id=None):
        return rm.build_view_rows(self.project, self.source_id(source_id))

    def levels(self, source_id=None):
        return rm.build_levels(self.project, self.source_id(source_id))

    def observation_groups(self, source_id=None):
        return rm.build_observation_groups(self.project, self.source_id(source_id))

    def questions(self, source_id=None, *, open_only=False):
        return rm.build_questions(self.project, self.source_id(source_id), open_only=open_only)

    def issues(self, source_id=None, *, include_closed=True):
        return rm.build_issue_rows(self.project, self.source_id(source_id), include_closed=include_closed)

    def readiness(self):
        return rm.build_readiness(self.project)

    def approved(self, source_id=None):
        return rm.build_approved(self.project, self.source_id(source_id))

    def decision_rows(self) -> list:
        rows = []
        for d in self.project.decisions:
            target = "project" if d.target.scope == TargetScope.PROJECT else f"{d.target.scope.value} {d.target.id}"
            change = f"{d.field} = {d.value!r}" + (f" (was {d.previous_value!r})" if d.previous_value is not None else "") if d.field else ""
            rows.append((d.id, d.author, d.source.value, d.status.value, target, d.instruction, change, d.reason or ""))
        return rows

    def evidence_for_object(self, object_id: str) -> list:
        return [f"{r.method} by {r.producer}" + (f": {r.note}" if r.note else "") + (f" [entity {r.source.entity_handle}]" if r.source.entity_handle else "")
                for r in self.project.provenance_for(Target.architectural(object_id))][:12]

    # ------------------------------------------------------------ preview and what a selection means on the drawing

    def preview(self, source_id=None) -> Optional[DrawingPreview]:
        sid = self.source_id(source_id)
        if sid not in self._previews and sid in self.documents:
            made = DrawingPreview(self.documents[sid])
            made.set_structural_filter({l.name: l.semantic_class for l in self.project.architecture_of(sid).layers}, block_meaning)
            self._previews[sid] = made
        return self._previews.get(sid)

    def view_overlays(self, source_id=None) -> Overlay:
        sid = self.source_id(source_id)
        arch = self.project.architecture_of(sid)
        style = {ReviewStatus.PROPOSED: "view_proposed", ReviewStatus.ACCEPTED: "view_accepted", ReviewStatus.REJECTED: "view_rejected"}
        o = Overlay()
        for v in arch.views:
            if v.review not in (ReviewStatus.SUPERSEDED, ReviewStatus.REJECTED) and v.view_type != ViewType.TITLE_BLOCK:
                o.rects.append((v.bbox, style.get(v.review, "view_proposed"), v.title or v.id, v.id))
        return o

    def overlay_for_object(self, object_id: str, style: str = "selected") -> Overlay:
        arch = next((a for a in self.project.architectures if a.has(object_id)), None)
        o = Overlay()
        if arch is None:
            return o
        obj = arch.get(object_id)
        box = getattr(obj, "bbox", None)
        if box:
            o.rects.append((box, style, (self.view_name(object_id) if hasattr(obj, "view_type") else getattr(obj, "title", None)) or object_id, object_id))
            o.focus = box
        geometry = getattr(obj, "geometry", None)
        if geometry:
            pts = [tuple(p) for p in geometry]
            o.polylines.append((pts + ([pts[0]] if getattr(obj, "closed", False) and len(pts) > 2 else []), style))
            o.focus = union_box([o.focus, (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))])
        entity_ids = list(getattr(obj, "entity_ids", ()) or ())
        if entity_ids and hasattr(obj, "kind"):
            o.entity_ids = entity_ids
            preview = self.preview(arch.drawing.id) if arch.drawing.id in self.documents else None
            if preview is not None:
                o.focus = union_box([o.focus, preview.box_of_entities(entity_ids)]) or o.focus
        return o

    def overlay_for_question(self, set_id: str) -> Overlay:
        s = self.project.get_interpretation_set(set_id)
        o = Overlay()
        for oid in rm.affected_ids(self.project, s):
            o.merge(self.overlay_for_object(oid, "unresolved" if s.status == SetStatus.OPEN else "approved"))
        return o

    def overlay_for_issue(self, issue_id: str) -> Overlay:
        row = next((r for r in rm.build_issue_rows(self.project) if r.id == issue_id), None)
        o = Overlay()
        for oid in (row.affected if row else []):
            o.merge(self.overlay_for_object(oid, "unresolved"))
        return o

    def overlay_approved(self, source_id=None) -> Overlay:
        sid = self.source_id(source_id)
        o = Overlay()
        for v in self.project.architecture_of(sid).views:
            if v.review == ReviewStatus.ACCEPTED:
                o.rects.append((v.bbox, "approved", v.title or v.id, v.id))
        return o

    # ------------------------------------------------------------ engineer decisions (each is a real, recorded decision)

    def _next_decision_id(self) -> str:
        numbers = [int(m.group(1)) for d in self.project.decisions if (m := re.fullmatch(r"ENG-(\d+)", d.id))]
        return f"ENG-{(max(numbers) if numbers else 0) + 1:04d}"

    def _decision(self, target: Target, instruction: str, reason: Optional[str], *, field: Optional[str] = None, value=None,
                  reason_code: Optional[str] = None) -> EngineeringDecision:
        if not self.engineer:
            raise ActionRefused("Enter the engineer's name before making a decision.")
        return EngineeringDecision(self._next_decision_id(), self.engineer, DecisionSource.ENGINEER, target,
                                   DecisionCategory.ARCHITECTURAL_COORDINATION, instruction, reason=(reason or "").strip() or None,
                                   status=DecisionStatus.ACCEPTED, field=field, value=value, reason_code=reason_code)

    def _refuse(self, exc: Exception):
        self.log("architectural.action_refused", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        raise ActionRefused(str(exc)) from None

    def accept_alternative(self, set_id: str, interpretation_id: str, reason: str = "") -> EngineeringDecision:
        """The engineer chooses one reading of an open question. Its effects (a value, a height, an alignment ...) are carried out through
        the ordinary decision machinery, the other readings are rejected, and the linked issues are resolved, all or nothing."""
        s = self.project.get_interpretation_set(set_id)
        try:
            alt = s.get(interpretation_id)
        except ValidationError as exc:
            self._refuse(exc)
        if s.status != SetStatus.OPEN or alt.status != InterpretationStatus.PROPOSED:
            raise ActionRefused(f"{set_id} has already been settled.")
        subject = s.subject if s.subject is not None else Target.architectural(self.source_id())
        text = f"Accepted '{alt.meaning.replace('_', ' ')}' for: {s.question}"
        decision = self._decision(subject, text, reason)
        try:
            if alt.effects:                                             # rehearse on a copy so a refusal leaves no orphan decision
                trial = OracleProject.from_dict(self.project.to_dict())
                trial.add_decision(self._decision(subject, text, reason))
                trial.accept_interpretation(set_id, interpretation_id, decision.id)
            self.project.add_decision(decision)
            self.project.accept_interpretation(set_id, interpretation_id, decision.id)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def reject_alternative(self, set_id: str, interpretation_id: str, reason: str = "") -> EngineeringDecision:
        s = self.project.get_interpretation_set(set_id)
        try:
            alt = s.get(interpretation_id)
        except ValidationError as exc:
            self._refuse(exc)
        if alt.status != InterpretationStatus.PROPOSED:
            raise ActionRefused(f"'{alt.meaning}' has already been {alt.status.value}.")
        subject = s.subject if s.subject is not None else Target.architectural(self.source_id())
        decision = self._decision(subject, f"Rejected '{alt.meaning.replace('_', ' ')}' for: {s.question}", reason)
        try:
            self.project.add_decision(decision)
            self.project.reject_interpretation(set_id, interpretation_id, decision.id)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def review_views(self, view_ids, *, accept: bool = True, reason: str = "") -> EngineeringDecision:
        ids = list(view_ids)
        if not ids:
            raise ActionRefused("Select at least one view.")
        decision = self._decision(Target.architectural(ids[0]), f"{'Approved' if accept else 'Rejected'} {len(ids)} view(s): {', '.join(ids[:6])}", reason)
        try:
            self.project.review_views(ids, decision, accept=accept)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    # ------------------------------------------------------------ the calmer review: rejecting views, reconsidering, Ask Engineer

    def _closable_sets(self, project: OracleProject, view_ids: set) -> list:
        """Open questions that are ABOUT the views being rejected (they would ask the engineer about something excluded)."""
        found = []
        for s in project.interpretation_sets:
            if s.status != SetStatus.OPEN:
                continue
            targets = set()
            if s.subject is not None and s.subject.scope == TargetScope.ARCHITECTURAL:
                targets.add(s.subject.id)
            for alt in s.alternatives:
                for e in alt.effects:
                    if e.kind in (EffectKind.ALIGN_VIEW, EffectKind.SPLIT_VIEW):
                        targets.add(e.params["view_id"])
                    elif e.kind == EffectKind.SET_VALUE and e.target.scope == TargetScope.ARCHITECTURAL:
                        targets.add(e.target.id)
            affected = set(rm.affected_ids(project, s))
            if (targets and targets <= view_ids) or (affected and affected <= view_ids):
                found.append(s)
        return found

    def _closable_issues(self, project: OracleProject, view_ids: set, closing_sets: list) -> list:
        set_ids = {s.id for s in closing_sets}
        found = []
        for row in rm.build_issue_rows(project, include_closed=False):
            if row.set_id in set_ids or (row.affected and set(row.affected) <= view_ids):
                found.append(row.id)
        return found

    def rejection_consequences(self, view_ids) -> dict:
        """What rejecting these views would also close, so the dialog can say so before the engineer decides."""
        ids = set(view_ids)
        sets = self._closable_sets(self.project, ids)
        return {"questions": len(sets), "issues": len(self._closable_issues(self.project, ids, sets))}

    def reject_views(self, view_ids, reason_code: str, explanation: str = "") -> EngineeringDecision:
        """The engineer excludes views from the architectural interpretation, with a reason. Nothing is deleted: the views, their
        observations and their evidence stay in the project, marked rejected under one recorded engineer decision that carries the
        reason. Questions and issues that only concern the rejected views are closed by the same decision (and say so)."""
        ids = list(dict.fromkeys(view_ids))
        if not ids:
            raise ActionRefused("Select at least one view.")
        if reason_code not in guide.REJECT_REASON_LABEL:
            raise ActionRefused("Choose why the view should be excluded.")
        if reason_code == "other" and not (explanation or "").strip():
            raise ActionRefused("Say why the view should be excluded.")
        names = guide.view_names(self.project, self.source_id_of(ids[0]))
        text = f"Rejected {len(ids)} view(s) ({', '.join(names.get(i, i) for i in ids[:4])}): {guide.REJECT_REASON_LABEL[reason_code]}"
        decision = self._decision(Target.architectural(ids[0]), text, explanation, reason_code=reason_code)

        def apply(project: OracleProject) -> None:
            project.review_views(ids, decision, accept=False)
            sets = self._closable_sets(project, set(ids))
            issues = self._closable_issues(project, set(ids), sets)
            for s in sets:
                for alt in s.alternatives:
                    if alt.status == InterpretationStatus.PROPOSED:
                        project.reject_interpretation(s.id, alt.id, decision.id)
            for issue_id in issues:
                if project.get_issue(issue_id).is_open:
                    project.accept_issue(issue_id, "Closed because the engineer rejected the view it concerns.", decision.id)

        try:
            apply(OracleProject.from_dict(self.project.to_dict()))          # rehearse: a refusal changes nothing
            apply(self.project)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def reconsider_views(self, view_ids, reason: str = "") -> EngineeringDecision:
        """Take an earlier decision about views back to 'needs review'. A new engineer decision; the earlier one stays on record."""
        ids = list(view_ids)
        if not ids:
            raise ActionRefused("Select at least one view.")
        decision = self._decision(Target.architectural(ids[0]), f"Reconsidered {len(ids)} view(s): {', '.join(ids[:6])}", reason)
        try:
            self.project.reconsider_views(ids, decision)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def source_id_of(self, object_id: str) -> str:
        arch = next((a for a in self.project.architectures if a.has(object_id)), None)
        if arch is None:
            raise ActionRefused("That item is not part of this project.")
        return arch.drawing.id

    def ask_engineer(self, target_id: Optional[str], statement: str, notes: str = "", set_id: Optional[str] = None):
        """The engineer says, in their own words, what Oracle should understand. The words are kept verbatim as an engineer clarification
        (identity, time, target, decision); if it answers an open question, that question is settled by the engineer's own answer. The words
        are never turned into model changes here: they are guidance flagged for the next stage."""
        statement = (statement or "").strip()
        if not statement:
            raise ActionRefused("Write what Oracle should understand.")
        target = Target.architectural(target_id) if target_id and target_id != "project" else Target.project()
        decision = self._decision(target, statement, notes)
        try:
            if set_id:
                clarification = self.project.answer_with_engineer_input(set_id, decision, statement, notes, target=target)
            else:
                clarification = self.project.record_clarification(decision, target, statement, notes)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return clarification

    def answer_height(self, set_id: str, height_mm: float):
        """The engineer supplies a storey height for an open height question. A VALUE, so it is applied as a structured effect."""
        s = self.project.get_interpretation_set(set_id)
        effect = next((e for a in s.alternatives for e in a.effects if e.kind == EffectKind.ACCEPT_HEIGHT), None)
        if effect is None:
            raise ActionRefused("This question is not about a storey height.")
        try:
            mm = float(height_mm)
        except (TypeError, ValueError):
            raise ActionRefused("Enter the height in millimetres.") from None
        if mm <= 0:
            raise ActionRefused("A storey height must be more than zero.")
        from oracle.interpretation.naming import level_display_name
        low, high = effect.params["from_level"], effect.params["to_level"]
        statement = f"The height from {level_display_name(low)} to {level_display_name(high)} is {mm:g} mm."
        target = s.subject if s.subject is not None else Target.architectural(self.source_id())
        decision = self._decision(target, statement, "Value entered by the engineer.")
        try:
            clarification = self.project.answer_with_engineer_input(
                set_id, decision, statement, "Value entered by the engineer.", target=target, effects=(Effect.accept_height(low, high, mm),))
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return clarification

    # ------------------------------------------------------------ the calmer review: what to show

    def overview(self, source_id=None):
        return guide.build_overview(self.project, self.source_id(source_id))

    def queue(self, source_id=None):
        return guide.build_queue(self.project, self.source_id(source_id))

    def view_entries(self, source_id=None):
        return guide.build_view_entries(self.project, self.source_id(source_id))

    def rejected_views(self, source_id=None):
        return guide.build_rejected(self.project, self.source_id(source_id))

    def card(self, key: str, source_id=None):
        return guide.build_card(self.project, self.source_id(source_id), key)

    def source_choices(self) -> list:
        """[(source id, label)]: the drawing's name and revision, for the source picker."""
        return guide.source_choices(self.project) if self.project else []

    def view_name(self, view_id: str) -> str:
        return guide.view_names(self.project, self.source_id_of(view_id)).get(view_id, view_id)

    def clarification_rows(self) -> list:
        """The engineer's own words, oldest first: (id, when, who, about, statement, what happened to it)."""
        rows = []
        for c in self.project.clarifications:
            about = "the project"
            if c.target.scope == TargetScope.ARCHITECTURAL:
                try:
                    sid = self.source_id_of(c.target.id)
                    about = guide.view_names(self.project, sid).get(c.target.id) or ("the drawing" if c.target.id == sid else c.target.id)
                except ActionRefused:
                    about = c.target.id
            fate = "applied to the model" if c.disposition == "applied" else "kept as guidance for the next stage"
            rows.append((c.id, guide.when(c.created_at), c.author, about, c.statement, fate))
        return rows

    def review_observations(self, observation_ids, *, accept: bool = True, reason: str = "") -> EngineeringDecision:
        ids = list(observation_ids)
        if not ids:
            raise ActionRefused("Select at least one observation.")
        decision = self._decision(Target.architectural(ids[0]), f"{'Approved' if accept else 'Rejected'} {len(ids)} observation(s)", reason)
        try:
            self.project.review_observations(ids, decision, accept=accept)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def approve_hint(self, observation_id: str, reason: str = "") -> EngineeringDecision:
        """Approve the reading Oracle proposed for an observation (for example 'column candidate'). It approves the PROPOSAL, nothing structural."""
        arch, obs = next(((a, a.get(observation_id)) for a in self.project.architectures if a.has(observation_id)), (None, None))
        if obs is None or getattr(obs, "hint", None) is None:
            raise ActionRefused("This observation carries no proposed reading to approve.")
        return self.set_value(Target.architectural(observation_id), "hint", obs.hint.value, reason,
                              instruction=f"Approved the proposed reading '{obs.hint.value.replace('_', ' ')}' for {observation_id}")

    def set_value(self, target: Target, field: str, value, reason: str = "", *, instruction: Optional[str] = None) -> EngineeringDecision:
        """Override or supply one value (a level's name or elevation, a view's title or level ...) through the decision system."""
        decision = self._decision(target, instruction or f"Set {field.replace('_', ' ')} of {target.scope.value} {target.id} to {value!r}", reason,
                                  field=field, value=value)
        try:
            self.project.set_value(target, field, value, decision)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def rename_level(self, level_id: str, name: str, reason: str = "") -> EngineeringDecision:
        if not name.strip():
            raise ActionRefused("A level needs a name.")
        return self.set_value(Target.level(level_id), "name", name.strip(), reason)

    def set_level_value(self, level_id: str, field: str, value_mm: float, reason: str = "") -> EngineeringDecision:
        """Set an established level's elevation or structural elevation (millimetres) as an engineer decision."""
        if field not in ("elevation_mm", "structural_elevation_mm"):
            raise ActionRefused(f"A level's {field} is not edited here.")
        return self.set_value(Target.level(level_id), field, float(value_mm), reason)

    def suggest_level_elevations(self, source_id=None) -> Optional[dict]:
        """What the drawing supports without doubt (or None): an Oracle SUGGESTION, relative to the lowest plan level, not a decision."""
        return suggest_elevations(self.project, self.source_id(source_id))

    def establish_levels(self, elevations_mm: dict, *, elevation_type: str = "unspecified", reason: str = "", source_id=None) -> EngineeringDecision:
        if not elevations_mm:
            raise ActionRefused("There are no levels to establish.")
        sid = self.source_id(source_id)
        decision = self._decision(Target.project(), f"Established {len(elevations_mm)} building level(s) from {sid}", reason)
        try:
            establish_levels(self.project, elevations_mm, decision, elevation_type=elevation_type, source_id=sid)
        except (ValidationError, ValueError) as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def set_view_field(self, view_id: str, field: str, value, reason: str = "") -> EngineeringDecision:
        if field not in ("title", "level_key", "view_type", "section_label", "orientation"):
            raise ActionRefused(f"A view's {field} is not edited here.")
        return self.set_value(Target.architectural(view_id), field, value, reason)

    def align_view(self, view_id: str, dx: float, dy: float, reason: str = "", *, set_id=None, interpretation_id=None,
                   rotation_deg: float = 0.0) -> EngineeringDecision:
        try:
            decision = align_view(self.project, view_id, (float(dx), float(dy)), engineer=self.engineer, reason=(reason or "Set by the engineer."),
                                  set_id=set_id, interpretation_id=interpretation_id, rotation_deg=float(rotation_deg))
        except (ValidationError, ValueError) as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    # ------------------------------------------------------------ point-pick alignment, split preview, view type

    def view_box(self, view_id: str) -> tuple:
        return self.project.architecture_of(self.source_id_of(view_id)).get(view_id).bbox

    def plan_choices(self, view_id: str) -> list:
        """The other floor plans of the same source that this plan can be aligned to: [(id, name)], plans already on the building frame first."""
        arch = self.project.architecture_of(self.source_id_of(view_id))
        names = guide.view_names(self.project, arch.drawing.id)
        plans = [v for v in arch.views if v.view_type == ViewType.FLOOR_PLAN and v.id != view_id and v.review not in (ReviewStatus.SUPERSEDED, ReviewStatus.REJECTED)
                 and v.frame_id is not None]
        plans.sort(key=lambda v: (v.variant is not None, v.alignment_frame_id is None, v.id))
        return [(v.id, names[v.id]) for v in plans]

    def alignment_preview(self, view_id: str, reference_id: str, pairs: list):
        """Where the plan would land on the reference plan, from the engineer's point pairs. Reads only: the project is not touched."""
        arch = self.project.architecture_of(self.source_id_of(view_id))
        view, reference = arch.get(view_id), arch.get(reference_id)
        if view.view_type != ViewType.FLOOR_PLAN:
            raise ActionRefused("Only a floor plan can be aligned to another plan.")
        preview = self.preview(arch.drawing.id) if arch.drawing.id in self.documents else None
        paths = [preview.paths[i].points for i in preview.paths_for(view.entity_ids) if not preview.is_hidden(i, True)] if preview is not None else []
        try:
            return alignment.solve(arch, view, reference, pairs, paths)
        except (ValueError, ValidationError) as exc:
            raise ActionRefused(str(exc)) from None

    def apply_alignment(self, view_id: str, reference_id: str, pairs: list, note: str = "") -> EngineeringDecision:
        """The engineer accepts the previewed alignment: recorded as one engineer decision (through the ordinary alignment mechanism). The source
        coordinates are not changed; the points they picked are kept in the decision's reason."""
        solution = self.alignment_preview(view_id, reference_id, pairs)
        names = guide.view_names(self.project, self.source_id_of(view_id))
        picked = "; ".join(f"({p[0]:.1f}, {p[1]:.1f}) on {names[view_id]} = ({q[0]:.1f}, {q[1]:.1f}) on {names.get(reference_id, reference_id)}" for p, q in pairs)
        reason = (note.strip() + " " if note and note.strip() else "") + f"Point-picked alignment (source units): {picked}."
        return self.align_view(view_id, solution.translation[0], solution.translation[1], reason, rotation_deg=solution.rotation_deg)

    def _split_plan(self, view_id: str, axis: str, coordinate: float):
        arch = next((a for a in self.project.architectures if a.has(view_id)), None)
        if arch is None:
            raise ActionRefused(f"{view_id} is not a view of this project.")
        view = arch.get(view_id)
        preview = self.preview(arch.drawing.id) if arch.drawing.id in self.documents else None
        if preview is None:
            raise ActionRefused("Splitting needs the drawing linework to place each entity; use 'Reload linework' first.")
        axis = axis.lower()
        if axis not in ("x", "y"):
            raise ActionRefused("Split along x or y.")
        low, high = [], []
        for eid in view.entity_ids:
            box = preview.entity_boxes.get(eid)
            if box is None:
                continue
            centre = (box[0] + box[2]) / 2 if axis == "x" else (box[1] + box[3]) / 2
            (low if centre < float(coordinate) else high).append(eid)
        if not low or not high or len(low) + len(high) != len(view.entity_ids):
            raise ActionRefused(f"Splitting {self.view_name(view_id)} along {axis} = {float(coordinate):g} would leave one part empty; choose a dividing line inside the view.")
        return arch, view, preview, axis, low, high

    def split_preview(self, view_id: str, axis: str, coordinate: float) -> dict:
        """What splitting would make: the dividing line and the two resulting regions. Reads only."""
        _arch, view, preview, axis, low, high = self._split_plan(view_id, axis, coordinate)
        x0, y0, x1, y1 = view.bbox
        line = [(coordinate, y0), (coordinate, y1)] if axis == "x" else [(x0, coordinate), (x1, coordinate)]
        return {"line": line, "low": preview.box_of_entities(low), "high": preview.box_of_entities(high), "counts": (len(low), len(high))}

    def change_view_type(self, view_id: str, new_type: str, note: str = "") -> EngineeringDecision:
        """The engineer corrects what kind of view Oracle took this to be. The view is NOT rejected or replaced: its type is changed by an engineer
        decision that keeps the previous value (Oracle's reading), so both stay in the history."""
        arch = next((a for a in self.project.architectures if a.has(view_id)), None)
        if arch is None:
            raise ActionRefused(f"{view_id} is not a view of this project.")
        allowed = {value for value, _label in guide.VIEW_TYPE_CHOICES}
        if new_type not in allowed:
            raise ActionRefused("Choose one of: " + ", ".join(label for _v, label in guide.VIEW_TYPE_CHOICES) + ".")
        view = arch.get(view_id)
        if view.review == ReviewStatus.SUPERSEDED:
            raise ActionRefused("This view was merged or split; change the views that replaced it.")
        if view.view_type.value == new_type:
            raise ActionRefused(f"This view is already a {guide.type_label(new_type).lower()}.")
        name = guide.view_names(self.project, arch.drawing.id)[view_id]
        text = f"Corrected the type of {name} from {guide.type_label(view.view_type.value)} to {guide.type_label(new_type)}"
        if view.level_key and new_type != "floor_plan":
            text += f" (a section, elevation or detail has no floor level, so the level {guide.level_display_name(view.level_key)} no longer applies)"
        decision = self._decision(Target.architectural(view_id), text, note, field="view_type", value=new_type)
        try:
            self.project.change_view_type(view_id, new_type, decision)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision

    def _next_view_ids(self, count: int) -> list:
        numbers = [int(v.id[5:]) for a in self.project.architectures for v in a.views if v.id[5:].isdigit()]
        start = max(numbers) + 1 if numbers else 1
        return [f"VIEW-{n:02d}" for n in range(start, start + count)]

    def merge_views(self, view_ids, reason: str = "") -> str:
        ids = list(view_ids)
        if len(ids) < 2:
            raise ActionRefused("Select two or more views to merge.")
        (new_id,) = self._next_view_ids(1)
        decision = self._decision(Target.architectural(ids[0]), f"Merged {', '.join(ids)} into one view ({new_id})", reason)
        try:
            self.project.merge_views(ids, new_id, decision)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return new_id

    def split_view(self, view_id: str, axis: str, coordinate: float, reason: str = "") -> list:
        """Split a view along x = coordinate or y = coordinate (source drawing units), each source entity going to the side its centre is on."""
        _arch, _view, preview, axis, low, high = self._split_plan(view_id, axis, coordinate)
        ids = self._next_view_ids(2)
        parts = {ids[0]: {"bbox": preview.box_of_entities(low), "entity_ids": low}, ids[1]: {"bbox": preview.box_of_entities(high), "entity_ids": high}}
        decision = self._decision(Target.architectural(view_id), f"Split {view_id} along {axis} = {float(coordinate):g} into {ids[0]} and {ids[1]}", reason)
        try:
            self.project.split_view(view_id, parts, decision)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return ids

    def accept_issue(self, issue_id: str, reason: str) -> EngineeringDecision:
        """The engineer knowingly accepts an issue as it stands (with a stated reason). It stays on record as accepted, not as fixed."""
        if not (reason or "").strip():
            raise ActionRefused("Accepting an issue needs a reason.")
        try:
            issue = self.project.get_issue(issue_id)
        except ValidationError as exc:
            self._refuse(exc)
        decision = self._decision(issue.target, f"Accepted issue {issue_id}: {issue.message[:120]}", reason)
        try:
            self.project.add_decision(decision)
            self.project.accept_issue(issue_id, reason.strip(), decision.id)
        except ValidationError as exc:
            self._refuse(exc)
        self.dirty = True
        return decision


def _default_engineer() -> str:
    try:
        return getpass.getuser()
    except Exception:                                                   # noqa: BLE001
        return ""
