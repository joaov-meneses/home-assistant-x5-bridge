import queue
import unittest

from x5_bridge import bridge


class FakeMqttClient:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.messages.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain,
            }
        )


class FakeLocalDevice:
    def __init__(self) -> None:
        self.calls = []

    def set_value(self, dp_id, value, nowait=False):
        self.calls.append((dp_id, value, nowait))
        return None


class FakeGateway:
    def __init__(self, heartbeat_result) -> None:
        self.heartbeat_result = heartbeat_result
        self.timeouts = []
        self.heartbeat_calls = []

    def set_socketTimeout(self, timeout):
        self.timeouts.append(timeout)

    def heartbeat(self, nowait=True):
        self.heartbeat_calls.append(nowait)
        return self.heartbeat_result


def curtain_mapping():
    return {
        "1": {
            "code": "control",
            "type": "enum",
            "values": {"range": ["open", "stop", "close", "continue"]},
            "writable": True,
        },
        "2": {
            "code": "percent_control",
            "type": "integer",
            "values": {"min": 0, "max": 100, "step": 1, "scale": 0},
            "writable": True,
        },
        "3": {
            "code": "percent_state",
            "type": "integer",
            "values": {"min": 0, "max": 100, "step": 1, "scale": 0},
            "readable": True,
        },
        "5": {
            "code": "control_back_mode",
            "type": "enum",
            "values": {"range": ["forward", "back"]},
            "writable": True,
        },
        "7": {
            "code": "work_state",
            "type": "enum",
            "values": {"range": ["opening", "closing"]},
            "readable": True,
        },
    }


def yale_lia_mapping():
    return {
        "1": {"code": "unlock_fingerprint", "type": "integer"},
        "2": {"code": "unlock_password", "type": "integer"},
        "5": {"code": "unlock_card", "type": "integer"},
        "7": {"code": "unlock_key", "type": "integer"},
        "10": {
            "code": "residual_electricity",
            "type": "integer",
            "values": {"min": 0, "max": 100, "step": 1, "scale": 0, "unit": "%"},
        },
        "41": {"code": "unlock_remote", "type": "integer"},
    }


