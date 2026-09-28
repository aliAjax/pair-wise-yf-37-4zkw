import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'case', {'person_id': 'P-9', 'onset_date': '2026-01-01', 'location': 'A', 'symptoms': ['fever']}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'triage',
                {'clinician': 'C-1'},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'case', {'person_id': 'P-9', 'onset_date': '2026-01-01', 'location': 'A', 'symptoms': ['fever']}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"),
                entity["id"],
                'triage',
                {'clinician': 'C-1'},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'case',
            {'person_id': 'P-9', 'onset_date': '2026-01-01', 'location': 'A', 'symptoms': ['fever']},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'case',
            {'person_id': 'P-9', 'onset_date': '2026-01-01', 'location': 'A', 'symptoms': ['fever']},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_stale_update_is_rejected_not_silently_dropped(self):
        # 窗口重算等路径会在服务端推进版本；之后的陈旧写入必须报冲突而不是被丢弃
        case = self.service.create(
            Actor("admin", "admin"), 'case',
            {'person_id': 'P-10', 'onset_date': '2026-03-01', 'location': 'A', 'symptoms': ['fever']},
        )
        contact = self.service.create(
            Actor("admin", "admin"), 'contact',
            {'case_id': case["id"], 'person_id': 'P-11', 'exposure_start': '2026-02-25'},
        )
        self.service.transition(
            Actor("admin", "admin"), contact["id"], 'begin_followup', {}
        )
        self.service.transition(
            Actor("admin", "admin"), case["id"], 'triage', {'clinician': 'C-1'}
        )
        # 确诊会重算接触者窗口并推进其版本
        self.service.transition(
            Actor("admin", "admin"), case["id"], 'lab_positive',
            {'lab_id': 'L-1', 'result': 'positive'},
        )
        stale_version = 2  # begin_followup 后的版本
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"), contact["id"], 'record_followup',
                {'followup_date': '2026-02-26', 'symptoms': []},
                expected_version=stale_version,
            )
        current = self.service.get(contact["id"])
        self.assertNotIn('2026-02-26', current["data"].get("followups", {}))


if __name__ == "__main__":
    unittest.main()
