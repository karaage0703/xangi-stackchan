import json
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from .app_types import BridgeConfig
from .stackchan import StackchanConfig


DEFAULT_CONFIG_PATH = Path.home() / ".xangi" / "xangi-stackchan" / "config.json"
DEFAULT_INSTANCE_ID = "default"
CONFIG_SCHEMA_VERSION = 2


# --- file-level helpers --------------------------------------------------


def load_config_file(path: Path) -> dict[str, Any]:
    """Read the on-disk config file in v2 schema.

    Returns ``{"version": 2, "instances": {...}}``. If the file does not
    exist, returns an empty v2 document. If the file is the legacy v1 flat
    schema, it is migrated to v2 in-memory (the migration is persisted only
    when something is saved back).
    """
    if not path.exists():
        return {"version": CONFIG_SCHEMA_VERSION, "instances": {}}
    raw = json.loads(path.read_text())
    return migrate_to_v2(raw)


def save_config_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def migrate_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Promote a v1 flat config to the v2 ``instances`` namespace.

    A v1 file is recognised by the absence of a ``version`` key (or
    ``version<2``). Its flat fields are wrapped into ``instances.default``.
    """
    if not isinstance(raw, dict):
        return {"version": CONFIG_SCHEMA_VERSION, "instances": {}}
    version = raw.get("version")
    if isinstance(version, int) and version >= CONFIG_SCHEMA_VERSION:
        instances = raw.get("instances")
        if not isinstance(instances, dict):
            instances = {}
        return {"version": CONFIG_SCHEMA_VERSION, "instances": dict(instances)}

    # v1 fallback: take every top-level key as a flat config for "default".
    flat = {k: v for k, v in raw.items() if k != "version"}
    instances: dict[str, Any] = {}
    if flat:
        instances[DEFAULT_INSTANCE_ID] = flat
    return {"version": CONFIG_SCHEMA_VERSION, "instances": instances}


def load_instance_dict(path: Path, instance_id: str) -> dict[str, Any]:
    """Return the per-instance dict, or {} if the instance is unknown."""
    doc = load_config_file(path)
    instances = doc.get("instances", {})
    data = instances.get(instance_id)
    return dict(data) if isinstance(data, dict) else {}


def save_instance_dict(path: Path, instance_id: str, data: dict[str, Any]) -> None:
    """Persist ``data`` into ``instances[instance_id]`` of the v2 file."""
    doc = load_config_file(path)
    instances = doc.setdefault("instances", {})
    instances[instance_id] = dict(data)
    doc["instances"] = instances
    doc["version"] = CONFIG_SCHEMA_VERSION
    save_config_file(path, doc)


# --- BridgeConfig <-> dict ----------------------------------------------


def config_to_dict(config: BridgeConfig) -> dict[str, Any]:
    return {
        "xangi_url": config.xangi_url,
        "thread_id": config.thread_id or "",
        "wifi": config.stackchan.wifi,
        "host": config.stackchan.host,
        "port": config.stackchan.port,
        "baud": config.stackchan.baud,
        "device_profile": config.stackchan.device_profile,
        "max_wav_bytes": config.stackchan.max_wav_bytes,
        "skip_move_during_wav": config.stackchan.skip_move_during_wav,
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
        "face_mode": config.face_mode,
        "sprite_sheet": config.sprite_sheet,
        "sprite_jpeg_quality": config.sprite_jpeg_quality,
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
        "voice_conversation": config.voice_conversation,
        "voice_app_session_id": config.voice_app_session_id,
        "voice_silence_dbfs": config.voice_silence_dbfs,
        "voice_silence_seconds": config.voice_silence_seconds,
        "voice_max_seconds": config.voice_max_seconds,
        "voice_initial_grace_seconds": config.voice_initial_grace_seconds,
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
        device_profile=str(data.get("device_profile", base.stackchan.device_profile)),
        max_wav_bytes=_int_or(data.get("max_wav_bytes"), base.stackchan.max_wav_bytes),
        skip_move_during_wav=_bool(data.get("skip_move_during_wav", base.stackchan.skip_move_during_wav)),
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
        face_mode=str(data.get("face_mode", base.face_mode)),
        sprite_sheet=str(data.get("sprite_sheet", base.sprite_sheet)),
        sprite_jpeg_quality=_clamp(
            _int_or(data.get("sprite_jpeg_quality"), base.sprite_jpeg_quality), 1, 95
        ),
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
        voice_conversation=_bool(data.get("voice_conversation", base.voice_conversation)),
        voice_app_session_id=str(
            data.get("voice_app_session_id", base.voice_app_session_id)
        ),
        voice_silence_dbfs=_float_or(
            data.get("voice_silence_dbfs"), base.voice_silence_dbfs
        ),
        voice_silence_seconds=_float_or(
            data.get("voice_silence_seconds"), base.voice_silence_seconds
        ),
        voice_max_seconds=_float_or(
            data.get("voice_max_seconds"), base.voice_max_seconds
        ),
        voice_initial_grace_seconds=_float_or(
            data.get("voice_initial_grace_seconds"), base.voice_initial_grace_seconds
        ),
    )


class RuntimeState:
    def __init__(
        self,
        config: BridgeConfig,
        config_path: Path,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        self._lock = Lock()
        self._config = config
        self._version = 0
        self.config_path = config_path
        self.instance_id = instance_id
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
        # Phase 2: 音声対話モードの coordinator (VoiceConversation インスタンス)。
        # run_bridge が config.voice_conversation=True + StackchanSerial backend のとき
        # set_voice_conversation で登録。`/api/voice/history` から history が読める。
        self._voice_conv: object | None = None

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

    def set_voice_conversation(self, voice_conv: object | None) -> None:
        with self._lock:
            self._voice_conv = voice_conv

    def get_voice_conversation(self) -> object | None:
        with self._lock:
            return self._voice_conv

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            data = config_to_dict(self._config)
            data["version"] = self._version
            data["config_path"] = str(self.config_path)
            data["instance_id"] = self.instance_id
            return data

    def update(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._config = merge_config(self._config, data)
            self._version += 1
            saved = config_to_dict(self._config)
            save_instance_dict(self.config_path, self.instance_id, saved)
            saved["version"] = self._version
            saved["config_path"] = str(self.config_path)
            saved["instance_id"] = self.instance_id
            return saved


# --- legacy compat aliases -----------------------------------------------
#
# These kept the previous flat-file behaviour. We keep thin shims so that
# callers (and tests) which still reach for ``load_config_dict`` keep
# working against the v2 file format. They always operate on the default
# instance — multi-instance code should use the explicit helpers above.


def load_config_dict(path: Path) -> dict[str, Any]:
    """Deprecated: return the ``default`` instance dict from a v2 file."""
    return load_instance_dict(path, DEFAULT_INSTANCE_ID)


def save_config_dict(path: Path, data: dict[str, Any]) -> None:
    """Deprecated: persist ``data`` into the ``default`` instance."""
    save_instance_dict(path, DEFAULT_INSTANCE_ID, data)
