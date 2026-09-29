import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class RetestClosureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", item["version"], "north")
        self.authorized_at = item["payload"]["suspend_authorized_at"]
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], "north")
        self.item_id = item["id"]
        self.version = item["version"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _when(self, hours):
        base = datetime.fromisoformat(self.authorized_at.replace("Z", "+00:00"))
        return (base + timedelta(hours=hours)).isoformat()

    def _retest(self, strength, measured_at, region="north", location="cell-7", role="field_operator", actor="field-1"):
        item = self.service.get_item(self.item_id)
        result = self.service.act(
            self.item_id, "retest",
            {"strength_dbm": strength, "measured_at": measured_at, "location": location},
            actor, role, item["version"], region,
        )
        self.version = result["version"]
        return result

    def _resolve(self):
        item = self.service.get_item(self.item_id)
        return self.service.act(self.item_id, "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")

    def test_resolve_without_retest_is_rejected(self):
        with self.assertRaises(DomainError) as context:
            self._resolve()
        self.assertEqual(context.exception.code, "retest_required")
        self.assertEqual(context.exception.status, 409)

    def test_over_limit_retest_blocks_resolution_even_if_checked(self):
        item = self._retest(-50, self._when(1))
        self.assertFalse(item["closure"]["cleared"])
        self.assertEqual(item["closure"]["latest_retest"]["strength_dbm"], -50.0)
        with self.assertRaises(DomainError) as context:
            self._resolve()
        self.assertEqual(context.exception.code, "interference_present")

    def test_other_region_retest_does_not_count(self):
        # 现场人员属于其他区域，复测只进入历史，不能作为结案依据
        item = self._retest(-90, self._when(1), region="south")
        self.assertEqual(item["closure"]["eligible_retests"], 0)
        self.assertFalse(item["closure"]["cleared"])
        self.assertIn("区域不一致", item["retests"][0]["excluded_reason"])
        with self.assertRaises(DomainError) as context:
            self._resolve()
        self.assertEqual(context.exception.code, "retest_required")

    def test_retest_before_authorization_does_not_count(self):
        item = self._retest(-90, self._when(-1))
        self.assertEqual(item["closure"]["eligible_retests"], 0)
        self.assertIn("早于授权", item["retests"][0]["excluded_reason"])

    def test_only_latest_eligible_retest_decides(self):
        self._retest(-90, self._when(1))
        item = self._retest(-55, self._when(3))
        # 最新一条超标，结案被拦
        self.assertFalse(item["closure"]["cleared"])
        with self.assertRaises(DomainError):
            self._resolve()
        item = self._retest(-82, self._when(5))
        self.assertTrue(item["closure"]["cleared"])
        resolved = self._resolve()
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["payload"]["resolution"]["retest"]["strength_dbm"], -82.0)

    def test_late_over_limit_retest_after_resolution_reopens_and_keeps_history(self):
        self._retest(-85, self._when(1))
        resolved = self._resolve()
        self.assertEqual(resolved["status"], "resolved")
        original_resolution = dict(resolved["payload"]["resolution"])

        reopened = self._retest(-50, self._when(7))
        self.assertEqual(reopened["status"], "coordinating")
        self.assertNotIn("resolution", reopened["payload"])
        history = reopened["payload"]["resolution_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["evidence"], original_resolution["evidence"])
        self.assertEqual(history[0]["superseded_by_retest_seq"], 2)
        self.assertFalse(reopened["closure"]["cleared"])
        # 审计链保留结案与翻案记录
        event_types = [event["event_type"] for event in reopened["audit"]]
        self.assertIn("resolve", event_types)
        reopen_events = [e for e in reopened["audit"] if e["event_type"] == "retest" and e["payload"].get("reopened")]
        self.assertEqual(len(reopen_events), 1)

    def test_late_old_result_only_enters_history(self):
        self._retest(-50, self._when(1))
        self._retest(-85, self._when(3))
        resolved = self._resolve()
        self.assertEqual(resolved["status"], "resolved")
        # 补录一条测量时间更早的旧超标结果：当前结论不变
        item = self._retest(-40, self._when(2))
        self.assertEqual(item["status"], "resolved")
        self.assertIn("resolution", item["payload"])
        self.assertEqual(len(item["payload"]["retests"]), 3)
        self.assertTrue(item["closure"]["cleared"])
        self.assertEqual(item["closure"]["latest_retest"]["measured_at"], self._when(3))

    def test_reopen_then_clear_retest_allows_resolve_again(self):
        self._retest(-85, self._when(1))
        self._resolve()
        self._retest(-50, self._when(7))
        self._retest(-90, self._when(9))
        resolved = self._resolve()
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(len(resolved["payload"]["resolution_history"]), 1)

    def test_field_operator_is_required_for_retest(self):
        with self.assertRaises(DomainError) as context:
            self.service.act(
                self.item_id, "retest",
                {"strength_dbm": -85, "measured_at": self._when(1), "location": "cell-7"},
                "coord-1", "coordinator", self.version, "north",
            )
        self.assertEqual(context.exception.code, "forbidden")

    def test_retest_requires_expected_version(self):
        with self.assertRaises(DomainError) as context:
            self.service.act(
                self.item_id, "retest",
                {"strength_dbm": -85, "measured_at": self._when(1), "location": "cell-7"},
                "field-1", "field_operator", None, "north",
            )
        self.assertEqual(context.exception.code, "expected_version_required")


if __name__ == "__main__":
    unittest.main()
