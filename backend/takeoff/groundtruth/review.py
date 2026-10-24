"""Review workflow: phase machine, candidate review, blind scan, reconciliation.

Phases per sheet: pass1 -> blind_scan -> reconcile -> verified. Every action
checks the sheet's phase, so the rules hold for any client (UI, API, tests):

* pass1      annotate; accept/correct/reject candidates; mark cells pass1_done
* blind_scan annotations and candidates are inaccessible (BlindScanInProgress);
             record independent observations; mark cells blind_scan_done
* reconcile  match / promote / dismiss observations; decide annotations not
             seen in the blind scan; complete and verify annotations
* verified   read-only until an explicit, logged reopen
"""

from __future__ import annotations

from takeoff.groundtruth import checks as K
from takeoff.groundtruth import models as M
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.protocol import DUPLICATE_TOLERANCE_PT, MATCH_TOLERANCE_PT
from takeoff.groundtruth.sheets import SheetSpec
from takeoff.groundtruth.store import Store


class ReviewError(ValueError):
    code = "invalid"


class PhaseError(ReviewError):
    code = "wrong_phase"


class BlindScanInProgress(PhaseError):
    code = "blind_scan_in_progress"


class GateError(ReviewError):
    code = "gate_not_met"

    def __init__(self, transition: str, reasons: list[str]):
        super().__init__(f"cannot {transition}: " + "; ".join(reasons))
        self.reasons = reasons


SYSTEM = "system"


