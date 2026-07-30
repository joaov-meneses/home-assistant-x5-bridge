from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
import tinytuya


VERSION = "0.8.1"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


X5_IP = env("X5_IP")
X5_DEVICE_ID = env("X5_DEVICE_ID")
X5_LOCAL_KEY = env("X5_LOCAL_KEY")
X5_VERSION = float(env("X5_VERSION", "3.5"))
AUTO_SYNC = env_bool("AUTO_SYNC", False)
SYNC_INTERVAL_MINUTES = max(env_int("SYNC_INTERVAL_MINUTES", 10), 1)
TUYA_REGION = env("TUYA_REGION", "us-e")
TUYA_ACCESS_ID = env("TUYA_ACCESS_ID")
TUYA_ACCESS_SECRET = env("TUYA_ACCESS_SECRET")
TUYA_CLOUD_DEVICE_ID = env("TUYA_CLOUD_DEVICE_ID") or X5_DEVICE_ID
CREATE_UNKNOWN_DP_ENTITIES = env_bool("CREATE_UNKNOWN_DP_ENTITIES", True)
MQTT_HOST = env("MQTT_HOST")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USERNAME = env("MQTT_USERNAME")
MQTT_PASSWORD = env("MQTT_PASSWORD")
TOPIC_PREFIX = env("TOPIC_PREFIX", "x5").strip("/")
DEBUG = env_bool("DEBUG")

AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}/bridge/availability"
INVENTORY_TOPIC = f"{TOPIC_PREFIX}/inventory"
ALL_EVENTS_TOPIC = f"{TOPIC_PREFIX}/events"
STOP = False
GATEWAY_ONLINE = False
LAST_AVAILABILITY_STATE = ""
LAST_AVAILABILITY_AT = 0.0
AVAILABILITY_REFRESH_SECONDS = 30.0
LISTEN_POLL_SECONDS = 0.25
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_TIMEOUT_SECONDS = 1.0
SUBDEVICE_STATUS_INTERVAL_SECONDS = 30.0
SUBDEVICE_STATUS_TIMEOUT_SECONDS = 2.0

AVAILABILITY_LOCK = threading.Lock()
COMMAND_QUEUE: queue.Queue[tuple[str, str, Any]] = queue.Queue()
SYNC_RESULT_QUEUE: queue.Queue[tuple[list[dict[str, Any]] | None, Exception | None]] = queue.Queue()
SYNC_THREAD_LOCK = threading.Lock()
SYNC_THREAD: threading.Thread | None = None

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("x5_bridge")


def stop_handler(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "device"


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def preferred_device_name(meta: dict[str, Any], fallback: str = "") -> str:
    for key in ("custom_name", "name", "product_name", "model", "id", "node_id"):
        value = clean_text(meta.get(key))
        if value:
            return value
    return fallback


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
            if key != "device"
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return repr(value)


def tuya_error_message(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    error = value.get("Error")
    code = value.get("Err")
    if error:
        return f"{error} (codigo {code})" if code not in (None, "", 0, "0") else str(error)
    if code not in (None, "", 0, "0"):
        return f"Erro Tuya {code}"
    return ""


def extract_dps(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    direct = message.get("dps")
    if isinstance(direct, dict):
        return {str(key): value for key, value in direct.items()}
    data = message.get("data")
    if isinstance(data, dict) and isinstance(data.get("dps"), dict):
        return {str(key): value for key, value in data["dps"].items()}
    return {}


def extract_cid(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("cid"):
        return str(message["cid"])
    data = message.get("data")
    if isinstance(data, dict) and data.get("cid"):
        return str(data["cid"])
    return ""


def normalize_on_off(value: Any) -> str | None:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, (int, float)):
        return "ON" if value != 0 else "OFF"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "on", "open", "opened", "aberta", "aberto"}:
            return "ON"
        if normalized in {"false", "0", "off", "closed", "close", "fechada", "fechado"}:
            return "OFF"
    return None


def normalize_alarm(value: Any) -> str | None:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, (int, float)):
        return "ON" if value != 0 else "OFF"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {
            "true",
            "1",
            "on",
            "alarm",
            "alert",
            "detected",
            "detect",
            "wet",
            "water",
            "leak",
            "flood",
            "rain",
            "raining",
            "presence",
            "triggered",
        }:
            return "ON"
        if normalized in {
            "false",
            "0",
            "off",
            "normal",
            "clear",
            "dry",
            "none",
            "undetected",
            "no_alarm",
        }:
            return "OFF"
    return None


def normalize_number(value: Any, scale: int = 0) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value) if "." in value else int(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        divisor = 10 ** scale
        result = value / divisor if divisor > 1 else value
        if isinstance(result, float) and result.is_integer():
            return int(result)
        return result
    return None


def mapping_code(mapping: dict[str, Any], dp_id: str) -> str:
    item = mapping.get(str(dp_id), {})
    if isinstance(item, dict):
        return str(item.get("code", "")).lower()
    return ""


def mapping_type(mapping: dict[str, Any], dp_id: str) -> str:
    item = mapping.get(str(dp_id), {})
    if isinstance(item, dict):
        return str(item.get("type", "")).lower()
    return ""


def mapping_values(mapping: dict[str, Any], dp_id: str) -> dict[str, Any]:
    item = mapping.get(str(dp_id), {})
    if isinstance(item, dict) and isinstance(item.get("values"), dict):
        return item["values"]
    return {}


def mapping_scale(mapping: dict[str, Any], dp_id: str) -> int:
    values = mapping_values(mapping, dp_id)
    try:
        return int(values.get("scale", 0))
    except (TypeError, ValueError):
        return 0


def mapping_item(mapping: dict[str, Any], dp_id: str) -> dict[str, Any]:
    item = mapping.get(str(dp_id), {})
    return item if isinstance(item, dict) else {}


def mapping_writable(mapping: dict[str, Any], dp_id: str) -> bool:
    item = mapping_item(mapping, dp_id)
    if item.get("writable") is True:
        return True
    mode = str(item.get("mode") or item.get("access_mode") or item.get("accessMode") or "").lower()
    return "w" in mode


def dp_for_codes(mapping: dict[str, Any], codes: set[str]) -> str:
    for dp_id in mapping:
        if mapping_code(mapping, str(dp_id)) in codes:
            return str(dp_id)
    return ""


def parse_mapping_values(dp_type: str, raw_values: Any) -> Any:
    if raw_values in ("", None):
        return {}
    if isinstance(raw_values, dict):
        values = raw_values
    elif dp_type.lower() == "string":
        values = raw_values
    else:
        try:
            values = json.loads(raw_values)
        except (TypeError, json.JSONDecodeError):
            values = raw_values
    if isinstance(values, dict) and values.get("unit"):
        values["unit"] = (
            str(values["unit"])
            .replace("\u2109", "F")
            .replace("\u2103", "C")
            .replace("f", "F")
            .replace("c", "C")
            .replace("\u79d2", "s")
        )
    return values


def merge_spec_item(dst: dict[str, Any], src: dict[str, Any], readable: bool, writable: bool) -> None:
    code = str(src.get("code") or "")
    dp_id = str(src.get("dp_id") or code)
    dp_type = str(src.get("type") or "")
    if not dp_id:
        return
    item = dst.setdefault(dp_id, {})
    item.setdefault("code", code)
    item.setdefault("type", dp_type)
    values = parse_mapping_values(dp_type, src.get("values"))
    if values not in ("", None, {}):
        item["values"] = values
    elif "values" not in item:
        item["values"] = {}
    if dp_type.lower() == "json" and src.get("values") is not None:
        item["raw_values"] = src.get("values")
    item["readable"] = bool(item.get("readable")) or readable
    item["writable"] = bool(item.get("writable")) or writable


def enhanced_mapping_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    result = spec.get("result") if isinstance(spec, dict) else None
    if not isinstance(result, dict):
        return {}
    mapping: dict[str, Any] = {}
    for item in result.get("status") or []:
        if isinstance(item, dict):
            merge_spec_item(mapping, item, readable=True, writable=False)
    for item in result.get("functions") or []:
        if isinstance(item, dict):
            merge_spec_item(mapping, item, readable=False, writable=True)
    return mapping


def merge_mapping(base: dict[str, Any], enhanced: dict[str, Any]) -> dict[str, Any]:
    merged = {str(key): value for key, value in base.items() if isinstance(value, dict)}
    for dp_id, item in enhanced.items():
        current = dict(merged.get(str(dp_id), {}))
        current.update({key: value for key, value in item.items() if value not in (None, "")})
        current["readable"] = bool(current.get("readable")) or bool(item.get("readable"))
        current["writable"] = bool(current.get("writable")) or bool(item.get("writable"))
        merged[str(dp_id)] = current
    return merged


def friendly_dp_name(code: str, fallback: str) -> str:
    names = {
        "switch": "Interruptor",
        "child_lock": "Trava infantil",
        "backlight_switch": "Luz de fundo",
        "countdown": "Temporizador",
        "sensitivity": "Sensibilidade",
        "illuminance_sampling": "Intervalo de iluminância",
        "illuminance_interval": "Intervalo de iluminância",
        "motion_detection_sensitivity": "Sensibilidade de movimento",
        "presence_sensitivity": "Sensibilidade de presença",
        "indicator": "Indicador",
        "led": "LED",
        "power_on_behavior": "Comportamento ao energizar",
        "mode": "Modo",
    }
    normalized = code.strip().lower()
    return names.get(normalized) or normalized.replace("_", " ").title() or fallback


def enum_options(values: dict[str, Any]) -> list[str]:
    raw = values.get("range") or values.get("ranges")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def scale_value(value: Any, scale: int) -> float | int | None:
    number = normalize_number(value)
    if number is None:
        return None
    divisor = 10 ** scale
    result = number / divisor if divisor > 1 else number
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def unscale_value(value: float | int, scale: int) -> float | int:
    multiplier = 10 ** scale
    result = value * multiplier if multiplier > 1 else value
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def descriptor_for(device: "BridgeDevice") -> str:
    return " ".join(
        item.lower()
        for item in (device.name, device.product_name, device.model, device.category, device.product_id)
        if item
    )


@dataclass
class EntitySpec:
    component: str
    object_id: str
    config: dict[str, Any]
    dp_id: str = ""
    kind: str = ""
    retain_state: bool = True


@dataclass
class BridgeDevice:
    id: str
    node_id: str
    name: str
    custom_name: str = ""
    category: str = ""
    product_name: str = ""
    product_id: str = ""
    model: str = ""
    mapping: dict[str, Any] = field(default_factory=dict)
    local: tinytuya.Device | None = None
    online: bool | None = None
    last_dps: dict[str, Any] = field(default_factory=dict)
    discovered_raw_dps: set[str] = field(default_factory=set)
    suppressed_raw_dps: set[str] = field(default_factory=set)
    entities: dict[str, EntitySpec] = field(default_factory=dict)
    obsolete_discovery_topics: set[str] = field(default_factory=set)
    cover_cleanup_done: bool = False
    lock_cleanup_done: bool = False
    cover_control_dp: str = ""
    cover_position_control_dp: str = ""
    cover_position_state_dp: str = ""
    cover_work_state_dp: str = ""

    @property
    def uid(self) -> str:
        return slugify(self.id or self.node_id)

    @property
    def slug(self) -> str:
        return slugify(self.name or self.product_name or self.id or self.node_id)

    @property
    def topic_base(self) -> str:
        return f"{TOPIC_PREFIX}/devices/{self.uid}"

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_base}/availability"

    @property
    def cover_dp_ids(self) -> set[str]:
        return {
            dp_id
            for dp_id in (
                self.cover_control_dp,
                self.cover_position_control_dp,
                self.cover_position_state_dp,
                self.cover_work_state_dp,
            )
            if dp_id
        }

    @property
    def ha_device(self) -> dict[str, Any]:
        is_yale_lia = self.product_id == "avdayhvk" or (
            self.category.lower() == "ms"
            and "yale door lock" in descriptor_for(self)
        )
        return {
            "identifiers": [f"x5_{self.uid}"],
            "name": self.name or self.product_name or self.id,
            "manufacturer": "Yale" if is_yale_lia else "Tuya/OEM",
            "model": "LIA" if is_yale_lia else self.model or self.product_name or self.category or "Subdevice",
            "via_device": "x5_bridge",
        }