class EntityDiscoveryTests(unittest.TestCase):
    def test_raw_entity_keeps_its_discovery_config(self):
        device = bridge.BridgeDevice(
            id="raw-device",
            node_id="raw-node",
            name="Raw device",
        )

        bridge.add_raw_entity(device, "99", 42)

        spec = device.entities["x5_raw_device_raw_dp_99"]
        self.assertEqual(spec.component, "sensor")
        self.assertEqual(spec.config["name"], "DP 99")
        self.assertEqual(spec.config["state_topic"], "x5/devices/raw_device/raw/99")

    def test_tuya_curtain_mapping_creates_native_cover(self):
        device = bridge.BridgeDevice(
            id="curtain-device",
            node_id="curtain-node",
            name="Cortina inteligente",
            category="cl",
            mapping=curtain_mapping(),
        )

        bridge.infer_entities(device)

        covers = [spec for spec in device.entities.values() if spec.kind == "cover"]
        self.assertEqual(len(covers), 1)
        cover = covers[0]
        self.assertEqual(cover.config["device_class"], "curtain")
        self.assertTrue(cover.config["command_topic"].endswith("/set/1"))
        self.assertTrue(cover.config["set_position_topic"].endswith("/set/2"))
        self.assertTrue(cover.config["position_topic"].endswith("/cover/position"))
        self.assertFalse(
            any(
                spec.kind in {"select", "number_control", "raw"}
                and spec.dp_id in {"1", "2", "3", "7"}
                for spec in device.entities.values()
            )
        )

    def test_curtain_fallback_uses_observed_x5_dps(self):
        device = bridge.BridgeDevice(
            id="oem-curtain",
            node_id="oem-node",
            name="Cortina Inteligente Zigbee Slim",
            last_dps={
                "1": "stop",
                "2": 0,
                "3": 50,
                "5": "forward",
                "7": "opening",
            },
        )

        bridge.infer_entities(device)

        self.assertEqual(device.cover_control_dp, "1")
        self.assertEqual(device.cover_position_control_dp, "2")
        self.assertEqual(device.cover_position_state_dp, "3")
        self.assertEqual(device.cover_work_state_dp, "7")

    def test_cover_state_and_position_are_normalized(self):
        client = FakeMqttClient()
        device = bridge.BridgeDevice(
            id="curtain-device",
            node_id="curtain-node",
            name="Cortina",
            category="cl",
            mapping=curtain_mapping(),
        )
        bridge.infer_entities(device)

        bridge.publish_cover_state(client, device, "3", 75)
        bridge.publish_cover_state(client, device, "7", "opening")

        states = {(item["topic"], item["payload"]) for item in client.messages}
        self.assertIn(("x5/devices/curtain_device/cover/position", "75"), states)
        self.assertIn(("x5/devices/curtain_device/cover/state", "opening"), states)

    def test_yale_lia_profile_creates_native_lock_and_diagnostics(self):
        device = bridge.BridgeDevice(
            id="yale-device",
            node_id="00158d0000000001",
            name="Fechadura Porta da Frente",
            category="ms",
            product_name="Yale Door Lock",
            product_id="avdayhvk",
            mapping=yale_lia_mapping(),
        )

        bridge.infer_entities(device)

        lock = device.entities["x5_yale_device_lock_101"]
        self.assertEqual(lock.component, "lock")
        self.assertEqual(lock.config["command_topic"], "x5/devices/yale_device/set/101")
        self.assertEqual(lock.config["payload_lock"], "false")
        self.assertEqual(lock.config["payload_unlock"], "true")
        self.assertEqual(device.ha_device["manufacturer"], "Yale")
        self.assertEqual(device.ha_device["model"], "LIA")
        self.assertIn("x5_yale_device_battery_10", device.entities)
        self.assertIn("x5_yale_device_unlock_password_2", device.entities)
        self.assertIn("x5_yale_device_lock_alarm_9", device.entities)
        self.assertEqual(
            device.suppressed_raw_dps,
            {"1", "2", "5", "7", "9", "10", "41", "101"},
        )
        for dp_id in device.suppressed_raw_dps:
            self.assertIn(
                f"homeassistant/sensor/x5_yale_device_raw_dp_{dp_id}/config",
                device.obsolete_discovery_topics,
            )
            self.assertIn(
                f"homeassistant/binary_sensor/x5_yale_device_raw_dp_{dp_id}/config",
                device.obsolete_discovery_topics,
            )

    def test_yale_lia_profile_does_not_recreate_suppressed_raw_dps(self):
        client = FakeMqttClient()
        device = bridge.BridgeDevice(
            id="yale-device",
            node_id="00158d0000000001",
            name="Fechadura Porta da Frente",
            category="ms",
            product_name="Yale Door Lock",
            product_id="avdayhvk",
            mapping=yale_lia_mapping(),
        )

        bridge.publish_dps(
            client,
            device,
            {
                "2": 0,
                "7": 0,
                "9": "Master_Code_Change",
                "10": 79,
                "41": 0,
                "101": False,
            },
        )

        self.assertFalse(any(spec.kind == "raw" for spec in device.entities.values()))
        discovery_messages = {
            (item["topic"], item["payload"])
            for item in client.messages
            if item["topic"].endswith("/config")
        }
        for dp_id in {"2", "7", "9", "10", "41", "101"}:
            self.assertIn(
                (
                    f"homeassistant/sensor/x5_yale_device_raw_dp_{dp_id}/config",
                    "",
                ),
                discovery_messages,
            )

    def test_yale_lia_lock_state_uses_dp_101_boolean(self):
        client = FakeMqttClient()
        device = bridge.BridgeDevice(
            id="yale-device",
            node_id="00158d0000000001",
            name="Yale Door Lock",
            category="ms",
            product_id="avdayhvk",
            mapping=yale_lia_mapping(),
        )
        bridge.infer_entities(device)

        bridge.publish_entity_state(client, device, "101", True)
        bridge.publish_entity_state(client, device, "101", False)

        states = [
            item["payload"]
            for item in client.messages
            if item["topic"] == "x5/devices/yale_device/lock/101"
        ]
        self.assertEqual(states, ["UNLOCKED", "LOCKED"])


