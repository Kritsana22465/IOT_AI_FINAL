from __future__ import annotations

import json
import math
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "runs" / "object-detection" / "iot-components" / "weights" / "best.pt"
PAGE_PATH = ROOT / "web" / "index.html"
CLASS_NAMES = ("Banana Pi", "Relay module", "Ultrasonic HC-SR04")
SENSOR_TIMEOUT_SECONDS = 3


class CameraRequest(BaseModel):
    index: int = Field(default=0, ge=0, le=10)
    confidence: float = Field(default=0.5, ge=0, le=1)
    iou: float = Field(default=0.25, ge=0, le=1)


class SerialConnectRequest(BaseModel):
    port: str = Field(min_length=1, max_length=100)
    baudrate: int = Field(default=115200, ge=300, le=1_000_000)


class RelayRequest(BaseModel):
    enabled: bool


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
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._capture: cv2.VideoCapture | None = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._model: YOLO | None = None
        self._jpeg = placeholder_frame("Camera stopped")
        self._state: dict[str, Any] = {
            "running": False,
            "camera_index": None,
            "counts": {name: 0 for name in CLASS_NAMES},
            "total": 0,
            "fps": 0,
            "error": None,
            "updated_at": None,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self, request: CameraRequest) -> None:
        with self._lock:
            if self._running:
                raise ValueError("กล้องกำลังทำงานอยู่แล้ว")
            if not MODEL_PATH.is_file():
                raise FileNotFoundError(
                    f"ไม่พบโมเดลที่ {MODEL_PATH} กรุณารัน train.py ก่อน"
                )

            try:
                if self._model is None:
                    self._model = YOLO(str(MODEL_PATH))
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
            except Exception:
                self._state["running"] = False
                raise

            self._capture = capture
            self._running = True
            self._state.update(
                {
                    "running": True,
                    "camera_index": request.index,
                    "counts": {name: 0 for name in CLASS_NAMES},
                    "total": 0,
                    "fps": 0,
                    "error": None,
                    "updated_at": None,
                }
            )
            self._worker = threading.Thread(
                target=self._process_frames,
                args=(capture, request.confidence, request.iou),
                name="camera-inference",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()
        with self._lock:
            if self._capture is not None:
                self._capture.release()
            self._capture = None
            self._worker = None
            self._state.update(
                {
                    "running": False,
                    "camera_index": None,
                    "counts": {name: 0 for name in CLASS_NAMES},
                    "total": 0,
                    "fps": 0,
                    "updated_at": None,
                }
            )
            self._jpeg = placeholder_frame("Camera stopped")

    def latest_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def _process_frames(
        self, capture: cv2.VideoCapture, confidence: float, iou: float
    ) -> None:
        previous_frame_time = time.perf_counter()
        try:
            while True:
                with self._lock:
                    if not self._running:
                        break
                    model = self._model
                if model is None:
                    raise RuntimeError("กล้องหรือโมเดลยังไม่พร้อมใช้งาน")

                success, frame = capture.read()
                if not success:
                    raise RuntimeError("อ่านภาพจากกล้องไม่สำเร็จ")

                result = model.predict(
                    frame, conf=confidence, iou=iou, verbose=False
                )[0]
                annotated = result.plot()
                counts = {name: 0 for name in CLASS_NAMES}
                if result.boxes is not None:
                    for class_id in result.boxes.cls.int().tolist():
                        name = result.names[class_id]
                        counts[name] = counts.get(name, 0) + 1

                now = time.perf_counter()
                fps = 1 / max(now - previous_frame_time, 1e-6)
                previous_frame_time = now
                encoded_ok, encoded_frame = cv2.imencode(
                    ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82]
                )
                if not encoded_ok:
                    raise RuntimeError("แปลงภาพจากกล้องเป็น JPEG ไม่สำเร็จ")
                with self._lock:
                    if not self._running:
                        break
                    self._jpeg = encoded_frame.tobytes()
                    self._state.update(
                        {
                            "running": True,
                            "counts": counts,
                            "total": sum(counts.values()),
                            "fps": round(fps, 1),
                            "error": None,
                            "updated_at": time.time(),
                        }
                    )
        except Exception as error:
            with self._lock:
                self._state["error"] = str(error)
                self._running = False
        finally:
            with self._lock:
                capture.release()
                if self._capture is capture:
                    self._capture = None
                self._state["running"] = False
                self._state["camera_index"] = None


