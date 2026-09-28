import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, evaluate_release
from src.service import DomainService


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.case = self.service.create(
            self.actor, "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )
        self.service.transition(self.actor, self.case["id"], "triage", {"clinician": "C-1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _contact(self, exposure_start="2026-02-25", exposure_end=None):
        data = {"case_id": self.case["id"], "person_id": "P-2", "exposure_start": exposure_start}
        if exposure_end:
            data["exposure_end"] = exposure_end
        contact = self.service.create(self.actor, "contact", data)
        self.service.transition(self.actor, contact["id"], "begin_followup", {})
        return self.service.get(contact["id"])

    def _record(self, contact_id, day, symptoms):
        return self.service.transition(
            self.actor, contact_id, "record_followup",
            {"followup_date": day, "symptoms": symptoms},
        )

    def _fill_asymptomatic(self, contact_id, start, count):
        first = date.fromisoformat(start)
        for offset in range(count):
            self._record(contact_id, (first + timedelta(days=offset)).isoformat(), [])

    def test_exposure_end_defaults_to_start_and_window_is_14_days(self):
        contact = self._contact("2026-02-25")
        self.assertEqual(contact["data"]["exposure_end"], "2026-02-25")
        self.assertEqual(contact["data"]["followup_start"], "2026-02-26")
        self.assertEqual(contact["data"]["due_at"], "2026-03-11")

    def test_one_record_per_day_duplicate_rejected(self):
        contact = self._contact()
        self._record(contact["id"], "2026-02-26", ["fever"])
        with self.assertRaises(ConflictError) as ctx:
            self._record(contact["id"], "2026-02-26", [])
        self.assertIn("一人一天只收一份", str(ctx.exception))
        # 发热记录仍保留，未被覆盖
        stored = self.service.get(contact["id"])["data"]["followups"]["2026-02-26"]
        self.assertEqual(stored["symptoms"], ["fever"])

    def test_revise_keeps_old_value_in_history(self):
        contact = self._contact()
        self._record(contact["id"], "2026-02-26", ["fever"])
        self.service.transition(
            self.actor, contact["id"], "revise_followup",
            {"followup_date": "2026-02-26", "symptoms": [], "reason": "误录，本人无发热"},
        )
        stored = self.service.get(contact["id"])["data"]["followups"]["2026-02-26"]
        self.assertEqual(stored["symptoms"], [])
        self.assertEqual(len(stored["revisions"]), 1)
        self.assertEqual(stored["revisions"][0]["symptoms"], ["fever"])
        self.assertEqual(stored["revisions"][0]["reason"], "误录，本人无发热")

    def test_revise_without_record_fails(self):
        contact = self._contact()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, contact["id"], "revise_followup",
                {"followup_date": "2026-03-01", "symptoms": [], "reason": "x"},
            )

    def test_record_before_window_start_rejected(self):
        contact = self._contact()
        with self.assertRaises(ValidationError) as ctx:
            self._record(contact["id"], "2026-02-24", [])
        self.assertIn("不在当前观察期", str(ctx.exception))

    def test_record_after_base_window_extends_on_fever(self):
        # 基础窗 02-26..03-11；到期后仍可登记，新发热把窗口顺延 14 天
        contact = self._contact()
        cid = contact["id"]
        self._record(cid, "2026-03-15", ["fever"])
        self._fill_asymptomatic(cid, "2026-03-16", 14)
        entity = self.service.transition(
            self.actor, cid, "complete_followup", {"as_of": "2026-03-29"}
        )
        self.assertEqual(entity["status"], "completed")
        self.assertEqual(
            entity["data"]["release_evaluation"]["last_abnormal_date"], "2026-03-15"
        )

    def test_release_blocked_without_records_with_reasons(self):
        contact = self._contact()
        result = evaluate_release(self.service.get(contact["id"])["data"], as_of="2026-03-11")
        self.assertFalse(result["eligible"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("缺少 14 天", result["reasons"][0])
        self.assertEqual(len(result["missing_dates"]), 14)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.actor, contact["id"], "complete_followup", {"as_of": "2026-03-11"}
            )
        self.assertIn("暂不能解除", str(ctx.exception))

    def test_abnormal_restarts_clock_and_extends_window(self):
        contact = self._contact()
        cid = contact["id"]
        # 02-26..03-05 八天无症状
        self._fill_asymptomatic(cid, "2026-02-26", 8)
        # 03-06 发热（第 9 天）
        self._record(cid, "2026-03-06", ["fever", "cough"])
        # 重新起算：03-07..03-20 再连续 14 天无症状
        self._fill_asymptomatic(cid, "2026-03-07", 14)
        entity = self.service.transition(
            self.actor, cid, "complete_followup", {"as_of": "2026-03-20"}
        )
        self.assertEqual(entity["status"], "completed")
        self.assertEqual(entity["data"]["completed_at"], "2026-03-20")
        self.assertEqual(
            entity["data"]["release_evaluation"]["last_abnormal_date"], "2026-03-06"
        )

        # 只到 03-19：满窗但差一天无症状记录
        contact2 = self._contact()
        self._fill_asymptomatic(contact2["id"], "2026-02-26", 8)
        self._record(contact2["id"], "2026-03-06", ["fever"])
        self._fill_asymptomatic(contact2["id"], "2026-03-07", 13)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.actor, contact2["id"], "complete_followup", {"as_of": "2026-03-19"}
            )
        self.assertIn("缺少 1 天", str(ctx.exception))
        self.assertIn("2026-03-20", str(ctx.exception))

    def test_confirm_case_recalculates_window_from_last_exposure(self):
        contact = self.service.create(
            self.actor, "contact",
            {"case_id": self.case["id"], "person_id": "P-3",
             "exposure_start": "2026-02-20", "exposure_end": "2026-02-22"},
        )
        self.service.transition(self.actor, contact["id"], "begin_followup", {})
        # 窗口按最后接触日 02-22 起算
        stored = self.service.get(contact["id"])
        self.assertEqual(stored["data"]["followup_start"], "2026-02-23")
        self.assertEqual(stored["data"]["due_at"], "2026-03-08")

        # 先完成一轮观察
        self._fill_asymptomatic(contact["id"], "2026-02-23", 14)
        done = self.service.transition(
            self.actor, contact["id"], "complete_followup", {"as_of": "2026-03-08"}
        )
        self.assertEqual(done["status"], "completed")

        # 病例此时确诊：关联接触者重新起算并重新打开
        self.service.transition(
            self.actor, self.case["id"], "lab_positive",
            {"lab_id": "L-9", "result": "positive"},
        )
        reopened = self.service.get(contact["id"])
        self.assertEqual(reopened["status"], "following")
        self.assertEqual(reopened["data"]["followup_start"], "2026-02-23")
        self.assertEqual(reopened["data"]["due_at"], "2026-03-08")
        self.assertIn("window_recalculated_at", reopened["data"])
        # 历史随访仍在
        self.assertEqual(len(reopened["data"]["followups"]), 14)

        actions = [row["action"] for row in self.service.audit_log(contact["id"])]
        self.assertIn("recalculate_window", actions)

    def test_contact_requires_existing_case(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor, "contact",
                {"case_id": "missing-case", "person_id": "P-4", "exposure_start": "2026-02-25"},
            )

    def test_timeline_reports_followups_and_evaluation(self):
        contact = self._contact()
        self._record(contact["id"], "2026-02-26", ["fever"])
        timeline = self.service.timeline(contact["id"])
        self.assertEqual(len(timeline["followups"]), 1)
        self.assertEqual(timeline["followups"][0]["symptom_labels"], ["发热"])
        self.assertFalse(timeline["release_evaluation"]["eligible"])
        self.assertEqual(timeline["release_evaluation"]["last_abnormal_date"], "2026-02-26")
        self.assertGreaterEqual(len(timeline["audit"]), 2)


if __name__ == "__main__":
    unittest.main()
