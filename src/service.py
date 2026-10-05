import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine

OFFLINE_INSPECTION_OUTCOMES = ("passed", "failed")


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _offline_identity(source_id, record_id):
    digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
    return "offline-" + digest, digest


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
        status = self.rules.initial_status(kind, payload)
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
        return updated

    def merge_offline(self, actor, records):
        """Merge a batch of offline field records into their equipment.

        Each record carries a stable (source_id, record_id) identity, so a
        failed upload can be retried as a whole: records that were already
        merged are returned as-is and never written twice.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        return [self._merge_one(actor, raw) for raw in records]

    def _merge_one(self, actor, raw):
        if not isinstance(raw, dict):
            raise ValidationError("each offline record must be an object")
        source_id = str(raw.get("source_id", "")).strip()
        record_id = str(raw.get("record_id", "")).strip()
        if not source_id or not record_id:
            raise ValidationError("source_id and record_id are required")
        marker_id, digest = _offline_identity(source_id, record_id)
        existing = self.repository.get_entity(marker_id)
        if existing:
            stored = existing["data"].get("record")
            if stored is not None and _canonical(stored) != _canonical(raw):
                raise ConflictError(
                    "offline record %s/%s already merged with different content"
                    % (source_id, record_id)
                )
            return existing
        payload = dict(raw)
        self.rules.validate_create(actor, "offline_record", payload, self._lookup)
        equipment = self._resolve_equipment(payload)
        field = str(payload.get("field")).strip()
        category = payload.get("category", payload.get("record_type"))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "source_id": source_id,
            "record_id": record_id,
            "value": payload.get("value"),
            "recorded_at": payload.get("recorded_at"),
            "merged_at": now,
        }
        if category:
            entry["category"] = category
        equipment, first_write = self._apply_offline_entry(actor, equipment, field, entry)
        inspection_id = None
        outcome = str(payload.get("value", "")).strip().lower()
        if category == "inspection" and outcome in OFFLINE_INSPECTION_OUTCOMES:
            inspection_id = "inspection-" + digest
            self._materialize_inspection(actor, inspection_id, equipment, payload, outcome)
        marker_data = {
            "record": payload,
            "source_id": source_id,
            "record_id": record_id,
            "equipment_id": equipment["id"],
            "field": field,
            "first_write": first_write,
            "merged_at": now,
        }
        if inspection_id:
            marker_data["inspection_id"] = inspection_id
        try:
            marker = self.repository.create_entity(
                marker_id, "offline_record", "merged", marker_data, actor.user_id
            )
        except sqlite3.IntegrityError:
            return self.repository.get_entity(marker_id)
        self.audit.record(
            marker_id,
            actor,
            "merge_offline",
            None,
            "merged",
            {"source_id": source_id, "record_id": record_id, "equipment_id": equipment["id"], "field": field},
        )
        return marker

    def _resolve_equipment(self, payload):
        if payload.get("equipment_id"):
            equipment = self.repository.get_entity(str(payload["equipment_id"]))
            if equipment and equipment["kind"] == "equipment":
                return equipment
        if payload.get("asset_no"):
            found = self.repository.find_entities("equipment", "asset_no", payload["asset_no"])
            if found:
                return found[0]
        raise ValidationError("offline record requires a known equipment_id or asset_no")

    def _apply_offline_entry(self, actor, equipment, field, entry):
        """Append the entry to the equipment; the first writer keeps the field.

        Every source is preserved under data["offline_entries"][field] with its
        own write times, so a later phone never overwrites an earlier record.
        """
        for _attempt in range(3):
            current = self.repository.get_entity(equipment["id"])
            if not current:
                raise NotFoundError("entity not found: " + equipment["id"])
            data = copy.deepcopy(current["data"])
            entries = data.setdefault("offline_entries", {}).setdefault(field, [])
            if any(
                item.get("source_id") == entry["source_id"] and item.get("record_id") == entry["record_id"]
                for item in entries
            ):
                return current, False
            entries.append(entry)
            first_write = field not in data
            if first_write:
                data[field] = entry["value"]
            try:
                updated = self.repository.update_entity(
                    current["id"], current["version"], current["status"], data
                )
            except ConflictError:
                continue
            self.audit.record(
                current["id"],
                actor,
                "merge_offline",
                current["status"],
                updated["status"],
                {
                    "source_id": entry["source_id"],
                    "record_id": entry["record_id"],
                    "field": field,
                    "value": entry["value"],
                    "recorded_at": entry["recorded_at"],
                    "first_write": first_write,
                },
            )
            return updated, first_write
        raise ConflictError("equipment is being updated concurrently; retry the batch")

    def _materialize_inspection(self, actor, inspection_id, equipment, payload, outcome):
        """Register an offline-recorded inspection result as a ledger inspection.

        The permit rule still applies unchanged: an open remediation on the
        equipment blocks the permit even when this inspection passed.
        """
        if self.repository.get_entity(inspection_id):
            return
        data = {
            "equipment_id": equipment["id"],
            "scheduled_at": payload.get("recorded_at"),
            "findings": payload.get("findings", ""),
            "source": "offline",
            "source_id": payload.get("source_id"),
            "record_id": payload.get("record_id"),
            "recorded_at": payload.get("recorded_at"),
        }
        try:
            self.repository.create_entity(inspection_id, "inspection", outcome, data, actor.user_id)
        except sqlite3.IntegrityError:
            return
        self.audit.record(
            inspection_id,
            actor,
            "merge_offline",
            None,
            outcome,
            {
                "equipment_id": equipment["id"],
                "source_id": payload.get("source_id"),
                "record_id": payload.get("record_id"),
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

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
