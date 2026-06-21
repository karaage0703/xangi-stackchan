"""ダンスデモ用の DanceLoop と run_demo。

xangi SSE 経路の TalkingSway (ランダム揺れ ±数°) と独立した、
BPM 駆動の連続パターン首振りを提供する。`settings_server` 経由の
`POST /api/demo` と CLI `scripts/dance_demo.py` の両方から使う。
"""

from __future__ import annotations

import io
import math
import os
import sys
import threading
import time
import wave
from typing import TypedDict

from .app_types import BridgeConfig
from .tts import PiperProcess, downsample_wav, split_text, voicevox_synthesize


class DancePattern(TypedDict):
    bpm: float
    yaw_amp: float
    pitch_amp: float
    yaw_period_beats: float
    pitch_period_beats: float
    pitch_offset: float


# yaw / pitch はファーム SAFE 範囲 (yaw ±100°, pitch ±30°) の内側で抑える。
PRESETS: dict[str, DancePattern] = {
    "happy": {
        "bpm": 120.0,
        "yaw_amp": 20.0,
        "pitch_amp": 5.0,
        "yaw_period_beats": 2.0,
        "pitch_period_beats": 1.0,
        "pitch_offset": 2.0,
    },
    "chill": {
        "bpm": 70.0,
        "yaw_amp": 10.0,
        "pitch_amp": 2.0,
        "yaw_period_beats": 4.0,
        "pitch_period_beats": 3.0,
        "pitch_offset": 0.0,
    },
    "wave": {
        "bpm": 100.0,
        "yaw_amp": 15.0,
        "pitch_amp": 5.0,
        "yaw_period_beats": 2.0,
        "pitch_period_beats": 1.0,
        "pitch_offset": 0.0,
    },
}


def wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return frames / rate if rate else 0.0
    except (wave.Error, EOFError):
        return 0.0


def resolve_pattern(preset_name: str, bpm_override: float | None = None) -> DancePattern:
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset: {preset_name}; choices={sorted(PRESETS)}")
    pattern: DancePattern = dict(PRESETS[preset_name])  # type: ignore[assignment]
    if bpm_override is not None:
        pattern["bpm"] = float(bpm_override)
    return pattern


class DanceLoop:
    """BPM 駆動の連続 sin/cos パターンで首を振り続けるスレッド管理。"""

    def __init__(
        self,
        backend,
        pattern: DancePattern,
        idle_yaw: float,
        idle_pitch: float,
        send_interval_factor: float = 0.5,
    ):
        self._backend = backend
        self._pattern = pattern
        self._idle_yaw = idle_yaw
        self._idle_pitch = idle_pitch
        self._send_interval = max(0.05, 60.0 / float(pattern["bpm"]) * send_interval_factor)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: list[float | None] = [None, None]

    def __enter__(self) -> "DanceLoop":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._send_move(self._idle_yaw, self._idle_pitch)

    def _run(self) -> None:
        bpm = float(self._pattern["bpm"])
        yaw_amp = float(self._pattern["yaw_amp"])
        pitch_amp = float(self._pattern["pitch_amp"])
        yaw_period = float(self._pattern["yaw_period_beats"]) * 60.0 / bpm
        pitch_period = float(self._pattern["pitch_period_beats"]) * 60.0 / bpm
        pitch_offset = float(self._pattern["pitch_offset"])
        t0 = time.time()
        while not self._stop.is_set():
            t = time.time() - t0
            yaw = yaw_amp * math.sin(2 * math.pi * t / yaw_period) + self._idle_yaw
            pitch = (
                pitch_amp * math.sin(2 * math.pi * t / pitch_period)
                + pitch_offset
                + self._idle_pitch
            )
            self._send_move(yaw, pitch)
            if self._stop.wait(self._send_interval):
                break

    def _send_move(self, yaw: float, pitch: float) -> None:
        # 0.5° 未満の差分は冗長 (app.set_move_if_needed と同じしきい値)
        if (
            self._current[0] is not None
            and self._current[1] is not None
            and abs(self._current[0] - yaw) < 0.5
            and abs(self._current[1] - pitch) < 0.5
        ):
            return
        if not _backend_ready(self._backend):
            return
        try:
            self._backend.send_command(f"MOVE:{yaw:.1f},{pitch:.1f}")
        except Exception as exc:
            print(f"[dance] MOVE error: {exc}", file=sys.stderr, flush=True)
            return
        self._current[0] = yaw
        self._current[1] = pitch


def synthesize_text(
    text: str,
    config: BridgeConfig,
    piper_process: PiperProcess | None,
) -> list[tuple[str, bytes]]:
    chunks = split_text(text, max_len=12)
    if config.tts == "piper":
        if not piper_process:
            raise RuntimeError("piper process is not initialized")
        wavs = piper_process.synthesize_many(chunks)
        downsample_factor = int(os.environ.get("STACKCHAN_TTS_DOWNSAMPLE", "2"))
        if downsample_factor > 1:
            wavs = [downsample_wav(wav, downsample_factor) for wav in wavs]
        return list(zip(chunks, wavs))
    if config.tts == "voicevox":
        return [
            (chunk, voicevox_synthesize(chunk, config.voicevox_url, config.voicevox_speaker))
            for chunk in chunks
        ]
    raise RuntimeError(f"dance demo requires TTS (got tts={config.tts})")