class Workflow:
    def __init__(self, store: Store, specs: dict[str, SheetSpec]):
        self.store = store
        self.specs = specs
        for spec in specs.values():
            self._ensure_sheet(spec)

    # --- setup and lookups -----------------------------------------------------------------

    def _ensure_sheet(self, spec: SheetSpec) -> None:
        if self.store.find("sheets", spec.sheet_id) is None:
            self.store.insert("sheets", M.SheetReview(sheet_id=spec.sheet_id, page_number=spec.page_number,
                                                      region_bbox_pt=spec.region_bbox_pt), SYSTEM)
        existing = {c.id for c in self.store.list("cells", spec.sheet_id)}
        for c in spec.cells:
            if c["id"] not in existing:
                self.store.insert("cells", M.ReviewCell(id=c["id"], sheet_id=spec.sheet_id, row=c["row"], col=c["col"],
                                                        bbox=c["bbox"], bbox_pt=c["bbox_pt"]), SYSTEM)

    def spec(self, sheet_id: str) -> SheetSpec:
        if sheet_id not in self.specs:
            raise ReviewError(f"unknown sheet {sheet_id!r}")
        return self.specs[sheet_id]

    def sheet(self, sheet_id: str) -> M.SheetReview:
        self.spec(sheet_id)
        return self.store.get("sheets", sheet_id)

    def phase(self, sheet_id: str) -> str:
        return self.sheet(sheet_id).phase

    def _require(self, sheet_id: str, *phases: str) -> M.SheetReview:
        s = self.sheet(sheet_id)
        if s.phase not in phases:
            if s.phase == "blind_scan":
                raise BlindScanInProgress(f"sheet {sheet_id} is in blind scan; annotations and candidates are hidden")
            raise PhaseError(f"sheet {sheet_id} is in phase {s.phase!r}; this action needs {list(phases)}")
        return s

    def require_not_blind(self, sheet_id: str) -> None:
        """For read endpoints that expose annotations, candidates or warnings."""
        if self.phase(sheet_id) == "blind_scan":
            raise BlindScanInProgress(f"sheet {sheet_id} is in blind scan; annotations and candidates are hidden")

    def _annotation(self, annotation_id: str) -> M.Annotation:
        a = self.store.get("annotations", annotation_id)
        if a.deleted:
            raise ReviewError("annotation is deleted")
        return a

    def _point_pt(self, spec: SheetSpec, point: dict) -> tuple[float, float]:
        x, y = spec.to_pt((point["u"], point["v"]))
        return (round(x, 3), round(y, 3))

    # --- region and cells ---------------------------------------------------------------------

    def confirm_region(self, sheet_id: str, actor: str, confirmed: bool, reason: str | None = None) -> M.SheetReview:
        s = self._require(sheet_id, "pass1")
        if not confirmed and not (reason and reason.strip()):
            raise ReviewError("flagging the region requires a reason")
        new = s.model_copy(update={"region_confirmed": confirmed, "region_flag_reason": None if confirmed else reason})
        return self.store.replace("sheets", new, actor, "confirm_region" if confirmed else "flag_region")

    def mark_cell(self, sheet_id: str, cell_id: str, actor: str, done: bool = True) -> M.ReviewCell:
        s = self._require(sheet_id, "pass1", "blind_scan")
        cell = self.store.get("cells", cell_id)
        if cell.sheet_id != sheet_id:
            raise ReviewError("cell belongs to another sheet")
        if s.phase == "pass1":
            if done:
                open_here = [cl for cl in K.candidate_clusters(self.store, self.spec(sheet_id))
                             if cl["cell_id"] == cell_id and cl["unreviewed"]]
                if open_here:
                    raise ReviewError(f"{len(open_here)} candidate clusters in this cell are unreviewed")
            upd = {"pass1_done": done, "pass1_by": actor if done else None, "pass1_at": M.utcnow() if done else None}
            action = "pass1_done" if done else "pass1_undone"
        else:
            upd = {"blind_scan_done": done, "blind_scan_by": actor if done else None,
                   "blind_scan_at": M.utcnow() if done else None}
            action = "blind_scan_done" if done else "blind_scan_undone"
        return self.store.replace("cells", cell.model_copy(update=upd), actor, action)

    # --- pass 1: annotations ---------------------------------------------------------------------

    def create_annotation(self, sheet_id: str, actor: str, category: str, subtype: str, point: dict,
                          **fields) -> M.Annotation:
        s = self._require(sheet_id, "pass1", "reconcile")
        spec = self.spec(sheet_id)
        if fields.get("review_state") == "verified":
            raise ReviewError("annotations are verified only during reconciliation, via verify")
        prov = fields.pop("provenance", {"origin": "manual"})
        if prov.get("origin") != "manual":
            raise ReviewError("use accept_cluster or promote for candidate or blind-scan origins")
        a = M.Annotation(sheet_id=sheet_id, page_number=spec.page_number, category=category, subtype=subtype,
                         point=point, point_pt=self._point_pt(spec, point), provenance=prov, created_by=actor,
                         **fields)
        if s.phase == "reconcile":
            # Added after the blind scan: no independent observation exists, so say so.
            a = a.model_copy(update={"blind_confirmation": "not_seen_in_blind_scan",
                                     "blind_decision_reason": fields.get("blind_decision_reason") or
                                     "added during reconciliation"})
        return self.store.create_annotation(a, actor)

    def update_annotation(self, annotation_id: str, actor: str, **changes) -> M.Annotation:
        a = self._annotation(annotation_id)
        self._require(a.sheet_id, "pass1", "reconcile")
        if changes.get("review_state") == "verified":
            raise ReviewError("use verify to mark an annotation verified")
        if "provenance" in changes or "blind_confirmation" in changes:
            raise ReviewError("provenance and blind confirmation are set by the workflow, not edited")
        if "point" in changes:
            changes["point_pt"] = self._point_pt(self.spec(a.sheet_id), changes["point"])
        if a.review_state == "verified" and set(changes) - {"review_state"}:
            changes.setdefault("review_state", "draft")  # any substantive edit needs re-verification
        return self.store.update_annotation(annotation_id, actor, **changes)

    def delete_annotation(self, annotation_id: str, actor: str, reason: str) -> M.Annotation:
        a = self._annotation(annotation_id)
        self._require(a.sheet_id, "pass1", "reconcile")
        if not (reason and reason.strip()):
            raise ReviewError("deleting an annotation requires a reason")
        return self.store.delete_annotation(annotation_id, actor, reason)

    # --- pass 1: candidates ------------------------------------------------------------------------

    def clusters(self, sheet_id: str) -> list[dict]:
        self.require_not_blind(sheet_id)
        return K.candidate_clusters(self.store, self.spec(sheet_id))

    def _cluster(self, sheet_id: str, cluster_id: str) -> dict:
        for cl in K.candidate_clusters(self.store, self.spec(sheet_id)):
            if cl["id"] == cluster_id:
                return cl
        raise ReviewError(f"unknown candidate cluster {cluster_id!r}")

    def accept_cluster(self, sheet_id: str, cluster_id: str, actor: str, category: str | None = None,
                       subtype: str | None = None, point: dict | None = None, **fields) -> M.Annotation:
        """Turn a candidate cluster into one draft annotation.

        Outcome 'accepted' when the reviewer keeps the best member's class hint and
        position; 'corrected' when the class or position was changed.
        """
        self._require(sheet_id, "pass1")
        cl = self._cluster(sheet_id, cluster_id)
        if any(m["outcome"] != "unreviewed" for m in cl["members"]):
            raise ReviewError("cluster already reviewed")
        best = cl["members"][0]
        category = category or best["category_hint"]
        subtype = subtype or best["subtype_hint"]
        if not category or not subtype:
            raise ReviewError("this candidate has no class hint; choose a category and subtype")
        corrected = (category, subtype) != (best["category_hint"], best["subtype_hint"]) or point is not None
        spec = self.spec(sheet_id)
        pt = point or cl["point"]
        prov = M.Provenance(origin="candidate", candidate_ids=[m["candidate_id"] for m in cl["members"]],
                            generator=best["generator"], generator_version=best["generator_version"],
                            score=best["score"])
        with self.store.transaction() as conn:
            a = M.Annotation(sheet_id=sheet_id, page_number=spec.page_number, category=category, subtype=subtype,
                             point=pt, point_pt=self._point_pt(spec, pt), bbox=cl["bbox"] if point is None else None,
                             provenance=prov, created_by=actor, **fields)
            self.store.create_annotation(a, actor, conn=conn)
            for m in cl["members"]:
                self.store.replace("candidate_reviews", M.CandidateReview(
                    candidate_id=m["candidate_id"], outcome="corrected" if corrected else "accepted",
                    annotation_id=a.id, actor=actor, at=M.utcnow()), actor, "corrected" if corrected else "accepted",
                    conn=conn)
        return a

    def reject_cluster(self, sheet_id: str, cluster_id: str, actor: str, reason: str) -> list[str]:
        self._require(sheet_id, "pass1")
        cl = self._cluster(sheet_id, cluster_id)
        return self._reject([m["candidate_id"] for m in cl["members"] if m["outcome"] == "unreviewed"], actor, reason)

    def reject_unreviewed_in_cell(self, sheet_id: str, cell_id: str, actor: str, reason: str) -> list[str]:
        self._require(sheet_id, "pass1")
        ids = [m["candidate_id"] for cl in K.candidate_clusters(self.store, self.spec(sheet_id)) if cl["cell_id"] == cell_id
               for m in cl["members"] if m["outcome"] == "unreviewed"]
        return self._reject(ids, actor, reason)

    def _reject(self, candidate_ids: list[str], actor: str, reason: str) -> list[str]:
        if not (reason and reason.strip()):
            raise ReviewError("rejecting candidates requires a reason")
        with self.store.transaction() as conn:
            for cid in candidate_ids:
                self.store.replace("candidate_reviews", M.CandidateReview(
                    candidate_id=cid, outcome="rejected", reason=reason, actor=actor, at=M.utcnow()),
                    actor, "rejected", conn=conn)
        return candidate_ids

    # --- phase transitions ------------------------------------------------------------------------------

    def start_blind_scan(self, sheet_id: str, actor: str) -> M.SheetReview:
        s = self._require(sheet_id, "pass1")
        reasons = K.gate_pass1_to_blind(self.store, self.spec(sheet_id))
        if reasons:
            raise GateError("start the blind scan", reasons)
        return self.store.replace("sheets", s.model_copy(update={"phase": "blind_scan",
                                                                 "blind_scan_started_at": M.utcnow()}),
                                  actor, "start_blind_scan")

    def open_reconcile(self, sheet_id: str, actor: str) -> M.SheetReview:
        """Closes the blind scan for good, then reveals annotations for reconciliation."""
        s = self._require(sheet_id, "blind_scan")
        reasons = K.gate_blind_to_reconcile(self.store, self.spec(sheet_id))
        if reasons:
            raise GateError("open reconciliation", reasons)
        return self.store.replace("sheets", s.model_copy(update={"phase": "reconcile",
                                                                 "blind_scan_closed_at": M.utcnow()}),
                                  actor, "open_reconcile")

    def complete_sheet(self, sheet_id: str, actor: str) -> M.SheetReview:
        s = self._require(sheet_id, "reconcile")
        reasons = K.gate_reconcile_to_verified(self.store, self.spec(sheet_id))
        if reasons:
            raise GateError("verify the sheet", reasons)
        return self.store.replace("sheets", s.model_copy(update={"phase": "verified",
                                                                 "reconcile_completed_at": M.utcnow()}),
                                  actor, "verify_sheet")

    def reopen_sheet(self, sheet_id: str, actor: str, reason: str) -> M.SheetReview:
        s = self._require(sheet_id, "verified")
        if not (reason and reason.strip()):
            raise ReviewError("reopening a verified sheet requires a reason")
        return self.store.replace("sheets", s.model_copy(update={"phase": "reconcile", "reconcile_completed_at": None}),
                                  actor, f"reopen: {reason}")

    # --- blind scan ---------------------------------------------------------------------------------------

    def add_observation(self, sheet_id: str, actor: str, point: dict, category: str,
                        subtype: str | None = None, note: str | None = None) -> M.BlindObservation:
        self._require(sheet_id, "blind_scan")
        spec = self.spec(sheet_id)
        o = M.BlindObservation(sheet_id=sheet_id, cell_id=spec.cell_at((point["u"], point["v"])), point=point,
                               category=category, subtype=subtype, note=note, created_by=actor)
        return self.store.insert("observations", o, actor)

    def update_observation(self, observation_id: str, actor: str, **changes) -> M.BlindObservation:
        o = self.store.get("observations", observation_id)
        self._require(o.sheet_id, "blind_scan")
        if o.deleted:
            raise ReviewError("observation is deleted")
        allowed = {"point", "category", "subtype", "note"}
        if set(changes) - allowed:
            raise ReviewError(f"only {sorted(allowed)} can change during the blind scan")
        data = {**o.model_dump(), **changes}
        if "point" in changes:
            data["cell_id"] = self.spec(o.sheet_id).cell_at((changes["point"]["u"], changes["point"]["v"]))
        return self.store.replace("observations", M.BlindObservation.model_validate(data), actor, "update")

    def delete_observation(self, observation_id: str, actor: str) -> M.BlindObservation:
        o = self.store.get("observations", observation_id)
        self._require(o.sheet_id, "blind_scan")
        return self.store.replace("observations", o.model_copy(update={"deleted": True}), actor, "delete")

    def observations(self, sheet_id: str) -> list[M.BlindObservation]:
        self.spec(sheet_id)
        return self.store.list("observations", sheet_id)

    # --- reconciliation -----------------------------------------------------------------------------------------

    def suggestions(self, sheet_id: str) -> list[dict]:
        """For each pending observation: nearest annotation within MATCH_TOLERANCE_PT, with conflict flags."""
        self._require(sheet_id, "reconcile", "verified")
        spec = self.spec(sheet_id)
        anns = self.store.list("annotations", sheet_id)
        out = []
        for o in self.store.list("observations", sheet_id):
            if o.reconciliation != "pending":
                continue
            near = sorted(((spec.distance_pt((o.point.u, o.point.v), (a.point.u, a.point.v)), a) for a in anns),
                          key=lambda t: t[0])
            in_range = [(d, a) for d, a in near if d <= MATCH_TOLERANCE_PT]
            best = in_range[0] if in_range else None
            conflicts = []
            if best and best[1].category != o.category:
                conflicts.append(f"nearest annotation is {best[1].category}, observation says {o.category}")
            if len(in_range) > 1:
                conflicts.append(f"{len(in_range)} annotations within {MATCH_TOLERANCE_PT} pt")
            if best and best[1].blind_confirmation == "confirmed":
                conflicts.append("nearest annotation is already matched")
            out.append({"observation_id": o.id, "suggested_annotation_id": best[1].id if best else None,
                        "distance_pt": round(best[0], 2) if best else None, "conflicts": conflicts})
        return out

    def _pending_observation(self, observation_id: str) -> M.BlindObservation:
        o = self.store.get("observations", observation_id)
        self._require(o.sheet_id, "reconcile")
        if o.deleted:
            raise ReviewError("observation was deleted during the blind scan")
        if o.reconciliation != "pending":
            raise ReviewError(f"observation already {o.reconciliation}")
        return o

    def match(self, observation_id: str, annotation_id: str, actor: str, reason: str | None = None) -> M.Annotation:
        """Link an observation to the annotation of the same device. Creates no record."""
        o = self._pending_observation(observation_id)
        a = self._annotation(annotation_id)
        if a.sheet_id != o.sheet_id:
            raise ReviewError("observation and annotation are on different sheets")
        if a.blind_confirmation == "confirmed":
            raise ReviewError("annotation is already matched; dismiss the duplicate observation instead")
        if a.blind_confirmation == "added_from_blind_scan":
            raise ReviewError("annotation was promoted from another observation; dismiss this one instead")
        d = self.spec(o.sheet_id).distance_pt((o.point.u, o.point.v), (a.point.u, a.point.v))
        if d > MATCH_TOLERANCE_PT and not (reason and reason.strip()):
            raise ReviewError(f"observation is {d:.1f} pt from the annotation; matching beyond "
                              f"{MATCH_TOLERANCE_PT} pt requires a reason")
        with self.store.transaction() as conn:
            self.store.replace("observations", o.model_copy(update={"reconciliation": "matched",
                                                                    "annotation_id": a.id, "reason": reason}),
                               actor, "matched", conn=conn)
            a = self.store.update_annotation(a.id, actor, action="blind_matched", conn=conn,
                                             blind_confirmation="confirmed", blind_decision_reason=None)
        return a

    def promote(self, observation_id: str, actor: str, subtype: str | None = None, **fields) -> M.Annotation:
        """Create the one annotation for a device pass 1 missed."""
        o = self._pending_observation(observation_id)
        spec = self.spec(o.sheet_id)
        for a in self.store.list("annotations", o.sheet_id):
            if a.category == o.category and spec.distance_pt((o.point.u, o.point.v), (a.point.u, a.point.v)) <= DUPLICATE_TOLERANCE_PT:
                raise ReviewError(f"a {a.category} annotation already exists within {DUPLICATE_TOLERANCE_PT} pt; "
                                  "match the observation to it instead")
        subtype = subtype or o.subtype
        if not subtype:
            raise ReviewError("choose a subtype to promote this observation")
        point = o.point.model_dump()
        with self.store.transaction() as conn:
            a = M.Annotation(sheet_id=o.sheet_id, page_number=spec.page_number, category=o.category, subtype=subtype,
                             point=point, point_pt=self._point_pt(spec, point),
                             provenance={"origin": "blind_scan", "observation_id": o.id},
                             blind_confirmation="added_from_blind_scan", created_by=actor,
                             notes=fields.pop("notes", o.note), **fields)
            self.store.create_annotation(a, actor, conn=conn)
            self.store.replace("observations", o.model_copy(update={"reconciliation": "promoted", "annotation_id": a.id}),
                               actor, "promoted", conn=conn)
        return a

    def dismiss(self, observation_id: str, actor: str, reason: str) -> M.BlindObservation:
        o = self._pending_observation(observation_id)
        if not (reason and reason.strip()):
            raise ReviewError("dismissing an observation requires a reason")
        return self.store.replace("observations", o.model_copy(update={"reconciliation": "dismissed", "reason": reason}),
                                  actor, "dismissed")

    def unseen(self, sheet_id: str) -> list[M.Annotation]:
        """Annotations no blind observation has matched and no decision has covered yet."""
        self._require(sheet_id, "reconcile", "verified")
        return [a for a in self.store.list("annotations", sheet_id) if a.blind_confirmation is None]

    def decide_unseen(self, annotation_id: str, actor: str, decision: str, reason: str) -> M.Annotation:
        """Explicit decision for an annotation the blind scan did not find: keep or delete."""
        a = self._annotation(annotation_id)
        self._require(a.sheet_id, "reconcile")
        if a.blind_confirmation is not None:
            raise ReviewError("annotation already has a blind-scan decision")
        if any(o.reconciliation == "pending" for o in self.store.list("observations", a.sheet_id)):
            raise ReviewError("reconcile every blind observation before deciding unseen annotations")
        if not (reason and reason.strip()):
            raise ReviewError("a decision on an unseen annotation requires a reason")
        if decision == "keep":
            return self.store.update_annotation(a.id, actor, action="unseen_kept",
                                                blind_confirmation="not_seen_in_blind_scan", blind_decision_reason=reason)
        if decision == "delete":
            with self.store.transaction() as conn:
                self.store.update_annotation(a.id, actor, action="unseen_marked", conn=conn,
                                             blind_confirmation="not_seen_in_blind_scan", blind_decision_reason=reason)
                return self.store.delete_annotation(a.id, actor, reason, conn=conn)
        raise ReviewError("decision must be 'keep' or 'delete'")

    def verify(self, annotation_id: str, actor: str) -> M.Annotation:
        a = self._annotation(annotation_id)
        self._require(a.sheet_id, "reconcile")
        if a.blind_confirmation is None:
            raise ReviewError("match it to a blind observation or decide it as unseen before verifying")
        if a.room is None:
            raise ReviewError("assign a room (or Exterior / Unlabeled) before verifying")
        return self.store.update_annotation(a.id, actor, action="verify", review_state="verified")

    # --- warnings ------------------------------------------------------------------------------------------------

    def warnings(self, sheet_id: str) -> list[dict]:
        self.require_not_blind(sheet_id)
        return K.warnings(self.store, self.spec(sheet_id))

    def acknowledge_warning(self, sheet_id: str, warning_id: str, actor: str, reason: str) -> M.WarningAck:
        self._require(sheet_id, "pass1", "reconcile")
        if warning_id not in {w["id"] for w in K.warnings(self.store, self.spec(sheet_id))}:
            raise ReviewError("no such current warning")
        ack = M.WarningAck(id=warning_id, sheet_id=sheet_id, reason=reason, actor=actor)
        if self.store.find("warning_acks", warning_id):
            return self.store.replace("warning_acks", ack, actor, "re_acknowledge")
        return self.store.insert("warning_acks", ack, actor, "acknowledge")

    # --- progress -------------------------------------------------------------------------------------------------

    def progress(self, sheet_id: str) -> dict:
        s = self.sheet(sheet_id)
        spec = self.spec(sheet_id)
        if s.phase == "blind_scan":
            return {"phase": s.phase, **K.blind_progress(self.store, spec)}
        return {"phase": s.phase, "region_confirmed": s.region_confirmed, **K.progress(self.store, spec)}

    @staticmethod
    def taxonomy() -> dict:
        return T.as_dict()
