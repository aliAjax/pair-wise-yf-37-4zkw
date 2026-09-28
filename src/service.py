from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import OBSERVATION_DAYS, RuleEngine
from .rules import to_day as _to_day


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
        payload = dict(data or {})
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )

        if entity["kind"] == "contact":
            if action == "record_followup":
                return self._record_followup(actor, entity, patch)
            if action == "amend_followup":
                return self._amend_followup(actor, entity, patch)
            if action == "complete_followup":
                return self._complete_followup(actor, entity, expected, patch)

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

        # 病例确诊后，相关接触者按最后接触日重新计算观察窗
        if entity["kind"] == "case" and action == "lab_positive":
            self._recalculate_contacts_on_confirm(actor, entity_id)

        return updated

    def _record_followup(self, actor, contact, patch):
        followup_date = patch["followup_date"]
        stored = self.repository.insert_followup(
            contact["id"], followup_date, patch, actor.user_id
        )
        self.audit.record(
            contact["id"],
            actor,
            "record_followup",
            contact["status"],
            contact["status"],
            {"followup": stored},
        )
        return contact

    def _amend_followup(self, actor, contact, patch):
        followup_date = patch["followup_date"]
        reason = str(patch.pop("reason") or "").strip()
        if not reason:
            raise ValidationError("missing required field: reason")
        existing = self.repository.find_followup(contact["id"], followup_date)
        if not existing:
            raise ValidationError(
                "no followup recorded for %s on %s; submit it before amending"
                % (contact["id"], followup_date)
            )
        old_value = {
            "symptoms": existing.get("symptoms", []),
            "temperature": existing.get("temperature"),
            "abnormal": existing.get("abnormal"),
        }
        followup_id = existing["id"]
        updated_followup = self.repository.amend_followup(
            followup_id, patch, actor.user_id, reason
        )
        self.audit.record(
            contact["id"],
            actor,
            "amend_followup",
            contact["status"],
            contact["status"],
            {
                "followup_date": followup_date,
                "reason": reason,
                "old": old_value,
                "new": {
                    "symptoms": updated_followup.get("symptoms", []),
                    "temperature": updated_followup.get("temperature"),
                    "abnormal": updated_followup.get("abnormal"),
                },
            },
        )
        return contact

    def _complete_followup(self, actor, contact, expected_version, patch):
        as_of = patch.pop("as_of", None)
        followups = self.repository.list_followups(contact["id"])
        decision = self.rules.evaluate_release(contact["data"], followups, as_of)
        if not decision["eligible"]:
            raise ValidationError(
                "followup release rejected: %s" % decision["reason"]
            )
        next_data = dict(contact["data"])
        next_data.update(patch)
        next_data["released_at"] = decision["as_of"]
        next_data["release_anchor_date"] = decision["anchor_date"]
        updated = self.repository.update_entity(
            contact["id"], expected_version, "completed", next_data
        )
        self.audit.record(
            contact["id"],
            actor,
            "complete_followup",
            contact["status"],
            updated["status"],
            {
                "patch": patch,
                "release_check": decision,
            },
        )
        return updated

    def _recalculate_contacts_on_confirm(self, actor, case_id):
        contacts = self._lookup("contact", "case_id", case_id) or []
        for contact in contacts:
            if contact["status"] == "completed":
                continue
            data = dict(contact["data"])
            last_contact = self.rules.last_contact_date(data)
            due_at = _to_day(self.rules.observation_due(last_contact))
            data["last_contact_date"] = _to_day(last_contact)
            data["due_at"] = due_at
            updated = self.repository.update_entity(
                contact["id"], contact["version"], contact["status"], data
            )
            self.audit.record(
                contact["id"],
                actor,
                "case_confirmed_recalculate",
                contact["status"],
                contact["status"],
                {
                    "case_id": case_id,
                    "last_contact_date": _to_day(last_contact),
                    "due_at": due_at,
                    "observation_days": OBSERVATION_DAYS,
                },
            )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def timeline(self, entity_id, as_of=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        followups = []
        revisions = []
        release = None
        if entity["kind"] == "contact":
            followups = self.repository.list_followups(entity_id)
            revisions = self.repository.list_followup_revisions(entity_id)
            release = self.rules.evaluate_release(entity["data"], followups, as_of)
        return {
            "entity": entity,
            "followups": followups,
            "revisions": revisions,
            "release": release,
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
