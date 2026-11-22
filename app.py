from __future__ import annotations

import base64
import json
import logging
import math
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterator, Literal
from urllib.parse import quote

import boto3
import cv2
import numpy as np
import paho.mqtt.client as mqtt
import psycopg
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import BaseModel, Field, ValidationError
from ultralytics import YOLO
from unknown_matching import ObjectSignature, describe_object, match_objects


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "best.pt"
PAGE_PATH = ROOT / "web" / "index.html"
SNAPSHOT_ROOT = Path(
    os.getenv("SNAPSHOT_DIR", str(ROOT / "data" / "snapshots"))
).resolve()
BACKGROUND_REFERENCE_PATH = Path(
    os.getenv(
        "BACKGROUND_REFERENCE_PATH",
        str(ROOT / "data" / "background_reference.jpg"),
    )
).resolve()
SNAPSHOT_BUCKET = os.getenv("SNAPSHOT_BUCKET", "").strip()
IMAGE_STORAGE = os.getenv("IMAGE_STORAGE", "filesystem").strip().lower()
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    database_user = quote(os.getenv("DB_USER", "iot_user"), safe="")
    database_password = quote(os.getenv("DB_PASSWORD", "iot_password"), safe="")
    database_host = os.getenv("DB_HOST", "localhost")
    database_port = os.getenv("DB_PORT", "5432")
    database_name = quote(os.getenv("DB_NAME", "iot_dashboard"), safe="")
    DATABASE_URL = (
        f"postgresql://{database_user}:{database_password}"
        f"@{database_host}:{database_port}/{database_name}"
    )
CLASS_NAMES = (
    "Relay module",
    "Triple syringe",
    "Ultrasonic HC-SR04",
    "Unknown",
)
UNKNOWN_LABEL = "Unknown"
MODEL_CLASS_LABELS = {
    "relay_module": "Relay module",
    "relay module": "Relay module",
    "triple_syringe": "Triple syringe",
    "triple syringe": "Triple syringe",
    "ultrasonic_hc-sr04": "Ultrasonic HC-SR04",
    "ultrasonic hc-sr04": "Ultrasonic HC-SR04",
    "unknown": UNKNOWN_LABEL,
}
ITEM_TO_YOLO_CLASS = {
    "relay": "relay_module",
    "relay_module": "relay_module",
    "triple_syringe": "triple_syringe",
    "ultrasonic": "ultrasonic_hc-sr04",
    "ultrasonic_hc-sr04": "ultrasonic_hc-sr04",
    "unknown": "unknown",
}
SENSOR_TIMEOUT_SECONDS = float(os.getenv("SENSOR_TIMEOUT_SECONDS", "10"))
REARM_SECONDS = float(os.getenv("REARM_SECONDS", "10"))
ARDUINO_API_KEY = os.getenv("ARDUINO_API_KEY", "").strip()
MQTT_HOST = os.getenv("MQTT_HOST", "broker.hivemq.com").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "8884"))
MQTT_TRANSPORT = os.getenv(
    "MQTT_TRANSPORT", "websockets" if MQTT_PORT == 8884 else "tcp"
).strip().lower()
MQTT_TLS = os.getenv(
    "MQTT_TLS", "1" if MQTT_PORT in (8883, 8884) else "0"
).strip().lower() in {"1", "true", "yes", "on"}
MQTT_WS_PATH = os.getenv("MQTT_WS_PATH", "/mqtt").strip()
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_QUEUE_TOPIC = os.getenv("MQTT_QUEUE_TOPIC", "armbot-010eb21b/cmd/queue").strip()
MQTT_EVENT_TOPIC = os.getenv("MQTT_EVENT_TOPIC", "armbot-010eb21b/event").strip()
MQTT_STATUS_TOPIC = os.getenv("MQTT_STATUS_TOPIC", "armbot-010eb21b/status").strip()
MQTT_ARMBOT_PREFIX = MQTT_QUEUE_TOPIC.rsplit("/cmd/", 1)[0]
MQTT_RESULT_TOPIC = (
    os.getenv("MQTT_RESULT_TOPIC", "").strip() or f"{MQTT_ARMBOT_PREFIX}/inspection/result"
)
MQTT_ARMBOT_EVENT_TOPIC = f"{MQTT_ARMBOT_PREFIX}/event"
MQTT_UNKNOWN_TOPIC = f"{MQTT_ARMBOT_PREFIX}/unknown"
MQTT_UNKNOWN_IMAGE_TOPIC = f"{MQTT_UNKNOWN_TOPIC}/image"
UNKNOWN_IMAGE_MAX_BYTES = 1024 * 1024
UNKNOWN_IMAGE_PAIR_SECONDS = 5
LOGGER = logging.getLogger(__name__)


class CameraRequest(BaseModel):
    index: int = Field(default=0, ge=0, le=10)
    confidence: float = Field(default=0.5, ge=0, le=1)
    iou: float = Field(default=0.25, ge=0, le=1)
    mode: Literal["browser", "local"] = "browser"
    trigger_id: str | None = Field(default=None, max_length=100)
    record_events: bool = True
    receive_job_id: str | None = Field(default=None, max_length=100)


class IRSensorRequest(BaseModel):
    object_present: bool


class CaptureAckRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)


class CancelQueueRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200, pattern=r"\S")


class ReceivingItem(BaseModel):
    equipment: Literal[
        "relay_module", "triple_syringe", "ultrasonic_hc-sr04", "unknown"
    ] = Field(alias="class")
    quantity: int = Field(ge=1, le=100)


class ReceivingRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    items: list[ReceivingItem]


