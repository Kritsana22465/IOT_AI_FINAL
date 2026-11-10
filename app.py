from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import quote

import boto3
import cv2
import numpy as np
import psycopg
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from psycopg.types.json import Json
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "best.pt"
PAGE_PATH = ROOT / "web" / "index.html"
SNAPSHOT_ROOT = Path(
    os.getenv("SNAPSHOT_DIR", str(ROOT / "data" / "snapshots"))
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
CLASS_NAMES = ("Banana Pi", "Relay module", "Ultrasonic HC-SR04")
MODEL_CLASS_LABELS = {
    "bananapi": "Banana Pi",
    "relay_module": "Relay module",
    "ultrasonic_hc-sr04": "Ultrasonic HC-SR04",
}
SENSOR_TIMEOUT_SECONDS = float(os.getenv("SENSOR_TIMEOUT_SECONDS", "5"))
REARM_SECONDS = float(os.getenv("REARM_SECONDS", "10"))
ARDUINO_API_KEY = os.getenv("ARDUINO_API_KEY", "").strip()


class CameraRequest(BaseModel):
    index: int = Field(default=0, ge=0, le=10)
    confidence: float = Field(default=0.5, ge=0, le=1)
    iou: float = Field(default=0.25, ge=0, le=1)
    mode: Literal["browser", "local"] = "browser"
    trigger_id: str | None = Field(default=None, max_length=100)


class IRSensorRequest(BaseModel):
    object_present: bool


class CaptureAckRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)


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
        self, image: bytes, labels: dict[str, int], total: int
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
                connection.commit()
        except Exception:
            if not self.database_images:
                self._delete_image(relative_path)
            raise
        if row is None:
            raise RuntimeError("บันทึกเหตุการณ์ไม่สำเร็จ")
        return self._payload(row)

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


class CameraService:
    def __init__(self, event_store: EventStore) -> None:
        self._lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._capture: cv2.VideoCapture | None = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._model: YOLO | None = None
        self._event_store = event_store
        self._jpeg = placeholder_frame("Camera stopped")
        self._confidence = 0.5
        self._iou = 0.25
        self._last_frame_time: float | None = None
        self._event_saved = False
        self._frames_processed = 0
        self._state: dict[str, Any] = {
            "running": False,
            "camera_index": None,
            "counts": {name: 0 for name in CLASS_NAMES},
            "total": 0,
            "fps": 0,
            "error": None,
            "updated_at": None,
            "last_event_id": None,
            "last_event_at": None,
            "capture_trigger_id": None,
            "capture_completed": False,
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
        self, camera_index: str | int | None, trigger_id: str | None = None
    ) -> None:
        self._event_saved = False
        self._frames_processed = 0
        self._state.update(
            {
                "running": camera_index is not None,
                "camera_index": camera_index,
                "counts": {name: 0 for name in CLASS_NAMES},
                "total": 0,
                "fps": 0,
                "error": None,
                "updated_at": None,
                "last_event_id": None,
                "last_event_at": None,
                "capture_trigger_id": trigger_id,
                "capture_completed": False,
            }
        )

    def start(self, request: CameraRequest) -> None:
        if request.mode == "browser":
            self.start_browser(request)
            return
        self.start_local(request)

    def start_browser(self, request: CameraRequest) -> None:
        with self._lock:
            if self._running:
                raise ValueError("กล้องกำลังทำงานอยู่แล้ว")
            self._load_model()
            self._confidence = request.confidence
            self._iou = request.iou
            self._capture = None
            self._worker = None
            self._running = True
            self._last_frame_time = None
            self._reset_state("browser", request.trigger_id)

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
            self._reset_state(request.index, request.trigger_id)
            self._worker = threading.Thread(
                target=self._process_local_frames,
                args=(capture,),
                name="camera-inference",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            worker = self._worker
            capture = self._capture
            self._worker = None
            self._capture = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
        if capture is not None:
            capture.release()
        with self._lock:
            self._reset_state(None)
            self._jpeg = placeholder_frame("Camera stopped")

    def latest_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def _detect(self, frame: np.ndarray) -> tuple[bytes, dict[str, int], int]:
        model = self._model
        if model is None:
            raise RuntimeError("โมเดลยังไม่พร้อมใช้งาน")
        with self._inference_lock:
            result = model.predict(
                frame, conf=self._confidence, iou=self._iou, verbose=False
            )[0]
        annotated = result.plot()
        counts = {name: 0 for name in CLASS_NAMES}
        if result.boxes is not None:
            for class_id in result.boxes.cls.int().tolist():
                model_name = result.names[class_id]
                name = MODEL_CLASS_LABELS.get(model_name)
                if name in counts:
                    counts[name] += 1
        success, encoded = cv2.imencode(
            ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82]
        )
        if not success:
            raise RuntimeError("แปลงภาพจากกล้องเป็น JPEG ไม่สำเร็จ")
        return encoded.tobytes(), counts, sum(counts.values())

    def _publish(self, jpeg: bytes, counts: dict[str, int], total: int) -> None:
        event_to_save = total > 0
        with self._lock:
            self._jpeg = jpeg
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
                (event_to_save or self._state["capture_trigger_id"] is not None)
                and self._frames_processed >= 2
                and not self._event_saved
            )
            if event_to_save:
                self._event_saved = True
        if event_to_save:
            labels = {name: count for name, count in counts.items() if count}
            try:
                event = self._event_store.create(jpeg, labels, total)
                with self._lock:
                    self._state["last_event_id"] = event["id"]
                    self._state["last_event_at"] = event["detected_at"]
                    self._state["capture_completed"] = True
            except Exception as error:
                with self._lock:
                    self._event_saved = False
                    self._state["error"] = f"บันทึกรูปเหตุการณ์ไม่ได้: {error}"

    def process_uploaded_frame(self, content: bytes) -> bytes:
        with self._lock:
            if not self._running:
                raise RuntimeError("กรุณาเริ่มกล้องก่อนส่งภาพ")
        frame = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("ไฟล์ภาพจากกล้องไม่ถูกต้อง")
        started = time.perf_counter()
        jpeg, counts, total = self._detect(frame)
        with self._lock:
            self._frames_processed += 1
        now = time.perf_counter()
        fps = 1 / max(now - (self._last_frame_time or started), 1e-6)
        self._last_frame_time = now
        self._publish(jpeg, counts, total)
        with self._lock:
            self._state["fps"] = round(fps, 1)
        return jpeg

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
                jpeg, counts, total = self._detect(frame)
                with self._lock:
                    self._frames_processed += 1
                now = time.perf_counter()
                fps = 1 / max(now - (self._last_frame_time or started), 1e-6)
                self._last_frame_time = now
                self._publish(jpeg, counts, total)
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
camera = CameraService(event_store)
arduino = ArduinoWifi(event_store)


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
    yield
    camera.stop()


app = FastAPI(title="IoT Object Detection Dashboard", lifespan=lifespan)


@app.get("/")
def dashboard() -> FileResponse:
    if not PAGE_PATH.is_file():
        raise HTTPException(status_code=500, detail="ไม่พบไฟล์หน้า Dashboard")
    return FileResponse(PAGE_PATH)


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {"camera": camera.state(), "arduino": arduino.state()}


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
def stop_camera() -> dict[str, Any]:
    camera.stop()
    return camera.state()


@app.post("/api/camera/frame")
async def camera_frame(file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        content = await file.read()
        jpeg = camera.process_uploaded_frame(content)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    encoded = base64.b64encode(jpeg).decode("ascii")
    return {
        "image": f"data:image/jpeg;base64,{encoded}",
        "camera": camera.state(),
    }


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
