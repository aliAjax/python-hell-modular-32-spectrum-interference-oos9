import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def _future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past():
    return "2020-01-01T00:00:00+00:00"


class SourceBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {
                "frequency_mhz": 2400.0,
                "bandwidth_mhz": 20.0,
                "station_id": "ST-01",
                "region": "north",
                "strength_dbm": -55,
                "detected_at": "2026-09-27T10:00:00+00:00",
                "reporter": "monitor-1",
            },
            "analyst-1",
            "analyst",
        )

    def tearDown(self):
        self.repo = None
        os.unlink(self.tmp.name)
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + suffix)
            except OSError:
                pass

    def _source(self, external_id, observed_at, strength, **extra):
        payload = {
            "source_type": "station",
            "external_id": external_id,
            "observed_at": observed_at,
            "strength_dbm": strength,
            "region": "north",
        }
        payload.update(extra)
        return payload

    def test_measurement_record_drives_baseline(self):
        self.assertEqual(self.item["payload"]["strength_dbm"], -55.0)
        self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:05:00+00:00", -65),
            "m2",
            "monitor",
            "north",
        )
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["payload"]["strength_dbm"], -65.0)
        self.assertEqual(item["payload"]["baseline_observed_at"], "2026-09-27T10:05:00+00:00")
        self.assertEqual(item["payload"]["baseline_source_id"], item["sources"][0]["id"])
        changed = [e for e in item["audit"] if e["event_type"] == "baseline_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["payload"]["old_strength_dbm"], -55.0)
        self.assertEqual(changed[0]["payload"]["new_strength_dbm"], -65.0)

    def test_duplicate_source_keeps_latest_observation_and_trail(self):
        first = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:05:00+00:00", -65),
            "m2",
            "monitor",
            "north",
        )
        self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:10:00+00:00", -70, expected_version=first["version"]),
            "m2",
            "monitor",
            "north",
        )
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["sources"]), 1)
        self.assertEqual(item["sources"][0]["payload"]["strength_dbm"], -70.0)
        self.assertEqual(item["sources"][0]["observed_at"], "2026-09-27T10:10:00+00:00")
        replaced = [e for e in item["audit"] if e["event_type"] == "source_replaced"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0]["payload"]["old_strength_dbm"], -65.0)
        self.assertEqual(replaced[0]["payload"]["old_observed_at"], "2026-09-27T10:05:00+00:00")
        self.assertEqual(replaced[0]["payload"]["new_strength_dbm"], -70.0)
        self.assertEqual(replaced[0]["payload"]["new_observed_at"], "2026-09-27T10:10:00+00:00")

    def test_duplicate_same_strength_is_idempotent(self):
        first = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:05:00+00:00", -65),
            "m2",
            "monitor",
            "north",
        )
        again = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:20:00+00:00", -65),
            "m3",
            "monitor",
            "north",
        )
        self.assertEqual(again["op"], "noop")
        self.assertEqual(again["version"], first["version"])
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["sources"]), 1)
        dup = [e for e in item["audit"] if e["event_type"] == "source_duplicate"]
        self.assertEqual(len(dup), 1)

    def test_duplicate_older_observation_is_rejected(self):
        first = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:10:00+00:00", -70),
            "m2",
            "monitor",
            "north",
        )
        with self.assertRaises(ConflictError) as context:
            self.service.add_source(
                self.item["id"],
                self._source("ST-02", "2026-09-27T09:00:00+00:00", -80, expected_version=first["version"]),
                "m3",
                "monitor",
                "north",
            )
        self.assertEqual(context.exception.code, "stale_observation")

    def test_source_update_requires_expected_version(self):
        first = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:05:00+00:00", -65),
            "m2",
            "monitor",
            "north",
        )
        with self.assertRaises(DomainError) as context:
            self.service.add_source(
                self.item["id"],
                self._source("ST-02", "2026-09-27T10:10:00+00:00", -70),
                "m2",
                "monitor",
                "north",
            )
        self.assertEqual(context.exception.status, 400)
        self.assertEqual(context.exception.code, "expected_version_required")

    def test_concurrent_source_update_later_loses(self):
        first = self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:05:00+00:00", -65),
            "m2",
            "monitor",
            "north",
        )
        self.service.add_source(
            self.item["id"],
            self._source("ST-02", "2026-09-27T10:10:00+00:00", -70, expected_version=first["version"]),
            "m2",
            "monitor",
            "north",
        )
        with self.assertRaises(ConflictError) as context:
            self.service.add_source(
                self.item["id"],
                self._source("ST-02", "2026-09-27T10:15:00+00:00", -72, expected_version=first["version"]),
                "m3",
                "monitor",
                "north",
            )
        self.assertEqual(context.exception.code, "version_conflict")
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["sources"][0]["payload"]["strength_dbm"], -70.0)

    def test_baseline_change_after_locate_flags_review(self):
        item = self.service.act(self.item["id"], "assess", {}, "analyst-1", "analyst", self.item["version"])
        item = self.service.act(
            self.item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"]
        )
        self.assertFalse(item["payload"]["review_required"])
        self.service.add_source(
            self.item["id"],
            self._source("ST-03", "2026-09-27T10:25:00+00:00", -40),
            "m4",
            "monitor",
            "north",
        )
        item = self.service.get_item(self.item["id"])
        self.assertTrue(item["payload"]["review_required"])
        self.assertEqual(item["assessment"]["level"], "critical")
        review = [e for e in item["audit"] if e["event_type"] == "review_required"]
        self.assertEqual(len(review), 1)
        # re-assessing clears the review flag
        item = self.service.act(self.item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        self.assertFalse(item["payload"]["review_required"])

    def test_cross_region_action_rejected(self):
        with self.assertRaises(DomainError) as context:
            self.service.act(
                self.item["id"],
                "suspend",
                {"authorization_code": "REG-X"},
                "c-east",
                "coordinator",
                self.item["version"],
                "east",
            )
        self.assertEqual(context.exception.code, "region_mismatch")

    def test_delegation_allows_cross_region_then_auto_revokes(self):
        item = self.service.act(self.item["id"], "assess", {}, "analyst-1", "analyst", self.item["version"])
        item = self.service.act(
            self.item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"]
        )
        self.service.act(
            self.item["id"],
            "delegate",
            {"delegate_to": "c-east", "delegate_region": "east", "expires_at": _future()},
            "c-north",
            "coordinator",
            item["version"],
            "north",
        )
        item = self.service.get_item(self.item["id"])
        item = self.service.act(
            self.item["id"], "suspend", {"authorization_code": "REG-N-1"}, "c-east", "coordinator", item["version"], "east"
        )
        self.assertEqual(item["status"], "suspended")

        # backdate the delegation to simulate expiry
        conn = self.repo.connect()
        try:
            import json

            row = conn.execute("SELECT payload FROM items WHERE id=?", (self.item["id"],)).fetchone()
            payload = json.loads(row["payload"])
            payload["delegations"][0]["expires_at"] = _past()
            conn.execute("UPDATE items SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), self.item["id"]))
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(DomainError) as context:
            self.service.act(
                self.item["id"],
                "coordinate",
                {"coordination_agreement": "AGC-1"},
                "c-east",
                "coordinator",
                item["version"],
                "east",
            )
        self.assertEqual(context.exception.code, "region_mismatch")
        item = self.service.get_item(self.item["id"])
        revoked = [d for d in item["payload"]["delegations"] if d["revoked_at"]]
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0]["revoke_reason"], "expired")
        events = [e for e in item["audit"] if e["event_type"] == "delegation_revoked"]
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
