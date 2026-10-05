import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def iso(dt):
    return dt.isoformat()


class MeasurementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.base = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -60,
            "detected_at": iso(self.base),
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _source(self, observed_offset_minutes, strength, external_id="EXT-1", source_type="monitoring"):
        return {
            "source_type": source_type,
            "external_id": external_id,
            "observed_at": iso(self.base + timedelta(minutes=observed_offset_minutes)),
            "strength_dbm": strength,
            "region": "north",
            "station_id": "ST-09",
        }

    def test_later_observation_replaces_and_leaves_trace(self):
        r1 = self.service.add_source(self.item["id"], self._source(5, -55), "m", "monitor")
        self.assertEqual(r1["outcome"], "insert")
        r2 = self.service.add_source(self.item["id"], self._source(10, -30), "m", "monitor")
        self.assertEqual(r2["outcome"], "superseded")

        item = self.service.get_item(self.item["id"])
        # 当前基准取观测更晚的强度
        self.assertEqual(item["payload"]["strength_dbm"], -30.0)
        # 唯一来源记录只保留一条，payload 已为最新
        sources = item["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["payload"]["strength_dbm"], -30.0)
        # 被替换记录连同时间留痕
        revisions = sources[0]["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["payload"]["strength_dbm"], -55.0)
        self.assertEqual(revisions[0]["observed_at"], iso(self.base + timedelta(minutes=5)))
        self.assertTrue(revisions[0]["replaced_at"])
        self.assertEqual(revisions[0]["replaced_by"], "m")
        event_types = [e["event_type"] for e in item["audit"]]
        self.assertIn("source_superseded", event_types)

    def test_duplicate_same_strength_keeps_one(self):
        self.service.add_source(self.item["id"], self._source(5, -55), "m", "monitor")
        r2 = self.service.add_source(self.item["id"], self._source(10, -55), "m", "monitor")
        self.assertEqual(r2["outcome"], "ignored")
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["sources"]), 1)
        self.assertEqual(item["payload"]["strength_dbm"], -55.0)
        self.assertEqual(
            [e["event_type"] for e in item["audit"]].count("source_deduplicated"), 1
        )

    def test_earlier_observation_is_rejected(self):
        self.service.add_source(self.item["id"], self._source(10, -30), "m", "monitor")
        with self.assertRaises(ConflictError) as ctx:
            self.service.add_source(self.item["id"], self._source(6, -50), "m", "monitor")
        self.assertEqual(ctx.exception.code, "stale_measurement")
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["payload"]["strength_dbm"], -30.0)

    def test_simultaneous_later_arrival_cannot_override(self):
        self.service.add_source(self.item["id"], self._source(10, -55), "station-a", "monitor")
        with self.assertRaises(ConflictError) as ctx:
            self.service.add_source(self.item["id"], self._source(10, -30), "station-b", "monitor")
        self.assertEqual(ctx.exception.code, "simultaneous_measurement")
        item = self.service.get_item(self.item["id"])
        # 先到的为准
        self.assertEqual(item["payload"]["strength_dbm"], -55.0)
        self.assertEqual(item["sources"][0]["payload"]["strength_dbm"], -55.0)

    def test_concurrent_same_timestamp_first_commit_wins(self):
        """两个监测站并发提交同一观测时刻的补录，只允许先提交的一条落库。"""
        barrier = threading.Barrier(2)
        outcomes = []

        def submit(strength, station, bucket):
            barrier.wait()
            try:
                result = self.service.add_source(
                    self.item["id"], self._source(15, strength), station, "monitor"
                )
                bucket.append(("ok", result["outcome"]))
            except ConflictError as exc:
                bucket.append(("conflict", exc.code))

        t1 = threading.Thread(target=submit, args=(-55, "station-a", outcomes))
        t2 = threading.Thread(target=submit, args=(-30, "station-b", outcomes))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(code for _, code in outcomes), ["insert", "simultaneous_measurement"])
        sources = self.service.get_item(self.item["id"])["sources"]
        self.assertEqual(len(sources), 1)

    def test_baseline_change_invalidates_assessment_and_prompts_review(self):
        # 评估、定位，进入处置
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst")
        old_level = item["payload"]["assessment"]["level"]
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "f", "field_operator")
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-N-1"}, "c", "coordinator", item["version"], "north")

        # 更晚观测的强信号补录
        self.service.add_source(item["id"], self._source(20, -30), "m", "monitor")
        item = self.service.get_item(item["id"])

        # 旧评估失效留痕，按新强度重算
        history = item["payload"].get("assessment_history", [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["strength_dbm"], -60.0)
        self.assertEqual(item["payload"]["assessment"]["level"], item["assessment"]["level"])
        # 已进入处置：处理人看到复核提示
        self.assertTrue(item["review_required"])
        self.assertEqual(item["review_required"]["old_level"], old_level)
        self.assertEqual(item["review_required"]["new_level"], "critical")
        self.assertIn("复核", item["review_required"]["message"])

        # 处理人确认复核后提示消除
        item = self.service.act(item["id"], "acknowledge_review", {"note": "已重算并确认"}, "c", "coordinator")
        self.assertIsNone(item["review_required"])
        self.assertFalse(item["payload"].get("review_required"))
        self.assertEqual(len(item["payload"]["review_acknowledgements"]), 1)

    def test_baseline_change_before_assessment_recalculates_silently(self):
        # 尚未评估：更新基准不产生复核提示，也不固化正式评估
        self.service.add_source(self.item["id"], self._source(5, -30), "m", "monitor")
        item = self.service.get_item(self.item["id"])
        self.assertIsNone(item["review_required"])
        self.assertNotIn("assessment", item["payload"])
        # get_item 仍按最新强度试算展示
        self.assertEqual(item["assessment"]["level"], "critical")
        # 首次正式评估仍可进行
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst")
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")

    def test_baseline_picks_latest_observation_across_sources(self):
        self.service.add_source(self.item["id"], self._source(5, -55, external_id="A"), "a", "monitor")
        self.service.add_source(self.item["id"], self._source(15, -30, external_id="B"), "b", "monitor")
        self.service.add_source(self.item["id"], self._source(8, -40, external_id="C"), "c", "monitor")
        item = self.service.get_item(self.item["id"])
        # 观测时刻 15 分钟的 B 最晚，作为基准
        self.assertEqual(item["payload"]["strength_dbm"], -30.0)
        self.assertEqual(item["payload"]["baseline_basis"]["ref"], "source:2")

    def test_manual_correction_triggers_review_when_in_handling(self):
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst")
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.8}, "f", "field_operator")
        item = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -25, "reason": "calibrated"}, "a", "analyst")
        self.assertEqual(item["payload"]["strength_dbm"], -25.0)
        self.assertTrue(item["review_required"])
        self.assertEqual(item["review_required"]["new_level"], "critical")


class RegionAndDelegationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 5800.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-02",
            "region": "west",
            "strength_dbm": -55,
            "detected_at": "2026-10-01T11:00:00+00:00",
            "reporter": "monitor-2",
        }, "m", "monitor")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_cross_region_actions_rejected(self):
        item = self.service.act(self.item["id"], "assess", {}, "m", "monitor")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "cancel", {"reason": "x"}, "c-east", "coordinator", item["version"], "east"
            )
        self.assertEqual(ctx.exception.code, "region_mismatch")
        self.assertEqual(ctx.exception.status, 403)

    def test_delegation_allows_cross_region_then_expires(self):
        future = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        delegation = self.service.grant_delegation(
            {"grantee": "c-east", "region": "west", "expires_at": future, "note": "代管两天"},
            "c-west", "coordinator", "west",
        )
        self.assertEqual(delegation["revoked_at"], None)

        # 代管期间可办理
        item = self.service.act(
            self.item["id"], "cancel", {"reason": "ok"}, "c-east", "coordinator", self.item["version"], "east"
        )
        self.assertEqual(item["status"], "cancelled")

        # 到期后自动收回：新事件不能再凭该授权办理
        other = self.service.create_item({
            "frequency_mhz": 5801.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-03",
            "region": "west",
            "strength_dbm": -55,
            "detected_at": "2026-10-01T12:00:00+00:00",
            "reporter": "monitor-2",
        }, "m", "monitor")
        time.sleep(2.5)
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                other["id"], "cancel", {"reason": "late"}, "c-east", "coordinator", other["version"], "east"
            )
        self.assertEqual(ctx.exception.code, "region_mismatch")
        expired = self.repo.get_delegation(delegation["id"])
        self.assertTrue(expired["revoked_at"])
        self.assertEqual(expired["revoke_reason"], "expired")

    def test_regulator_not_subject_to_region(self):
        item = self.service.act(self.item["id"], "assess", {}, "m", "monitor")
        item = self.service.act(
            item["id"], "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator"
        )
        item = self.service.act(
            item["id"], "suspend", {"authorization_code": "REG-W-1"}, "reg", "regulator", item["version"], "east"
        )
        self.assertEqual(item["status"], "suspended")

    def test_only_coordinator_can_grant_delegation(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with self.assertRaises(DomainError) as ctx:
            self.service.grant_delegation(
                {"grantee": "x", "region": "west", "expires_at": future}, "a", "analyst", "west"
            )
        self.assertEqual(ctx.exception.status, 403)

    def test_coordinator_cannot_grant_for_other_region(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with self.assertRaises(DomainError) as ctx:
            self.service.grant_delegation(
                {"grantee": "x", "region": "north", "expires_at": future}, "c", "coordinator", "west"
            )
        self.assertEqual(ctx.exception.code, "region_mismatch")


if __name__ == "__main__":
    unittest.main()