class Registry:
    def __init__(self) -> None:
        self.gateway: tinytuya.Device | None = None
        self.by_id: dict[str, BridgeDevice] = {}
        self.by_cid: dict[str, BridgeDevice] = {}

    def all(self) -> list[BridgeDevice]:
        return sorted(self.by_id.values(), key=lambda item: item.name.lower())

    def add_or_update(self, meta: dict[str, Any]) -> BridgeDevice | None:
        dev_id = str(meta.get("id") or "").strip()
        node_id = str(meta.get("node_id") or "").strip()
        if not dev_id or not node_id:
            return None
        existing = self.by_id.get(dev_id)
        if existing:
            device = existing
        else:
            device = BridgeDevice(
                id=dev_id,
                node_id=node_id,
                name=preferred_device_name(meta, dev_id),
            )
            self.by_id[dev_id] = device
            self.by_cid[node_id] = device

        previous_name = device.name
        device.node_id = node_id
        if "custom_name" in meta:
            device.custom_name = clean_text(meta.get("custom_name"))
        device.name = (
            device.custom_name
            or clean_text(meta.get("name"))
            or clean_text(meta.get("product_name"))
            or device.name
            or dev_id
        )
        device.category = clean_text(meta.get("category")) or device.category
        device.product_name = clean_text(meta.get("product_name")) or device.product_name
        device.product_id = clean_text(meta.get("product_id")) or device.product_id
        device.model = clean_text(meta.get("model")) or device.model
        mapping = meta.get("mapping")
        if isinstance(mapping, dict):
            device.mapping = mapping
        if self.gateway and device.local is None:
            device.local = tinytuya.Device(dev_id=device.id, cid=device.node_id, parent=self.gateway)
        if previous_name and previous_name != device.name:
            LOG.info("Nome Tuya atualizado: %s -> %s", previous_name, device.name)
        return device

    def attach_gateway(self, gateway: tinytuya.Device) -> None:
        self.gateway = gateway
        for device in self.by_id.values():
            device.local = tinytuya.Device(dev_id=device.id, cid=device.node_id, parent=gateway)

    def find_for_event(self, event: dict[str, Any]) -> BridgeDevice | None:
        raw_device = event.get("device")
        if raw_device is not None:
            for device in self.by_id.values():
                if device.local is raw_device:
                    return device
        cid = extract_cid(event)
        if cid:
            return self.by_cid.get(cid)
        return None


REGISTRY = Registry()


def gateway_device_payload() -> dict[str, Any]:
    return {
        "identifiers": ["x5_bridge"],
        "name": "Gateway X5",
        "manufacturer": "Tuya/OEM",
        "model": "X5",
    }


def discovery_topic(spec: EntitySpec) -> str:
    return f"homeassistant/{spec.component}/{spec.object_id}/config"


def publish_config(client: mqtt.Client, spec: EntitySpec) -> None:
    client.publish(discovery_topic(spec), json.dumps(spec.config, ensure_ascii=False), qos=1, retain=True)


