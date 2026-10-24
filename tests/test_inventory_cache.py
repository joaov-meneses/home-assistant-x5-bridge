import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from x5_bridge import bridge


class InventoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for key, value in {
            "REGISTRY": bridge.Registry(),
            "INVENTORY_CACHE_PATH": Path(self.temp.name) / "inventory.json",
            "X5_DEVICE_ID": "test-gateway", "SYNC_RESULT_QUEUE": queue.Queue(),
            "NEXT_SYNC_ALLOWED_AT": 0.0, "SYNC_FAILURES": 0, "SYNC_THREAD": None,
        }.items():
            p = patch.object(bridge, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.meta = {
            "id": "wall-switch", "node_id": "physical-node", "sub": True,
            "parent": "test-gateway", "name": "Sala", "key": "never-persist",
            "mapping": {"1": {"code": "switch_1", "type": "boolean", "writable": True,
                              "custom_name": "Lustre"}},
        }

    def test_restart_restores_routing_and_channel_names_without_cloud(self):
        bridge.import_cloud_inventory([self.meta])
        data = bridge.INVENTORY_CACHE_PATH.read_text()
        self.assertNotIn("never-persist", data)
        bridge.REGISTRY = bridge.Registry()
        with patch.object(bridge, "fetch_cloud_devices", side_effect=RuntimeError("quota")):
            self.assertEqual(bridge.load_inventory_cache(), 1)
        device = bridge.REGISTRY.by_id["wall-switch"]
        self.assertIs(bridge.REGISTRY.by_cid["physical-node"], device)
        bridge.infer_entities(device)
        spec = device.entities["x5_wall_switch_switch_1"]
        self.assertEqual(spec.config["name"], "Lustre")
        self.assertEqual(spec.config["command_topic"], "x5/devices/wall_switch/set/1")

    def test_cloud_error_or_empty_result_does_not_erase_cache(self):
        bridge.import_cloud_inventory([self.meta])
        original = bridge.INVENTORY_CACHE_PATH.read_bytes()
        bridge.SYNC_RESULT_QUEUE.put((None, RuntimeError("28841004 quota exhausted")))
        bridge.process_inventory_sync_results(None)
        bridge.import_cloud_inventory([])
        self.assertEqual(bridge.INVENTORY_CACHE_PATH.read_bytes(), original)
        self.assertEqual(len(bridge.REGISTRY.by_id), 1)
        self.assertGreaterEqual(bridge.NEXT_SYNC_ALLOWED_AT, 6 * 3600)

    def test_wrong_gateway_or_malformed_cache_is_ignored_atomically(self):
        bridge.import_cloud_inventory([self.meta])
        good = json.loads(bridge.INVENTORY_CACHE_PATH.read_text())
        for invalid in [dict(good, gateway_id="another-gateway"),
                        dict(good, devices=[self.meta, {"id": "broken"}]),
                        dict(good, devices=[dict(self.meta, mapping=[])])]:
            bridge.REGISTRY = bridge.Registry()
            bridge.INVENTORY_CACHE_PATH.write_text(json.dumps(invalid))
            self.assertEqual(bridge.load_inventory_cache(), 0)
            self.assertFalse(bridge.REGISTRY.by_id)

    def test_unknown_events_cannot_bypass_cloud_retry_delay(self):
        bridge.NEXT_SYNC_ALLOWED_AT = 1000
        with patch.object(bridge, "cloud_enabled", return_value=True), \
             patch.object(bridge.time, "monotonic", return_value=999), \
             patch.object(bridge.threading, "Thread") as thread:
            for _ in range(20):
                bridge.start_inventory_sync()
            thread.assert_not_called()

    def test_node_change_updates_route_and_recreates_local_device(self):
        device = bridge.REGISTRY.add_or_update(self.meta)
        device.local = object()
        bridge.REGISTRY.add_or_update(dict(self.meta, node_id="new-node"))
        self.assertNotIn("physical-node", bridge.REGISTRY.by_cid)
        self.assertIs(bridge.REGISTRY.by_cid["new-node"], device)
        self.assertIsNone(device.local)

    def test_tinytuya_string_payload_preserves_quota_error_for_backoff(self):
        response = {"Error": "Error Response from Tuya Cloud", "Err": "913",
                    "Payload": "Error from Tuya Cloud: Code 28841004: 'IoT Core trial quota is exhausted.'"}
        with patch.object(bridge.tinytuya, "Cloud") as cloud:
            cloud.return_value.getdevices.return_value = response
            with self.assertRaisesRegex(RuntimeError, "28841004") as error:
                bridge.fetch_cloud_devices()
        bridge.SYNC_RESULT_QUEUE.put((None, error.exception))
        with patch.object(bridge.time, "monotonic", return_value=1000):
            bridge.process_inventory_sync_results(None)
        self.assertEqual(bridge.NEXT_SYNC_ALLOWED_AT, 22600)


if __name__ == "__main__":
    unittest.main()