STATUS_LIGHTS = (
    ("PUZZLE", "puzzle"),
    ("STACKLED", "stack_led"),
)

DEMO_SPRITE_RESUME_DELAY_SECONDS = 0.35
DEMO_MOTION_START_DELAY_SECONDS = 0.25


def _backend_ready(backend) -> bool:
    connected = getattr(backend, "is_connected", True)
    if isinstance(connected, bool) and not connected:
        return False
    if hasattr(backend, "actor") and getattr(backend, "actor") is None:
        return False
    return True


def _status_lights_supported(backend, config: BridgeConfig) -> list[str]:
    if not config.puzzle_light_enabled:
        return []
    if not _backend_ready(backend):
        return []
    try:
        status = backend.send_command("STATUS")
    except Exception:
        return []
    return [
        command
        for command, status_key in STATUS_LIGHTS
        if status.get(status_key)
    ]


def _send_status_lights(backend, commands: list[str], pattern: str) -> None:
    if not pattern:
        return
    if not _backend_ready(backend):
        return
    for command in commands:
        try:
            result = backend.send_command(f"{command}:{pattern}")
            print(f"[dance] {command}:{pattern} -> {result}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[dance] {command}:{pattern} error: {exc}", file=sys.stderr, flush=True)


def _send_demo_face(backend, config: BridgeConfig, expression: str) -> None:
    if not expression:
        return
    if (config.face_mode or "avatar").strip().lower() == "sprite":
        return
    if not _backend_ready(backend):
        return
    try:
        backend.send_command(f"FACE:{expression}")
    except Exception:
        pass


def run_demo(
    backend,
    piper_process: PiperProcess | None,
    config: BridgeConfig,
    text: str,
    preset_name: str = "happy",
    bpm_override: float | None = None,
    idle_yaw: float | None = None,
    idle_pitch: float | None = None,
    face: str | None = None,
    before_wav_send=None,
    after_wav_send=None,
) -> dict[str, object]:
    """Run dance demo synchronously: TTS → send_wav loop wrapped in DanceLoop.

    backend / piper_process / config は run_bridge と共有可。
    backend.send_command / send_wav は RLock で直列化されているので、
    同時に xangi SSE 発話が来てもキューイングされて壊れない。
    """
    text = (text or "").strip()
    if not text:
        return {"status": "error", "error": "empty text"}

    pattern = resolve_pattern(preset_name, bpm_override)
    base_yaw = config.move_idle_yaw if idle_yaw is None else idle_yaw
    base_pitch = config.move_idle_pitch if idle_pitch is None else idle_pitch
    face_used = face if face is not None else config.face_talking

    status_lights = _status_lights_supported(backend, config)
    if status_lights:
        _send_status_lights(backend, status_lights, config.puzzle_thinking)

    try:
        synthesized = synthesize_text(text, config, piper_process)
        total_audio_seconds = sum(wav_duration_seconds(wav) for _, wav in synthesized)
        total_bytes = sum(len(wav) for _, wav in synthesized)

        if status_lights:
            _send_status_lights(backend, status_lights, config.puzzle_talking)

        _send_demo_face(backend, config, face_used)

        chunk_results: list[dict[str, object]] = []
        send_seconds_last = 0.0
        remaining = 0.0
        for idx, (chunk, wav) in enumerate(synthesized, start=1):
            started = time.time()
            try:
                if before_wav_send is not None:
                    before_wav_send()
                result = backend.send_wav(
                    wav,
                    chunk_size=config.serial_chunk,
                    chunk_delay=config.serial_delay,
                )
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
            send_seconds_last = time.time() - started
            chunk_audio_seconds = wav_duration_seconds(wav)
            chunk_results.append(
                {
                    "chunk": idx,
                    "text": chunk,
                    "bytes": len(wav),
                    "audio_seconds": round(chunk_audio_seconds, 2),
                    "send_seconds": round(send_seconds_last, 2),
                    "result": result,
                }
            )
            remaining += max(0.0, chunk_audio_seconds - send_seconds_last)

        # WAV のシリアル転送が終わってから少し待って sprite / MOVE を戻す。
        # 再生開始直後は CoreS3 側の音声処理と描画・サーボ開始が重なると
        # 一瞬だけ表示ノイズが出やすいので、発話の立ち上がりを避ける。
        if after_wav_send is not None:
            if remaining > 0.0:
                resume_delay = min(DEMO_SPRITE_RESUME_DELAY_SECONDS, remaining)
                time.sleep(resume_delay)
                remaining = max(0.0, remaining - resume_delay)
            after_wav_send()

        if remaining > 0.0:
            motion_delay = min(DEMO_MOTION_START_DELAY_SECONDS, remaining)
            if motion_delay > 0.0:
                time.sleep(motion_delay)
                remaining = max(0.0, remaining - motion_delay)
            with DanceLoop(backend, pattern, base_yaw, base_pitch):
                time.sleep(remaining + 0.3)
        else:
            time.sleep(0.3)

        if face_used:
            _send_demo_face(backend, config, config.face_idle)
    finally:
        if status_lights:
            _send_status_lights(backend, status_lights, config.puzzle_idle)

    return {
        "status": "ok",
        "preset": preset_name,
        "bpm": pattern["bpm"],
        "chunks": chunk_results,
        "total_bytes": total_bytes,
        "total_audio_seconds": round(total_audio_seconds, 2),
    }
