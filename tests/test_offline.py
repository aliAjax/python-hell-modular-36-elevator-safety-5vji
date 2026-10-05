import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("insp-1", "inspector")
        self.equipment = self.service.create(self.admin, "equipment", {
            "asset_no": "E-900",
            "equipment_type": "elevator",
            "location": "Shaft 3",
            "inspection_interval_days": 365,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, source, record_id, field, value, **extra):
        payload = {
            "source_id": source,
            "record_id": record_id,
            "equipment_id": self.equipment["id"],
            "category": "inspection",
            "field": field,
            "value": value,
            "recorded_at": "2026-10-05T08:00:00Z",
        }
        payload.update(extra)
        return payload

    def merge(self, records, actor=None):
        return self.service.merge_offline(actor or self.inspector, records)

    def equipment_now(self):
        return self.service.get(self.equipment["id"])

    def test_two_phones_same_field_keep_both_sources(self):
        first = self.record("phone-a", "a-1", "brake_check", "ok", recorded_at="2026-10-05T08:00:00Z")
        second = self.record("phone-b", "b-1", "brake_check", "worn", recorded_at="2026-10-05T08:05:00Z")
        self.merge([first])
        self.merge([second])
        equipment = self.equipment_now()
        self.assertEqual(equipment["data"]["brake_check"], "ok")
        entries = equipment["data"]["offline_entries"]["brake_check"]
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["source_id"] for e in entries], ["phone-a", "phone-b"])
        self.assertEqual(entries[0]["recorded_at"], "2026-10-05T08:00:00Z")
        self.assertEqual(entries[1]["recorded_at"], "2026-10-05T08:05:00Z")
        self.assertTrue(all(e["merged_at"] for e in entries))

    def test_replayed_batch_does_not_rewrite(self):
        batch = [
            self.record("phone-a", "a-1", "brake_check", "ok"),
            self.record("phone-a", "a-2", "door_check", "ok"),
        ]
        first_markers = self.merge(batch)
        version_after_first = self.equipment_now()["version"]
        second_markers = self.merge(batch)
        self.assertEqual([m["id"] for m in first_markers], [m["id"] for m in second_markers])
        equipment = self.equipment_now()
        self.assertEqual(equipment["version"], version_after_first)
        self.assertEqual(len(equipment["data"]["offline_entries"]["brake_check"]), 1)
        self.assertEqual(len(equipment["data"]["offline_entries"]["door_check"]), 1)

    def test_failed_batch_can_be_retried_as_a_whole(self):
        good = self.record("phone-a", "a-1", "brake_check", "ok")
        bad = self.record("phone-a", "a-2", "door_check", "ok", equipment_id="missing")
        with self.assertRaises(ValidationError):
            self.merge([good, bad])
        self.assertEqual(self.equipment_now()["data"]["brake_check"], "ok")
        fixed = self.record("phone-a", "a-2", "door_check", "ok")
        self.merge([good, fixed])
        equipment = self.equipment_now()
        self.assertEqual(len(equipment["data"]["offline_entries"]["brake_check"]), 1)
        self.assertEqual(equipment["data"]["door_check"], "ok")

    def test_same_identity_with_different_content_conflicts(self):
        self.merge([self.record("phone-a", "a-1", "brake_check", "ok")])
        with self.assertRaises(ConflictError):
            self.merge([self.record("phone-a", "a-1", "brake_check", "worn")])

    def test_offline_passed_inspection_blocked_by_open_remediation(self):
        remediation = self.service.create(self.admin, "remediation", {
            "equipment_id": self.equipment["id"],
            "issue": "door alignment",
            "owner": "Maint",
            "due_at": "2026-10-10",
        })
        self.merge([self.record("phone-a", "a-1", "conclusion", "passed")])
        inspections = self.service.list("inspection")
        self.assertEqual(len(inspections), 1)
        self.assertEqual(inspections[0]["status"], "passed")
        self.assertEqual(inspections[0]["data"]["equipment_id"], self.equipment["id"])
        permit = self.service.create(self.admin, "permit", {
            "equipment_id": self.equipment["id"],
            "purpose": "return_to_service",
            "requested_by": "ops",
        })
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "grant", {})
        remediation = self.service.transition(self.admin, remediation["id"], "submit_evidence", {"evidence": "IMG-9"})
        remediation = self.service.transition(self.admin, remediation["id"], "verify", {})
        self.service.transition(self.admin, remediation["id"], "close", {})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})
        self.assertEqual(permit["status"], "granted")

    def test_viewer_cannot_upload_offline_records(self):
        with self.assertRaises(PermissionDenied):
            self.service.merge_offline(Actor("viewer", "viewer"), [self.record("phone-a", "a-1", "brake_check", "ok")])

    def test_value_key_is_required(self):
        payload = self.record("phone-a", "a-1", "brake_check", "ok")
        del payload["value"]
        with self.assertRaises(ValidationError):
            self.merge([payload])

    def test_merge_by_asset_no(self):
        payload = self.record("phone-b", "b-1", "brake_check", "ok")
        del payload["equipment_id"]
        payload["asset_no"] = "E-900"
        self.merge([payload])
        self.assertEqual(self.equipment_now()["data"]["brake_check"], "ok")


if __name__ == "__main__":
    unittest.main()
