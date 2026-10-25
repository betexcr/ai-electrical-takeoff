"""TakeoffService: reviewer edits, acceptance, the ``model_disagrees`` lifecycle and counts.

Detectors propose, reviewers decide, and saved device records determine the official totals.

* **An explicit, saved correction is an acceptance decision** (spec §4.13). A proposed device that is corrected
  becomes ``accepted`` / ``corrected_and_accepted`` unless a flag still blocks it; then the correction is saved,
  the device stays proposed and the response names the blocking flags.
* **The model is never authoritative** (§5.9.3). Keeping a classification an open ``model_disagrees`` flag
  disagrees with needs a reviewer's reason; a correction that matches the model closes the flag.
* **Condition flags** (duplicate, room) are recomputed after every change, in the same transaction.

* **Bulk accept is one transaction and is undone as a whole** (spec 4.9). It takes only what
  :func:`takeoff.app.flags.bulk_eligible` allows, after the reviewer confirmed the count they saw; the undo restores
  a device only while nobody has touched it since (see :meth:`TakeoffService.undo_bulk`).

Every mutation runs in one ``store.transaction()``; the store appends an event for every row it writes, so the
history keeps the before and after of each change, its actor and its time. A refused mutation writes nothing.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from takeoff.app import counts as COUNTS
from takeoff.app import flags as F
from takeoff.app.merge import verify_usable_for
from takeoff.app.models import BulkAction, BulkBefore, Device, Evidence, Flag, ModelVerdict, RunDetection
from takeoff.app.rooms import propose_room
from takeoff.app.store import TakeoffStore
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.models import NormPoint, RoomRef, new_id, utcnow
from takeoff.pdf import coords as C

ERROR_STATUS = {
    "not_found": 404,
    "invalid": 400,
    "outside_region": 400,
    "reason_required": 400,
    "ai_disagreement_reason_required": 409,
    "manual_cannot_be_rejected": 400,
    "detection_cannot_be_deleted": 400,
    "blocked_by_flags": 409,
    "bulk_count_changed": 409,
    "run_in_progress": 409,
    "invalid_pdf": 400,
}

EDITABLE = ("category", "subtype", "room", "point", "notes")
HUMAN_ORDER = ("category", "subtype", "room", "point", "review_state")
CONFLICT_CHOICES = ("keep", "change_category", "separate_device")

# Flag resolutions (``Flag.resolution``).
RESOLVED_BY_CORRECTION = "resolved_by_correction"
KEPT_OVER_AI = "kept_over_ai: {}"
ACKNOWLEDGED = "acknowledged: {}"
REJECTED_BY_REVIEWER = "rejected_by_reviewer"  # model_disagrees, spec §5.9.3
ACCEPTED_BY_REVIEWER = "accepted by reviewer"
OTHER_REJECTED = "rejected by reviewer"
DEVICE_DELETED = "device deleted: {}"
KEPT_EXISTING = "kept_existing"
SEPARATE_DEVICE = "separate_device: {}"

ACCEPTED_ON_CORRECTION = "accepted by reviewer (saved correction)"

# Decision flags a saved correction closes when it accepts the device, as accept() does (ruling R12);
# model_disagrees follows its own reason rule.
CLOSED_ON_CORRECTED_ACCEPT = tuple(t for t in F.DECISION_TYPES if t != "model_disagrees")
# Closed only by accept, a saved correction or reject (spec §5.9.2(4), ruling R11); never acknowledged.
NOT_ACKNOWLEDGEABLE = F.VALIDATION_TYPES + ("ai_only_candidate",)

# ``BulkAction.sheet_id`` of a bulk accept that spans every sheet of the document.
ALL_SHEETS = "*"
SKIPPED_CHANGED = "changed_after_bulk"

AI_ONLY_TEXT = "AI-proposed location: check position before accepting"
FAKE_SUFFIX = " · test double, not a model"

# Delete is for mistaken manual entries and duplicates; reject is the decision on a detection (spec §5.2, final
# review I4). A deleted detection would otherwise come back as a fresh proposal on the next Analyze.
DELETE_DETECTED_MESSAGE = ("Use Reject for a detection that is not a real device. Reject keeps it from being "
                           "proposed again; Delete is for manual mistakes and duplicates.")


class TakeoffError(Exception):
    """A refused operation: ``code`` (stable, for the API), HTTP ``status`` and optional ``details``."""

    def __init__(self, code: str, message: str | None = None, details: dict | None = None):
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown TakeoffError code {code!r}")
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message or code
        self.details = details or {}
        super().__init__(self.message)


@dataclass(frozen=True)
class _Sheet:
    document_id: str
    sheet_id: str
    rooms: list[dict]
    geometry: C.PageGeometry
    region_pt: tuple[float, float, float, float]


def _has_text(s: str | None) -> bool:
    return bool(s and s.strip())


def _flag_brief(f: Flag) -> dict:
    return {"id": f.id, "type": f.type, "severity": f.severity, "details": f.details}


def _human(existing, *fields) -> list[str]:
    s = set(existing) | set(fields)
    return [f for f in HUMAN_ORDER if f in s]


class TakeoffService:
    def __init__(self, store: TakeoffStore, views: Callable[[str, str], dict]):
        self.store = store
        self.views = views

    # --- reads -------------------------------------------------------------------------------------

    def devices(self, document_id: str, sheet_id: str | None = None, include_deleted: bool = False) -> list[Device]:
        return [d for d in self.store.list("devices", sheet_id, include_deleted=include_deleted)
                if d.document_id == document_id]

    def counts(self, document_id: str) -> dict:
        devices = self.devices(document_id, include_deleted=True)
        flags = [f for f in self.store.list("flags") if f.document_id == document_id and f.open]
        return self.derive_counts(document_id, devices, flags)

    def derive_counts(self, document_id: str, devices: list[Device], flags: list[Flag]) -> dict:
        """:func:`counts.derive` over the given records, with the sheets' rooms (the export derives its totals from
        the very device list it writes, in the same call, through this).

        Never fails for a sheet whose view cannot be read (its PDF moved or is missing): the devices are still
        counted under their saved rooms and the sheet is listed in ``rooms_unavailable`` with the reason."""
        rooms, unavailable = {}, {}
        for sheet_id in sorted({d.sheet_id for d in devices if not d.deleted and d.review_state == "accepted"}):
            try:
                rooms[sheet_id] = self.views(document_id, sheet_id)["rooms"]
            except LookupError:  # no view for the sheet: rows fall back to the counted devices' rooms
                pass
            except TakeoffError as e:  # the view cannot be built now: same fallback, and say so
                unavailable[sheet_id] = e.message
        return COUNTS.derive(devices, flags, rooms, unavailable=unavailable)

    def device_detail(self, device_id: str) -> dict:
        device = self._get_device(device_id)
        evidence = [e for e in self.store.list("evidence") if e.device_id == device_id]
        rows = [{"evidence": e, "run": self.store.find("runs", e.run_id),
                 "detection": self.store.find("detections", e.detection_id)} for e in evidence]
        flags = [f for f in self.store.list("flags", device.sheet_id) if f.device_id == device_id]
        verdicts = self._linked_verdicts(device, rows, flags)
        history = list(self.store.events("device", device_id))
        for f in flags:
            history += self.store.events("flag", f.id)
        for e in evidence:
            history += self.store.events("evidence", e.id)
        history.sort(key=lambda ev: ev.id)
        return {"device": device, "evidence": rows, "flags": flags, "verdicts": verdicts,
                "ai_summary": self._ai_summary(device, rows, flags, verdicts), "history": history}

    # --- mutations ---------------------------------------------------------------------------------

    def create_device(self, document_id: str, sheet_id: str, actor: str, category: str, subtype: str, point,
                      room=None, notes: str | None = None) -> Device:
        if not any(s.document_id == document_id for s in self.store.list("sheets", sheet_id)):
            raise TakeoffError("not_found", f"sheet {sheet_id!r} of document {document_id[:12]} not found")
        sheet = self._sheet(document_id, sheet_id)
        status = T.default_status(category) if isinstance(category, str) else ""
        self._validate_classification(category, subtype, status, notes)
        norm, point_pt = self._point(sheet, point)
        if room is not None:
            room_ref, room_source = self._room(sheet, room), "reviewer"
        else:
            room_ref, room_source = self._propose(sheet, point_pt)
        now = utcnow()
        device = Device(id=new_id(), document_id=document_id, sheet_id=sheet_id, category=category, subtype=subtype,
                        status=status, point=norm, point_pt=point_pt, room=room_ref, room_source=room_source,
                        review_state="accepted", origin="manual", accept_kind="manual",
                        human_set=_human((), "category", "subtype", "point", "review_state",
                                         *(("room",) if room is not None else ())),
                        notes=notes, created_at=now, updated_at=now)
        with self.store.transaction() as conn:
            device = self.store.insert("devices", device, actor, "create_manual", conn=conn)
            self._sync(conn, sheet, actor, now)
        return device

    def update_device(self, device_id: str, actor: str, changes: dict, reason: str | None = None) -> dict:
        if not isinstance(changes, Mapping) or not changes:
            raise TakeoffError("invalid", "no changes given")
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise TakeoffError("invalid", f"fields cannot be edited: {unknown}")
        found = self._get_device(device_id)
        sheet = self._sheet(found.document_id, found.sheet_id)
        with self.store.transaction() as conn:
            return self._update(conn, sheet, device_id, actor, dict(changes), reason, utcnow())

    def accept(self, device_id: str, actor: str, reason: str | None = None) -> Device:
        found = self._get_device(device_id)
        sheet = self._sheet(found.document_id, found.sheet_id)
        now = utcnow()
        with self.store.transaction() as conn:
            before = self._live_device(device_id)
            new = before
            if before.review_state != "accepted":
                new = before.model_copy(update={"review_state": "accepted", "accept_kind": "accepted",
                                                "human_set": _human(before.human_set, "review_state")})
            open_flags = self._sync(conn, sheet, actor, now, pending=[new])
            blockers = F.acceptance_blockers(device_id, open_flags, reason)
            conditions = [f for f in blockers if f.type in F.VALIDATION_TYPES]
            if conditions:
                raise TakeoffError("blocked_by_flags", "open review flags block this acceptance",
                                   {"blocking_flags": [_flag_brief(f) for f in conditions]})
            if blockers:
                raise TakeoffError("ai_disagreement_reason_required",
                                   "the AI disagrees with this classification; give a reason for keeping it",
                                   {"blocking_flags": [_flag_brief(f) for f in blockers]})
            for f in self._mine(open_flags, device_id, F.DECISION_TYPES):
                if f.type == "model_disagrees":
                    self._close(conn, f, actor, KEPT_OVER_AI.format(reason.strip()), now)
                else:
                    self._close(conn, f, actor, ACCEPTED_BY_REVIEWER, now)
            if new is before:
                return before
            return self._write(conn, before, new, actor, "accept", now)

    def reject(self, device_id: str, actor: str, reason: str | None = None) -> Device:
        found = self._get_device(device_id)
        if found.origin == "manual":
            raise TakeoffError("manual_cannot_be_rejected", "a manual device is deleted, not rejected")
        sheet = self._sheet(found.document_id, found.sheet_id)
        now = utcnow()
        with self.store.transaction() as conn:
            before = self._live_device(device_id)
            new = before
            if before.review_state != "rejected":
                notes = before.notes
                if _has_text(reason):
                    notes = f"{notes}\n[rejected] {reason.strip()}" if notes else f"[rejected] {reason.strip()}"
                new = before.model_copy(update={"review_state": "rejected", "accept_kind": None, "notes": notes,
                                                "human_set": _human(before.human_set, "review_state")})
            open_flags = self._sync(conn, sheet, actor, now, pending=[new])
            for f in self._mine(open_flags, device_id, F.DECISION_TYPES):
                self._close(conn, f, actor, REJECTED_BY_REVIEWER if f.type == "model_disagrees" else OTHER_REJECTED,
                            now)
            if new is before:
                return before
            return self._write(conn, before, new, actor, "reject", now)

    def delete(self, device_id: str, actor: str, reason: str) -> Device:
        """Soft-delete a manual device, or a detector / AI device with an open ``duplicate_warning`` (the duplicate
        of another device); any other detection is refused with ``detection_cannot_be_deleted`` (reject it)."""
        if not _has_text(reason):
            raise TakeoffError("reason_required", "deleting a device requires a reason")
        found = self._get_device(device_id)
        sheet = self._sheet(found.document_id, found.sheet_id)
        now = utcnow()
        with self.store.transaction() as conn:
            before = self._live_device(device_id)
            if before.origin != "manual" and not any(
                    f.open and f.device_id == device_id and f.type == "duplicate_warning"
                    for f in self.store.list("flags", before.sheet_id)):
                raise TakeoffError("detection_cannot_be_deleted", DELETE_DETECTED_MESSAGE)
            new = before.model_copy(update={"deleted": True, "delete_reason": reason.strip()})
            open_flags = self._sync(conn, sheet, actor, now, pending=[new])
            # A deleted device is in no figure; its decision flags would otherwise stay open forever.
            for f in self._mine(open_flags, device_id, F.DECISION_TYPES):
                self._close(conn, f, actor, DEVICE_DELETED.format(reason.strip()), now)
            return self._write(conn, before, new, actor, "delete", now)

    def acknowledge_flag(self, flag_id: str, actor: str, reason: str) -> Flag:
        if not _has_text(reason):
            raise TakeoffError("reason_required", "acknowledging a flag requires a reason")
        with self.store.transaction() as conn:
            flag = self._get_flag(flag_id)
            if not flag.open:
                raise TakeoffError("invalid", f"flag {flag_id} is already closed")
            if flag.type in F.VALIDATION_TYPES:
                # It would re-open at the next recompute while its condition holds: fix the condition instead.
                raise TakeoffError("invalid", f"a {flag.type} flag clears when its condition is fixed")
            if flag.type in NOT_ACKNOWLEDGEABLE:
                raise TakeoffError("invalid", "an AI-only candidate is decided by accept, a saved correction or reject")
            return self._close(conn, flag, actor, ACKNOWLEDGED.format(reason.strip()), utcnow())

    def resolve_conflict(self, flag_id: str, actor: str,
                         choice: Literal["keep", "change_category", "separate_device"]) -> Device:
        if choice not in CONFLICT_CHOICES:
            raise TakeoffError("invalid", f"choice must be one of {CONFLICT_CHOICES}")
        flag = self._get_flag(flag_id)
        if flag.type != "classification_conflict" or not flag.open:
            raise TakeoffError("invalid", "only an open classification_conflict flag can be resolved this way")
        found = self._get_device(flag.device_id)
        sheet = self._sheet(found.document_id, found.sheet_id)
        now = utcnow()
        with self.store.transaction() as conn:
            flag = self._get_flag(flag_id)
            if not flag.open:
                raise TakeoffError("invalid", "the flag is already closed")
            device = self._live_device(flag.device_id)
            if choice == "keep":
                self._close(conn, flag, actor, KEPT_EXISTING, now)
                return device
            if choice == "change_category":
                changes = {"category": flag.details.get("run_category"), "subtype": flag.details.get("run_subtype")}
                out = self._update(conn, sheet, device.id, actor, changes, None, now)
                current = self.store.get("flags", flag_id)
                if current.open:  # the classification already equalled the run's: nothing changed it
                    self._close(conn, current, actor, RESOLVED_BY_CORRECTION, now)
                return out["device"]
            det = self.store.find("detections", flag.details.get("detection_id") or "")
            if det is None:
                raise TakeoffError("not_found", "the flag's detection is not stored")
            new = self._device_from_detection(sheet, det, now)
            new = self.store.insert("devices", new, actor, "create_separate", conn=conn)
            self.store.insert("evidence", Evidence(id=new_id(), device_id=new.id, detection_id=det.id,
                                                   run_id=det.run_id, outcome="created", distance_pt=0.0),
                              actor, "create", conn=conn)
            self._close(conn, flag, actor, SEPARATE_DEVICE.format(new.id), now)
            self._sync(conn, sheet, actor, now)
            return new

    def resync_flags(self, document_id: str, sheet_id: str, conn=None, *, actor: str = F.SYSTEM_ACTOR) -> list[Flag]:
        """Recompute one sheet's condition flags from its saved devices (after a run's merge, spec §5.3.3).

        Runs inside ``conn`` (the caller's open transaction, e.g. the analyzer's merge) or in a transaction of its
        own. Every stored flag id is passed as ``taken_ids`` and the sheet's devices as ``devices`` (ruling R8).
        Returns the sheet's open flags afterwards (document-scoped).
        """
        sheet = self._sheet(document_id, sheet_id)
        now = utcnow()
        if conn is not None:
            return self._sync(conn, sheet, actor, now)
        with self.store.transaction() as c:
            return self._sync(c, sheet, actor, now)

    # --- bulk accept and undo (spec 4.9) ---------------------------------------------------------------

    def bulk_eligible_ids(self, document_id: str, sheet_id: str | None = None) -> list[str]:
        """Ids a bulk accept would take now: ``len(...)`` is *W_unflagged*, the bulk button's N (spec 4.17)."""
        self._bulk_scope(document_id, sheet_id)
        return self._eligible(document_id, sheet_id)

    def bulk_accept(self, document_id: str, actor: str, expected_count: int,
                    sheet_id: str | None = None) -> BulkAction:
        """Accept every unflagged proposal of the document (or one sheet), all or nothing.

        ``expected_count`` is the N the reviewer confirmed. Eligibility is recomputed inside the transaction; if it
        no longer equals ``expected_count`` nothing is written and ``bulk_count_changed`` (409) is raised, so a
        flag or an edit that appeared after the dialog opened can never be accepted by surprise. One immutable
        :class:`BulkAction` records the ids, the actor and each device's previous state; ``before[id].revision`` is
        the device's revision *after* this bulk write, which is what :meth:`undo_bulk` compares. A failure
        anywhere (including the audit insert) rolls back every device write.
        """
        if not _has_text(actor):
            raise TakeoffError("invalid", "an actor is required")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
            raise TakeoffError("invalid", "expected_count must be a non-negative integer")
        self._bulk_scope(document_id, sheet_id)
        now = utcnow()
        with self.store.transaction() as conn:
            ids = self._eligible(document_id, sheet_id)  # recomputed inside the transaction
            if len(ids) != expected_count:
                raise TakeoffError("bulk_count_changed",
                                   f"{len(ids)} proposals are eligible now, not the {expected_count} confirmed",
                                   {"expected": expected_count, "current": len(ids)})
            if not ids:
                raise TakeoffError("invalid", "no unflagged proposals to accept")
            before: dict[str, BulkBefore] = {}
            for device_id in ids:
                old = self._live_device(device_id)
                new = old.model_copy(update={"review_state": "accepted", "accept_kind": "bulk_accepted",
                                             "human_set": _human(old.human_set, "review_state")})
                written = self._write(conn, old, new, actor, "bulk_accept", now)
                before[device_id] = BulkBefore(review_state=old.review_state, accept_kind=old.accept_kind,
                                               revision=written.revision)
            # Accepting does not change any condition flag (duplicates and rooms ignore proposed vs accepted),
            # so no flag sync is needed here.
            action = BulkAction(document_id=document_id, sheet_id=sheet_id or ALL_SHEETS, kind="bulk_accept",
                                device_ids=ids, before=before, actor=actor, created_at=now)
            return self.store.insert("bulk_actions", action, actor, "bulk_accept", conn=conn)

    def undo_bulk(self, bulk_id: str, actor: str) -> BulkAction:
        """Undo a bulk accept as a whole, without losing history; a second undo of it is refused (``invalid``).

        A device is restored to its recorded ``review_state`` / ``accept_kind`` only if its current revision still
        equals ``before[id].revision``, the revision the bulk accept left it at. Any later write (a reject, a
        correction, a notes-only save, a delete) bumps the revision, so that device keeps its newer state, is not
        in ``device_ids`` and is listed in ``skipped`` with both revisions. Restoring is a new write (the revision
        goes forward, the device's event log keeps the accept and the undo), and the undo is itself a new immutable
        ``BulkAction(kind="undo", undo_of=bulk_id)``.
        """
        if not _has_text(actor):
            raise TakeoffError("invalid", "an actor is required")
        original = self.store.find("bulk_actions", bulk_id)
        if original is None:
            raise TakeoffError("not_found", f"bulk action {bulk_id} not found")
        if original.kind != "bulk_accept":
            raise TakeoffError("invalid", "only a bulk accept can be undone")
        now = utcnow()
        with self.store.transaction() as conn:
            if any(a.undo_of == bulk_id for a in self.store.list("bulk_actions")):  # checked under the write lock
                raise TakeoffError("invalid", f"bulk action {bulk_id} was already undone")
            restored: list[str] = []
            skipped: list[dict] = []
            for device_id in original.device_ids:
                recorded = original.before[device_id]
                current = self.store.find("devices", device_id)
                if current is None or current.document_id != original.document_id:
                    skipped.append({"device_id": device_id, "reason": "missing",
                                    "recorded_revision": recorded.revision, "current_revision": None})
                elif current.revision != recorded.revision:
                    skipped.append({"device_id": device_id, "reason": SKIPPED_CHANGED,
                                    "recorded_revision": recorded.revision, "current_revision": current.revision})
                else:
                    human = current.human_set
                    if recorded.review_state == "proposed":  # the bulk accept had added it
                        human = [f for f in human if f != "review_state"]
                    new = current.model_copy(update={"review_state": recorded.review_state,
                                                     "accept_kind": recorded.accept_kind, "human_set": human})
                    self._write(conn, current, new, actor, "bulk_undo", now)
                    restored.append(device_id)
            action = BulkAction(document_id=original.document_id, sheet_id=original.sheet_id, kind="undo",
                                device_ids=restored, undo_of=bulk_id, skipped=skipped, actor=actor, created_at=now)
            return self.store.insert("bulk_actions", action, actor, "undo", conn=conn)

    def _bulk_scope(self, document_id: str, sheet_id: str | None) -> None:
        if sheet_id is not None and not any(s.document_id == document_id for s in self.store.list("sheets", sheet_id)):
            raise TakeoffError("not_found", f"sheet {sheet_id!r} of document {document_id[:12]} not found")

    def _eligible(self, document_id: str, sheet_id: str | None) -> list[str]:
        """Live proposals that pass :func:`flags.bulk_eligible` against the document's open flags, in store order."""
        open_flags: dict[str, list[Flag]] = {}
        for f in self.store.list("flags", sheet_id):
            if f.document_id == document_id and f.open:
                open_flags.setdefault(f.device_id, []).append(f)
        return [d.id for d in self.devices(document_id, sheet_id) if F.bulk_eligible(d, open_flags.get(d.id, ()))]

    # --- internals: the edit ------------------------------------------------------------------------

    def _update(self, conn, sheet: _Sheet, device_id: str, actor: str, changes: dict, reason: str | None,
                now: str) -> dict:
        before = self._live_device(device_id)
        upd: dict[str, Any] = {}
        # 1. validate against the taxonomy and the region
        category = changes.get("category", before.category)
        subtype = changes.get("subtype", before.subtype)
        notes = changes.get("notes", before.notes)
        status = before.status
        if category != before.category or not status:
            status = T.default_status(category) if isinstance(category, str) else ""
        if {"category", "subtype"} & changes.keys() or ("notes" in changes and subtype is not None):
            self._validate_classification(category, subtype, status, notes)
        if notes is not None and not isinstance(notes, str):
            raise TakeoffError("invalid", "notes must be text")
        upd.update(category=category, subtype=subtype, notes=notes, status=status)
        if "point" in changes:
            norm, point_pt = self._point(sheet, changes["point"])
            upd.update(point=norm, point_pt=point_pt)
        # 2. human_set, and the room's source
        if "room" in changes:
            upd.update(room=self._room(sheet, changes["room"]), room_source="reviewer")
        elif "point" in changes and before.room_source != "reviewer":
            room, source = self._propose(sheet, upd["point_pt"])  # a proposal follows the moved device
            upd.update(room=room, room_source=source)
        new = before.model_copy(update=upd)
        # Only an actual change to the classification, room or position is a correction (ruling R13).
        # Confirming a proposed room as the reviewer's choice changes ``room_source``, so it counts.
        class_changed = (new.category, new.subtype) != (before.category, before.subtype)
        changed = {f for f, on in (("category", new.category != before.category),
                                   ("subtype", new.subtype != before.subtype),
                                   ("room", "room" in changes and (new.room != before.room
                                                                  or before.room_source != "reviewer")),
                                   ("point", new.point_pt != before.point_pt or new.point != before.point)) if on}
        if "point" not in changed and "room" not in changes:
            new = new.model_copy(update={"room": before.room, "room_source": before.room_source})
        # 2. human_set records the fields this save actually changed
        new = new.model_copy(update={"human_set": _human(before.human_set, *changed)})

        # 3. the validation flags
        open_flags = self._sync(conn, sheet, actor, now, pending=[new])
        if not changed:  # a notes-only or no-op save: no decision is taken, the review state never changes
            device = self._write(conn, before, new, actor, "update", now)
            return {"device": device, "blocking_flags": []}
        closed: set[str] = set()
        # 4. classification_conflict, when the classification changed
        if class_changed:
            for f in self._mine(open_flags, device_id, ("classification_conflict",)):
                self._close(conn, f, actor, RESOLVED_BY_CORRECTION, now)
                closed.add(f.id)
        # 5. model_disagrees: resolved only by a classification change that lands on the AI's answer
        for f in self._mine(open_flags, device_id, ("model_disagrees",)):
            ai_category, ai_subtype = f.details.get("ai_category"), f.details.get("ai_subtype")
            if class_changed and category == ai_category and (ai_subtype is None or subtype == ai_subtype):
                self._close(conn, f, actor, RESOLVED_BY_CORRECTION, now)
                closed.add(f.id)
            elif _has_text(reason):
                self._close(conn, f, actor, KEPT_OVER_AI.format(reason.strip()), now)
                closed.add(f.id)
        remaining = [f for f in open_flags if f.id not in closed]

        # 6. acceptance
        blockers: list[Flag] = []
        action = "update"
        if before.review_state == "proposed":
            blockers = F.acceptance_blockers(device_id, remaining, reason)
            if not blockers:
                new = new.model_copy(update={"review_state": "accepted", "accept_kind": "corrected_and_accepted",
                                             "human_set": _human(new.human_set, "review_state")})
                for f in self._mine(remaining, device_id, CLOSED_ON_CORRECTED_ACCEPT):
                    self._close(conn, f, actor, ACCEPTED_ON_CORRECTION, now)
                action = "correct_and_accept"
        device = self._write(conn, before, new, actor, action, now)
        return {"device": device, "blocking_flags": blockers}

    # --- internals: rows ------------------------------------------------------------------------------

    def _get_device(self, device_id: str) -> Device:
        d = self.store.find("devices", device_id)
        if d is None:
            raise TakeoffError("not_found", f"device {device_id} not found")
        return d

    def _live_device(self, device_id: str) -> Device:
        d = self._get_device(device_id)
        if d.deleted:
            raise TakeoffError("invalid", f"device {device_id} is deleted")
        return d

    def _get_flag(self, flag_id: str) -> Flag:
        f = self.store.find("flags", flag_id)
        if f is None:
            raise TakeoffError("not_found", f"flag {flag_id} not found")
        return f

    def _write(self, conn, before: Device, new: Device, actor: str, action: str, now: str) -> Device:
        data = {**new.model_dump(), "revision": before.revision + 1, "updated_at": now}
        return self.store.replace("devices", Device.model_validate(data), actor, action, conn=conn)

    def _close(self, conn, flag: Flag, actor: str, resolution: str, now: str) -> Flag:
        closed = flag.model_copy(update={"open": False, "resolution": resolution, "resolved_by": actor,
                                         "resolved_at": now})
        return self.store.replace("flags", closed, actor, "resolve", conn=conn)

    @staticmethod
    def _mine(flags: list[Flag], device_id: str, types) -> list[Flag]:
        return [f for f in flags if f.open and f.device_id == device_id and f.type in types]

    def _sync(self, conn, sheet: _Sheet, actor: str, now: str, pending: list[Device] = ()) -> list[Flag]:
        """Recompute the sheet's condition flags, with ``pending`` (not yet written) device states.

        Returns the sheet's open flags afterwards (document-scoped).
        """
        by_id = {d.id: d for d in self.devices(sheet.document_id, sheet.sheet_id, include_deleted=True)}
        for d in pending:
            by_id[d.id] = d
        devices = list(by_id.values())
        open_flags = [f for f in self.store.list("flags", sheet.sheet_id)
                      if f.document_id == sheet.document_id and f.open]
        taken = [row[0] for row in conn.execute("SELECT id FROM flags")]  # every stored flag id (ruling R8)
        to_open, to_close = F.sync_validation(open_flags, F.conditions(devices, sheet.rooms),
                                              devices=devices, taken_ids=taken, now=now)
        for f in to_open:
            self.store.insert("flags", f, actor, "open", conn=conn)
        for f in to_close:
            self.store.replace("flags", f, actor, "condition_cleared", conn=conn)
        cleared = {f.id for f in to_close}
        return [f for f in open_flags if f.id not in cleared] + to_open

    # --- internals: the sheet --------------------------------------------------------------------------

    def _sheet(self, document_id: str, sheet_id: str) -> _Sheet:
        """The sheet's rooms, geometry and region, fetched before a transaction starts."""
        try:
            v = self.views(document_id, sheet_id)
        except LookupError as e:  # KeyError / IndexError from the sheet lookup
            raise TakeoffError("not_found", f"sheet {sheet_id!r} of document {document_id[:12]} not found") from e
        g = v["geometry"]
        return _Sheet(document_id=document_id, sheet_id=sheet_id, rooms=v["rooms"],
                      geometry=C.PageGeometry(*g["crop_native"], rotation=g["rotation"]),
                      region_pt=tuple(float(x) for x in v["region"]["bbox_pt"]))

    @staticmethod
    def _point(sheet: _Sheet, point) -> tuple[NormPoint, tuple[float, float]]:
        try:
            if isinstance(point, NormPoint):
                u, v = point.u, point.v
            elif isinstance(point, Mapping):
                u, v = point["u"], point["v"]
            else:
                u, v = point
            u, v = float(u), float(v)
        except (KeyError, TypeError, ValueError) as e:
            raise TakeoffError("invalid", "point must be normalized {u, v}") from e
        if not (math.isfinite(u) and math.isfinite(v)):
            raise TakeoffError("invalid", "point must be finite")
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            raise TakeoffError("outside_region", "the point is outside the page")
        x, y = C.normalized_to_unrotated(sheet.geometry, (u, v))
        x0, y0, x1, y1 = sheet.region_pt
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            raise TakeoffError("outside_region", "the point is outside the sheet's electrical region")
        return NormPoint(u=u, v=v), (round(x, 3), round(y, 3))

    @staticmethod
    def _room_key(room: dict | RoomRef) -> tuple:
        bbox = room["label_bbox_pt"] if isinstance(room, Mapping) else room.label_bbox_pt
        kind = room["kind"] if isinstance(room, Mapping) else room.kind
        name = room["name"] if isinstance(room, Mapping) else room.name
        return kind, name, tuple(float(x) for x in bbox) if bbox is not None else None

    def _room(self, sheet: _Sheet, room) -> RoomRef:
        """A room the reviewer picked: it must be one of the sheet's rooms (each label instance is its own)."""
        try:
            data = room.model_dump() if isinstance(room, RoomRef) else {
                k: room.get(k) for k in ("kind", "name", "label_bbox_pt")}
            ref = RoomRef.model_validate(data)
        except Exception as e:  # pydantic ValidationError, or not a mapping
            raise TakeoffError("invalid", f"invalid room: {e}") from e
        if self._room_key(ref) not in {self._room_key(r) for r in sheet.rooms}:
            raise TakeoffError("invalid", f"room {ref.name!r} is not a room of sheet {sheet.sheet_id}")
        return ref

    @staticmethod
    def _propose(sheet: _Sheet, point_pt) -> tuple[RoomRef | None, str | None]:
        prop = propose_room(point_pt, sheet.rooms)
        if prop.room is None:
            return None, None
        # propose_room may hand back the caller's own room dict: copy it into a RoomRef
        return RoomRef.model_validate({k: prop.room.get(k) for k in ("kind", "name", "label_bbox_pt")}), \
            "proposed_nearest_label"

    def _device_from_detection(self, sheet: _Sheet, det: RunDetection, now: str) -> Device:
        room, source = self._propose(sheet, det.point_pt)
        return Device(id=new_id(), document_id=det.document_id, sheet_id=det.sheet_id, category=det.category,
                      subtype=det.subtype, status=det.status or T.default_status(det.category),
                      point=det.point.model_copy(), point_pt=det.point_pt, room=room, room_source=source,
                      review_state="proposed", origin="ai" if det.source == "ai_discover" else "detector",
                      created_by_run=det.run_id, created_at=now, updated_at=now)

    @staticmethod
    def _validate_classification(category, subtype, status, notes) -> None:
        if not isinstance(category, str) or not isinstance(subtype, str):
            raise TakeoffError("invalid", "category and subtype are required")
        try:
            T.validate_device(category, subtype, status, notes)
        except T.TaxonomyError as e:
            raise TakeoffError("invalid", str(e)) from e

    # --- internals: the AI second opinion ----------------------------------------------------------------

    def _linked_verdicts(self, device: Device, rows: list[dict], flags: list[Flag],
                         pool: list[ModelVerdict] | None = None) -> list[ModelVerdict]:
        """Verdicts linked through the device's evidence (and its flags), in store order.

        ``pool`` is a prefetched list of the sheet's verdicts (the export reads once, under the store lock).
        """
        det_ids = {r["evidence"].detection_id for r in rows}
        run_ids = {r["evidence"].run_id for r in rows}
        wanted = {r["detection"].verdict_id for r in rows if r["detection"] is not None and r["detection"].verdict_id}
        wanted |= {f.details["verdict_id"] for f in flags if f.details.get("verdict_id")}
        pool = self.store.list("verdicts", device.sheet_id) if pool is None else pool
        return [v for v in pool
                if v.id in wanted or (v.run_id in run_ids and det_ids & set(v.detection_ids))]

    @staticmethod
    def _describe_ai(category, subtype) -> str:
        if category is None:
            return "missing verdict row"  # the flag or verdict carries no row for this item
        if category == "not_a_device":  # the verify schema's answer for "this is not a device"
            return "not a device"
        return f"{category} / {subtype}" if subtype else category

    def _ai_summary(self, device: Device, rows: list[dict], flags: list[Flag],
                    verdicts: list[ModelVerdict]) -> dict:
        """The panel's ``AI second opinion`` line, from the latest run with evidence for this device."""
        by_id = {v.id: v for v in verdicts}

        def out(status, text, verdict: ModelVerdict | None = None, **extra) -> dict:
            impl = verdict.implementation if verdict is not None else None
            return {"status": status, "text": text + (FAKE_SUFFIX if impl == "fake" else ""),
                    "implementation": impl, "verdict_id": verdict.id if verdict is not None else None, **extra}

        def discover_verdict(rows_: list[dict]) -> ModelVerdict | None:
            """The discover verdict behind AI-discovered evidence, so a test double is labelled (M-b)."""
            for r in rows_:
                det = r["detection"]
                if det is not None and det.source == "ai_discover" and det.verdict_id in by_id:
                    return by_id[det.verdict_id]
            return None

        def disagrees(f: Flag) -> dict:
            verdict = by_id.get(f.details.get("verdict_id"))
            extra = dict(flag_id=f.id, open=f.open, resolution=f.resolution)
            if not f.open and f.resolution == RESOLVED_BY_CORRECTION:
                # the reviewer corrected the device to the AI's answer: they agree now
                return out("agrees", "AI second opinion: agrees (resolved by correction)", verdict, **extra)
            what = self._describe_ai(f.details.get("ai_category"), f.details.get("ai_subtype"))
            return out("disagrees", f"AI second opinion: disagrees ({what})", verdict, what=what, **extra)

        disagreements = [f for f in flags if f.type == "model_disagrees"]
        open_md = [f for f in disagreements if f.open]
        if open_md:
            return disagrees(open_md[-1])
        if not rows:
            reason = "added manually" if device.origin == "manual" else "no detection evidence"
            return out("not_verified", f"AI second opinion: not verified ({reason})", reason=reason)

        latest = max(r["evidence"].run_id for r in rows)  # run ids are run-NNN, zero-padded
        here = [r for r in rows if r["evidence"].run_id == latest]
        closed_md = [f for f in disagreements if f.run_id == latest]
        if closed_md:
            return disagrees(closed_md[-1])
        det_ids = {r["evidence"].detection_id for r in here}
        for v in verdicts:
            if v.kind != "verify" or v.run_id != latest or not det_ids & set(v.detection_ids):
                continue
            mine = sorted(det_ids & set(v.detection_ids))
            usable = [d for d in mine if verify_usable_for(v, d)]  # a parse error spares the batch's other items
            if not usable:
                reason = f"AI error: {(v.error or {}).get('code', 'error')}"
                return out("not_verified", f"AI second opinion: not verified ({reason})", v, reason=reason)
            for det_id in usable:
                ag = (v.agreement or {}).get(det_id)
                if ag is None:
                    continue
                if ag.category and ag.subtype is not False:
                    return out("agrees", "AI second opinion: agrees", v)
                row = next((x for x in ((v.output or {}).get("verdicts") or []) if x.get("item_id") == det_id), {})
                what = self._describe_ai(row.get("category"), row.get("subtype"))
                return out("disagrees", f"AI second opinion: disagrees ({what})", v, what=what)
            reason = "no usable AI answer"
            return out("not_verified", f"AI second opinion: not verified ({reason})", v, reason=reason)
        corroborating = [r for r in here if r["evidence"].outcome == "ai_corroborates"]
        if corroborating:
            return out("agrees", "AI second opinion: agrees", discover_verdict(corroborating), source="discover")
        if device.origin == "ai" and any(r["detection"] is not None and r["detection"].source == "ai_discover"
                                         for r in here):
            return out("ai_only", AI_ONLY_TEXT, discover_verdict(here))
        reason = "not checked by AI"
        return out("not_verified", f"AI second opinion: not verified ({reason})", reason=reason)