class EventStore:
    def __init__(
        self,
        database_url: str,
        snapshot_root: Path,
        snapshot_bucket: str = "",
        aws_region: str | None = None,
    ) -> None:
        self.database_url = database_url
        self.snapshot_root = snapshot_root
        self.snapshot_bucket = snapshot_bucket
        self.database_images = IMAGE_STORAGE == "database"
        self.history_revision = uuid.uuid4().hex
        self._s3 = (
            boto3.client("s3", region_name=aws_region) if snapshot_bucket else None
        )
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS device_events (
                    id BIGSERIAL PRIMARY KEY,
                    detected_at TIMESTAMPTZ NOT NULL,
                    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
                    total INTEGER NOT NULL DEFAULT 0,
                    image_path TEXT NOT NULL,
                    image_data BYTEA,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute(
                """
                ALTER TABLE device_events
                ADD COLUMN IF NOT EXISTS image_data BYTEA
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS device_events_detected_at_idx
                ON device_events (detected_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receiving_history (
                    id BIGSERIAL PRIMARY KEY,
                    job_id TEXT NOT NULL CHECK (job_id <> ''),
                    armbot_job_id TEXT,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status TEXT NOT NULL,
                    requested JSONB NOT NULL,
                    detected JSONB NOT NULL,
                    message TEXT NOT NULL,
                    event_id BIGINT UNIQUE REFERENCES device_events(id) ON DELETE SET NULL
                )
                """
            )
            connection.execute(
                """
                ALTER TABLE receiving_history
                ADD COLUMN IF NOT EXISTS mqtt_result_pending BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS receiving_mqtt_pending_idx
                ON receiving_history (id)
                WHERE mqtt_result_pending AND event_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receiving_pick_images (
                    id BIGSERIAL PRIMARY KEY,
                    queue_id BIGINT NOT NULL REFERENCES receiving_history(id),
                    captured_at DOUBLE PRECISION NOT NULL,
                    image_data BYTEA NOT NULL,
                    UNIQUE (queue_id, captured_at)
                )
                """
            )
            connection.execute(
                """
                UPDATE receiving_history
                SET status = 'interrupted', message = 'ระบบเริ่มใหม่ก่อนจบการตรวจ',
                    updated_at = NOW()
                WHERE status IN ('waiting_ir', 'in_progress')
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sensor_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sensor_state (id, payload)
                VALUES (1, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (Json({"armed": True}),),
            )
            connection.commit()

    def load_sensor_state(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM sensor_state WHERE id = 1"
            ).fetchone()
        return dict(row["payload"] or {}) if row is not None else {}

    def save_sensor_state(self, payload: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE sensor_state
                SET payload = %s, updated_at = NOW()
                WHERE id = 1
                """,
                (Json(payload),),
            )
            connection.commit()

    @staticmethod
    def _payload(row: dict[str, Any]) -> dict[str, Any]:
        detected_at = row["detected_at"]
        if isinstance(detected_at, datetime):
            detected_at = detected_at.isoformat()
        return {
            "id": row["id"],
            "detected_at": detected_at,
            "labels": row["labels"] or {},
            "total": row["total"],
            "image_url": f"/api/events/{row['id']}/image",
        }

    def create(
        self, image: bytes, labels: dict[str, int], total: int,
        *, queue_id: int | None = None,
    ) -> dict[str, Any]:
        event_key = uuid.uuid4().hex
        relative_path = f"{event_key}.jpg"
        if not self.database_images:
            self._store_image(relative_path, image)
        detected_at = datetime.now(timezone.utc)
        try:
            with self.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO device_events
                        (detected_at, labels, total, image_path, image_data)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, detected_at, labels, total, image_path
                    """,
                    (
                        detected_at,
                        Json(labels),
                        total,
                        relative_path,
                        image if self.database_images else None,
                    ),
                ).fetchone()
                if row is None:
                    raise RuntimeError("บันทึกเหตุการณ์ไม่สำเร็จ")
                if queue_id is not None:
                    updated = connection.execute(
                        """
                        UPDATE receiving_history
                        SET event_id = %s, detected = %s, status = 'completed',
                            message = 'รับของเรียบร้อย', updated_at = NOW(),
                            mqtt_result_pending = TRUE
                        WHERE id = %s AND status = 'in_progress'
                        """,
                        (row["id"], Json(labels), queue_id),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("ไม่พบคิวที่กำลังตรวจ")
                connection.commit()
        except Exception:
            if not self.database_images:
                self._delete_image(relative_path)
            raise
        if queue_id is not None:
            self.history_revision = uuid.uuid4().hex
        return self._payload(row)

    def pending_mqtt_result(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT q.id AS queue_id, q.job_id, q.armbot_job_id, q.event_id,
                       q.requested, q.detected, e.detected_at
                FROM receiving_history q
                JOIN device_events e ON e.id = q.event_id
                WHERE q.mqtt_result_pending AND q.event_id IS NOT NULL
                ORDER BY q.id
                LIMIT 1
                """
            ).fetchone()

    def acknowledge_mqtt_result(self, queue_id: int, event_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE receiving_history SET mqtt_result_pending = FALSE
                WHERE id = %s AND event_id = %s
                """,
                (queue_id, event_id),
            )

    def add_queue(self, job: dict[str, Any]) -> int:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO receiving_history
                    (job_id, armbot_job_id, status, requested, detected, message)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    job["job_id"], job.get("armbot_job_id"), job["status"],
                    Json(job["requested"]), Json(job["detected"]), job["message"],
                ),
            ).fetchone()
        self.history_revision = uuid.uuid4().hex
        return row["id"]

    def update_queue(self, job: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE receiving_history
                SET status = %s, requested = %s, detected = %s, message = %s,
                    armbot_job_id = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    job["status"], Json(job["requested"]), Json(job["detected"]),
                    job["message"], job.get("armbot_job_id"), job["history_id"],
                ),
            )
        self.history_revision = uuid.uuid4().hex

    def save_pick_images(self, queue_id: int, images: list[tuple[float, bytes]]) -> None:
        if not images:
            return
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO receiving_pick_images (queue_id, captured_at, image_data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (queue_id, captured_at) DO NOTHING
                    """,
                    [(queue_id, captured_at, image) for captured_at, image in images],
                )
                added = cursor.rowcount > 0
                if added:
                    connection.execute(
                        "UPDATE receiving_history SET updated_at = NOW() WHERE id = %s",
                        (queue_id,),
                    )
        if added:
            self.history_revision = uuid.uuid4().hex

    def list_queues(self, limit: int, before_id: int | None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT id, job_id, armbot_job_id, received_at, updated_at,
                       status, requested, detected, message
                FROM receiving_history
                WHERE (%s::bigint IS NULL OR id < %s)
                ORDER BY id DESC
                LIMIT %s
                """,
                (before_id, before_id, limit),
            ).fetchall()

    def get_queue(self, queue_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT q.*, e.detected_at
                FROM receiving_history q
                LEFT JOIN device_events e ON e.id = q.event_id
                WHERE q.id = %s
                """,
                (queue_id,),
            ).fetchone()
            if row is None:
                return None
            photos = connection.execute(
                "SELECT id FROM receiving_pick_images WHERE queue_id = %s ORDER BY captured_at",
                (queue_id,),
            ).fetchall()
        row["pick_images"] = [
            f"/api/receiving-history/{queue_id}/images/{photo['id']}" for photo in photos
        ]
        row["inspection_image"] = (
            f"/api/events/{row['event_id']}/image" if row["event_id"] is not None else None
        )
        return row

    def pick_image_bytes(self, queue_id: int, image_id: int) -> bytes | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT image_data FROM receiving_pick_images WHERE queue_id = %s AND id = %s",
                (queue_id, image_id),
            ).fetchone()
        return bytes(row["image_data"]) if row is not None else None

    def _store_image(self, relative_path: str, image: bytes) -> None:
        if self.database_images:
            return
        if self._s3 is not None:
            self._s3.put_object(
                Bucket=self.snapshot_bucket,
                Key=f"snapshots/{relative_path}",
                Body=image,
                ContentType="image/jpeg",
                ServerSideEncryption="AES256",
            )
            return
        (self.snapshot_root / relative_path).write_bytes(image)

    def _delete_image(self, relative_path: str) -> None:
        if self.database_images:
            return
        if self._s3 is not None:
            self._s3.delete_object(
                Bucket=self.snapshot_bucket,
                Key=f"snapshots/{relative_path}",
            )
            return
        (self.snapshot_root / relative_path).unlink(missing_ok=True)

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, detected_at, labels, total, image_path
                FROM device_events
                ORDER BY detected_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [self._payload(row) for row in rows]

    def get(self, event_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id, detected_at, labels, total, image_path
                FROM device_events
                WHERE id = %s
                """,
                (event_id,),
            ).fetchone()
        return self._payload(row) if row is not None else None

    def delete(self, event_id: int) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT image_path, image_data
                FROM device_events
                WHERE id = %s
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE receiving_history SET updated_at = NOW() WHERE event_id = %s",
                (event_id,),
            )
            connection.execute(
                "DELETE FROM device_events WHERE id = %s",
                (event_id,),
            )
            connection.commit()

        self.history_revision = uuid.uuid4().hex
        if row["image_data"] is None:
            self._delete_image(row["image_path"])
        return True

    def image_bytes(self, event_id: int) -> bytes | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT image_path, image_data FROM device_events WHERE id = %s",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        if row["image_data"] is not None:
            return bytes(row["image_data"])
        relative_path = row["image_path"]
        if self._s3 is not None:
            result = self._s3.get_object(
                Bucket=self.snapshot_bucket,
                Key=f"snapshots/{relative_path}",
            )
            return result["Body"].read()
        path = (self.snapshot_root / relative_path).resolve()
        if self.snapshot_root not in path.parents:
            raise RuntimeError("เส้นทางรูปภาพไม่ถูกต้อง")
        return path.read_bytes() if path.is_file() else None


def placeholder_frame(message: str) -> bytes:
    frame = np.full((360, 640, 3), 255, dtype=np.uint8)
    cv2.putText(
        frame,
        message,
        (35, 185),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (75, 85, 99),
        2,
        cv2.LINE_AA,
    )
    success, encoded = cv2.imencode(".jpg", frame)
    if not success:
        raise RuntimeError("Could not encode the camera placeholder image")
    return encoded.tobytes()


class MqttReceivingService:
    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store
        self._lock = threading.RLock()
        self._client: mqtt.Client | None = None
        self._job: dict[str, Any] | None = None
        self._unknown_image_info: tuple[float, dict[str, Any]] | None = None
        self._unknown_references: dict[
            tuple[str, float], ObjectSignature
        ] = {}
        self._pending_pick_images: dict[tuple[str, float], bytes] = {}
        self._last_messages: dict[str, dict[str, Any]] = {}
        self._test_event: threading.Event | None = None
        self._test_topics_seen: set[str] = set()
        self._mqtt_connected = False
        self._mqtt_error: str | None = None
        self._results_ready = threading.Event()
        self._results_stop = threading.Event()
        self._results_worker: threading.Thread | None = None

    def start(self) -> None:
        if not all(
            (
                MQTT_HOST,
                MQTT_QUEUE_TOPIC,
                MQTT_EVENT_TOPIC,
                MQTT_STATUS_TOPIC,
                MQTT_RESULT_TOPIC,
            )
        ):
            raise RuntimeError("MQTT host and topic settings must not be empty")
        if (
            MQTT_RESULT_TOPIC in (
                MQTT_QUEUE_TOPIC, MQTT_EVENT_TOPIC, MQTT_STATUS_TOPIC,
                MQTT_ARMBOT_EVENT_TOPIC, MQTT_UNKNOWN_TOPIC, MQTT_UNKNOWN_IMAGE_TOPIC,
            )
            or MQTT_RESULT_TOPIC.startswith(f"{MQTT_ARMBOT_PREFIX}/cmd/")
            or any(character in MQTT_RESULT_TOPIC for character in ("#", "+", "\0"))
        ):
            raise RuntimeError("MQTT_RESULT_TOPIC must be a separate result topic")
        if not 1 <= MQTT_PORT <= 65535:
            raise RuntimeError("MQTT_PORT must be between 1 and 65535")
        if MQTT_TRANSPORT not in {"tcp", "websockets"}:
            raise RuntimeError("MQTT_TRANSPORT must be tcp or websockets")
        if MQTT_TRANSPORT == "websockets" and not MQTT_WS_PATH.startswith("/"):
            raise RuntimeError("MQTT_WS_PATH must start with /")
        if bool(MQTT_USERNAME) != bool(MQTT_PASSWORD):
            with self._lock:
                self._mqtt_error = (
                    "กำหนด MQTT_USERNAME และ MQTT_PASSWORD พร้อมกัน"
                )
            LOGGER.error(self._mqtt_error)
            return

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"iot-dashboard-{uuid.uuid4().hex[:12]}",
            transport=MQTT_TRANSPORT,
        )
        if MQTT_TRANSPORT == "websockets":
            client.ws_set_options(path=MQTT_WS_PATH)
        if MQTT_TLS:
            client.tls_set()
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        try:
            client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_start()
            self._results_stop.clear()
            self._results_worker = threading.Thread(
                target=self._publish_results,
                args=(client,),
                name="mqtt-inspection-results",
                daemon=True,
            )
            self._results_worker.start()
        except (OSError, ValueError) as error:
            with self._lock:
                self._mqtt_error = str(error)
            LOGGER.exception("Could not start MQTT client")

    def stop(self) -> None:
        self._results_stop.set()
        self._results_ready.set()
        if self._results_worker is not None:
            self._results_worker.join(timeout=6)
            self._results_worker = None
        client = self._client
        if client is not None:
            client.disconnect()
            client.loop_stop()
            self._client = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job": self._job_snapshot_locked(),
            }

    def test_subscriptions(self, timeout_seconds: float = 4.0) -> dict[str, Any]:
        client = self._client
        topics = (MQTT_QUEUE_TOPIC,)
        if client is None or not self._mqtt_connected:
            return {
                "connected": False,
                "subscribed": False,
                "topics": [
                    {
                        "topic": topic,
                        "subscribed": False,
                        "message_received": False,
                        "payload": None,
                    }
                    for topic in topics
                ],
                "message": "MQTT ยังไม่เชื่อมต่อ",
            }

        started_at = time.time()
        test_event = threading.Event()
        with self._lock:
            self._test_event = test_event
            self._test_topics_seen = set()
        try:
            result, _ = client.subscribe([(topic, 1) for topic in topics])
            if result != mqtt.MQTT_ERR_SUCCESS:
                return {
                    "connected": True,
                    "subscribed": False,
                    "topics": [
                        {
                            "topic": topic,
                            "subscribed": False,
                            "message_received": False,
                            "payload": None,
                        }
                        for topic in topics
                    ],
                    "message": f"MQTT subscribe ไม่สำเร็จ: code {result}",
                }
            test_event.wait(timeout_seconds)
            with self._lock:
                topic_results = []
                for topic in topics:
                    last = self._last_messages.get(topic)
                    received_during_test = bool(
                        last is not None and last["received_at"] >= started_at
                    )
                    topic_results.append(
                        {
                            "topic": topic,
                            "subscribed": True,
                            "message_received": received_during_test,
                            "received_at": last["received_at"] if last else None,
                            "payload": last["payload"] if last else None,
                            "note": (
                                None
                                if received_during_test
                                else "ยังไม่มีข้อความใหม่ในช่วงทดสอบ"
                            ),
                        }
                    )
            return {
                "connected": True,
                "subscribed": True,
                "waited_seconds": timeout_seconds,
                "topics": topic_results,
                "message": "subscribe สำเร็จ",
            }
        finally:
            with self._lock:
                self._test_event = None
                self._test_topics_seen = set()

    def attach_camera(self, job_id: str) -> None:
        with self._lock:
            job = self._job
            if (
                job is None
                or job["job_id"] != job_id
                or job["status"] != "waiting_ir"
            ):
                raise ValueError("MQTT receiving request is not waiting for IR")
            updated = {
                **job,
                "status": "in_progress",
                "message": "IR ตรวจพบอุปกรณ์ · เปิดกล้องรอของให้ครบ",
            }
            self._event_store.update_queue(updated)
            job.update(updated)
            job_state = self._job_snapshot_locked()
        self._publish_job_state(job_state, "processing")

    def observe(
        self,
        job_id: str,
        counts: dict[str, int],
        save_result: Callable[[dict[str, int], int], None],
    ) -> None:
        job_state: dict[str, Any] | None = None
        event_type = "progress"
        with self._lock:
            job = self._job
            if (
                job is None
                or job["job_id"] != job_id
                or job["status"] != "in_progress"
            ):
                return

            detected = {}
            for name in job["requested"]:
                observed = counts.get(name, 0)
                if name != UNKNOWN_LABEL:
                    observed = max(job["detected"][name], observed)
                detected[name] = min(observed, job["requested"][name])

            complete = all(
                detected[name] == quantity
                for name, quantity in job["requested"].items()
            )
            if complete:
                save_result(detected, job["history_id"])
                job.update(
                    status="completed", message="รับของเรียบร้อย",
                    success=True, detected=detected,
                )
                job_state = self._job_snapshot_locked()
                event_type = "completed"
            elif detected != job["detected"]:
                detected_count = sum(detected.values())
                requested_count = sum(job["requested"].values())
                message = (
                    f"ตรวจพบ {detected_count}/{requested_count} รายการ "
                    "· เปิดกล้องรอของที่เหลือ"
                )
                updated = {**job, "detected": detected, "message": message}
                self._event_store.update_queue(updated)
                job.update(updated)
                job_state = self._job_snapshot_locked()

        if job_state is not None:
            self._publish_job_state(job_state, event_type)
            if event_type == "completed":
                self._results_ready.set()

    def abort_camera(self, job_id: str) -> None:
        with self._lock:
            job = self._job
            if (
                job is None
                or job["job_id"] != job_id
                or job["status"] != "in_progress"
            ):
                return
            updated = {
                **job,
                "status": "incomplete",
                "message": "หยุดการตรวจสอบก่อนอุปกรณ์จะครบ",
                "success": False,
            }
            try:
                self._event_store.update_queue(updated)
            except psycopg.Error:
                LOGGER.exception("Could not record the stopped receiving job %s", job_id)
            job.update(updated)
            result = self._job_snapshot_locked()
        self._publish_job_state(result, "incomplete")

    def cancel_queue(self, history_id: int, reason: str) -> dict[str, Any]:
        with self._lock:
            job = self._job
            if (
                job is None
                or job["history_id"] != history_id
                or job["status"] not in ("waiting_ir", "in_progress")
            ):
                raise ValueError("คิวนี้ไม่ได้อยู่ระหว่างรอหรือกำลังตรวจ")
            updated = {
                **job,
                "status": "cancelled",
                "message": f"ยกเลิกการตรวจรับ: {reason}",
                "success": False,
            }
            self._event_store.update_queue(updated)
            job.update(updated)
            result = self._job_snapshot_locked()
            self._publish_job_state(result, "cancelled")
            return result

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        if reason_code != 0:
            error = f"MQTT connection rejected: {reason_code}"
            with self._lock:
                self._mqtt_connected = False
                self._mqtt_error = error
            LOGGER.error(error)
            return
        result, _ = client.subscribe(
            [
                (MQTT_QUEUE_TOPIC, 1),
                (MQTT_ARMBOT_EVENT_TOPIC, 1),
                (MQTT_UNKNOWN_TOPIC, 1),
                (MQTT_UNKNOWN_IMAGE_TOPIC, 0),
            ]
        )
        with self._lock:
            self._mqtt_connected = result == mqtt.MQTT_ERR_SUCCESS
            self._mqtt_error = (
                None
                if self._mqtt_connected
                else f"MQTT subscribe failed with code {result}"
            )
        if not self._mqtt_connected:
            LOGGER.error(self._mqtt_error)
        self._results_ready.set()

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        with self._lock:
            self._mqtt_connected = False
            self._unknown_image_info = None
            if reason_code != 0:
                self._mqtt_error = f"MQTT disconnected: {reason_code}"
                LOGGER.warning(self._mqtt_error)

    def _on_connect_fail(
        self, _client: mqtt.Client, _userdata: Any
    ) -> None:
        with self._lock:
            self._mqtt_connected = False
            self._mqtt_error = "เชื่อมต่อ MQTT broker ไม่สำเร็จ"
        LOGGER.warning(self._mqtt_error)

    def _on_message(
        self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        try:
            self._handle_message(message)
        except psycopg.Error:
            LOGGER.exception("Could not persist a receiving update from %s", message.topic)

    def _handle_message(self, message: mqtt.MQTTMessage) -> None:
        if message.topic == MQTT_UNKNOWN_IMAGE_TOPIC:
            if not message.retain:
                self._receive_unknown_image(message.payload)
            return
        if message.topic != MQTT_QUEUE_TOPIC:
            if not message.retain:
                self._receive_armbot_message(message.topic, message.payload)
            return
        payload: Any = None
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("MQTT payload must be a JSON object")

            with self._lock:
                self._last_messages[message.topic] = {
                    "received_at": time.time(),
                    "payload": payload,
                }
                if self._test_event is not None:
                    self._test_topics_seen.add(message.topic)
                    if self._test_topics_seen == {MQTT_QUEUE_TOPIC}:
                        self._test_event.set()

            source = payload.get("source", "mqtt")
            if not isinstance(source, str):
                raise TypeError("source must be a string")

            item_id = payload.get("item_id") or payload.get("text")
            if payload.get("type") == "unknown" or payload.get("barcode") == "ARM-0099":
                item_id = "unknown"
            if item_id is not None:
                if not isinstance(item_id, str):
                    raise TypeError("item_id must be a string")
                yolo_class = ITEM_TO_YOLO_CLASS.get(item_id.strip().lower())
                if yolo_class is None:
                    LOGGER.warning("Unknown MQTT item_id: %s", item_id)
                    job_id = payload.get("job_id")
                    if isinstance(job_id, str) and 0 < len(job_id.strip()) <= 100:
                        quantity = payload.get("count", payload.get("quantity", 1))
                        if type(quantity) is not int or not 1 <= quantity <= 100:
                            quantity = 1
                        self._event_store.add_queue(
                            {
                                "job_id": job_id.strip(), "status": "rejected",
                                "requested": {item_id.strip()[:100]: quantity},
                                "detected": {}, "message": "อุปกรณ์นี้ยังไม่รองรับการตรวจ",
                            }
                        )
                    self._publish_event(
                        {
                            "type": "unknown",
                            "item_id": item_id,
                            "source": source,
                        }
                    )
                    return

                quantity = payload.get("count", payload.get("quantity", 1))
                request = ReceivingRequest(
                    job_id=str(payload.get("job_id") or uuid.uuid4().hex),
                    items=[{"class": yolo_class, "quantity": quantity}],
                )
                self.submit(
                    request,
                    item_id=item_id.strip().lower(),
                    source=source,
                    armbot_job_id=self._explicit_armbot_job_id(payload),
                )
                return

            request = ReceivingRequest(**payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValidationError,
        ) as error:
            LOGGER.warning("Ignoring invalid MQTT receiving request: %s", error)
            self._publish_event(
                {
                    "type": "rejected",
                    "job_id": payload.get("job_id") if isinstance(payload, dict) else None,
                    "message": "รูปแบบรายการ MQTT ไม่ถูกต้อง",
                }
            )
            return
        item_id = None
        if len(request.items) == 1:
            item_id = next(
                (
                    alias
                    for alias, model_class in ITEM_TO_YOLO_CLASS.items()
                    if model_class == request.items[0].equipment
                ),
                None,
            )
        self.submit(
            request,
            item_id=item_id,
            source=payload.get("source", "mqtt"),
            armbot_job_id=self._explicit_armbot_job_id(payload),
        )

    @staticmethod
    def _explicit_armbot_job_id(payload: dict[str, Any]) -> str | None:
        job_id = payload.get("armbot_job_id")
        return str(job_id) if type(job_id) is int else None

    def _receive_armbot_message(self, topic: str, raw: bytes) -> None:
        if topic not in (MQTT_ARMBOT_EVENT_TOPIC, MQTT_UNKNOWN_TOPIC) or len(raw) > 8192:
            return
        if topic == MQTT_UNKNOWN_TOPIC:
            with self._lock:
                self._unknown_image_info = None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        if topic == MQTT_UNKNOWN_TOPIC:
            if payload.get("type") == "unknown_object":
                with self._lock:
                    self._unknown_image_info = (time.monotonic(), payload)
            return
        if payload.get("type") != "queued":
            return
        # ARMbot's acknowledgment contains a nested job. Our own queue events don't.
        robot_job = payload.get("job")
        if not isinstance(robot_job, dict) or robot_job.get("item_id") != "unknown":
            return
        arm_id = robot_job.get("id")
        source = robot_job.get("source")
        count = robot_job.get("count")
        if (
            type(arm_id) is not int
            or not isinstance(source, str)
            or type(count) is not int
        ):
            return
        if not 1 <= count <= 100:
            return
        with self._lock:
            job = self._job
            if job is not None:
                if job.get("armbot_job_id") == str(arm_id):
                    return
                if job["status"] in ("waiting_ir", "in_progress"):
                    if (
                        UNKNOWN_LABEL in job["requested"]
                        and job["source"] == source
                        and job.get("armbot_job_id") is None
                    ):
                        requested = dict(job["requested"])
                        if len(job["requested"]) == 1:
                            requested[UNKNOWN_LABEL] = count
                        updated = {
                            **job, "requested": requested, "armbot_job_id": str(arm_id),
                        }
                        self._event_store.save_pick_images(
                            job["history_id"],
                            [
                                (stamp, image)
                                for (pending_arm_id, stamp), image in self._pending_pick_images.items()
                                if pending_arm_id == str(arm_id)
                            ],
                        )
                        self._event_store.update_queue(updated)
                        job.update(updated)
                        self._pending_pick_images.clear()
                        self._unknown_references = {
                            key: value
                            for key, value in self._unknown_references.items()
                            if key[0] == str(arm_id)
                        }
                    return
        request = ReceivingRequest(
            job_id=f"armbot-unknown-{arm_id}-{uuid.uuid4().hex}",
            items=[ReceivingItem(**{"class": "unknown", "quantity": count})],
        )
        self.submit(request, item_id="unknown", source=source, armbot_job_id=str(arm_id))

    def _receive_unknown_image(self, raw: bytes) -> None:
        with self._lock:
            info = self._unknown_image_info
            self._unknown_image_info = None
            job = self._job
            if (
                info is None
                or job is None
                or job["status"] not in ("waiting_ir", "in_progress")
                or UNKNOWN_LABEL not in job["requested"]
            ):
                return
        received_at, metadata = info
        if time.monotonic() - received_at > UNKNOWN_IMAGE_PAIR_SECONDS:
            return
        arm_id = metadata.get("job_id")
        captured_at = metadata.get("time")
        if (
            type(arm_id) is not int
            or type(captured_at) not in (int, float)
            or not 0 < captured_at < math.inf
        ):
            return
        key = (str(arm_id), captured_at)
        box = metadata.get("box")
        size = metadata.get("image_size")
        if not isinstance(box, dict) or not isinstance(size, list) or len(size) != 2:
            return
        coordinates = [box.get(axis) for axis in ("x1", "y1", "x2", "y2")]
        if any(type(value) is not int for value in (*coordinates, *size)):
            return
        width, height = size
        if not (0 < width <= 4096 and 0 < height <= 4096):
            return
        x1, y1, x2, y2 = coordinates
        # ARMbot draws the box on the published JPEG. Trim its border before comparing.
        x1, y1 = max(0, x1 + 3), max(0, y1 + 3)
        x2, y2 = min(width, x2 - 3), min(height, y2 - 3)
        if min(x2 - x1, y2 - y1) < 16 or not (
            0 < len(raw) <= UNKNOWN_IMAGE_MAX_BYTES and raw.startswith(b"\xff\xd8")
        ):
            return
        with self._lock:
            arm_job_id = job.get("armbot_job_id")
            if (
                (arm_job_id is not None and key[0] != arm_job_id)
                or key in self._unknown_references
            ):
                return
        try:
            frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None or frame.shape[:2] != (height, width):
                return
            signature = describe_object(frame[y1:y2, x1:x2])
        except cv2.error:
            LOGGER.warning("Could not decode ARMbot image for job %s", arm_id)
            return
        with self._lock:
            if self._job is not job:
                return
            if job.get("armbot_job_id") == key[0]:
                self._event_store.save_pick_images(job["history_id"], [(captured_at, raw)])
            elif job.get("armbot_job_id") is None and len(self._pending_pick_images) < 8:
                self._pending_pick_images[key] = raw
            if signature is not None and len(self._unknown_references) < 100:
                self._unknown_references[key] = signature

    def unknown_references(
        self, job_id: str
    ) -> tuple[bool, tuple[ObjectSignature, ...]]:
        with self._lock:
            job = self._job
            if (
                job is None or job["job_id"] != job_id
                or UNKNOWN_LABEL not in job["requested"]
            ):
                return False, ()
            return len(job["requested"]) > 1, tuple(
                signature
                for (arm_id, _), signature in self._unknown_references.items()
                if arm_id == job.get("armbot_job_id")
            )

    def submit(
        self,
        request: ReceivingRequest,
        item_id: str | None = None,
        source: str = "mqtt",
        armbot_job_id: str | None = None,
    ) -> None:
        requested: dict[str, int] = {}
        for item in request.items:
            name = MODEL_CLASS_LABELS[item.equipment]
            requested[name] = requested.get(name, 0) + item.quantity
        queued_job = {
            "job_id": request.job_id,
            "status": "waiting_ir",
            "requested": requested,
            "detected": {name: 0 for name in requested},
            "item_id": item_id,
            "source": source,
            "armbot_job_id": armbot_job_id,
            "success": False,
            "message": "รอ IR ตรวจพบอุปกรณ์",
        }
        if not request.items:
            reason = "ต้องระบุอุปกรณ์อย่างน้อยหนึ่งรายการ"
        elif len(request.items) > 3:
            reason = "ระบุอุปกรณ์ได้สูงสุด 3 ประเภท"
        elif any(quantity > 100 for quantity in requested.values()):
            reason = "จำนวนต่ออุปกรณ์ต้องไม่เกิน 100"
        else:
            reason = None
        if reason:
            self._event_store.add_queue({**queued_job, "status": "rejected", "message": reason})
            self._publish_event({"type": "rejected", "job_id": request.job_id, "message": reason})
            return
        accepted = True
        event_type = "queued"
        with self._lock:
            job = self._job
            if job is not None and job["job_id"] == request.job_id:
                result = self._job_snapshot_locked()
                event_type = (
                    "processing"
                    if job["status"] in ("waiting_ir", "in_progress")
                    else job["status"]
                )
            elif job is not None and job["status"] in (
                "waiting_ir",
                "in_progress",
            ):
                result = None
                accepted = False
                self._event_store.add_queue(
                    {
                        **queued_job, "status": "rejected",
                        "message": "มีงานรับอุปกรณ์อื่นกำลังทำงานอยู่",
                    }
                )
            else:
                queued_job["history_id"] = self._event_store.add_queue(queued_job)
                self._unknown_references.clear()
                self._pending_pick_images.clear()
                self._unknown_image_info = None
                self._job = queued_job
                result = self._job_snapshot_locked()
        if not accepted:
            self._publish_event(
                {
                    "type": "rejected",
                    "job_id": request.job_id,
                    "item_id": item_id,
                    "source": source,
                    "message": "มีงานรับอุปกรณ์อื่นกำลังทำงานอยู่",
                }
            )
            return
        self._publish_job_state(result, event_type)

    def _job_snapshot_locked(self) -> dict[str, Any] | None:
        if self._job is None:
            return None
        job = dict(self._job)
        job["requested"] = dict(self._job["requested"])
        job["detected"] = dict(self._job["detected"])
        job["progress"] = {
            name: {
                "detected": min(self._job["detected"][name], quantity),
                "requested": quantity,
            }
            for name, quantity in self._job["requested"].items()
        }
        job["inspection_items"] = [
            {
                "name": name,
                "requested": quantity,
                "detected": min(self._job["detected"][name], quantity),
                "remaining": max(
                    0, quantity - self._job["detected"][name]
                ),
            }
            for name, quantity in self._job["requested"].items()
        ]
        job["count"] = sum(self._job["requested"].values())
        job["detected_count"] = sum(
            min(self._job["detected"][name], quantity)
            for name, quantity in self._job["requested"].items()
        )
        job["remaining_count"] = max(0, job["count"] - job["detected_count"])
        if UNKNOWN_LABEL in job["requested"]:
            job["unknown_reference_count"] = sum(
                arm_id == job["armbot_job_id"]
                for arm_id, _ in self._unknown_references
            )
        if len(self._job["requested"]) == 1:
            class_name = next(iter(self._job["requested"]))
            model_class = next(
                (
                    model_name
                    for model_name, label in MODEL_CLASS_LABELS.items()
                    if label == class_name
                ),
                None,
            )
            job["yolo_class"] = model_class
            if job["item_id"] is None and model_class is not None:
                job["item_id"] = next(
                    (
                        item_id
                        for item_id, mapped_class in ITEM_TO_YOLO_CLASS.items()
                        if mapped_class == model_class
                    ),
                    None,
                )
        return job

    def _publish_job_state(
        self, job: dict[str, Any] | None, event_type: str
    ) -> None:
        if job is None:
            return
        event_status = (
            "processing"
            if job["status"] in ("waiting_ir", "in_progress")
            else job["status"]
        )
        self._publish_event(
            {
                "type": event_type,
                "job_id": job["job_id"],
                "item_id": job.get("item_id"),
                "yolo_class": job.get("yolo_class"),
                "count": job["count"],
                "detected_count": job["detected_count"],
                "remaining_count": job["remaining_count"],
                "source": job.get("source", "mqtt"),
                "status": event_status,
                "message": job["message"],
            }
        )
        self._publish_status(
            {
                "state": (
                    "busy"
                    if job["status"] in ("waiting_ir", "in_progress")
                    else "ready"
                ),
                "current_item": job.get("item_id"),
                "remaining_count": job["remaining_count"],
                "job_id": job["job_id"],
            }
        )

    def _publish_event(self, payload: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            LOGGER.error("Cannot publish MQTT event: client is not started")
            return
        result = client.publish(
            MQTT_EVENT_TOPIC,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Could not publish MQTT event: code %s", result.rc)

    def _publish_status(self, payload: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            LOGGER.error("Cannot publish MQTT status: client is not started")
            return
        result = client.publish(
            MQTT_STATUS_TOPIC,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
            retain=True,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Could not publish MQTT status: code %s", result.rc)

    def _publish_results(self, client: mqtt.Client) -> None:
        while not self._results_stop.is_set():
            self._results_ready.wait()
            self._results_ready.clear()
            while not self._results_stop.is_set() and client.is_connected():
                try:
                    result = self._event_store.pending_mqtt_result()
                    if result is None:
                        break
                    self._send_inspection_result(client, result)
                except Exception:
                    LOGGER.exception("Could not deliver queued MQTT inspection result")
                    if not self._results_stop.wait(5):
                        self._results_ready.set()
                    break

    def _send_inspection_result(self, client: mqtt.Client, row: dict[str, Any]) -> None:
        event_id = row["event_id"]
        image = self._event_store.image_bytes(event_id)
        if image is None:
            if self._event_store.get(event_id) is None:
                return  # The user deleted the evidence after we read the pending row.
            raise RuntimeError(f"Missing MQTT inspection image for event {event_id}")
        payload = {
            "schema_version": 1,
            "type": "inspection_result",
            "status": "completed",
            "job_id": row["job_id"],
            "armbot_job_id": row["armbot_job_id"],
            "event_id": event_id,
            "completed_at": row["detected_at"].isoformat(),
            "requested": row["requested"],
            "detected": row["detected"],
            "image": {
                "content_type": "image/jpeg",
                "encoding": "base64",
                "data": base64.b64encode(image).decode("ascii"),
            },
        }
        message = client.publish(
            MQTT_RESULT_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1, retain=False
        )
        if message.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT result publish failed: {message.rc}")
        message.wait_for_publish(timeout=5)
        if not message.is_published():
            raise RuntimeError(f"MQTT broker has not acknowledged event {event_id}")
        self._event_store.acknowledge_mqtt_result(row["queue_id"], event_id)


def find_unknown_regions(
    frame: np.ndarray,
    background_reference: np.ndarray,
    known_boxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    reference = cv2.resize(
        background_reference,
        (frame.shape[1], frame.shape[0]),
        interpolation=cv2.INTER_AREA,
    )
    frame_lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    reference_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB)
    difference = np.max(cv2.absdiff(frame_lab, reference_lab), axis=2)
    foreground = cv2.GaussianBlur(difference, (5, 5), 0)
    _, foreground = cv2.threshold(foreground, 8, 255, cv2.THRESH_BINARY)
    foreground = cv2.morphologyEx(
        foreground,
        cv2.MORPH_OPEN,
        np.ones((3, 3), dtype=np.uint8),
    )
    foreground = cv2.morphologyEx(
        foreground,
        cv2.MORPH_CLOSE,
        np.ones((9, 9), dtype=np.uint8),
    )
    for x1, y1, x2, y2 in known_boxes:
        padding = 8
        cv2.rectangle(
            foreground,
            (max(0, x1 - padding), max(0, y1 - padding)),
            (
                min(frame.shape[1] - 1, x2 + padding),
                min(frame.shape[0] - 1, y2 + padding),
            ),
            0,
            thickness=cv2.FILLED,
        )
    contours, _ = cv2.findContours(
        foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    minimum_area = max(600, int(frame.shape[0] * frame.shape[1] * 0.001))
    return [
        cv2.boundingRect(contour)
        for contour in contours
        if cv2.contourArea(contour) >= minimum_area
    ]


@dataclass(frozen=True)
class DetectionBox:
    x1: int
    y1: int
    x2: int
    y2: int
    label: str
    confidence: float | None = None


@dataclass(frozen=True)
class CameraFrameResult:
    frame: np.ndarray
    boxes: list[DetectionBox]
    counts: dict[str, int]

    @cached_property
    def jpeg(self) -> bytes:
        annotated = self.frame.copy()
        for box in self.boxes:
            label = box.label
            color = (0, 191, 255) if box.confidence is None else (120, 220, 52)
            if box.confidence is not None:
                label = f"{label} {box.confidence:.2f}"
            cv2.rectangle(annotated, (box.x1, box.y1), (box.x2, box.y2), color, 2)
            cv2.putText(
                annotated, label, (box.x1, max(20, box.y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA,
            )
        success, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not success:
            raise RuntimeError("แปลงภาพจากกล้องเป็น JPEG ไม่สำเร็จ")
        return encoded.tobytes()


class CameraService:
    def __init__(
        self, event_store: EventStore, receiving_service: MqttReceivingService
    ) -> None:
        self._lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._capture: cv2.VideoCapture | None = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._model: YOLO | None = None
        self._background_reference: np.ndarray | None = None
        if BACKGROUND_REFERENCE_PATH.exists():
            self._background_reference = cv2.imread(
                str(BACKGROUND_REFERENCE_PATH), cv2.IMREAD_COLOR
            )
            if self._background_reference is None:
                raise RuntimeError(
                    "อ่านภาพอ้างอิงฉากหลังไม่ได้: "
                    f"{BACKGROUND_REFERENCE_PATH}"
                )
        self._event_store = event_store
        self._receiving_service = receiving_service
        self._jpeg = placeholder_frame("Camera stopped")
        self._preview: CameraFrameResult | None = None
        self._confidence = 0.5
        self._iou = 0.25
        self._last_frame_time: float | None = None
        self._event_saved = False
        self._frames_processed = 0
        self._record_events = True
        self._state: dict[str, Any] = {
            "running": False,
            "session_id": None,
            "camera_index": None,
            "record_events": True,
            "counts": {name: 0 for name in CLASS_NAMES},
            "total": 0,
            "fps": 0,
            "error": None,
            "updated_at": None,
            "last_event_id": None,
            "last_event_at": None,
            "capture_trigger_id": None,
            "capture_completed": False,
            "receiving_job_id": None,
            "background_ready": self._background_reference is not None,
        }

    def _load_model(self) -> None:
        if not MODEL_PATH.is_file():
            raise FileNotFoundError(
                f"ไม่พบไฟล์โมเดลที่ {MODEL_PATH}"
            )
        if self._model is None:
            self._model = YOLO(str(MODEL_PATH))

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["counts"] = dict(self._state["counts"])
            return state

    def _reset_state(
        self,
        camera_index: str | int | None,
        trigger_id: str | None = None,
        record_events: bool = True,
        receiving_job_id: str | None = None,
    ) -> None:
        self._event_saved = False
        self._frames_processed = 0
        self._record_events = record_events
        self._preview = None
        self._state.update(
            {
                "running": camera_index is not None,
                "session_id": uuid.uuid4().hex if camera_index is not None else None,
                "camera_index": camera_index,
                "record_events": record_events,
                "counts": {name: 0 for name in CLASS_NAMES},
                "total": 0,
                "fps": 0,
                "error": None,
                "updated_at": None,
                "last_event_id": None,
                "last_event_at": None,
                "capture_trigger_id": trigger_id,
                "capture_completed": False,
                "receiving_job_id": receiving_job_id,
                "background_ready": self._background_reference is not None,
            }
        )

    def start(self, request: CameraRequest) -> None:
        if request.receive_job_id is not None and request.mode != "browser":
            raise ValueError("MQTT receiving requires a browser camera")
        if request.mode == "browser":
            self.start_browser(request)
            return
        self.start_local(request)

    def start_browser(self, request: CameraRequest) -> None:
        with self._lock:
            if self._running:
                raise ValueError("กล้องกำลังทำงานอยู่แล้ว")
            self._load_model()
            if request.receive_job_id is not None:
                if self._background_reference is None:
                    raise ValueError(
                        "ยังไม่ได้ตั้งภาพอ้างอิงฉากหลัง "
                        "เปิดกล้องทดสอบขณะฉากว่างและบันทึกฉากหลังก่อน"
                    )
                self._receiving_service.attach_camera(request.receive_job_id)
            self._confidence = request.confidence
            self._iou = request.iou
            self._capture = None
            self._worker = None
            self._running = True
            self._last_frame_time = None
            self._reset_state(
                "browser",
                request.trigger_id,
                request.record_events and request.receive_job_id is None,
                request.receive_job_id,
            )

    def start_local(self, request: CameraRequest) -> None:
        with self._lock:
            if self._running:
                raise ValueError("กล้องกำลังทำงานอยู่แล้ว")
            self._load_model()
            capture = cv2.VideoCapture(request.index, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(request.index)
            if not capture.isOpened():
                capture.release()
                raise RuntimeError(
                    f"เปิดกล้องหมายเลข {request.index} ไม่ได้ "
                    "ตรวจสอบว่ากล้องไม่ถูกโปรแกรมอื่นใช้งาน"
                )
            self._confidence = request.confidence
            self._iou = request.iou
            self._capture = capture
            self._running = True
            self._last_frame_time = None
            self._reset_state(request.index, request.trigger_id, request.record_events)
            self._worker = threading.Thread(
                target=self._process_local_frames,
                args=(capture,),
                name="camera-inference",
                daemon=True,
            )
            self._worker.start()

    def cancel_receiving(self, history_id: int, reason: str) -> dict[str, Any]:
        with self._lock:
            job = self._receiving_service.cancel_queue(history_id, reason)
            if self._state["receiving_job_id"] == job["job_id"]:
                self._running = False
                self._reset_state(None)
                self._jpeg = placeholder_frame("Camera stopped")
            return job

    def stop(self, session_id: str | None = None) -> None:
        with self._lock:
            active_session = self._state["session_id"]
            if session_id is not None and session_id != active_session:
                return
            self._running = False
            worker = self._worker
            capture = self._capture
            receiving_job_id = self._state["receiving_job_id"]
            self._worker = None
            self._capture = None
            if worker is None:
                self._reset_state(None)
                self._jpeg = placeholder_frame("Camera stopped")
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
        if capture is not None:
            capture.release()
        if receiving_job_id is not None:
            self._receiving_service.abort_camera(receiving_job_id)
        if worker is not None:
            with self._lock:
                if self._state["session_id"] == active_session:
                    self._reset_state(None)
                    self._jpeg = placeholder_frame("Camera stopped")

    def latest_jpeg(self) -> bytes:
        with self._lock:
            preview = self._preview
            placeholder = self._jpeg
        return preview.jpeg if preview is not None else placeholder

    def set_background_reference(self, content: bytes) -> dict[str, Any]:
        frame = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("ไฟล์ภาพอ้างอิงฉากหลังไม่ถูกต้อง")
        with self._lock:
            if (
                not self._running
                or self._state["camera_index"] != "browser"
                or self._state["receiving_job_id"] is not None
                or self._record_events
            ):
                raise ValueError("ตั้งฉากหลังได้เฉพาะตอนเปิดกล้องทดสอบ")
            success, encoded = cv2.imencode(".jpg", frame)
            if not success:
                raise RuntimeError("เข้ารหัสภาพอ้างอิงฉากหลังไม่สำเร็จ")
            BACKGROUND_REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = BACKGROUND_REFERENCE_PATH.with_name(
                f"{BACKGROUND_REFERENCE_PATH.stem}.tmp.jpg"
            )
            temporary_path.write_bytes(encoded.tobytes())
            temporary_path.replace(BACKGROUND_REFERENCE_PATH)
            self._background_reference = frame
            self._state["background_ready"] = True
            return self.state()

    def _detect(self, frame: np.ndarray) -> CameraFrameResult:
        model = self._model
        if model is None:
            raise RuntimeError("โมเดลยังไม่พร้อมใช้งาน")
        with self._inference_lock:
            result = model.predict(
                frame, conf=self._confidence, iou=self._iou, verbose=False
            )[0]
        counts = {name: 0 for name in CLASS_NAMES}
        detections: list[DetectionBox] = []
        known_boxes: list[tuple[int, int, int, int]] = []
        if result.boxes is not None:
            for class_id, box, confidence in zip(
                result.boxes.cls.int().tolist(),
                result.boxes.xyxy.int().tolist(),
                result.boxes.conf.tolist(),
            ):
                model_name = result.names[class_id].casefold()
                name = MODEL_CLASS_LABELS.get(model_name)
                if name in counts and name != UNKNOWN_LABEL:
                    counts[name] += 1
                    known_boxes.append(tuple(box))
                    detections.append(DetectionBox(*box, name, round(float(confidence), 3)))
        with self._lock:
            receiving_job_id = self._state["receiving_job_id"]
        background_reference = self._background_reference
        if background_reference is not None:
            boxes_to_draw = []
            label = UNKNOWN_LABEL
            if receiving_job_id is None:
                boxes_to_draw = find_unknown_regions(frame, background_reference, known_boxes)
            else:
                mixed_order, references = self._receiving_service.unknown_references(
                    receiving_job_id
                )
                if references:
                    regions = find_unknown_regions(
                        frame, background_reference, known_boxes if mixed_order else []
                    )
                    candidates = [
                        describe_object(frame[y : y + height, x : x + width])
                        for x, y, width, height in regions
                    ]
                    matched = match_objects(references, candidates)
                    boxes_to_draw = [regions[index] for index in sorted(matched)]
                    label = "Unknown match"
            counts[UNKNOWN_LABEL] = len(boxes_to_draw)
            for x, y, width, height in boxes_to_draw:
                detections.append(DetectionBox(x, y, x + width, y + height, label))
        return CameraFrameResult(frame, detections, counts)

    def _publish(self, result: CameraFrameResult) -> None:
        counts = result.counts
        total = sum(counts.values())
        event_to_save = self._record_events and total > 0
        with self._lock:
            self._preview = result
            self._state.update(
                {
                    "running": self._running,
                    "counts": counts,
                    "total": total,
                    "error": None,
                    "updated_at": time.time(),
                }
            )
            event_to_save = (
                self._record_events
                and (event_to_save or self._state["capture_trigger_id"] is not None)
                and self._frames_processed >= 2
                and not self._event_saved
            )
            if event_to_save:
                self._event_saved = True
            receiving_job_id = self._state["receiving_job_id"]
            if receiving_job_id is not None and self._running and not self._event_saved:
                def save_inspection(labels: dict[str, int], queue_id: int) -> None:
                    event = self._event_store.create(
                        result.jpeg, labels, sum(labels.values()), queue_id=queue_id
                    )
                    self._event_saved = True
                    self._state["last_event_id"] = event["id"]
                    self._state["last_event_at"] = event["detected_at"]
                    self._state["capture_completed"] = True

                try:
                    self._receiving_service.observe(
                        receiving_job_id, counts, save_inspection
                    )
                except Exception as error:
                    self._state["error"] = f"บันทึกรูปเหตุการณ์ไม่ได้: {error}"
        if event_to_save:
            labels = {name: count for name, count in counts.items() if count}
            try:
                event = self._event_store.create(result.jpeg, labels, total)
                with self._lock:
                    self._state["last_event_id"] = event["id"]
                    self._state["last_event_at"] = event["detected_at"]
                    self._state["capture_completed"] = True
            except Exception as error:
                with self._lock:
                    self._event_saved = False
                    self._state["error"] = f"บันทึกรูปเหตุการณ์ไม่ได้: {error}"

    def process_uploaded_frame(
        self, content: bytes, session_id: str | None = None, preview: bool = True
    ) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                raise RuntimeError("กรุณาเริ่มกล้องก่อนส่งภาพ")
            active_session = self._state["session_id"]
            if session_id is not None and session_id != active_session:
                raise ValueError("รอบการตรวจด้วยกล้องจบแล้ว")
        frame = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("ไฟล์ภาพจากกล้องไม่ถูกต้อง")
        started = time.perf_counter()
        result = self._detect(frame)
        with self._lock:
            if self._state["session_id"] != active_session:
                raise ValueError("รอบการตรวจด้วยกล้องจบแล้ว")
            self._frames_processed += 1
            now = time.perf_counter()
            fps = 1 / max(now - (self._last_frame_time or started), 1e-6)
            self._last_frame_time = now
            self._publish(result)
            self._state["fps"] = round(fps, 1)
            camera_state = self.state()
        height, width = frame.shape[:2]
        payload: dict[str, Any] = {
            "camera": camera_state,
            "width": width,
            "height": height,
            "boxes": [asdict(box) for box in result.boxes],
        }
        if preview:
            encoded = base64.b64encode(result.jpeg).decode("ascii")
            payload["image"] = f"data:image/jpeg;base64,{encoded}"
        return payload

    def _process_local_frames(self, capture: cv2.VideoCapture) -> None:
        try:
            while True:
                with self._lock:
                    if not self._running:
                        break
                success, frame = capture.read()
                if not success:
                    raise RuntimeError("อ่านภาพจากกล้องไม่สำเร็จ")
                started = time.perf_counter()
                result = self._detect(frame)
                with self._lock:
                    self._frames_processed += 1
                now = time.perf_counter()
                fps = 1 / max(now - (self._last_frame_time or started), 1e-6)
                self._last_frame_time = now
                self._publish(result)
                with self._lock:
                    self._state["fps"] = round(fps, 1)
        except Exception as error:
            with self._lock:
                self._state["error"] = str(error)
                self._running = False
        finally:
            capture.release()
            with self._lock:
                if self._capture is capture:
                    self._capture = None
                self._worker = None
                self._state["running"] = False
                self._state["camera_index"] = None


class ArduinoWifi:
    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "connected": False,
            "source": "Wi-Fi",
            "object_present": None,
            "ir_last_seen": None,
            "last_seen": None,
            "error": None,
            "capture_request_id": None,
            "capture_pending": False,
            "armed": True,
            "clear_since": None,
        }

    def load(self) -> None:
        saved = self._event_store.load_sensor_state()
        with self._lock:
            for key in (
                "object_present",
                "ir_last_seen",
                "last_seen",
                "capture_request_id",
                "capture_pending",
                "armed",
                "clear_since",
            ):
                if key in saved:
                    self._state[key] = saved[key]

    def _persistent_state_locked(self) -> dict[str, Any]:
        return {
            key: self._state[key]
            for key in (
                "object_present",
                "ir_last_seen",
                "last_seen",
                "capture_request_id",
                "capture_pending",
                "armed",
                "clear_since",
            )
        }

    def _persist(self, payload: dict[str, Any]) -> None:
        try:
            self._event_store.save_sensor_state(payload)
        except Exception as error:
            with self._lock:
                self._state["error"] = f"บันทึกสถานะ IR ไม่ได้: {error}"

    def _rearm_if_ready_locked(self, now: float) -> bool:
        clear_since = self._state["clear_since"]
        if (
            self._state["object_present"] is False
            and not self._state["armed"]
            and clear_since is not None
            and now - clear_since >= REARM_SECONDS
        ):
            self._state["armed"] = True
            return True
        return False

    def state(self) -> dict[str, Any]:
        needs_persist = False
        with self._lock:
            needs_persist = self._rearm_if_ready_locked(time.time())
            state = dict(self._state)
        now = time.time()
        ready = self._is_sensor_ready(state["ir_last_seen"], now)
        state["connected"] = ready
        state["ir_ready"] = ready
        if state["object_present"] is False and state["clear_since"] is not None:
            state["rearm_in_seconds"] = max(
                0, round(REARM_SECONDS - (now - state["clear_since"]), 1)
            )
        else:
            state["rearm_in_seconds"] = 0 if state["armed"] else REARM_SECONDS
        if needs_persist:
            self._persist(
                {
                    key: state[key]
                    for key in (
                        "object_present",
                        "ir_last_seen",
                        "last_seen",
                        "capture_request_id",
                        "capture_pending",
                        "armed",
                        "clear_since",
                    )
                }
            )
        return state

    @staticmethod
    def _is_sensor_ready(last_seen: float | None, now: float) -> bool:
        return last_seen is not None and 0 <= now - last_seen <= SENSOR_TIMEOUT_SECONDS

    def receive_ir(self, object_present: bool) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._rearm_if_ready_locked(now)
            previous = self._state["object_present"]
            if object_present and self._state["armed"]:
                self._state["capture_request_id"] = uuid.uuid4().hex
                self._state["capture_pending"] = True
                self._state["armed"] = False
            elif not object_present and previous is not False:
                self._state["clear_since"] = now
            self._state.update(
                {
                    "connected": True,
                    "object_present": object_present,
                    "ir_last_seen": now,
                    "last_seen": now,
                    "error": None,
                }
            )
            persistent = self._persistent_state_locked()
        self._persist(persistent)
        return self.state()

    def acknowledge_capture(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            if self._state["capture_request_id"] == request_id:
                self._state["capture_pending"] = False
            persistent = self._persistent_state_locked()
        self._persist(persistent)
        return self.state()


event_store = EventStore(
    DATABASE_URL,
    SNAPSHOT_ROOT,
    snapshot_bucket=SNAPSHOT_BUCKET,
    aws_region=AWS_REGION,
)
receiving = MqttReceivingService(event_store)
camera = CameraService(event_store, receiving)
arduino = ArduinoWifi(event_store)
browser_frame_gate = threading.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            event_store.initialize()
            arduino.load()
            last_error = None
            break
        except Exception as error:
            last_error = error
            if attempt < 9:
                time.sleep(2)
    if last_error is not None:
        raise RuntimeError(f"เชื่อมต่อ PostgreSQL ไม่สำเร็จ: {last_error}") from last_error
    receiving.start()
    yield
    camera.stop()
    receiving.stop()


app = FastAPI(title="IoT Object Detection Dashboard", lifespan=lifespan)


@app.get("/")
def dashboard() -> FileResponse:
    if not PAGE_PATH.is_file():
        raise HTTPException(status_code=500, detail="ไม่พบไฟล์หน้า Dashboard")
    return FileResponse(PAGE_PATH)


@app.get("/api/status")
def status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "camera": camera.state(),
        "arduino": arduino.state(),
        "receiving": receiving.state(),
        "history_revision": event_store.history_revision,
    }


@app.post("/api/mqtt/test")
def test_mqtt_subscriptions() -> dict[str, Any]:
    return receiving.test_subscriptions()


@app.post("/api/camera/start")
def start_camera(request: CameraRequest) -> dict[str, Any]:
    try:
        camera.start(request)
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"เริ่มกล้องไม่ได้: {error}") from error
    return camera.state()


@app.post("/api/camera/stop")
def stop_camera(session_id: str | None = Query(default=None, max_length=100)) -> dict[str, Any]:
    camera.stop(session_id)
    return camera.state()


@app.post("/api/camera/frame")
async def camera_frame(
    file: UploadFile = File(...),
    session_id: str | None = Query(default=None, max_length=100),
    # Older browser tabs still use the server-rendered preview.
    preview: bool = Query(default=True),
) -> dict[str, Any]:
    if not browser_frame_gate.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="กำลังประมวลผลเฟรมก่อนหน้า")
    try:
        content = await file.read()
        return await run_in_threadpool(camera.process_uploaded_frame, content, session_id, preview)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        browser_frame_gate.release()


@app.post("/api/camera/background-reference")
async def camera_background_reference(file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        content = await file.read()
        return camera.set_background_reference(content)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/camera/stream")
def camera_stream() -> StreamingResponse:
    def frames():
        while True:
            frame = camera.latest_jpeg()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.1)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/events")
def list_events(
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return {"events": event_store.list(limit)}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"อ่านประวัติไม่ได้: {error}") from error


@app.get("/api/receiving-history")
def list_receiving_history(
    response: Response,
    limit: int = Query(default=20, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    rows = event_store.list_queues(limit + 1, before_id)
    return {
        "queues": rows[:limit],
        "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


@app.get("/api/receiving-history/{queue_id}")
def receiving_history_detail(queue_id: int, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    row = event_store.get_queue(queue_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ไม่พบคิวนี้")
    return row


@app.post("/api/receiving-history/{queue_id}/cancel")
def cancel_receiving_queue(queue_id: int, request: CancelQueueRequest) -> dict[str, Any]:
    sensor = arduino.state()
    try:
        job = camera.cancel_receiving(queue_id, request.reason.strip())
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if sensor["capture_pending"] and sensor["capture_request_id"]:
        arduino.acknowledge_capture(sensor["capture_request_id"])
    return {"job": job, "camera": camera.state()}


@app.get("/api/receiving-history/{queue_id}/images/{image_id}")
def receiving_pick_image(queue_id: int, image_id: int) -> Response:
    image = event_store.pick_image_bytes(queue_id, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="ไม่พบรูปของคิวนี้")
    return Response(content=image, media_type="image/jpeg")


@app.get("/api/events/{event_id}/image")
def event_image(event_id: int) -> FileResponse:
    try:
        image = event_store.image_bytes(event_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    if image is None:
        raise HTTPException(status_code=404, detail="ไม่พบรูปเหตุการณ์")
    return Response(content=image, media_type="image/jpeg")


@app.get("/api/events/{event_id}")
def get_event(event_id: int) -> dict[str, Any]:
    try:
        event = event_store.get(event_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"อ่านรายละเอียดไม่ได้: {error}") from error
    if event is None:
        raise HTTPException(status_code=404, detail="ไม่พบรายการเหตุการณ์")
    return event


@app.delete("/api/events/{event_id}")
def delete_event(event_id: int) -> dict[str, Any]:
    try:
        deleted = event_store.delete(event_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"ลบประวัติไม่ได้: {error}") from error
    if not deleted:
        raise HTTPException(status_code=404, detail="ไม่พบรายการเหตุการณ์")
    return {"deleted": True, "id": event_id}


@app.post("/api/capture/ack")
def acknowledge_capture(request: CaptureAckRequest) -> dict[str, Any]:
    return arduino.acknowledge_capture(request.request_id)


@app.post("/api/arduino/ir")
def receive_arduino_ir(
    request: IRSensorRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    if ARDUINO_API_KEY and not x_api_key:
        raise HTTPException(status_code=401, detail="ต้องระบุ X-API-Key")
    if ARDUINO_API_KEY and not secrets.compare_digest(x_api_key or "", ARDUINO_API_KEY):
        raise HTTPException(status_code=403, detail="X-API-Key ไม่ถูกต้อง")
    return arduino.receive_ir(request.object_present)
