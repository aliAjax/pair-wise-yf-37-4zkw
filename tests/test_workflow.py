import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [
            {'op': 'create', 'as': 'case', 'kind': 'case', 'data': {'person_id': 'P-1', 'onset_date': '2026-03-01', 'location': 'District-A', 'symptoms': ['fever']}},
            {'op': 'transition', 'target': 'case', 'action': 'triage', 'data': {'clinician': 'C-1'}, 'expect': 'investigating'},
            {'op': 'transition', 'target': 'case', 'action': 'lab_positive', 'data': {'lab_id': 'L-1', 'result': 'positive'}, 'expect': 'confirmed'},
            {'op': 'transition', 'target': 'case', 'action': 'recover', 'data': {'recovered_at': '2026-03-10'}, 'expect': 'recovered'},
            {'op': 'transition', 'target': 'case', 'action': 'close', 'data': {'outcome': 'recovered'}, 'expect': 'closed'},
            {'op': 'create', 'as': 'contact', 'kind': 'contact', 'data': {'case_id': '{case}', 'person_id': 'P-2', 'exposure_start': '2026-02-25'}},
            {'op': 'transition', 'target': 'contact', 'action': 'begin_followup', 'data': {'followup_start': '2026-03-02', 'due_at': '2026-03-16'}, 'expect': 'following'},
        ]
        for step in steps:
            entity = self._run_step(step, created)
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

        contact_id = created["contact"]
        contact = self.service.get(contact_id)
        # 窗口由最后接触日(2026-02-25)自动算出，忽略调用方传入的旧日期
        self.assertEqual(contact["data"]["followup_start"], "2026-02-26")
        self.assertEqual(contact["data"]["due_at"], "2026-03-11")

        # 连续 14 天每日随访，均无症状
        first = date(2026, 2, 26)
        for offset in range(14):
            day = (first + timedelta(days=offset)).isoformat()
            self.service.transition(
                self.actor, contact_id, 'record_followup',
                {'followup_date': day, 'symptoms': []},
            )

        # 观察期未满不能解除
        blocked = None
        try:
            self.service.transition(
                self.actor, contact_id, 'complete_followup',
                {'as_of': '2026-03-05'},
            )
        except Exception as exc:
            blocked = exc
        self.assertIsNotNone(blocked)
        self.assertIn("观察期未满", str(blocked))

        entity = self.service.transition(
            self.actor, contact_id, 'complete_followup', {'as_of': '2026-03-11'}
        )
        self.assertEqual(entity["status"], "completed")
        self.assertEqual(entity["data"]["completed_at"], "2026-03-11")

        timeline = self.service.timeline(contact_id)
        self.assertEqual(len(timeline["followups"]), 14)
        self.assertTrue(timeline["release_evaluation"]["eligible"])

    def _run_step(self, step, created):
        if step["op"] == "create":
            entity = self.service.create(
                self.actor,
                step["kind"],
                _resolve(step.get("data", {}), created),
                step.get("idempotency_key"),
            )
            created[step["as"]] = entity["id"]
            return entity
        return self.service.transition(
            self.actor,
            created[step["target"]],
            step["action"],
            _resolve(step.get("data", {}), created),
            step.get("expected_version"),
        )


if __name__ == "__main__":
    unittest.main()
