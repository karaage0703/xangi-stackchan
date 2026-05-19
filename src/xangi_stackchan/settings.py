import json
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from .app_types import BridgeConfig
from .stackchan import StackchanConfig


DEFAULT_CONFIG_PATH = Path.home() / ".xangi" / "xangi-stackchan" / "config.json"


def load_config_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_config_dict(path: Path, data: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def config_to_dict(config: BridgeConfig) -> dict[str, Any]:
    return {
        "xangi_url": config.xangi_url,
        "thread_id": config.thread_id or "",
        "wifi": config.stackchan.wifi,
        "host": config.stackchan.host,
        "port": config.stackchan.port,
        "baud": config.stackchan.baud,
        "volume": config.volume,
        "tts": config.tts,
        "piper_bin": config.piper_bin,
        "piper_model": config.piper_model,
        "piper_speaker": config.piper_speaker,
        "voicevox_url": config.voicevox_url,
        "voicevox_speaker": config.voicevox_speaker,
        "serial_chunk": config.serial_chunk,
        "serial_delay": config.serial_delay,
        "stackchan_retry_seconds": config.stackchan_retry_seconds,
        "face_idle": config.face_idle,
        "face_thinking": config.face_thinking,
        "face_talking": config.face_talking,
        "face_error": config.face_error,
        "stream_timeout": config.stream_timeout,
        "retry_seconds": config.retry_seconds,
        "max_retry_seconds": config.max_retry_seconds,
        "move_enabled": config.move_enabled,
        "move_idle_yaw": config.move_idle_yaw,
        "move_idle_pitch": config.move_idle_pitch,
        "move_thinking_yaw": config.move_thinking_yaw,
        "move_thinking_pitch": config.move_thinking_pitch,
        "move_error_yaw": config.move_error_yaw,
        "move_error_pitch": config.move_error_pitch,
        "move_talking_sway_yaw": config.move_talking_sway_yaw,
        "move_talking_sway_pitch": config.move_talking_sway_pitch,
        "move_talking_sway_interval": config.move_talking_sway_interval,
    }


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _none_if_empty(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _int_or(value: Any, fallback: int) -> int:
    if value is None or str(value).strip() == "":
        return fallback
    return int(value)


def _float_or(value: Any, fallback: float) -> float:
    if value is None or str(value).strip() == "":
        return fallback
    return float(value)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def merge_config(base: BridgeConfig, data: dict[str, Any]) -> BridgeConfig:
    stackchan = replace(
        base.stackchan,
        wifi=_bool(data.get("wifi", base.stackchan.wifi)),
        host=str(data.get("host", base.stackchan.host)),
        port=str(data.get("port", base.stackchan.port)),
        baud=_int_or(data.get("baud"), base.stackchan.baud),
    )
    return replace(
        base,
        xangi_url=str(data.get("xangi_url", base.xangi_url)),
        thread_id=_none_if_empty(data.get("thread_id", base.thread_id or "")),
        stackchan=stackchan,
        volume=_clamp(_int_or(data.get("volume"), base.volume), 0, 255),
        tts=str(data.get("tts", base.tts)),
        piper_bin=str(data.get("piper_bin", base.piper_bin)),
        piper_model=str(data.get("piper_model", base.piper_model)),
        piper_speaker=_int_or(data.get("piper_speaker"), base.piper_speaker),
        voicevox_url=str(data.get("voicevox_url", base.voicevox_url)),
        voicevox_speaker=_int_or(data.get("voicevox_speaker"), base.voicevox_speaker),
        serial_chunk=_int_or(data.get("serial_chunk"), base.serial_chunk),
        serial_delay=_float_or(data.get("serial_delay"), base.serial_delay),
        stackchan_retry_seconds=_float_or(
            data.get("stackchan_retry_seconds"), base.stackchan_retry_seconds
        ),
        face_idle=str(data.get("face_idle", base.face_idle)),
        face_thinking=str(data.get("face_thinking", base.face_thinking)),
        face_talking=str(data.get("face_talking", base.face_talking)),
        face_error=str(data.get("face_error", base.face_error)),
        stream_timeout=_int_or(data.get("stream_timeout"), base.stream_timeout),
        retry_seconds=_float_or(data.get("retry_seconds"), base.retry_seconds),
        max_retry_seconds=_float_or(data.get("max_retry_seconds"), base.max_retry_seconds),
        move_enabled=_bool(data.get("move_enabled", base.move_enabled)),
        move_idle_yaw=_float_or(data.get("move_idle_yaw"), base.move_idle_yaw),
        move_idle_pitch=_float_or(data.get("move_idle_pitch"), base.move_idle_pitch),
        move_thinking_yaw=_float_or(data.get("move_thinking_yaw"), base.move_thinking_yaw),
        move_thinking_pitch=_float_or(data.get("move_thinking_pitch"), base.move_thinking_pitch),
        move_error_yaw=_float_or(data.get("move_error_yaw"), base.move_error_yaw),
        move_error_pitch=_float_or(data.get("move_error_pitch"), base.move_error_pitch),
        move_talking_sway_yaw=_float_or(
            data.get("move_talking_sway_yaw"), base.move_talking_sway_yaw
        ),
        move_talking_sway_pitch=_float_or(
            data.get("move_talking_sway_pitch"), base.move_talking_sway_pitch
        ),
        move_talking_sway_interval=_float_or(
            data.get("move_talking_sway_interval"), base.move_talking_sway_interval
        ),
    )


class RuntimeState:
    def __init__(self, config: BridgeConfig, config_path: Path):
        self._lock = Lock()
        self._config = config
        self._version = 0
        self.config_path = config_path
        # backend / piper_process は run_bridge が config 適用後に set_runtime で
        # 登録する。settings_server の `/api/demo` がそれを取って共有する。
        # run_bridge 専有ではなく Lock 経由で共有するので、send_command /
        # send_wav 側の RLock と組み合わせて同時アクセスを直列化する。
        self._backend: object | None = None
        self._piper_process: object | None = None
        # Phase 1A: 直近の CAPTURE 結果メモリキャッシュ。`/api/camera/snapshot.jpg`
        # と `/api/camera/status` で参照する。
        # 形式: {"jpeg": bytes, "captured_at": float (host epoch),
        #        "width": W, "height": H, "size": N, "error": Optional[str],
        #        "captured_at_device_ms": Optional[int]}
        self._last_capture: dict[str, Any] | None = None

    def snapshot(self) -> tuple[BridgeConfig, int]:
        with self._lock:
            return self._config, self._version

    def set_runtime(self, backend: object | None, piper_process: object | None) -> None:
        with self._lock:
            self._backend = backend
            self._piper_process = piper_process

    def get_runtime(self) -> tuple[object | None, object | None]:
        with self._lock:
            return self._backend, self._piper_process

    def set_last_capture(self, capture: dict[str, Any] | None) -> None:
        with self._lock:
            self._last_capture = capture

    def get_last_capture(self) -> dict[str, Any] | None:
        with self._lock:
            return self._last_capture

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            data = config_to_dict(self._config)
            data["version"] = self._version
            data["config_path"] = str(self.config_path)
            return data

    def update(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._config = merge_config(self._config, data)
            self._version += 1
            saved = config_to_dict(self._config)
            save_config_dict(self.config_path, saved)
            saved["version"] = self._version
            saved["config_path"] = str(self.config_path)
            return saved
