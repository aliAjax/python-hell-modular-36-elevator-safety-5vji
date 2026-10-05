import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(
            self.actor,
            "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def offline(self, source_id, record_id, equipment_id, record_kind, fields, recorded_at):
        return {
            "source_id": source_id,
            "record_id": record_id,
            "equipment_id": equipment_id,
            "record_kind": record_kind,
            "fields": fields,
            "recorded_at": recorded_at,
        }

    def test_same_field_from_two_phones_keeps_both_sources_and_times(self):
        equipment = self.equipment()
        self.service.merge_offline(
            self.actor,
            [self.offline("phone-1", "r1", equipment["id"], "inspection", {"result": "passed", "findings": "normal"}, "2026-09-27T09:00:00Z")],
        )
        self.service.merge_offline(
            self.actor,
            [self.offline("phone-2", "r2", equipment["id"], "inspection", {"result": "passed", "findings": "normal"}, "2026-09-27T09:05:00Z")],
        )
        merged = self.service.get(equipment["id"])
        result_log = merged["data"]["offline_fields"]["result"]
        self.assertEqual(len(result_log), 2)
        self.assertEqual(result_log[0]["source_id"], "phone-1")
        self.assertEqual(result_log[0]["recorded_at"], "2026-09-27T09:00:00Z")
        self.assertEqual(result_log[1]["source_id"], "phone-2")
        self.assertEqual(result_log[1]["recorded_at"], "2026-09-27T09:05:00Z")
        findings_log = merged["data"]["offline_fields"]["findings"]
        self.assertEqual(len(findings_log), 2)
        # The later record must not overwrite the earlier one.
        self.assertEqual(findings_log[0]["source_id"], "phone-1")
        self.assertEqual(findings_log[1]["source_id"], "phone-2")

    def test_retrying_a_batch_does_not_duplicate_records(self):
        equipment = self.equipment()
        batch = [
            self.offline("phone-1", "r1", equipment["id"], "inspection", {"result": "passed"}, "2026-09-27T09:00:00Z"),
            self.offline("phone-2", "r2", equipment["id"], "maintenance", {"note": "lubricated"}, "2026-09-27T09:10:00Z"),
        ]
        first = self.service.merge_offline(self.actor, batch)
        self.assertEqual(len(first), 2)
        count_after_first = len(self.service.list("offline_record"))
        self.assertEqual(count_after_first, 2)
        # Retry the whole batch after a (simulated) failed upload.
        second = self.service.merge_offline(self.actor, batch)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(self.service.list("offline_record")), 2)
        merged = self.service.get(equipment["id"])
        self.assertEqual(len(merged["data"]["offline_fields"]["result"]), 1)
        self.assertEqual(len(merged["data"]["offline_fields"]["note"]), 1)

    def test_open_remediation_blocks_permit_even_with_offline_passed_inspection(self):
        equipment = self.equipment()
        self.service.merge_offline(
            self.actor,
            [self.offline("phone-1", "r1", equipment["id"], "inspection", {"result": "passed"}, "2026-09-27T09:00:00Z")],
        )
        remediation = self.service.create(
            self.actor,
            "remediation",
            {"equipment_id": equipment["id"], "issue": "door alignment", "owner": "Maint", "due_at": "2026-10-01"},
        )
        permit = self.service.create(
            self.actor,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.service.transition(self.actor, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.actor, permit["id"], "grant", {})
        self.assertIn("open remediation", str(ctx.exception))
        # Close the remediation, then the permit can be granted.
        self.service.transition(self.actor, remediation["id"], "submit_evidence", {"evidence": "IMG-1"})
        self.service.transition(self.actor, remediation["id"], "verify", {})
        self.service.transition(self.actor, remediation["id"], "close", {})
        permit = self.service.transition(self.actor, permit["id"], "grant", {})
        self.assertEqual(permit["status"], "granted")

    def test_offline_passed_inspection_allows_permit_when_remediation_closed(self):
        equipment = self.equipment()
        self.service.merge_offline(
            self.actor,
            [self.offline("phone-1", "r1", equipment["id"], "inspection", {"result": "passed"}, "2026-09-27T09:00:00Z")],
        )
        permit = self.service.create(
            self.actor,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.service.transition(self.actor, permit["id"], "request_review", {})
        permit = self.service.transition(self.actor, permit["id"], "grant", {})
        self.assertEqual(permit["status"], "granted")

    def test_offline_record_requires_existing_equipment(self):
        with self.assertRaises(ValidationError):
            self.service.merge_offline(
                self.actor,
                [self.offline("phone-1", "r1", "missing", "inspection", {"result": "passed"}, "2026-09-27T09:00:00Z")],
            )

    def test_offline_record_rejects_invalid_record_kind(self):
        equipment = self.equipment()
        with self.assertRaises(ValidationError):
            self.service.merge_offline(
                self.actor,
                [self.offline("phone-1", "r1", equipment["id"], "permit", {"result": "passed"}, "2026-09-27T09:00:00Z")],
            )

    def test_offline_record_requires_non_empty_fields(self):
        equipment = self.equipment()
        with self.assertRaises(ValidationError):
            self.service.merge_offline(
                self.actor,
                [self.offline("phone-1", "r1", equipment["id"], "inspection", {}, "2026-09-27T09:00:00Z")],
            )

    def test_offline_record_requires_source_and_record_id(self):
        equipment = self.equipment()
        with self.assertRaises(ValidationError):
            self.service.merge_offline(
                self.actor,
                [{"equipment_id": equipment["id"], "record_kind": "inspection", "fields": {"result": "passed"}, "recorded_at": "2026-09-27T09:00:00Z"}],
            )


if __name__ == "__main__":
    unittest.main()
