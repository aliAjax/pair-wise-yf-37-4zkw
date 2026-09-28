from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    evaluate_release,
    last_exposure_day,
    observation_window,
    stamp,
    to_iso,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if (
            entity["kind"] == "case"
            and action == "lab_positive"
            and updated["status"] == "confirmed"
        ):
            self._recalculate_contact_windows(actor, updated)
        return updated

    def _recalculate_contact_windows(self, actor, confirmed_case):
        """病例确诊后，关联接触者按最后接触日重新计算观察窗口。"""
        contacts = self.repository.find_entities(
            "contact", "case_id", confirmed_case["id"]
        )
        affected = []
        for contact in contacts:
            start, due = observation_window(last_exposure_day(contact["data"]))
            new_data = dict(contact["data"])
            new_data["followup_start"] = to_iso(start)
            new_data["due_at"] = to_iso(due)
            new_data["window_recalculated_at"] = stamp()
            new_data["window_source"] = {
                "case_id": confirmed_case["id"],
                "confirmed_at": confirmed_case["updated_at"],
            }
            was_completed = contact["status"] == "completed"
            next_status = "following" if was_completed else contact["status"]
            updated = self.repository.update_entity(
                contact["id"], contact["version"], next_status, new_data
            )
            self.audit.record(
                contact["id"],
                actor,
                "recalculate_window",
                contact["status"],
                next_status,
                {
                    "case_id": confirmed_case["id"],
                    "followup_start": to_iso(start),
                    "due_at": to_iso(due),
                    "reopened": was_completed,
                },
            )
            affected.append(updated["id"])
        return affected

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def timeline(self, entity_id):
        entity = self.get(entity_id)
        if entity["kind"] != "contact":
            raise ValidationError("时间线仅支持接触者(contact)对象")
        followups = []
        for day in sorted((entity["data"].get("followups") or {}).keys()):
            record = dict(entity["data"]["followups"][day])
            record["revision_count"] = len(record.get("revisions", []))
            followups.append(record)
        return {
            "contact": entity,
            "release_evaluation": evaluate_release(entity["data"]),
            "followups": followups,
            "audit": self.repository.list_audit(entity_id=entity_id),
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
