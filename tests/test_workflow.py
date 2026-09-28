import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import OBSERVATION_DAYS
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

    def _run_steps(self, steps, created):
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

    def test_full_workflow(self):
        created = {}
        steps = [
            {'op': 'create', 'as': 'case', 'kind': 'case', 'data': {'person_id': 'P-1', 'onset_date': '2026-03-01', 'location': 'District-A', 'symptoms': ['fever']}},
            {'op': 'transition', 'target': 'case', 'action': 'triage', 'data': {'clinician': 'C-1'}, 'expect': 'investigating'},
            {'op': 'transition', 'target': 'case', 'action': 'lab_positive', 'data': {'lab_id': 'L-1', 'result': 'positive'}, 'expect': 'confirmed'},
            {'op': 'create', 'as': 'contact', 'kind': 'contact', 'data': {'case_id': '{case}', 'person_id': 'P-2', 'exposure_start': '2026-02-25'}},
            {'op': 'transition', 'target': 'contact', 'action': 'begin_followup', 'data': {'followup_start': '2026-03-02', 'due_at': 'IGNORED'}, 'expect': 'following'},
        ]
        self._run_steps(steps, created)

        contact = self.service.get(created["contact"])
        # due_at 必须按最后接触日重新计算，忽略请求里的值
        self.assertEqual(contact["data"]["due_at"], "2026-03-11")

        # 最后接触日次日起连续14天每天一份无症状随访
        anchor = date(2026, 2, 25)
        for offset in range(1, OBSERVATION_DAYS + 1):
            day = (anchor + timedelta(days=offset)).isoformat()
            self.service.transition(
                self.actor,
                created["contact"],
                "record_followup",
                {"followup_date": day, "symptoms": []},
            )

        completed = self.service.transition(
            self.actor,
            created["contact"],
            "complete_followup",
            {"outcome": "14天无发热、咳嗽等症状，解除观察"},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["released_at"], date.today().isoformat())

        # 病例后续恢复、关闭
        for action, payload in (
            ("recover", {"recovered_at": "2026-03-20"}),
            ("close", {"outcome": "recovered"}),
        ):
            entity = self.service.transition(self.actor, created["case"], action, payload)
        self.assertEqual(entity["status"], "closed")


if __name__ == "__main__":
    unittest.main()