class ArduinoSerial:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connection: Any = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._state: dict[str, Any] = {
            "connected": False,
            "port": None,
            "distance_cm": None,
            "ultrasonic_last_seen": None,
            "object_present": None,
            "ir_last_seen": None,
            "relay": None,
            "last_seen": None,
            "error": None,
        }

    def ports(self) -> list[dict[str, str]]:
        try:
            from serial.tools import list_ports
        except ImportError as error:
            raise RuntimeError(
                "ยังไม่ได้ติดตั้ง pyserial กรุณารัน pip install -r requirements.txt"
            ) from error
        return [
            {"device": port.device, "description": port.description}
            for port in list_ports.comports()
        ]

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        now = time.time()
        state["ir_ready"] = self._is_sensor_ready(
            state["connected"], state["ir_last_seen"], now
        )
        state["ultrasonic_ready"] = self._is_sensor_ready(
            state["connected"], state["ultrasonic_last_seen"], now
        )
        return state

    @staticmethod
    def _is_sensor_ready(connected: bool, last_seen: float | None, now: float) -> bool:
        return (
            connected
            and last_seen is not None
            and 0 <= now - last_seen <= SENSOR_TIMEOUT_SECONDS
        )

    def connect(self, port: str, baudrate: int) -> None:
        self.disconnect()
        try:
            if port not in {item["device"] for item in self.ports()}:
                raise ValueError(f"ไม่พบพอร์ต Serial {port}")
            import serial

            connection = serial.Serial(
                port, baudrate=baudrate, timeout=0.2, write_timeout=1
            )
        except ImportError as error:
            raise RuntimeError(
                "ยังไม่ได้ติดตั้ง pyserial กรุณารัน pip install -r requirements.txt"
            ) from error
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        except Exception as error:
            raise RuntimeError(f"เชื่อมต่อ Arduino ที่ {port} ไม่สำเร็จ: {error}") from error

        with self._lock:
            self._connection = connection
            self._running = True
            self._state.update(
                {
                    "connected": True,
                    "port": port,
                    "distance_cm": None,
                    "ultrasonic_last_seen": None,
                    "object_present": None,
                    "ir_last_seen": None,
                    "relay": None,
                    "last_seen": None,
                    "error": None,
                }
            )
            self._worker = threading.Thread(
                target=self._read_messages, name="arduino-serial", daemon=True
            )
            self._worker.start()

    def disconnect(self) -> None:
        with self._lock:
            self._running = False
            worker = self._worker
            connection = self._connection
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
        with self._lock:
            if connection is not None:
                connection.close()
            self._connection = None
            self._worker = None
            self._state.update(
                {
                    "connected": False,
                    "port": None,
                    "distance_cm": None,
                    "ultrasonic_last_seen": None,
                    "object_present": None,
                    "ir_last_seen": None,
                    "relay": None,
                    "last_seen": None,
                }
            )

    def set_relay(self, enabled: bool) -> None:
        with self._lock:
            connection = self._connection
            if not self._state["connected"] or connection is None:
                raise RuntimeError("กรุณาเชื่อมต่อ Arduino ก่อนสั่งรีเลย์")
            message = json.dumps(
                {"command": "relay", "value": enabled}, separators=(",", ":")
            )
            try:
                connection.write((message + "\n").encode("utf-8"))
            except Exception as error:
                self._state["error"] = f"ส่งคำสั่งไป Arduino ไม่สำเร็จ: {error}"
                raise RuntimeError(self._state["error"]) from error

    def _read_messages(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                connection = self._connection
            if connection is None:
                return
            try:
                raw_message = connection.readline()
                if not raw_message:
                    continue
                message = json.loads(raw_message.decode("utf-8").strip())
                if not isinstance(message, dict):
                    raise ValueError("ข้อมูลจาก Arduino ต้องเป็น JSON object")

                with self._lock:
                    distance = message.get("distance_cm")
                    if (
                        isinstance(distance, (int, float))
                        and not isinstance(distance, bool)
                        and math.isfinite(distance)
                        and distance >= 0
                    ):
                        self._state["distance_cm"] = round(float(distance), 1)
                        self._state["ultrasonic_last_seen"] = time.time()
                    object_present = message.get(
                        "object_present", message.get("ir_detected")
                    )
                    if isinstance(object_present, bool):
                        self._state["object_present"] = object_present
                        self._state["ir_last_seen"] = time.time()
                    relay = message.get("relay", message.get("relay_state"))
                    if isinstance(relay, bool):
                        self._state["relay"] = relay
                    self._state["last_seen"] = time.time()
                    self._state["error"] = None
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                with self._lock:
                    self._state["error"] = f"อ่านข้อมูลจาก Arduino ไม่ได้: {error}"
            except Exception as error:
                with self._lock:
                    if self._running:
                        self._state["error"] = f"การเชื่อมต่อ Serial มีปัญหา: {error}"
                        self._state["connected"] = False
                        self._running = False
                return


camera = CameraService()
arduino = ArduinoSerial()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    camera.stop()
    arduino.disconnect()


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
        raise HTTPException(
            status_code=500, detail=f"เริ่มกล้องไม่ได้: {error}"
        ) from error
    return camera.state()


@app.post("/api/camera/stop")
def stop_camera() -> dict[str, Any]:
    camera.stop()
    return camera.state()


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


@app.get("/api/arduino/ports")
def arduino_ports() -> dict[str, Any]:
    try:
        return {"ports": arduino.ports()}
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/arduino/connect")
def connect_arduino(request: SerialConnectRequest) -> dict[str, Any]:
    try:
        arduino.connect(request.port, request.baudrate)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return arduino.state()


@app.post("/api/arduino/disconnect")
def disconnect_arduino() -> dict[str, Any]:
    arduino.disconnect()
    return arduino.state()


@app.post("/api/arduino/relay")
def set_relay(request: RelayRequest) -> dict[str, Any]:
    try:
        arduino.set_relay(request.enabled)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"sent": True, "enabled": request.enabled}