class CommandQueueTests(unittest.TestCase):
    def setUp(self):
        bridge.REGISTRY.by_id.clear()
        bridge.REGISTRY.by_cid.clear()
        while True:
            try:
                bridge.COMMAND_QUEUE.get_nowait()
            except queue.Empty:
                break

    def tearDown(self):
        bridge.REGISTRY.by_id.clear()
        bridge.REGISTRY.by_cid.clear()

    def test_pending_command_is_sent_without_waiting_for_response(self):
        client = FakeMqttClient()
        local = FakeLocalDevice()
        device = bridge.BridgeDevice(
            id="curtain-device",
            node_id="curtain-node",
            name="Cortina",
            category="cl",
            mapping=curtain_mapping(),
            local=local,
        )
        bridge.REGISTRY.by_id[device.id] = device
        bridge.REGISTRY.by_cid[device.node_id] = device
        bridge.infer_entities(device)

        bridge.handle_command(
            client,
            "x5/devices/curtain_device/set/1",
            "open",
        )
        self.assertEqual(local.calls, [])

        bridge.process_pending_commands(client)

        self.assertEqual(local.calls, [("1", "open", True)])
        self.assertIn(
            ("x5/devices/curtain_device/cover/state", "opening"),
            {(item["topic"], item["payload"]) for item in client.messages},
        )

    def test_cover_position_is_validated(self):
        device = bridge.BridgeDevice(
            id="curtain-device",
            node_id="curtain-node",
            name="Cortina",
            category="cl",
            mapping=curtain_mapping(),
        )
        bridge.infer_entities(device)

        value = bridge.parse_command_payload(device, None, "2", "35")
        self.assertEqual(value, 35)
        with self.assertRaises(ValueError):
            bridge.parse_command_payload(device, None, "2", "101")

    def test_yale_lia_lock_command_is_sent_as_boolean(self):
        client = FakeMqttClient()
        local = FakeLocalDevice()
        device = bridge.BridgeDevice(
            id="yale-device",
            node_id="00158d0000000001",
            name="Yale Door Lock",
            category="ms",
            product_id="avdayhvk",
            mapping=yale_lia_mapping(),
            local=local,
        )
        bridge.REGISTRY.by_id[device.id] = device
        bridge.REGISTRY.by_cid[device.node_id] = device
        bridge.infer_entities(device)

        bridge.handle_command(client, "x5/devices/yale_device/set/101", "false")
        bridge.process_pending_commands(client)

        self.assertEqual(local.calls, [("101", False, True)])
        self.assertIn(
            ("x5/devices/yale_device/lock/101", "LOCKED"),
            {(item["topic"], item["payload"]) for item in client.messages},
        )


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        bridge.GATEWAY_ONLINE = False
        bridge.LAST_AVAILABILITY_STATE = ""
        bridge.LAST_AVAILABILITY_AT = 0.0
        bridge.REGISTRY.by_id.clear()
        bridge.REGISTRY.by_cid.clear()

    def tearDown(self):
        bridge.REGISTRY.by_id.clear()
        bridge.REGISTRY.by_cid.clear()

    def test_tuya_network_error_is_recognized(self):
        message = bridge.tuya_error_message(
            {
                "Error": "Network Error: Unable to Connect",
                "Err": "901",
                "Payload": None,
            }
        )

        self.assertIn("Unable to Connect", message)
        self.assertIn("901", message)

    def test_heartbeat_error_does_not_publish_online(self):
        client = FakeMqttClient()
        gateway = FakeGateway(
            {
                "Error": "Network Error: Unable to Connect",
                "Err": "901",
                "Payload": None,
            }
        )

        with self.assertRaises(ConnectionError):
            bridge.confirm_gateway_online(client, gateway)

        self.assertFalse(bridge.GATEWAY_ONLINE)
        self.assertFalse(
            any(
                item["topic"] == bridge.AVAILABILITY_TOPIC
                and item["payload"] == "online"
                for item in client.messages
            )
        )
        self.assertEqual(gateway.heartbeat_calls, [False])

    def test_subdevice_status_is_published_by_zigbee_eui(self):
        client = FakeMqttClient()
        online_device = bridge.BridgeDevice(
            id="online-device",
            node_id="00124b0000000001",
            name="Online",
        )
        offline_device = bridge.BridgeDevice(
            id="offline-device",
            node_id="00124b0000000002",
            name="Offline",
        )
        bridge.REGISTRY.by_id = {
            online_device.id: online_device,
            offline_device.id: offline_device,
        }
        bridge.REGISTRY.by_cid = {
            online_device.node_id: online_device,
            offline_device.node_id: offline_device,
        }

        applied = bridge.apply_subdevice_status(
            client,
            {
                "reqType": "subdev_online_stat_report",
                "data": {
                    "online": [online_device.node_id],
                    "offline": [offline_device.node_id],
                },
            },
        )

        self.assertTrue(applied)
        self.assertTrue(online_device.online)
        self.assertFalse(offline_device.online)
        states = {(item["topic"], item["payload"]) for item in client.messages}
        self.assertIn(("x5/devices/online_device/availability", "online"), states)
        self.assertIn(("x5/devices/offline_device/availability", "offline"), states)

    def test_device_entities_require_gateway_and_zigbee_availability(self):
        client = FakeMqttClient()
        device = bridge.BridgeDevice(
            id="contact-device",
            node_id="00124b0000000003",
            name="Contato",
        )
        bridge.add_contact_entity(device, "1")

        bridge.publish_device_discovery(client, device)

        spec = device.entities["x5_contact_device_contact_1"]
        self.assertEqual(spec.config["availability_mode"], "all")
        self.assertEqual(
            [item["topic"] for item in spec.config["availability"]],
            [bridge.AVAILABILITY_TOPIC, device.availability_topic],
        )
        connectivity = device.entities["x5_contact_device_connection"]
        self.assertEqual(
            connectivity.config["state_topic"],
            device.availability_topic,
        )

    def test_confirmed_heartbeat_publishes_online(self):
        client = FakeMqttClient()
        gateway = FakeGateway(None)

        bridge.confirm_gateway_online(client, gateway)

        self.assertTrue(bridge.GATEWAY_ONLINE)
        self.assertEqual(
            gateway.timeouts,
            [bridge.HEARTBEAT_TIMEOUT_SECONDS, bridge.LISTEN_POLL_SECONDS],
        )
        self.assertTrue(
            any(
                item["topic"] == bridge.AVAILABILITY_TOPIC
                and item["payload"] == "online"
                for item in client.messages
            )
        )


if __name__ == "__main__":
    unittest.main()