def publish_availability(client: mqtt.Client, state: str, force: bool = False) -> None:
    global GATEWAY_ONLINE, LAST_AVAILABILITY_AT, LAST_AVAILABILITY_STATE
    normalized = "online" if state == "online" else "offline"
    current = time.monotonic()
    with AVAILABILITY_LOCK:
        GATEWAY_ONLINE = normalized == "online"
        if (
            not force
            and LAST_AVAILABILITY_STATE == normalized
            and current - LAST_AVAILABILITY_AT < AVAILABILITY_REFRESH_SECONDS
        ):
            return
        LAST_AVAILABILITY_STATE = normalized
        LAST_AVAILABILITY_AT = current
    client.publish(AVAILABILITY_TOPIC, normalized, qos=1, retain=True)


def refresh_availability(client: mqtt.Client) -> None:
    publish_availability(client, "online" if GATEWAY_ONLINE else "offline", force=True)


def publish_gateway_discovery(client: mqtt.Client) -> None:
    configs = [
        EntitySpec(
            component="binary_sensor",
            object_id="x5_bridge_connection",
            config={
                "name": "Conexão local",
                "unique_id": "x5_bridge_connection",
                "state_topic": AVAILABILITY_TOPIC,
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "device": gateway_device_payload(),
            },
        ),
        EntitySpec(
            component="sensor",
            object_id="x5_bridge_inventory",
            config={
                "name": "Inventário",
                "unique_id": "x5_bridge_inventory",
                "state_topic": INVENTORY_TOPIC,
                "value_template": "{{ value_json.count }}",
                "json_attributes_topic": INVENTORY_TOPIC,
                "entity_category": "diagnostic",
                "device": gateway_device_payload(),
            },
        ),
    ]
    for spec in configs:
        publish_config(client, spec)


def dp_state_topic(device: BridgeDevice, dp_id: str) -> str:
    return f"{device.topic_base}/dps/{dp_id}"


def dp_command_topic(device: BridgeDevice, dp_id: str) -> str:
    return f"{device.topic_base}/set/{dp_id}"


def add_last_event_entity(device: BridgeDevice) -> None:
    object_id = f"x5_{device.uid}_last_event"
    device.entities[object_id] = EntitySpec(
        component="sensor",
        object_id=object_id,
        config={
            "name": "Último evento",
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/event",
            "value_template": "{{ value_json.received_at }}",
            "json_attributes_topic": f"{device.topic_base}/event",
            "device_class": "timestamp",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "diagnostic",
            "device": device.ha_device,
        },
    )


def add_connectivity_entity(device: BridgeDevice) -> None:
    object_id = f"x5_{device.uid}_connection"
    device.entities[object_id] = EntitySpec(
        component="binary_sensor",
        object_id=object_id,
        kind="connectivity",
        config={
            "name": "Conexão Zigbee",
            "unique_id": object_id,
            "state_topic": device.availability_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "diagnostic",
            "device": device.ha_device,
        },
    )


def add_contact_entity(device: BridgeDevice, dp_id: str, name: str = "Porta") -> None:
    object_id = f"x5_{device.uid}_contact_{dp_id}"
    state_topic = f"{device.topic_base}/contact/{dp_id}"
    device.entities[object_id] = EntitySpec(
        component="binary_sensor",
        object_id=object_id,
        dp_id=dp_id,
        kind="contact",
        config={
            "name": name,
            "unique_id": object_id,
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "door",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device.ha_device,
        },
    )


def add_motion_entity(device: BridgeDevice, dp_id: str) -> None:
    object_id = f"x5_{device.uid}_motion_{dp_id}"
    device.entities[object_id] = EntitySpec(
        component="binary_sensor",
        object_id=object_id,
        dp_id=dp_id,
        kind="motion",
        config={
            "name": "Movimento",
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/motion/{dp_id}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "motion",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device.ha_device,
        },
    )


def add_moisture_entity(device: BridgeDevice, dp_id: str, name: str = "Água detectada") -> None:
    object_id = f"x5_{device.uid}_moisture_{dp_id}"
    device.entities[object_id] = EntitySpec(
        component="binary_sensor",
        object_id=object_id,
        dp_id=dp_id,
        kind="moisture",
        config={
            "name": name,
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/moisture/{dp_id}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "moisture",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device.ha_device,
        },
    )


def add_switch_entity(device: BridgeDevice, dp_id: str, name: str, entity_category: str | None = None) -> None:
    object_id = f"x5_{device.uid}_switch_{dp_id}"
    config = {
        "name": name,
        "unique_id": object_id,
        "state_topic": f"{device.topic_base}/switch/{dp_id}",
        "command_topic": dp_command_topic(device, dp_id),
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device.ha_device,
    }
    if entity_category:
        config["entity_category"] = entity_category
    device.entities[object_id] = EntitySpec(
        component="switch",
        object_id=object_id,
        dp_id=dp_id,
        kind="switch",
        config=config,
    )


def add_lock_entity(device: BridgeDevice, dp_id: str, name: str = "Fechadura") -> None:
    object_id = f"x5_{device.uid}_lock_{dp_id}"
    for existing_id, spec in list(device.entities.items()):
        if existing_id != object_id and spec.dp_id == dp_id:
            device.obsolete_discovery_topics.add(discovery_topic(spec))
            del device.entities[existing_id]
    if not device.lock_cleanup_done:
        legacy_id = f"x5_{device.uid}_raw_dp_{dp_id}"
        device.obsolete_discovery_topics.update(
            {
                f"homeassistant/sensor/{legacy_id}/config",
                f"homeassistant/binary_sensor/{legacy_id}/config",
            }
        )
        device.lock_cleanup_done = True
    device.entities[object_id] = EntitySpec(
        component="lock",
        object_id=object_id,
        dp_id=dp_id,
        kind="lock",
        config={
            "name": name,
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/lock/{dp_id}",
            "command_topic": dp_command_topic(device, dp_id),
            "payload_lock": "false",
            "payload_unlock": "true",
            "state_locked": "LOCKED",
            "state_unlocked": "UNLOCKED",
            "optimistic": False,
            "icon": "mdi:lock-smart",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device.ha_device,
        },
    )


def add_number_control_entity(
    device: BridgeDevice,
    dp_id: str,
    name: str,
    values: dict[str, Any] | None = None,
    unit: str | None = None,
) -> None:
    values = values or {}
    object_id = f"x5_{device.uid}_number_{dp_id}"
    scale = mapping_scale(device.mapping, dp_id)
    config = {
        "name": name,
        "unique_id": object_id,
        "state_topic": f"{device.topic_base}/number/{dp_id}",
        "command_topic": dp_command_topic(device, dp_id),
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "entity_category": "config",
        "mode": "box",
        "device": device.ha_device,
    }
    for source_key, target_key in (("min", "min"), ("max", "max"), ("step", "step")):
        if source_key in values:
            scaled = scale_value(values[source_key], scale)
            if scaled is not None:
                config[target_key] = scaled
    if "step" not in config:
        config["step"] = 1 / (10 ** scale) if scale > 0 else 1
    if unit:
        config["unit_of_measurement"] = unit
    device.entities[object_id] = EntitySpec(
        component="number",
        object_id=object_id,
        dp_id=dp_id,
        kind="number_control",
        config=config,
    )


def add_select_entity(device: BridgeDevice, dp_id: str, name: str, options: list[str]) -> None:
    if not options:
        return
    object_id = f"x5_{device.uid}_select_{dp_id}"
    device.entities[object_id] = EntitySpec(
        component="select",
        object_id=object_id,
        dp_id=dp_id,
        kind="select",
        config={
            "name": name,
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/select/{dp_id}",
            "command_topic": dp_command_topic(device, dp_id),
            "options": options,
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "config",
            "device": device.ha_device,
        },
    )


