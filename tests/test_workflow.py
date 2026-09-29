import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from src.rules import CLEARANCE_STRENGTH_DBM


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.authorized_at = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create_in_handling(self, region="north"):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": region,
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        item = self.service.act(
            item["id"], "suspend",
            {"authorization_code": "REG-NORTH-1", "authorized_at": self.authorized_at.isoformat()},
            "coord-1", "coordinator", item["version"], region,
        )
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], region)
        return item

    def _retest(self, item, strength, minutes_after_auth, location="cell-7", region="north", actor="field-1", role="field_operator"):
        return self.service.add_retest(item["id"], {
            "strength_dbm": strength,
            "measured_at": (self.authorized_at + timedelta(minutes=minutes_after_auth)).isoformat(),
            "location": location,
        }, actor, role, region)

    def test_complete_interference_workflow(self):
        item = self._create_in_handling()
        self._retest(item, CLEARANCE_STRENGTH_DBM - 5, 10)
        item = self.service.get_item(item["id"])
        self.assertTrue(item["resolve_readiness"]["can_resolve"])
        item = self.service.act(item["id"], "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 7)
        basis = item["payload"]["resolution"]
        self.assertEqual(basis["basis_strength_dbm"], CLEARANCE_STRENGTH_DBM - 5)
        self.assertIn("resolution_history", item["payload"])

    def test_resolve_requires_post_authorization_retest(self):
        item = self._create_in_handling()
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "resolve", {"evidence": "scan-7", "measurement_cleared": True}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(context.exception.code, "missing_retest")

    def test_over_limit_retest_blocks_resolution(self):
        item = self._create_in_handling()
        self._retest(item, CLEARANCE_STRENGTH_DBM - 5, 10)
        self._retest(item, -55, 20)
        item = self.service.get_item(item["id"])
        self.assertFalse(item["resolve_readiness"]["can_resolve"])
        self.assertEqual(item["resolve_readiness"]["reason_code"], "interference_present")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(context.exception.code, "interference_present")

    def test_retest_before_authorization_does_not_count(self):
        item = self._create_in_handling()
        self._retest(item, -90, -30)
        item = self.service.get_item(item["id"])
        self.assertFalse(item["resolve_readiness"]["can_resolve"])
        self.assertEqual(item["resolve_readiness"]["reason_code"], "missing_retest")

    def test_late_over_limit_retest_reopens_and_keeps_resolution_record(self):
        item = self._create_in_handling()
        self._retest(item, -85, 10)
        item = self.service.act(item["id"], "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        first_resolution = dict(item["payload"]["resolution"])

        self._retest(item, -50, 60)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "coordinating")
        # 原结案记录保留
        self.assertEqual(item["payload"]["resolution"]["evidence"], first_resolution["evidence"])
        self.assertEqual(item["payload"]["resolution_history"][0]["evidence"], first_resolution["evidence"])
        reopen = item["payload"]["reopen_history"][-1]
        self.assertEqual(reopen["previous_resolution"]["evidence"], first_resolution["evidence"])
        self.assertFalse(item["resolve_readiness"]["can_resolve"])

    def test_old_result_arriving_late_only_enters_history(self):
        item = self._create_in_handling()
        self._retest(item, -85, 10)
        item = self.service.act(item["id"], "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        # 更晚的合格复测后，晚到的旧超标结果（时间更早）只是补录
        self._retest(item, -88, 90)
        self._retest(item, -50, 30)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(len(item["retests"]), 3)
        self.assertTrue(item["resolve_readiness"]["can_resolve"])
        self.assertEqual(item["resolve_readiness"]["latest_retest"]["strength_dbm"], -88)

    def test_reopen_then_clear_and_resolve_again(self):
        item = self._create_in_handling()
        self._retest(item, -85, 10)
        item = self.service.act(item["id"], "resolve", {"evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self._retest(item, -50, 60)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "coordinating")
        self._retest(item, -90, 90)
        item = self.service.get_item(item["id"])
        self.assertTrue(item["resolve_readiness"]["can_resolve"])
        item = self.service.act(item["id"], "resolve", {"evidence": "scan-9"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(len(item["payload"]["resolution_history"]), 2)
        self.assertEqual(item["payload"]["resolution"]["evidence"], "scan-9")
        # 第一次结案记录仍然保留在历史中
        self.assertEqual(item["payload"]["resolution_history"][0]["evidence"], "scan-7")

    def test_retest_region_enforcement(self):
        item = self._create_in_handling("north")
        with self.assertRaises(DomainError) as context:
            self._retest(item, -85, 10, region="east")
        self.assertEqual(context.exception.code, "region_mismatch")
        # 不传区域头或同区域可以提交，复测始终归入事件区域
        retest = self._retest(item, -85, 10, region=None)
        self.assertEqual(retest["region"], "north")

    def test_retest_requires_fields_and_valid_status(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-09",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        with self.assertRaises(DomainError) as state_error:
            self._retest(item, -85, 10)
        self.assertEqual(state_error.exception.code, "invalid_state")
        handling = self._create_in_handling()
        with self.assertRaises(DomainError):
            self.service.add_retest(handling["id"], {
                "strength_dbm": -85,
                "measured_at": (self.authorized_at + timedelta(minutes=10)).isoformat(),
            }, "field-1", "field_operator", "north")

    def test_coordinator_cannot_submit_retest(self):
        item = self._create_in_handling()
        with self.assertRaises(DomainError) as context:
            self._retest(item, -85, 10, actor="coord-1", role="coordinator", region="north")
        self.assertEqual(context.exception.code, "forbidden")


if __name__ == "__main__":
    unittest.main()
