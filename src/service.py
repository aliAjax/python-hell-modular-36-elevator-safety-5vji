import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        """Merge offline field records into their equipment.

        Each record is identified by the stable (source_id, record_id) pair.
        Re-uploading the same batch (or a failed batch) is a no-op for records
        that were already merged, so the whole batch can be retried safely.

        Observations are appended to the equipment's ``offline_fields`` log
        under each field name. The log is append-only: a later record never
        overwrites an earlier one, and every observation keeps its source and
        write time (``recorded_at``).
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        merged = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                merged.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(
                entity_id,
                actor,
                "merge_offline",
                None,
                entity["status"],
                {
                    "source_id": source_id,
                    "record_id": record_id,
                    "equipment_id": payload.get("equipment_id"),
                    "record_kind": payload.get("record_kind"),
                },
            )
            merged.append(entity)
        self._merge_equipment_fields(records)
        return merged

    def _merge_equipment_fields(self, records):
        """Append new field observations to each equipment's offline_fields log."""
        by_equipment = {}
        for raw in records:
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            equipment_id = str(raw.get("equipment_id", "")).strip()
            fields = raw.get("fields")
            if not equipment_id or not isinstance(fields, dict):
                continue
            bucket = by_equipment.setdefault(equipment_id, {})
            for field, value in fields.items():
                bucket.setdefault(str(field), []).append(
                    {
                        "source_id": source_id,
                        "record_id": record_id,
                        "value": value,
                        "recorded_at": str(raw.get("recorded_at", "")),
                        "merged_at": utcnow(),
                    }
                )
        for equipment_id, field_map in by_equipment.items():
            self._append_equipment_fields(equipment_id, field_map)

    def _append_equipment_fields(self, equipment_id, field_map, retries=5):
        """Append observations to the equipment's offline_fields log with retry.

        The log is append-only and keyed by (source_id, record_id), so retrying
        after a version conflict is safe: already-present observations are kept
        and only new ones are added.
        """
        for attempt in range(retries):
            equipment = self.repository.get_entity(equipment_id)
            if not equipment:
                return
            current = dict(equipment["data"].get("offline_fields") or {})
            changed = False
            for field, observations in field_map.items():
                log = list(current.get(field) or [])
                present = {(obs.get("source_id"), obs.get("record_id")) for obs in log}
                for obs in observations:
                    key = (obs["source_id"], obs["record_id"])
                    if key in present:
                        continue
                    log.append(obs)
                    present.add(key)
                    changed = True
                current[field] = log
            if not changed:
                return
            merged_data = dict(equipment["data"])
            merged_data["offline_fields"] = current
            try:
                self.repository.update_entity(
                    equipment_id, equipment["version"], equipment["status"], merged_data
                )
                return
            except ConflictError:
                if attempt == retries - 1:
                    raise

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
