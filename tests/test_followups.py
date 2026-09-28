import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import OBSERVATION_DAYS, RuleEngine
from src.service import DomainService


def day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


class FollowupRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine()
        self.service = DomainService(self.repo, self.rules)
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_contact(self, exposure_end=None, contact_start_offset=-13):
        case = self.service.create(
            self.actor,
            "case",
            {
                "person_id": "P-case",
                "onset_date": day(-20),
                "location": "A",
                "symptoms": ["fever"],
            },
        )
        data = {
            "case_id": case["id"],
            "person_id": "P-contact",
            "exposure_start": day(-30),
        }
        if exposure_end:
            data["exposure_end"] = exposure_end
        contact = self.service.create(self.actor, "contact", data)
        self.service.transition(
            self.actor,
            contact["id"],
            "begin_followup",
            {"followup_start": day(contact_start_offset)},
        )
        return contact["id"]

    def _daily(self, contact_id, symptoms=None, temperature=None, start=1):
        for offset in range(start, OBSERVATION_DAYS + 1):
            payload = {"followup_date": day(offset), "symptoms": symptoms or []}
            if temperature is not None:
                payload["temperature"] = temperature
            self.service.transition(self.actor, contact_id, "record_followup", payload)

    def test_one_person_one_record_per_day_and_history_kept(self):
        contact_id = self._make_contact()
        self.service.transition(
            self.actor,
            contact_id,
            "record_followup",
            {"followup_date": day(-5), "symptoms": ["fever"]},
        )
        # 同一天再次填报 -> 冲突，旧值不能被覆盖
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor,
                contact_id,
                "record_followup",
                {"followup_date": day(-5), "symptoms": []},
            )
        timeline = self.service.timeline(contact_id)
        self.assertEqual(len(timeline["followups"]), 1)
        self.assertEqual(timeline["followups"][0]["symptoms"], ["fever"])
        self.assertTrue(timeline["followups"][0]["abnormal"])

    def test_amendment_keeps_old_value(self):
        contact_id = self._make_contact()
        self.service.transition(
            self.actor,
            contact_id,
            "record_followup",
            {"followup_date": day(-5), "symptoms": ["fever"]},
        )
        self.service.transition(
            self.actor,
            contact_id,
            "amend_followup",
            {"followup_date": day(-5), "symptoms": [], "reason": "体温复核正常"},
        )
        timeline = self.service.timeline(contact_id)
        current = timeline["followups"][0]
        self.assertEqual(current["symptoms"], [])
        self.assertFalse(current["abnormal"])
        self.assertEqual(len(timeline["revisions"]), 1)
        revision = timeline["revisions"][0]
        self.assertEqual(revision["data"]["symptoms"], ["fever"])
        self.assertTrue(revision["data"]["abnormal"])
        self.assertEqual(revision["reason"], "体温复核正常")

    def test_abnormal_restarts_14_day_countdown(self):
        contact_id = self._make_contact()
        # 3天前出现发热
        self.service.transition(
            self.actor,
            contact_id,
            "record_followup",
            {"followup_date": day(-3), "symptoms": ["fever"]},
        )
        # 发热次日起连续无症状记录，但到今天只有3天 -> 不能解除
        for offset in (-2, -1, 0):
            self.service.transition(
                self.actor,
                contact_id,
                "record_followup",
                {"followup_date": day(offset), "symptoms": []},
            )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.actor,
                contact_id,
                "complete_followup",
                {"outcome": "解除"},
            )
        self.assertIn("观察期未满", str(ctx.exception))
        self.assertIn(day(-3), str(ctx.exception))

        timeline = self.service.timeline(contact_id)
        self.assertEqual(timeline["release"]["latest_abnormal_date"], day(-3))
        self.assertEqual(timeline["release"]["anchor_date"], day(-3))
        self.assertEqual(timeline["release"]["earliest_release_date"], day(11))

    def test_cough_and_temperature_are_abnormal(self):
        contact_id = self._make_contact()
        self.service.transition(
            self.actor,
            contact_id,
            "record_followup",
            {"followup_date": day(-5), "symptoms": ["咳嗽"], "temperature": 37.4},
        )
        timeline = self.service.timeline(contact_id)
        self.assertTrue(timeline["followups"][0]["abnormal"])

    def test_release_rejected_when_records_missing(self):
        # 最后接触日在15天前，观察期已满；只填10天记录，缺4天
        contact_id = self._make_contact(exposure_end=day(-15))
        for offset in range(1, 11):
            self.service.transition(
                self.actor,
                contact_id,
                "record_followup",
                {"followup_date": day(-15 + offset), "symptoms": []},
            )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.actor,
                contact_id,
                "complete_followup",
                {"outcome": "解除"},
            )
        self.assertIn("缺少有效随访记录", str(ctx.exception))
        timeline = self.service.timeline(contact_id)
        self.assertEqual(len(timeline["release"]["missing_dates"]), 4)
        self.assertFalse(timeline["release"]["eligible"])

    def test_full_clean_window_allows_release(self):
        contact_id = self._make_contact(exposure_end=day(-14))
        for offset in range(1, OBSERVATION_DAYS + 1):
            self.service.transition(
                self.actor,
                contact_id,
                "record_followup",
                {"followup_date": day(-14 + offset), "symptoms": []},
            )
        completed = self.service.transition(
            self.actor,
            contact_id,
            "complete_followup",
            {"outcome": "14天无症状，解除观察"},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["release_anchor_date"], day(-14))

    def test_confirm_case_recalculates_contacts_from_last_contact(self):
        case = self.service.create(
            self.actor,
            "case",
            {
                "person_id": "P-c",
                "onset_date": day(-20),
                "location": "A",
                "symptoms": ["fever"],
            },
        )
        contact = self.service.create(
            self.actor,
            "contact",
            {
                "case_id": case["id"],
                "person_id": "P-ct",
                "exposure_start": day(-30),
                "exposure_end": day(-20),
            },
        )
        self.service.transition(
            self.actor, case["id"], "triage", {"clinician": "C-1"}
        )
        updated_case = self.service.transition(
            self.actor,
            case["id"],
            "lab_positive",
            {"lab_id": "L-1", "result": "positive"},
        )
        self.assertEqual(updated_case["status"], "confirmed")
        contact = self.service.get(contact["id"])
        self.assertEqual(contact["data"]["last_contact_date"], day(-20))
        self.assertEqual(contact["data"]["due_at"], day(-6))
        audit = self.service.audit_log(contact["id"])
        self.assertTrue(
            any(item["action"] == "case_confirmed_recalculate" for item in audit)
        )

    def test_future_followup_date_rejected(self):
        contact_id = self._make_contact()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                contact_id,
                "record_followup",
                {"followup_date": day(3), "symptoms": []},
            )


if __name__ == "__main__":
    unittest.main()