def add_text_entity(device: BridgeDevice, dp_id: str, name: str) -> None:
    object_id = f"x5_{device.uid}_text_{dp_id}"
    device.entities[object_id] = EntitySpec(
        component="text",
        object_id=object_id,
        dp_id=dp_id,
        kind="text",
        config={
            "name": name,
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/text/{dp_id}",
            "command_topic": dp_command_topic(device, dp_id),
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "config",
            "device": device.ha_device,
        },
    )


def add_cover_entity(
    device: BridgeDevice,
    control_dp: str,
    position_control_dp: str = "",
    position_state_dp: str = "",
    work_state_dp: str = "",
) -> None:
    if not control_dp:
        return
    device.cover_control_dp = control_dp
    device.cover_position_control_dp = position_control_dp
    device.cover_position_state_dp = position_state_dp
    device.cover_work_state_dp = work_state_dp

    object_id = f"x5_{device.uid}_cover"
    for existing_id, spec in list(device.entities.items()):
        if existing_id != object_id and spec.dp_id in device.cover_dp_ids:
            device.obsolete_discovery_topics.add(discovery_topic(spec))
            del device.entities[existing_id]
    if not device.cover_cleanup_done:
        for dp_id in device.cover_dp_ids:
            legacy_ids = (
                ("select", f"x5_{device.uid}_select_{dp_id}"),
                ("number", f"x5_{device.uid}_number_{dp_id}"),
                ("text", f"x5_{device.uid}_text_{dp_id}"),
                ("switch", f"x5_{device.uid}_switch_{dp_id}"),
                ("sensor", f"x5_{device.uid}_raw_dp_{dp_id}"),
                ("binary_sensor", f"x5_{device.uid}_raw_dp_{dp_id}"),
            )
            device.obsolete_discovery_topics.update(
                f"homeassistant/{component}/{legacy_id}/config"
                for component, legacy_id in legacy_ids
            )
        device.cover_cleanup_done = True
    config = {
        "name": "Cortina",
        "unique_id": object_id,
        "command_topic": dp_command_topic(device, control_dp),
        "state_topic": f"{device.topic_base}/cover/state",
        "payload_open": "open",
        "payload_close": "close",
        "payload_stop": "stop",
        "state_open": "open",
        "state_opening": "opening",
        "state_closed": "closed",
        "state_closing": "closing",
        "state_stopped": "stopped",
        "device_class": "curtain",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device.ha_device,
    }
    if position_state_dp:
        config["position_topic"] = f"{device.topic_base}/cover/position"
        config["position_open"] = 100
        config["position_closed"] = 0
    if position_control_dp:
        config["set_position_topic"] = dp_command_topic(device, position_control_dp)

    device.entities[object_id] = EntitySpec(
        component="cover",
        object_id=object_id,
        dp_id=control_dp,
        kind="cover",
        config=config,
    )


def add_number_sensor(
    device: BridgeDevice,
    dp_id: str,
    object_suffix: str,
    name: str,
    device_class: str | None = None,
    unit: str | None = None,
    state_class: str | None = None,
    kind: str = "number",
) -> None:
    object_id = f"x5_{device.uid}_{object_suffix}_{dp_id}"
    config = {
        "name": name,
        "unique_id": object_id,
        "state_topic": f"{device.topic_base}/{object_suffix}/{dp_id}",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device.ha_device,
    }
    if device_class:
        config["device_class"] = device_class
    if unit:
        config["unit_of_measurement"] = unit
    if state_class:
        config["state_class"] = state_class
    device.entities[object_id] = EntitySpec(
        component="sensor",
        object_id=object_id,
        dp_id=dp_id,
        kind=kind,
        config=config,
    )


def add_enum_sensor(
    device: BridgeDevice,
    dp_id: str,
    object_suffix: str,
    name: str,
    value_template: str | None = None,
) -> None:
    object_id = f"x5_{device.uid}_{object_suffix}_{dp_id}"
    config = {
        "name": name,
        "unique_id": object_id,
        "state_topic": f"{device.topic_base}/{object_suffix}/{dp_id}",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device.ha_device,
    }
    if value_template:
        config["value_template"] = value_template
    device.entities[object_id] = EntitySpec(
        component="sensor",
        object_id=object_id,
        dp_id=dp_id,
        kind="enum",
        config=config,
    )


def add_raw_entity(device: BridgeDevice, dp_id: str, value: Any) -> None:
    if (
        not CREATE_UNKNOWN_DP_ENTITIES
        or dp_id in device.discovered_raw_dps
        or dp_id in device.suppressed_raw_dps
    ):
        return
    if any(spec.dp_id == dp_id for spec in device.entities.values()):
        return
    device.discovered_raw_dps.add(dp_id)
    suffix = f"raw_dp_{dp_id}"
    object_id = f"x5_{device.uid}_{suffix}"
    if isinstance(value, bool):
        component = "binary_sensor"
        config = {
            "name": f"DP {dp_id}",
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/raw/{dp_id}",
            "payload_on": "true",
            "payload_off": "false",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "diagnostic",
            "device": device.ha_device,
        }
    else:
        component = "sensor"
        config = {
            "name": f"DP {dp_id}",
            "unique_id": object_id,
            "state_topic": f"{device.topic_base}/raw/{dp_id}",
            "json_attributes_topic": f"{device.topic_base}/event",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "diagnostic",
            "device": device.ha_device,
        }
    device.entities[object_id] = EntitySpec(
        component=component,
        object_id=object_id,
        dp_id=dp_id,
        kind="raw",
        config=config,
    )


def suppress_raw_entities(device: BridgeDevice, dp_ids: set[str]) -> None:
    """Remove raw MQTT entities that have a friendlier product-specific entity."""
    normalized_dp_ids = {str(dp_id) for dp_id in dp_ids}
    device.suppressed_raw_dps.update(normalized_dp_ids)

    for object_id, spec in list(device.entities.items()):
        if spec.kind == "raw" and spec.dp_id in normalized_dp_ids:
            device.obsolete_discovery_topics.add(discovery_topic(spec))
            del device.entities[object_id]

    for dp_id in normalized_dp_ids:
        legacy_id = f"x5_{device.uid}_raw_dp_{dp_id}"
        device.obsolete_discovery_topics.update(
            {
                f"homeassistant/sensor/{legacy_id}/config",
                f"homeassistant/binary_sensor/{legacy_id}/config",
            }
        )


def has_command_entity(device: BridgeDevice, dp_id: str) -> bool:
    return any(
        spec.dp_id == dp_id and bool(spec.config.get("command_topic"))
        for spec in device.entities.values()
    )


def add_control_entity_from_mapping(device: BridgeDevice, dp_id: str) -> None:
    if has_command_entity(device, dp_id) or not mapping_writable(device.mapping, dp_id):
        return
    code = mapping_code(device.mapping, dp_id)
    dp_type = mapping_type(device.mapping, dp_id)
    values = mapping_values(device.mapping, dp_id)
    unit = str(values.get("unit") or "")
    name = friendly_dp_name(code, f"DP {dp_id}")

    if dp_type == "boolean":
        add_switch_entity(device, dp_id, name, entity_category="config" if not code.startswith("switch") else None)
    elif dp_type == "enum":
        add_select_entity(device, dp_id, name, enum_options(values))
    elif dp_type in {"integer", "value"} and {"min", "max"} <= set(values.keys()):
        add_number_control_entity(device, dp_id, name, values, unit or None)
    elif dp_type == "string":
        add_text_entity(device, dp_id, name)


def apply_known_product_profile(device: BridgeDevice) -> bool:
    descriptor = descriptor_for(device)

    # Yale LIA connected through Yale Connect/X5. Tuya's cloud specification
    # omits DP 101, but the local Zigbee events use it as the deadbolt state:
    # false = locked and true = unlocked. The same DP accepts boolean commands.
    if device.product_id == "avdayhvk" or (
        device.category.lower() == "ms"
        and "yale door lock" in descriptor
    ):
        # The product-specific entities below supersede these generic DPs.
        # Also clear retained MQTT discovery from older bridge versions.
        suppress_raw_entities(device, {"1", "2", "5", "7", "9", "10", "41", "101"})
        add_lock_entity(device, "101")
        add_number_sensor(device, "10", "battery", "Bateria", "battery", "%", "measurement", "battery")
        access_entities = (
            ("1", "unlock_fingerprint", "Último acesso por biometria"),
            ("2", "unlock_password", "Último acesso por senha"),
            ("5", "unlock_card", "Último acesso por cartão"),
            ("7", "unlock_key", "Último acesso por chave"),
            ("41", "unlock_remote", "Último acesso remoto"),
        )
        for dp_id, suffix, name in access_entities:
            add_number_sensor(device, dp_id, suffix, name)
            device.entities[f"x5_{device.uid}_{suffix}_{dp_id}"].config["entity_category"] = "diagnostic"
        add_enum_sensor(
            device,
            "9",
            "lock_alarm",
            "Último alerta",
            "{{ {'Tamper': 'Violação detectada', 'Deadbolt_Jammed': "
            "'Trava emperrada', 'Master_Code_Change': 'Código mestre alterado'}.get(value, value) }}",
        )
        device.entities[f"x5_{device.uid}_lock_alarm_9"].config["entity_category"] = "diagnostic"
        return True

    # HOBEIAN/Moes ZG-223Z. Zigbee2MQTT exposes: rainwater, illuminance,
    # sensitivity, illuminance_sampling and battery. Tuya LAN reports the
    # rainwater enum as "none"/"presence" on some firmwares.
    if "zg_223z" in slugify(descriptor) or "rainwater_detection_sensor" in slugify(descriptor):
        add_enum_sensor(
            device,
            "1",
            "rainwater",
            "Chuva",
            "{{ {'none': 'Sem chuva', 'presence': 'Chuva detectada'}.get(value | lower, value) }}",
        )
        add_moisture_entity(device, "1", "Chuva detectada")
        add_number_sensor(device, "102", "illuminance", "Iluminância", "illuminance", "lx", "measurement", "illuminance")
        add_number_sensor(device, "2", "sensitivity", "Sensibilidade", None, None, "measurement", "number")
        add_number_sensor(device, "101", "illuminance_sampling", "Intervalo de iluminância", None, "min", "measurement", "number")
        add_number_sensor(device, "104", "battery", "Bateria", "battery", "%", "measurement", "battery")
        add_number_control_entity(device, "2", "Sensibilidade", {"min": 0, "max": 9, "step": 1})
        add_number_control_entity(device, "101", "Intervalo de iluminância", {"min": 1, "max": 480, "step": 1}, "min")
        return True

    return False


def apply_cover_profile(device: BridgeDevice) -> bool:
    descriptor = descriptor_for(device)
    looks_like_cover = device.category.lower() in {"cl", "clkg"} or any(
        token in descriptor
        for token in ("curtain", "cortina", "blind", "shade", "roller shutter")
    )
    if not looks_like_cover:
        return False

    mapping = device.mapping
    control_dp = dp_for_codes(mapping, {"control", "curtain_control", "mach_operate"})
    position_control_dp = dp_for_codes(
        mapping,
        {"percent_control", "position_control", "position_set", "target_position"},
    )
    position_state_dp = dp_for_codes(
        mapping,
        {"percent_state", "position", "position_state", "current_position"},
    )
    work_state_dp = dp_for_codes(
        mapping,
        {"work_state", "curtain_state", "motor_state"},
    )

    if not control_dp:
        for dp_id, value in device.last_dps.items():
            if str(value).lower() in {"open", "close", "stop", "continue"}:
                control_dp = str(dp_id)
                break
    if not position_control_dp and "2" in device.last_dps:
        position_control_dp = "2"
    if not position_state_dp and "3" in device.last_dps:
        position_state_dp = "3"
    if not work_state_dp:
        for dp_id, value in device.last_dps.items():
            if str(value).lower() in {"opening", "closing"}:
                work_state_dp = str(dp_id)
                break

    if not control_dp:
        return False
    add_cover_entity(
        device,
        control_dp,
        position_control_dp,
        position_state_dp,
        work_state_dp,
    )
    return True


def infer_entities(device: BridgeDevice) -> None:
    add_last_event_entity(device)
    add_connectivity_entity(device)
    mapping = device.mapping or {}
    category = device.category.lower()
    descriptor = descriptor_for(device)
    looks_like_moisture = any(
        token in descriptor
        for token in ("rain", "water", "flood", "leak", "moisture", "chuva", "agua", "vazamento")
    )
    looks_like_rain = any(token in descriptor for token in ("rain", "chuva"))
    apply_known_product_profile(device)
    apply_cover_profile(device)

    for dp_id in sorted(mapping.keys(), key=lambda item: int(item) if str(item).isdigit() else str(item)):
        dp_id = str(dp_id)
        code = mapping_code(mapping, dp_id)
        dp_type = mapping_type(mapping, dp_id)
        values = mapping_values(mapping, dp_id)
        unit = str(values.get("unit") or "")

        if dp_id in device.cover_dp_ids:
            continue
        if category == "mcs" and code in {"switch", "doorcontact_state", "door_state", "contact"}:
            add_contact_entity(device, dp_id)
        elif code in {"pir", "presence", "motion_state", "motion", "occupancy"}:
            add_motion_entity(device, dp_id)
        elif any(token in code for token in ("water", "flood", "leak", "rain", "moisture")):
            add_moisture_entity(
                device,
                dp_id,
                "Chuva detectada" if "rain" in code or looks_like_rain else "Água detectada",
            )
        elif looks_like_moisture and (
            code in {"switch", "alarm", "state", "status"} or dp_type in {"boolean", "enum"}
        ):
            add_moisture_entity(device, dp_id, "Chuva detectada" if looks_like_rain else "Água detectada")
        elif "battery_percentage" in code:
            add_number_sensor(device, dp_id, "battery", "Bateria", "battery", "%", "measurement", "battery")
        elif code in {"battery", "residual_electricity"} or code.endswith("_battery"):
            # Tuya frequently omits unit/max metadata for percentage battery
            # DPs. Treat numeric battery readings consistently in Home Assistant.
            add_number_sensor(device, dp_id, "battery", "Bateria", "battery", "%", "measurement", "battery")
        elif "temperature" in code or code in {"temp_current", "va_temperature"}:
            add_number_sensor(device, dp_id, "temperature", "Temperatura", "temperature", unit or "C", "measurement", "temperature")
        elif "humidity" in code or code in {"va_humidity"}:
            add_number_sensor(device, dp_id, "humidity", "Umidade", "humidity", unit or "%", "measurement", "humidity")
        elif code.startswith("switch") and dp_type == "boolean":
            name = "Interruptor" if code == "switch" else code.replace("_", " ").title()
            add_switch_entity(device, dp_id, name)
        add_control_entity_from_mapping(device, dp_id)

    if category in {"kg", "cz", "tdq", "pc"} and not any(spec.kind == "switch" for spec in device.entities.values()):
        candidate_dp = "1" if "1" in mapping else "101" if "101" in mapping else ""
        if candidate_dp:
            add_switch_entity(device, candidate_dp, "Interruptor")


def publish_device_discovery(client: mqtt.Client, device: BridgeDevice) -> None:
    infer_entities(device)
    for topic in device.obsolete_discovery_topics:
        client.publish(topic, "", qos=1, retain=True)
    device.obsolete_discovery_topics.clear()
    current_device_payload = device.ha_device
    for spec in device.entities.values():
        spec.config["device"] = current_device_payload
        if spec.kind != "connectivity":
            spec.config.pop("availability_topic", None)
            spec.config.pop("payload_available", None)
            spec.config.pop("payload_not_available", None)
            spec.config["availability"] = [
                {
                    "topic": AVAILABILITY_TOPIC,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
                {
                    "topic": device.availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
            ]
            spec.config["availability_mode"] = "all"
        publish_config(client, spec)


def publish_inventory(client: mqtt.Client) -> None:
    payload = {
        "count": len(REGISTRY.by_id),
        "updated_at": now_iso(),
        "devices": [
            {
                "name": item.name,
                "custom_name": item.custom_name,
                "id": item.id,
                "node_id": item.node_id,
                "category": item.category,
                "product_name": item.product_name,
                "product_id": item.product_id,
                "mapping_dps": sorted(item.mapping.keys()),
                "writable_dps": sorted(
                    dp_id for dp_id in item.mapping.keys() if mapping_writable(item.mapping, dp_id)
                ),
            }
            for item in REGISTRY.all()
        ],
    }
    client.publish(INVENTORY_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)


def publish_all_discovery(client: mqtt.Client) -> None:
    publish_gateway_discovery(client)
    for device in REGISTRY.all():
        publish_device_discovery(client, device)
    publish_inventory(client)


def mqtt_client() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="x5-bridge",
        clean_session=True,
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.will_set(AVAILABILITY_TOPIC, payload="offline", qos=1, retain=True)

    def on_connect(client_obj, userdata, flags, reason_code, properties):
        del userdata, flags, properties
        if reason_code != 0:
            LOG.error("MQTT recusou a conexao: %s", reason_code)
            return
        LOG.info("Conectado ao MQTT em %s:%s", MQTT_HOST, MQTT_PORT)
        publish_all_discovery(client_obj)
        for device in REGISTRY.all():
            if device.online is not None:
                publish_device_availability(
                    client_obj,
                    device,
                    device.online,
                    force=True,
                )
        refresh_availability(client_obj)
        client_obj.subscribe(f"{TOPIC_PREFIX}/devices/+/set/+")

    def on_message(client_obj, userdata, message):
        del userdata
        handle_command(client_obj, message.topic, message.payload.decode("utf-8", errors="replace"))

    def on_disconnect(client_obj, userdata, disconnect_flags, reason_code, properties):
        del client_obj, userdata, disconnect_flags, properties
        if not STOP:
            LOG.warning("MQTT desconectado: %s", reason_code)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


def cloud_enabled() -> bool:
    return bool(AUTO_SYNC and TUYA_ACCESS_ID and TUYA_ACCESS_SECRET and TUYA_CLOUD_DEVICE_ID)


def enrich_cloud_device_name(cloud: tinytuya.Cloud, meta: dict[str, Any]) -> None:
    device_id = clean_text(meta.get("id"))
    if not device_id:
        return
    response = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}")
    if not isinstance(response, dict):
        raise RuntimeError(f"Resposta inesperada: {type(response).__name__}")
    if response.get("success") is False or "Error" in response:
        message = response.get("msg") or response.get("Error") or response.get("code") or "erro desconhecido"
        raise RuntimeError(str(message))
    details = response.get("result")
    if not isinstance(details, dict):
        raise RuntimeError("Resposta sem detalhes do dispositivo")

    # The current Tuya API separates the app-assigned name from the default
    # product name. Preserve an explicit empty custom_name so a rename removal
    # also propagates back to Home Assistant.
    if "custom_name" in details:
        meta["custom_name"] = clean_text(details.get("custom_name"))
    if not clean_text(meta.get("name")) and clean_text(details.get("name")):
        meta["name"] = clean_text(details.get("name"))
    for key in ("product_name", "model", "category"):
        if not clean_text(meta.get(key)) and clean_text(details.get(key)):
            meta[key] = clean_text(details.get(key))


def fetch_cloud_devices() -> list[dict[str, Any]]:
    cloud = tinytuya.Cloud(
        apiRegion=TUYA_REGION,
        apiKey=TUYA_ACCESS_ID,
        apiSecret=TUYA_ACCESS_SECRET,
        apiDeviceID=TUYA_CLOUD_DEVICE_ID,
    )
    result = cloud.getdevices(include_map=True)
    if isinstance(result, dict) and "Error" in result:
        raise RuntimeError(result.get("Error", "Erro desconhecido da Tuya Cloud"))
    if not isinstance(result, list):
        raise RuntimeError(f"Resposta inesperada da Tuya Cloud: {type(result).__name__}")
    for meta in result:
        if not isinstance(meta, dict) or not meta.get("id") or meta.get("id") == X5_DEVICE_ID:
            continue
        try:
            enrich_cloud_device_name(cloud, meta)
        except Exception as exc:
            LOG.warning(
                "Nao consegui obter o nome personalizado Tuya de %s: %s",
                meta.get("name") or meta.get("id"),
                exc,
            )
        try:
            spec = cloud.getdps(str(meta["id"]))
            enhanced = enhanced_mapping_from_spec(spec if isinstance(spec, dict) else {})
            if enhanced:
                base = meta.get("mapping") if isinstance(meta.get("mapping"), dict) else {}
                meta["mapping"] = merge_mapping(base, enhanced)
        except Exception as exc:
            LOG.warning("Nao consegui obter especificacao Tuya de %s: %s", meta.get("name") or meta.get("id"), exc)
    return result


def should_import_cloud_device(meta: dict[str, Any]) -> bool:
    if meta.get("id") == X5_DEVICE_ID:
        return False
    if not meta.get("sub") or not meta.get("node_id"):
        return False
    parent = str(meta.get("parent") or meta.get("gateway_id") or "")
    if parent and parent != X5_DEVICE_ID:
        return False
    return True


def import_cloud_inventory(devices: list[dict[str, Any]], client: mqtt.Client | None = None) -> None:
    imported = 0
    for meta in devices:
        if not should_import_cloud_device(meta):
            continue
        device = REGISTRY.add_or_update(meta)
        if device:
            imported += 1
            if client:
                publish_device_discovery(client, device)
    LOG.info("Inventario sincronizado: %s subdispositivo(s)", imported)
    if client:
        publish_inventory(client)


def sync_inventory(client: mqtt.Client | None = None) -> None:
    if not cloud_enabled():
        if AUTO_SYNC:
            LOG.warning("Auto sync habilitado, mas credenciais Tuya Cloud estao incompletas")
        if client:
            publish_all_discovery(client)
        return

    LOG.info("Sincronizando inventario com a Tuya Cloud...")
    import_cloud_inventory(fetch_cloud_devices(), client)


def start_inventory_sync() -> None:
    global SYNC_THREAD
    if not cloud_enabled():
        return
    with SYNC_THREAD_LOCK:
        if SYNC_THREAD and SYNC_THREAD.is_alive():
            return

        def worker() -> None:
            try:
                devices = fetch_cloud_devices()
                SYNC_RESULT_QUEUE.put((devices, None))
            except Exception as exc:
                SYNC_RESULT_QUEUE.put((None, exc))

        LOG.info("Sincronizando inventario com a Tuya Cloud...")
        SYNC_THREAD = threading.Thread(
            target=worker,
            name="x5-cloud-sync",
            daemon=True,
        )
        SYNC_THREAD.start()


def process_inventory_sync_results(client: mqtt.Client) -> None:
    while True:
        try:
            devices, error = SYNC_RESULT_QUEUE.get_nowait()
        except queue.Empty:
            return
        if error:
            LOG.error("Falha ao sincronizar inventario: %s", error)
            continue
        import_cloud_inventory(devices or [], client)


def build_gateway() -> tinytuya.Device:
    LOG.info("Criando conexao com o X5 em %s...", X5_IP)
    gateway = tinytuya.Device(
        dev_id=X5_DEVICE_ID,
        address=X5_IP,
        local_key=X5_LOCAL_KEY,
        persist=True,
        version=X5_VERSION,
    )
    gateway.set_socketPersistent(True)
    gateway.set_socketTimeout(HEARTBEAT_TIMEOUT_SECONDS)
    gateway.set_socketRetryLimit(1)
    gateway.set_socketRetryDelay(0.25)
    REGISTRY.attach_gateway(gateway)
    return gateway


def publish_entity_state(client: mqtt.Client, device: BridgeDevice, dp_id: str, value: Any) -> None:
    for spec in device.entities.values():
        if spec.dp_id != dp_id:
            continue
        if spec.kind == "lock":
            normalized = normalize_on_off(value)
            if normalized is not None:
                state = "UNLOCKED" if normalized == "ON" else "LOCKED"
                client.publish(spec.config["state_topic"], state, qos=1, retain=spec.retain_state)
        elif spec.kind in {"contact", "motion", "switch"}:
            normalized = normalize_on_off(value)
            if normalized is not None:
                client.publish(spec.config["state_topic"], normalized, qos=1, retain=spec.retain_state)
        elif spec.kind == "moisture":
            normalized = normalize_alarm(value)
            if normalized is not None:
                client.publish(spec.config["state_topic"], normalized, qos=1, retain=spec.retain_state)
        elif spec.kind == "enum":
            client.publish(spec.config["state_topic"], str(value), qos=1, retain=spec.retain_state)
        elif spec.kind == "select":
            client.publish(spec.config["state_topic"], str(value), qos=1, retain=spec.retain_state)
        elif spec.kind == "text":
            client.publish(spec.config["state_topic"], str(value), qos=1, retain=spec.retain_state)
        elif spec.kind in {"battery", "temperature", "humidity", "illuminance", "number", "number_control"}:
            number = normalize_number(value, mapping_scale(device.mapping, dp_id))
            if number is not None:
                client.publish(spec.config["state_topic"], str(number), qos=1, retain=spec.retain_state)
        elif spec.kind == "raw":
            client.publish(spec.config["state_topic"], json.dumps(value, ensure_ascii=False), qos=1, retain=spec.retain_state)


def normalize_cover_state(value: Any) -> str | None:
    normalized = str(value).strip().lower()
    return {
        "open": "opening",
        "opening": "opening",
        "close": "closing",
        "closing": "closing",
        "stop": "stopped",
        "stopped": "stopped",
        "fully_open": "open",
        "opened": "open",
        "fully_closed": "closed",
        "closed": "closed",
    }.get(normalized)


def publish_cover_state(client: mqtt.Client, device: BridgeDevice, dp_id: str, value: Any) -> None:
    if not device.cover_control_dp:
        return
    state_topic = f"{device.topic_base}/cover/state"
    if dp_id in {device.cover_position_control_dp, device.cover_position_state_dp}:
        position = normalize_number(value, mapping_scale(device.mapping, dp_id))
        if position is not None:
            position = max(0, min(100, position))
            client.publish(
                f"{device.topic_base}/cover/position",
                str(position),
                qos=1,
                retain=True,
            )
            if position == 0:
                client.publish(state_topic, "closed", qos=1, retain=True)
            elif position == 100:
                client.publish(state_topic, "open", qos=1, retain=True)
        return

    if dp_id not in {device.cover_control_dp, device.cover_work_state_dp}:
        return
    state = normalize_cover_state(value)
    if state:
        client.publish(state_topic, state, qos=1, retain=True)


def publish_dps(client: mqtt.Client, device: BridgeDevice, dps: dict[str, Any]) -> None:
    if not dps:
        return
    device.last_dps.update(dps)
    infer_entities(device)
    for dp_id, value in dps.items():
        dp_id = str(dp_id)
        client.publish(dp_state_topic(device, dp_id), json.dumps(value, ensure_ascii=False), qos=1, retain=True)
        if dp_id not in device.cover_dp_ids:
            add_raw_entity(device, dp_id, value)
    publish_device_discovery(client, device)
    for dp_id, value in dps.items():
        dp_id = str(dp_id)
        publish_entity_state(client, device, dp_id, value)
        publish_cover_state(client, device, dp_id, value)


def publish_event(client: mqtt.Client, data: dict[str, Any]) -> None:
    publish_availability(client, "online")
    device = REGISTRY.find_for_event(data)
    event = json_safe(data)
    event["received_at"] = now_iso()
    if device:
        publish_device_availability(client, device, True)
        event["source"] = device.slug
        event["device_id"] = device.id
        event["node_id"] = device.node_id
        client.publish(f"{device.topic_base}/event", json.dumps(event, ensure_ascii=False), qos=1, retain=True)
        publish_dps(client, device, extract_dps(event))
        LOG.info("Evento de %s: %s", device.name, event)
    else:
        event["source"] = "gateway"
        LOG.debug("Evento do gateway: %s", event)
        if extract_cid(event):
            start_inventory_sync()
    client.publish(ALL_EVENTS_TOPIC, json.dumps(event, ensure_ascii=False), qos=0, retain=False)


def confirm_gateway_online(client: mqtt.Client, gateway: tinytuya.Device) -> None:
    gateway.set_socketTimeout(HEARTBEAT_TIMEOUT_SECONDS)
    try:
        result = gateway.heartbeat(nowait=False)
    finally:
        gateway.set_socketTimeout(LISTEN_POLL_SECONDS)
    error = tuya_error_message(result)
    if error:
        raise ConnectionError(f"heartbeat do X5 falhou: {error}")
    publish_availability(client, "online")
    if isinstance(result, dict) and (extract_cid(result) or extract_dps(result)):
        publish_event(client, result)


def publish_device_availability(
    client: mqtt.Client,
    device: BridgeDevice,
    online: bool,
    force: bool = False,
) -> None:
    if not force and device.online is online:
        return
    device.online = online
    client.publish(
        device.availability_topic,
        "online" if online else "offline",
        qos=1,
        retain=True,
    )


def apply_subdevice_status(client: mqtt.Client, response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    online = {str(item) for item in data.get("online", [])}
    offline = {str(item) for item in data.get("offline", [])}
    if not online and not offline:
        return False
    for device in REGISTRY.all():
        if device.node_id in online:
            publish_device_availability(client, device, True, force=True)
        elif device.node_id in offline:
            publish_device_availability(client, device, False, force=True)
    return True


def query_subdevice_status(
    client: mqtt.Client,
    gateway: tinytuya.Device,
) -> bool:
    gateway.set_socketTimeout(SUBDEVICE_STATUS_TIMEOUT_SECONDS)
    try:
        response = gateway.subdev_query()
    finally:
        gateway.set_socketTimeout(LISTEN_POLL_SECONDS)
    error = tuya_error_message(response)
    if error:
        raise ConnectionError(f"consulta de disponibilidade Zigbee falhou: {error}")
    if not apply_subdevice_status(client, response):
        LOG.warning("Resposta de disponibilidade Zigbee inesperada: %s", json_safe(response))
        return False
    LOG.debug("Disponibilidade Zigbee atualizada: %s", json_safe(response))
    return True


def command_spec_for(device: BridgeDevice, dp_id: str) -> EntitySpec | None:
    for spec in device.entities.values():
        if spec.dp_id == dp_id and spec.config.get("command_topic"):
            return spec
    return None


def parse_command_payload(device: BridgeDevice, spec: EntitySpec | None, dp_id: str, payload: str) -> Any:
    value = payload.strip()
    if spec and spec.kind == "cover":
        normalized = value.lower()
        if normalized not in {"open", "close", "stop", "continue"}:
            raise ValueError("cortina espera open, close ou stop")
        return normalized

    if dp_id == device.cover_position_control_dp:
        try:
            position = float(value)
        except ValueError as exc:
            raise ValueError("posicao da cortina deve ser numerica") from exc
        if not 0 <= position <= 100:
            raise ValueError("posicao da cortina deve estar entre 0 e 100")
        return int(position) if position.is_integer() else position

    if spec and spec.kind == "lock":
        normalized = value.upper()
        if normalized in {"UNLOCK", "UNLOCKED", "ON", "TRUE"}:
            return True
        if normalized in {"LOCK", "LOCKED", "OFF", "FALSE"}:
            return False
        raise ValueError("fechadura espera lock, unlock, true ou false")

    if spec and spec.kind == "switch":
        if value.upper() == "ON":
            return True
        if value.upper() == "OFF":
            return False
        parsed = json.loads(value)
        if not isinstance(parsed, bool):
            raise ValueError("switch espera ON, OFF, true ou false")
        return parsed

    if spec and spec.kind == "number_control":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = float(value)
        if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
            raise ValueError("number espera valor numerico")
        if "min" in spec.config and parsed < spec.config["min"]:
            raise ValueError(f"valor abaixo do minimo {spec.config['min']}")
        if "max" in spec.config and parsed > spec.config["max"]:
            raise ValueError(f"valor acima do maximo {spec.config['max']}")
        return unscale_value(parsed, mapping_scale(device.mapping, spec.dp_id))

    if spec and spec.kind == "select":
        options = [str(item) for item in spec.config.get("options", [])]
        if options and value not in options:
            raise ValueError(f"opcao invalida {value!r}; opcoes: {', '.join(options)}")
        return value

    if spec and spec.kind == "text":
        return value

    if value.upper() == "ON":
        return True
    if value.upper() == "OFF":
        return False
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def handle_command(client: mqtt.Client, topic: str, payload: str) -> None:
    parts = topic.split("/")
    if len(parts) < 5:
        return
    uid = parts[-3]
    dp_id = parts[-1]
    device = next((item for item in REGISTRY.by_id.values() if item.uid == uid), None)
    if not device or not device.local:
        LOG.warning("Comando ignorado; dispositivo nao encontrado para topico %s", topic)
        return
    spec = command_spec_for(device, dp_id)
    try:
        new_value = parse_command_payload(device, spec, dp_id, payload)
    except Exception as exc:
        LOG.warning("Comando invalido para %s DP %s: %s", device.name, dp_id, exc)
        return
    COMMAND_QUEUE.put((device.uid, dp_id, new_value))
    LOG.debug("Comando enfileirado para %s DP %s = %r", device.name, dp_id, new_value)


def process_pending_commands(client: mqtt.Client, limit: int = 1) -> None:
    for _ in range(limit):
        try:
            uid, dp_id, new_value = COMMAND_QUEUE.get_nowait()
        except queue.Empty:
            return
        device = next((item for item in REGISTRY.by_id.values() if item.uid == uid), None)
        if not device or not device.local:
            LOG.warning("Comando descartado; dispositivo %s nao esta conectado", uid)
            continue
        try:
            result = device.local.set_value(dp_id, new_value, nowait=True)
            error = tuya_error_message(result)
            if error:
                raise ConnectionError(error)
            device.last_dps[dp_id] = new_value
            client.publish(
                dp_state_topic(device, dp_id),
                json.dumps(new_value, ensure_ascii=False),
                qos=1,
                retain=True,
            )
            publish_entity_state(client, device, dp_id, new_value)
            publish_cover_state(client, device, dp_id, new_value)
            LOG.info("Comando enviado para %s DP %s = %r", device.name, dp_id, new_value)
        except Exception:
            LOG.exception("Falha ao enviar comando para %s DP %s", device.name, dp_id)


def query_initial_status(client: mqtt.Client, device: BridgeDevice) -> bool:
    if not device.local:
        return False
    try:
        initial = device.local.status()
        error = tuya_error_message(initial)
        if error:
            LOG.warning("Consulta inicial de %s falhou: %s", device.name, error)
            return False
        LOG.info("Status inicial de %s: %s", device.name, json_safe(initial))
        publish_dps(client, device, extract_dps(initial))
        return True
    except Exception as exc:
        LOG.warning("Consulta inicial de %s nao respondeu; continuarei escutando: %s", device.name, exc)
        return False


def listen_forever(client: mqtt.Client) -> None:
    next_sync = time.monotonic() + (SYNC_INTERVAL_MINUTES * 60)
    while not STOP:
        gateway = None
        try:
            gateway = build_gateway()
            publish_all_discovery(client)
            confirm_gateway_online(client, gateway)
            gateway.set_socketTimeout(2.0)
            for device in REGISTRY.all():
                query_initial_status(client, device)
            query_subdevice_status(client, gateway)
            gateway.set_socketTimeout(LISTEN_POLL_SECONDS)
            heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
            subdevice_status_at = time.monotonic() + SUBDEVICE_STATUS_INTERVAL_SECONDS
            while not STOP:
                process_pending_commands(client)
                process_inventory_sync_results(client)
                current = time.monotonic()
                if current >= next_sync:
                    start_inventory_sync()
                    next_sync = current + (SYNC_INTERVAL_MINUTES * 60)
                if current >= heartbeat_at:
                    confirm_gateway_online(client, gateway)
                    heartbeat_at = current + HEARTBEAT_INTERVAL_SECONDS
                if current >= subdevice_status_at:
                    query_subdevice_status(client, gateway)
                    subdevice_status_at = current + SUBDEVICE_STATUS_INTERVAL_SECONDS
                data = gateway.receive()
                if isinstance(data, dict):
                    error = tuya_error_message(data)
                    if error:
                        raise ConnectionError(error)
                    publish_event(client, data)
        except (OSError, socket.error, ValueError, KeyError, ConnectionError) as exc:
            LOG.warning("Conexao com o X5 interrompida: %s", exc)
        except Exception:
            LOG.exception("Erro inesperado no bridge")
        finally:
            publish_availability(client, "offline", force=True)
            if gateway is not None:
                try:
                    gateway.close()
                except Exception:
                    pass
        if not STOP:
            LOG.info("Nova tentativa em 5 segundos...")
            time.sleep(5)


def validate() -> None:
    required = {
        "X5_IP": X5_IP,
        "X5_DEVICE_ID": X5_DEVICE_ID,
        "X5_LOCAL_KEY": X5_LOCAL_KEY,
        "MQTT_HOST": MQTT_HOST,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Configuracoes obrigatorias ausentes: " + ", ".join(missing))
    if AUTO_SYNC and not cloud_enabled():
        raise RuntimeError(
            "Auto sync requer tuya_region, tuya_access_id, tuya_access_secret e tuya_cloud_device_id"
        )


def main() -> int:
    validate()
    if DEBUG:
        tinytuya.set_debug(True, color=False)
    try:
        sync_inventory(None)
    except Exception:
        LOG.exception("Falha na sincronizacao inicial; continuarei com os dispositivos ja configurados")
    client = mqtt_client()
    try:
        listen_forever(client)
    finally:
        try:
            publish_availability(client, "offline", force=True)
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
