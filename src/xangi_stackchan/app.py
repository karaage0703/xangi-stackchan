import argparse
import faulthandler
import json
import os
import random
import signal
import threading
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue

import requests

from .app_types import BridgeConfig
from .events import iter_xangi_events, normalize_xangi_stream_url
from .settings import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_INSTANCE_ID,
    RuntimeState,
    load_instance_dict,
    merge_config,
)
from .settings_server import DEFAULT_SETTINGS_PORT, start_settings_server
from .sprite_face import SPRITE_FPS, SpriteFaceRenderer
from .stackchan import DEFAULT_BAUD, DEFAULT_WIFI_HOST, StackchanConfig, StackchanSerial, apply_profile_defaults, create_backend
from .voice_conversation import VoiceConversation
from .head_pet import HeadPetReaction
from .tts import (
    DEFAULT_PIPER_BIN,
    DEFAULT_PIPER_MODEL,
    DEFAULT_TTS,
    DEFAULT_VOICEVOX_SPEAKER,
    DEFAULT_VOICEVOX_URL,
    PiperProcess,
    split_text,
    voicevox_synthesize,
    downsample_wav,
)


DEFAULT_XANGI_URL = "http://127.0.0.1:18888"
REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigChanged(Exception):
    pass


def log(payload: dict):
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


STATUS_LIGHTS = (
    ("PUZZLE", "puzzle", "puzzle_pattern"),
    ("STACKLED", "stack_led", "stack_led_pattern"),
)


def set_face_if_needed(backend, expression: str, current_face: list[str | None]):
    if not expression or current_face[0] == expression:
        return True
    try:
        result = backend.send_command(f"FACE:{expression}")
    except Exception as exc:
        log({"face": expression, "error": str(exc)})
        return False
    current_face[0] = expression
    log({"face": expression, "result": result})
    return True


def detect_puzzle_light_support(backend, config: BridgeConfig) -> dict[str, bool]:
    if not config.puzzle_light_enabled:
        return {}
    try:
        status = backend.send_command("STATUS")
    except Exception as exc:
        log({"puzzle": "status_error", "error": str(exc)})
        return {}
    supported = {
        command: bool(status.get(status_key))
        for command, status_key, _pattern_key in STATUS_LIGHTS
    }
    log({
        "status_lights": [
            command for command, is_supported in supported.items() if is_supported
        ],
        "patterns": {
            command: status.get(pattern_key)
            for command, _status_key, pattern_key in STATUS_LIGHTS
        },
    })
    return supported


def set_puzzle_light_if_needed(
    backend,
    config: BridgeConfig,
    pattern: str,
    current_puzzle: list[dict[str, str] | None],
    puzzle_supported: list[dict[str, bool]],
):
    supported = puzzle_supported[0] if puzzle_supported else {}
    if not config.puzzle_light_enabled or not supported or not pattern:
        return True
    current = current_puzzle[0] or {}
    ok = True
    for command, is_supported in supported.items():
        if not is_supported or current.get(command) == pattern:
            continue
        try:
            result = backend.send_command(f"{command}:{pattern}")
        except Exception as exc:
            log({"status_light": command, "pattern": pattern, "error": str(exc)})
            ok = False
            continue
        if isinstance(result, dict) and result.get("status") != "ok":
            log({"status_light": command, "pattern": pattern, "result": result})
            ok = False
            continue
        current[command] = pattern
        log({"status_light": command, "pattern": pattern, "result": result})
    current_puzzle[0] = current
    return ok


def _apply_head_touch_firmware_settings(
    backend,
    *,
    suppress_head_touch_avatar: bool,
    suppress_head_pet_sound: bool,
):
    """Apply firmware-side head touch feedback settings explicitly.

    These flags are sticky on the device.  If a previous voice-conversation
    run sent them off, switching back to LCD mic / normal pet mode must send
    them on again instead of relying on defaults.
    """
    result = {}
    try:
        result["head_touch_avatar"] = backend.send_command(
            f"HEADTOUCH_AVATAR:{'off' if suppress_head_touch_avatar else 'on'}"
        )
    except Exception as exc:
        result["head_touch_avatar_error"] = str(exc)
    try:
        result["head_pet_sound"] = backend.send_command(
            f"HEADPET_SOUND:{'off' if suppress_head_pet_sound else 'on'}"
        )
    except Exception as exc:
        result["head_pet_sound_error"] = str(exc)
    log({
        "head_touch_firmware": {
            "suppress_head_touch_avatar": suppress_head_touch_avatar,
            "suppress_head_pet_sound": suppress_head_pet_sound,
            "result": result,
        }
    })
    return result


def _resolve_repo_path(path: str) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _sprite_available(config: BridgeConfig, backend) -> bool:
    """sprite 顔モード (キャラ画像) が実際に使えるか。3 条件: face_mode=sprite /
    backend が IMAGE 対応 / spritesheet ファイルが存在。1 つでも欠ければ False を返し、
    呼び出し側は avatar (スタックチャン顔) にフォールバックする。spritesheet は
    .gitignore 対象なので、未配置環境では自動で avatar になる (= 既定の挙動)。"""
    if (config.face_mode or "avatar").strip().lower() != "sprite":
        return False
    if not hasattr(backend, "send_image"):
        return False
    try:
        return _resolve_repo_path(config.sprite_sheet).exists()
    except Exception:
        return False


def _get_sprite_renderer(config: BridgeConfig, sprite_renderer: list[SpriteFaceRenderer | None]) -> SpriteFaceRenderer:
    renderer = sprite_renderer[0]
    sheet_path = _resolve_repo_path(config.sprite_sheet)
    if (
        renderer is None
        or renderer.sheet_path != sheet_path
        or renderer.quality != config.sprite_jpeg_quality
    ):
        renderer = SpriteFaceRenderer(sheet_path, config.sprite_jpeg_quality)
        sprite_renderer[0] = renderer
    # lcd_mic_voice 時は各フレーム下部にマイクボタンを合成する (config 変更にも追従)。
    renderer.show_mic_button = config.lcd_mic_voice
    return renderer


def _send_sprite_frame(
    backend,
    config: BridgeConfig,
    expression: str,
    step: int,
    current_face: list[str | None],
    sprite_renderer: list[SpriteFaceRenderer | None],
):
    renderer = _get_sprite_renderer(config, sprite_renderer)
    key = f"sprite:{expression}:{step}:{config.sprite_sheet}:{config.sprite_jpeg_quality}"
    if current_face[0] == key:
        return True

    image = renderer.render_expression_frame(expression, step)
    result = backend.send_image(image, chunk_size=config.serial_chunk, chunk_delay=config.serial_delay)
    log({"face": expression, "mode": "sprite", "step": step, "bytes": len(image), "result": result})
    if result.get("status") == "ok":
        current_face[0] = key
        return True
    return False


class SpriteAnimationLoop:
    def __init__(self, backend, config: BridgeConfig, current_face: list[str | None], sprite_renderer: list[SpriteFaceRenderer | None]):
        self.backend = backend
        self.config = config
        self.current_face = current_face
        self.sprite_renderer = sprite_renderer
        self._expression = config.face_idle
        self._step = 0
        self._pause_until = 0.0
        self._manual_pause = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sprite-face-animation", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)

    def pause(self):
        with self._lock:
            self._manual_pause = True

    def resume(self):
        with self._lock:
            self._manual_pause = False

    def set_expression(self, expression: str):
        if not expression:
            return
        with self._lock:
            if self._expression != expression:
                self._expression = expression
                self._step = 0
            expression_now = self._expression
            step_now = self._step
        try:
            if not _send_sprite_frame(self.backend, self.config, expression_now, step_now, self.current_face, self.sprite_renderer):
                self._pause_until = time.time() + 5.0
        except Exception as exc:
            self._pause_until = time.time() + 5.0
            log({"face": expression_now, "mode": "sprite", "error": str(exc)})

    def _run(self):
        if SPRITE_FPS <= 0:
            return
        interval = 1.0 / SPRITE_FPS
        while not self._stop.wait(interval):
            if getattr(self.backend, "_mic_recording", False):
                continue
            if time.time() < self._pause_until:
                continue
            with self._lock:
                if self._manual_pause:
                    continue
                self._step += 1
                expression_now = self._expression
                step_now = self._step
            try:
                if not _send_sprite_frame(self.backend, self.config, expression_now, step_now, self.current_face, self.sprite_renderer):
                    self._pause_until = time.time() + 5.0
            except Exception as exc:
                self._pause_until = time.time() + 5.0
                log({"face": expression_now, "mode": "sprite", "error": str(exc)})


def set_visual_face_if_needed(
    backend,
    config: BridgeConfig,
    expression: str,
    current_face: list[str | None],
    sprite_renderer: list[SpriteFaceRenderer | None],
    sprite_animator: SpriteAnimationLoop | None = None,
):
    if not _sprite_available(config, backend):
        # sprite 不可 (avatar 指定 / IMAGE 非対応 / spritesheet 未配置) は
        # スタックチャン顔 (avatar) にフォールバック。
        return set_face_if_needed(backend, expression, current_face)

    if sprite_animator is not None:
        sprite_animator.set_expression(expression)
        return True
    try:
        return _send_sprite_frame(backend, config, expression, 0, current_face, sprite_renderer)
    except Exception as exc:
        log({"face": expression, "mode": "sprite", "error": str(exc)})
        return False


def set_move_if_needed(backend, yaw: float, pitch: float, current_move: list[float | None]):
    """Send MOVE:<yaw,pitch> only when target differs from last sent value.

    `current_move` carries the last sent [yaw, pitch] across calls so that
    redundant commands are skipped. Differences smaller than 0.5° are treated
    as the same target to avoid jitter when the talking sway loop samples a
    value very close to the previous one.
    """
    if (
        current_move[0] is not None
        and current_move[1] is not None
        and abs(current_move[0] - yaw) < 0.5
        and abs(current_move[1] - pitch) < 0.5
    ):
        return True
    try:
        result = backend.send_command(f"MOVE:{yaw:.1f},{pitch:.1f}")
    except Exception as exc:
        log({"move": [yaw, pitch], "error": str(exc)})
        return False
    current_move[0] = yaw
    current_move[1] = pitch
    log({"move": [yaw, pitch], "result": result})
    return True


class TalkingSway:
    """Context manager that wiggles the head while WAV playback runs.

    Picks a random offset within ±sway around the base pose every `interval`
    seconds, posts MOVE, and on exit returns the head to the base pose.
    """

    def __init__(
        self,
        backend,
        base_yaw: float,
        base_pitch: float,
        sway_yaw: float,
        sway_pitch: float,
        interval: float,
        current_move: list[float | None],
    ):
        self._backend = backend
        self._base_yaw = base_yaw
        self._base_pitch = base_pitch
        self._sway_yaw = max(0.0, sway_yaw)
        self._sway_pitch = max(0.0, sway_pitch)
        self._interval = max(0.2, interval)
        self._current_move = current_move
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self._sway_yaw == 0.0 and self._sway_pitch == 0.0:
            return self
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        set_move_if_needed(self._backend, self._base_yaw, self._base_pitch, self._current_move)

    def _run(self):
        assert self._stop is not None
        while not self._stop.is_set():
            yaw = self._base_yaw + random.uniform(-self._sway_yaw, self._sway_yaw)
            pitch = self._base_pitch + random.uniform(-self._sway_pitch, self._sway_pitch)
            set_move_if_needed(self._backend, yaw, pitch, self._current_move)
            if self._stop.wait(self._interval):
                break


def set_volume(backend, volume: int):
    try:
        result = backend.send_command(f"VOLUME:{volume}")
    except Exception as exc:
        log({"volume": volume, "error": str(exc)})
        return False
    log({"volume": volume, "result": result})
    return True


def synthesize_chunks(chunks: list[str], config: BridgeConfig, piper_process: PiperProcess | None):
    if config.tts == "none":
        return
    if config.tts == "piper":
        if not piper_process:
            raise RuntimeError("piper process is not initialized")
        started = time.time()
        wavs = piper_process.synthesize_many(chunks)
        tts_time = time.time() - started
        for idx, (chunk, wav) in enumerate(zip(chunks, wavs), start=1):
            yield idx, chunk, wav, tts_time if idx == 1 else 0.0
        return

    for idx, chunk in enumerate(chunks, start=1):
        started = time.time()
        wav = voicevox_synthesize(chunk, config.voicevox_url, config.voicevox_speaker)
        yield idx, chunk, wav, time.time() - started


def speak_text(backend, text: str, config: BridgeConfig, piper_process: PiperProcess | None):
    text = (text or "").strip()
    if not text or config.tts == "none":
        return

    chunks = split_text(text)
    log({"speaking_chunks": len(chunks)})
    wav_queue: Queue = Queue(maxsize=4)

    def tts_worker():
        try:
            for item in synthesize_chunks(chunks, config, piper_process):
                wav_queue.put(item)
        except Exception as exc:
            wav_queue.put({"error": str(exc)})
        finally:
            wav_queue.put(None)

    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(tts_worker)

    while True:
        item = wav_queue.get()
        if item is None:
            break
        if isinstance(item, dict) and "error" in item:
            log({"tts_error": item["error"]})
            break
        idx, chunk, wav, tts_time = item
        # 送信前にダウンサンプルして USB シリアル転送量を削減 (会話レイテンシ対策)。
        # STACKCHAN_TTS_DOWNSAMPLE=2 で 22050→11025Hz (約半分)。1 で無効。
        downsample_factor = int(os.environ.get("STACKCHAN_TTS_DOWNSAMPLE", "2"))
        if downsample_factor > 1:
            wav = downsample_wav(wav, downsample_factor)
        started = time.time()
        try:
            result = backend.send_wav(wav, chunk_size=config.serial_chunk, chunk_delay=config.serial_delay)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        log(
            {
                "chunk": idx,
                "chunks": len(chunks),
                "text": chunk,
                "tts_seconds": round(tts_time, 2),
                "send_seconds": round(time.time() - started, 2),
                "bytes": len(wav),
                "result": result,
            }
        )

    executor.shutdown(wait=False)


def open_backend_with_retry(config: BridgeConfig):
    while True:
        apply_profile_defaults(config.stackchan)
        backend = create_backend(config.stackchan)
        try:
            backend.open()
            log({"stackchan": "connected", "wifi": config.stackchan.wifi})
            return backend
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(
                {
                    "stackchan": "connect_error",
                    "error": str(exc),
                    "retry_seconds": config.stackchan_retry_seconds,
                }
            )
            time.sleep(config.stackchan_retry_seconds)


def should_handle_event(event: dict, config: BridgeConfig) -> bool:
    if config.thread_id and event.get("thread_id") != config.thread_id:
        return False
    if config.speak_platforms and event.get("platform") not in config.speak_platforms:
        return False
    return True


def close_runtime(
    backend,
    piper_process,
    current_face,
    current_move,
    config,
    voice_conv=None,
    state=None,
    sprite_animator=None,
    current_puzzle=None,
    puzzle_supported=None,
):
    try:
        if sprite_animator is not None:
            try:
                sprite_animator.stop()
            except Exception:
                pass
        if voice_conv is not None:
            try:
                voice_conv.stop()
            except Exception:
                pass
        if state is not None:
            try:
                state.set_voice_conversation(None)
            except Exception:
                pass
            try:
                head_pet = state.get_head_pet_reaction()
                if head_pet is not None:
                    try:
                        head_pet.stop()
                    except Exception:
                        pass
                state.set_head_pet_reaction(None)
            except Exception:
                pass
        if backend:
            if current_puzzle is not None and puzzle_supported is not None:
                set_puzzle_light_if_needed(
                    backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                )
            if (config.face_mode or "avatar").strip().lower() != "sprite":
                set_face_if_needed(backend, config.face_idle, current_face)
            if config.move_enabled:
                set_move_if_needed(
                    backend, config.move_idle_yaw, config.move_idle_pitch, current_move
                )
    finally:
        if piper_process:
            piper_process.close()
        if backend:
            backend.close()


def run_bridge(state: RuntimeState):
    backend = None
    piper_process = None
    voice_conv = None
    sprite_animator = None
    current_face: list[str | None] = [None]
    current_move: list[float | None] = [None, None]
    current_puzzle: list[dict[str, str] | None] = [None]
    puzzle_supported: list[dict[str, bool]] = [{}]
    sprite_renderer: list[SpriteFaceRenderer | None] = [None]
    active_version = -1
    active_turn = None

    try:
        while True:
            config, version = state.snapshot()
            if version != active_version:
                close_runtime(
                    backend, piper_process, current_face, current_move, config,
                    voice_conv, state, sprite_animator, current_puzzle, puzzle_supported
                )
                sprite_animator = None
                state.set_runtime(None, None)
                backend = open_backend_with_retry(config)
                piper_process = None
                if config.tts == "piper":
                    piper_process = PiperProcess(config.piper_bin, config.piper_model, config.piper_speaker)
                current_face = [None]
                current_move = [None, None]
                current_puzzle = [None]
                puzzle_supported = [{}]
                active_turn = None
                active_version = version
                set_volume(backend, config.volume)
                puzzle_supported[0] = detect_puzzle_light_support(backend, config)
                if config.face_rotation is not None:
                    try:
                        rot_result = backend.send_command(f"ROTATE:{config.face_rotation}")
                        log({"face_rotation": config.face_rotation, "result": rot_result})
                    except Exception as exc:
                        log({"face_rotation": config.face_rotation, "error": str(exc)})
                sprite_requested = (config.face_mode or "avatar").strip().lower() == "sprite"
                if _sprite_available(config, backend):
                    sprite_animator = SpriteAnimationLoop(backend, config, current_face, sprite_renderer)
                    sprite_animator.start()
                elif sprite_requested:
                    # sprite 指定だが使えない → avatar (スタックチャン顔) にフォールバック。
                    log({
                        "face_mode": "sprite->avatar fallback",
                        "reason": "no send_image" if not hasattr(backend, "send_image")
                        else "spritesheet not found",
                        "sprite_sheet": config.sprite_sheet,
                    })
                set_visual_face_if_needed(
                    backend, config, config.face_idle, current_face, sprite_renderer, sprite_animator
                )
                set_puzzle_light_if_needed(
                    backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                )
                if config.move_enabled:
                    set_move_if_needed(
                        backend, config.move_idle_yaw, config.move_idle_pitch, current_move
                    )
                state.set_runtime(backend, piper_process)
                log({"config_applied": version})

                # 音声対話モード起動 (シリアル backend のみ、WiFi backend は未対応)。
                # `backend.on_head_touch = self._on_head_touch` の bind が走り、以後
                # アタマセンサ press で録音 → STT → xangi POST /api/chat が回る。
                voice_conv = None
                # 音声入力モード: アタマセンサ press (voice_conversation) か、LCD 下部の
                # マイクボタン (lcd_mic_voice、cores3-main-0.17+) で録音→STT→xangi を起動。
                # lcd_mic_voice はアタマセンサを「なで反応」に残せるのが利点 (トリガ分離)。
                voice_enabled = config.voice_conversation or config.lcd_mic_voice
                if voice_enabled and isinstance(backend, StackchanSerial):
                    # アタマセンサを録音トリガに使う (voice_conversation) 場合のみ、
                    # ファーム側のなでなで feedback (Avatar + 埋め込み音声) を抑制する。
                    # press が録音開始と二重発火するのを避けるため。lcd_mic_voice では
                    # アタマセンサはなで反応に残すので抑制しない。
                    if config.voice_conversation:
                        _apply_head_touch_firmware_settings(
                            backend,
                            suppress_head_touch_avatar=True,
                            suppress_head_pet_sound=True,
                        )
                    # ファームが voice 対応 (cores3-main-0.9+) かを起動時に確認。
                    # 未対応なら head_touch event も MIC_START も来ないので
                    # 「動かない」と分かるよう WARN を出す。
                    fw_status = backend.send_command("STATUS")
                    fw_ver = str(fw_status.get("version", "unknown"))
                    if "mic_recording" not in fw_status or "head_touch" not in fw_status:
                        log({
                            "WARN": "firmware does not support voice_conversation",
                            "version": fw_ver,
                            "required": "cores3-main-0.9 or later",
                            "missing_fields": [
                                k for k in ("mic_recording", "head_touch")
                                if k not in fw_status
                            ],
                        })
                    def _vc_on_press(event):
                        log({"voice_press": event.get("gesture"), "at": event.get("at")})
                        if sprite_animator is not None:
                            sprite_animator.pause()
                        set_visual_face_if_needed(
                            backend, config, "listening", current_face, sprite_renderer, sprite_animator
                        )

                    def _vc_on_stop(stop_result):
                        log({"voice_stop": True,
                             "duration": stop_result.get("duration_seconds"),
                             "frames": stop_result.get("frames")})
                        # STT に進まない短すぎる録音 / MIC_START 失敗 / USB 切断復帰では
                        # on_transcribed が呼ばれない。press 時に pause した sprite を
                        # ここで必ず戻し、listening 表示に固まらないようにする。
                        if sprite_animator is not None:
                            sprite_animator.resume()
                        set_visual_face_if_needed(
                            backend, config, config.face_idle, current_face, sprite_renderer, sprite_animator
                        )
                        # デバッグ用 WAV 保存 (STACKCHAN_VC_SAVE_WAV=1 で有効化)。
                        # /tmp/voice_test_<ts>.wav に保存して aplay 等で実音確認できる。
                        if os.environ.get("STACKCHAN_VC_SAVE_WAV"):
                            wav = stop_result.get("wav", b"")
                            if wav:
                                path = f"/tmp/voice_test_{int(time.time())}.wav"
                                try:
                                    with open(path, "wb") as f:
                                        f.write(wav)
                                    log({"voice_wav_saved": path, "size": len(wav)})
                                except Exception as exc:
                                    log({"voice_wav_save_failed": str(exc)})

                    def _vc_on_transcribed(text, r):
                        log({"voice_stt": text, "language": r.get("language"),
                             "elapsed": r.get("elapsed_seconds")})
                        if not text.strip():
                            if sprite_animator is not None:
                                sprite_animator.resume()
                            set_visual_face_if_needed(
                                backend, config, config.face_idle, current_face, sprite_renderer, sprite_animator
                            )

                    voice_conv = VoiceConversation(
                        backend,
                        xangi_base_url=config.xangi_url,
                        app_session_id=config.voice_app_session_id,
                        silence_dbfs=config.voice_silence_dbfs,
                        silence_seconds=config.voice_silence_seconds,
                        max_record_seconds=config.voice_max_seconds,
                        initial_grace_seconds=config.voice_initial_grace_seconds,
                        on_press=_vc_on_press,
                        on_stop=_vc_on_stop,
                        on_transcribed=_vc_on_transcribed,
                        on_sent=lambda text, info: log(
                            {"voice_sent": text[:80], "info": info}
                        ),
                        # アタマセンサ press をトリガに使うのは voice_conversation の時だけ。
                        # lcd_mic_voice では LCD ボタンのみをトリガにし、アタマセンサは
                        # ファーム側のなで反応に残す。
                        trigger_head_touch=config.voice_conversation,
                        trigger_mic_button=True,
                    )
                    voice_conv.start()
                    log({
                        "voice_started": True,
                        "trigger_head_touch": config.voice_conversation,
                        "trigger_mic_button": True,
                        "mode": "voice_conversation" if config.voice_conversation else "lcd_mic_voice",
                    })
                state.set_voice_conversation(voice_conv)

                # なでなで反応モード: head_touch press/swipe で即セリフを喋る。
                # voice_conversation と同じ press を消費するので、voice 有効時は起動しない
                # (voice 優先)。ファーム側 applyHeadTouchAvatar が即 Happy 顔 + 吹き出しを
                # 出すので、HEADTOUCH_AVATAR は on のままにして「触った瞬間に顔 → 少し
                # 遅れて喋る」にする。
                head_pet = None
                if (
                    config.head_pet_reaction
                    and voice_conv is None
                    and isinstance(backend, StackchanSerial)
                ):
                    fw_status = backend.send_command("STATUS")
                    if "head_touch" not in fw_status:
                        log({
                            "WARN": "firmware has no head_touch; --head-pet-reaction "
                            "will not fire",
                            "version": str(fw_status.get("version", "unknown")),
                        })
                    # ファーム側スタンドアローンなでなで音 (cores3-main-0.16+) は抑制。
                    # host が TTS で多彩なセリフを喋らせるので、埋め込み音声と二重に
                    # 鳴らさない。旧ファームは "unknown command" で無害。
                    _apply_head_touch_firmware_settings(
                        backend,
                        suppress_head_touch_avatar=False,
                        suppress_head_pet_sound=True,
                    )

                    def _pet_speak(text):
                        # 発話前に talking 顔、終わったら idle に戻す。speak_text は
                        # send_wav 完了までブロックする (専用 thread から呼ばれる)。
                        if sprite_animator is not None:
                            sprite_animator.pause()
                        set_puzzle_light_if_needed(
                            backend, config, config.puzzle_talking, current_puzzle, puzzle_supported
                        )
                        set_visual_face_if_needed(
                            backend, config, config.face_talking, current_face,
                            sprite_renderer, sprite_animator,
                        )
                        try:
                            speak_text(backend, text, config, piper_process)
                        finally:
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                            )
                            set_visual_face_if_needed(
                                backend, config, config.face_idle, current_face,
                                sprite_renderer, sprite_animator,
                            )
                            if sprite_animator is not None:
                                sprite_animator.resume()

                    def _pet_on_react(phrase, event):
                        log({
                            "head_pet": phrase,
                            "gesture": event.get("gesture"),
                            "at": event.get("at"),
                        })

                    head_pet = HeadPetReaction(
                        backend,
                        speak=_pet_speak,
                        on_react=_pet_on_react,
                        phrases=config.head_pet_phrases or None,
                        cooldown_seconds=config.head_pet_cooldown_seconds,
                    )
                    head_pet.start()
                    log({
                        "head_pet_reaction": "started",
                        "phrases": len(head_pet.phrases),
                        "cooldown": config.head_pet_cooldown_seconds,
                    })
                state.set_head_pet_reaction(head_pet)

                if isinstance(backend, StackchanSerial):
                    suppress_head_touch = bool(config.voice_conversation)
                    suppress_head_pet_sound = bool(
                        config.voice_conversation or head_pet is not None
                    )
                    _apply_head_touch_firmware_settings(
                        backend,
                        suppress_head_touch_avatar=suppress_head_touch,
                        suppress_head_pet_sound=suppress_head_pet_sound,
                    )

                # 稼働中シリアル切断 (デバイス再起動 / USB 再列挙で ttyACMx が変わる)
                # からの自動再接続後に、デバイス状態を再初期化する。デバイス側は
                # 再起動して boot 状態 (デフォルト音量 / avatar 顔 / ファーム設定
                # リセット) に戻っているので、host が把握している状態を送り直す。
                # StackchanSerial._reconnect_loop の thread から呼ばれる。
                if isinstance(backend, StackchanSerial):
                    backend.reconnect_interval = max(config.stackchan_retry_seconds, 1.0)
                    suppress_head_touch = bool(config.voice_conversation)
                    suppress_head_pet_sound = bool(
                        config.voice_conversation or head_pet is not None
                    )

                    def _reinit_after_reconnect(
                        backend=backend,
                        config=config,
                        current_face=current_face,
                        current_move=current_move,
                        sprite_animator=sprite_animator,
                        current_puzzle=current_puzzle,
                        puzzle_supported=puzzle_supported,
                        suppress_head_touch=suppress_head_touch,
                        suppress_head_pet_sound=suppress_head_pet_sound,
                    ):
                        log({"serial": "reinit_after_reconnect"})
                        try:
                            set_volume(backend, config.volume)
                            if config.face_rotation is not None:
                                backend.send_command(f"ROTATE:{config.face_rotation}")
                            puzzle_supported[0] = detect_puzzle_light_support(backend, config)
                            _apply_head_touch_firmware_settings(
                                backend,
                                suppress_head_touch_avatar=suppress_head_touch,
                                suppress_head_pet_sound=suppress_head_pet_sound,
                            )
                            # sprite 差分の基準フレームを破棄して全画面再送 + 表情/首
                            # ポーズの強制再送 (current_* を None に戻す)。
                            renderer = sprite_renderer[0]
                            if renderer is not None:
                                renderer.reset_frame_state()
                            current_face[0] = None
                            current_move[0] = None
                            current_move[1] = None
                            current_puzzle[0] = None
                            set_visual_face_if_needed(
                                backend, config, config.face_idle, current_face,
                                sprite_renderer, sprite_animator,
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend, config.move_idle_yaw,
                                    config.move_idle_pitch, current_move,
                                )
                        except Exception as exc:
                            log({"serial": "reinit_error", "error": str(exc)})

                    backend.on_reconnected = _reinit_after_reconnect

            stream_url = normalize_xangi_stream_url(config.xangi_url)
            backoff = max(config.retry_seconds, 1.0)
            max_backoff = max(backoff, config.max_retry_seconds)

            while True:
                config, version = state.snapshot()
                if version != active_version:
                    log({"config_changed": version})
                    break
                try:
                    log({"_bridge_event": "connecting", "url": stream_url})
                    for event in iter_xangi_events(stream_url, timeout=config.stream_timeout):
                        config, version = state.snapshot()
                        if version != active_version:
                            raise ConfigChanged
                        if event.get("_sse_event") == "ready":
                            log({"ready": event})
                            continue
                        if not should_handle_event(event, config):
                            continue

                        event_type = event.get("type")
                        if not event_type:
                            continue

                        # マイク録音中 (voice_conversation の MIC_START 〜 MIC_STOP の
                        # 期間) は SSE event の actuator 反応 (FACE/MOVE/WAV) を全て
                        # skip。これで他チャンネル (Discord 等) からの message.delta
                        # / turn.complete がシリアルに同時アクセスして録音 PCM 破綻 +
                        # multi access on port 警告を起こすのを防ぐ。
                        # 自身の音声入力 → 応答経路は、stop_mic_recording (Speaker 復帰)
                        # 後に turn.started → turn.complete が来るので影響無し。
                        if (
                            isinstance(backend, StackchanSerial)
                            and getattr(backend, "_mic_recording", False)
                        ):
                            log({"_skip_event_during_mic": event_type, "turn_id": event.get("turn_id")})
                            continue

                        log(event)

                        if event_type == "turn.started":
                            active_turn = event.get("turn_id")
                            # ユーザがファーム LCD 長押しで前 turn を止めた状態
                            # (user_stopped=True) を新 turn 開始でリセット。これで
                            # 次の send_wav から通常動作復帰する。
                            if getattr(backend, "user_stopped", False):
                                backend.user_stopped = False
                                log({"user_stopped": "cleared", "reason": "turn.started"})
                            set_visual_face_if_needed(
                                backend, config, config.face_thinking, current_face, sprite_renderer, sprite_animator
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_thinking, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_thinking_yaw,
                                    config.move_thinking_pitch,
                                    current_move,
                                )
                        elif event_type == "message.delta":
                            if active_turn == event.get("turn_id"):
                                set_visual_face_if_needed(
                                    backend, config, config.face_talking, current_face, sprite_renderer, sprite_animator
                                )
                                set_puzzle_light_if_needed(
                                    backend, config, config.puzzle_talking, current_puzzle, puzzle_supported
                                )
                        elif event_type == "turn.complete":
                            active_turn = None
                            set_visual_face_if_needed(
                                backend, config, config.face_talking, current_face, sprite_renderer, sprite_animator
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_talking, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                if sprite_animator is not None:
                                    sprite_animator.pause()
                                with TalkingSway(
                                    backend,
                                    config.move_idle_yaw,
                                    config.move_idle_pitch,
                                    config.move_talking_sway_yaw,
                                    config.move_talking_sway_pitch,
                                    config.move_talking_sway_interval,
                                    current_move,
                                ):
                                    speak_text(
                                        backend, event.get("text", ""), config, piper_process
                                    )
                                if sprite_animator is not None:
                                    sprite_animator.resume()
                            else:
                                if sprite_animator is not None:
                                    sprite_animator.pause()
                                speak_text(backend, event.get("text", ""), config, piper_process)
                                if sprite_animator is not None:
                                    sprite_animator.resume()
                            set_visual_face_if_needed(
                                backend, config, config.face_idle, current_face, sprite_renderer, sprite_animator
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_idle_yaw,
                                    config.move_idle_pitch,
                                    current_move,
                                )
                        elif event_type == "turn.aborted":
                            active_turn = None
                            set_visual_face_if_needed(
                                backend, config, config.face_idle, current_face, sprite_renderer, sprite_animator
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_idle, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_idle_yaw,
                                    config.move_idle_pitch,
                                    current_move,
                                )
                        elif event_type == "agent.error":
                            active_turn = None
                            set_visual_face_if_needed(
                                backend, config, config.face_error, current_face, sprite_renderer, sprite_animator
                            )
                            set_puzzle_light_if_needed(
                                backend, config, config.puzzle_error, current_puzzle, puzzle_supported
                            )
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_error_yaw,
                                    config.move_error_pitch,
                                    current_move,
                                )
                    backoff = max(config.retry_seconds, 1.0)
                except ConfigChanged:
                    log({"config_changed": version})
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log({"_bridge_event": "stream_error", "error": str(exc), "retry_seconds": backoff})
                    time.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
    except KeyboardInterrupt:
        log({"stopped": True})
    finally:
        config, _ = state.snapshot()
        state.set_runtime(None, None)
        close_runtime(
            backend, piper_process, current_face, current_move, config,
            voice_conv, state, sprite_animator, current_puzzle, puzzle_supported
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Physical xangi pet bridge for stackchan family devices (K151 / stackchan-atama)")
    parser.add_argument("--xangi-url", default=DEFAULT_XANGI_URL)
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--stream-timeout", type=int, default=65)
    parser.add_argument("--retry-seconds", type=float, default=1.0)
    parser.add_argument("--max-retry-seconds", type=float, default=30.0)

    parser.add_argument("--wifi", action="store_true")
    parser.add_argument(
        "--simulator",
        action="store_true",
        help="USB/WiFi デバイスに接続せず、ブラウザ /simulator 用の in-memory "
        "バックエンドを使う。実機なしで FACE/MOVE/WAV/PUZZLE の流れを確認する。",
    )
    parser.add_argument("--host", default=DEFAULT_WIFI_HOST)
    parser.add_argument("--port", default="")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--device-profile", default="",
                        help="プリセット選択 (cores3_k151 / cores3_standalone / atoms3r / rt_beta)。"
                             "指定すると baud / max_wav_bytes の既定値が埋まる")
    parser.add_argument("--max-wav-bytes", type=int, default=0,
                        help="WAV サイズ上限 (byte)。0 = 無制限 (ファーム側に任せる)。"
                             "rt_beta profile は 96KB、atoms3r は 256KB が既定")
    parser.add_argument("--skip-move-during-wav", action="store_true",
                        help="WAV 再生中の MOVE 送信をスキップ (rt_beta 既定 ON)。"
                             "M5Stack Basic + アールティ PCB のように USB 給電と"
                             "サーボ電源を共有する構成で電流ラッシュ → USB 切断を回避")
    parser.add_argument("--volume", type=int, default=255)
    parser.add_argument("--serial-chunk", type=int, default=1024)
    parser.add_argument("--serial-delay", type=float, default=0.005)
    parser.add_argument("--stackchan-retry-seconds", type=float, default=3.0)

    parser.add_argument("--tts", choices=["piper", "voicevox", "none"], default=DEFAULT_TTS)
    parser.add_argument("--piper-bin", default=DEFAULT_PIPER_BIN)
    parser.add_argument("--piper-model", default=DEFAULT_PIPER_MODEL)
    parser.add_argument("--piper-speaker", type=int, default=0)
    parser.add_argument("--voicevox-url", default=DEFAULT_VOICEVOX_URL)
    parser.add_argument("--voicevox-speaker", type=int, default=DEFAULT_VOICEVOX_SPEAKER)

    parser.add_argument(
        "--face-rotation",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help="顔の向きを時計回りクオータターン数で指定 (0=自然 / 1=時計回り90度 / "
        "2=180度 / 3=時計回り270度)。AtomS3R (atoms3r-main) で有効。未指定なら "
        "ファーム既定 (起動時 時計回り90度) のまま。",
    )
    parser.add_argument("--face-idle", default="neutral")
    parser.add_argument("--face-thinking", default="doubt")
    parser.add_argument("--face-talking", default="happy")
    parser.add_argument("--face-error", default="sad")
    parser.add_argument(
        "--face-mode",
        choices=["avatar", "sprite"],
        default="sprite",
        help="sprite (既定): spritesheet から切り出した画像を IMAGE で LCD に送る "
        "(borot 等のキャラ顔)。avatar: M5Stack Avatar の表情名を FACE で送る "
        "(スタックチャン顔)。sprite が使えない時 (spritesheet 未配置 / IMAGE 非対応) は "
        "自動で avatar にフォールバックする。",
    )
    parser.add_argument(
        "--sprite-sheet",
        default="assets/pets/borot/spritesheet.webp",
        help="--face-mode sprite 時に使う spritesheet.webp のパス (既定: borot)。"
        "各自のスプライトを assets/pets/<name>/spritesheet.webp に置いて指定する。"
        "ファイルが無ければ avatar (スタックチャン顔) にフォールバック。",
    )
    parser.add_argument(
        "--sprite-jpeg-quality",
        type=int,
        default=50,
        help=(
            "--face-mode sprite 時にデバイスへ送る JPEG の品質 (1-95)。既定 50 = "
            "1 フレーム約 6KB (品質 85 は約 11KB)。会話モードではスプライトアニメの "
            "IMAGE 送信が録音・音声再生・会話イベントと USB シリアル帯域を奪い合うため、"
            "画像を軽くして輻輳を避ける。"
        ),
    )

    parser.add_argument("--move-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--move-idle-yaw", type=float, default=0.0)
    parser.add_argument("--move-idle-pitch", type=float, default=5.0)
    parser.add_argument("--move-thinking-yaw", type=float, default=-8.0)
    parser.add_argument("--move-thinking-pitch", type=float, default=5.0)
    parser.add_argument("--move-error-yaw", type=float, default=0.0)
    parser.add_argument("--move-error-pitch", type=float, default=-10.0)
    parser.add_argument("--move-talking-sway-yaw", type=float, default=4.0)
    parser.add_argument("--move-talking-sway-pitch", type=float, default=2.0)
    parser.add_argument("--move-talking-sway-interval", type=float, default=1.5)
    parser.add_argument(
        "--puzzle-light-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Puzzle Unit WS2812E を状態表示に使う。ファーム STATUS が puzzle:true "
        "の時だけ PUZZLE:<pattern> を送る。無効化は --no-puzzle-light-enabled。",
    )
    parser.add_argument("--puzzle-idle", default="off")
    parser.add_argument("--puzzle-thinking", default="thinking")
    parser.add_argument("--puzzle-talking", default="talking")
    parser.add_argument("--puzzle-error", default="error")

    parser.add_argument(
        "--voice-conversation",
        action="store_true",
        help="アタマ touch (Si12T) → 録音 → STT (faster-whisper) → xangi /api/chat "
        "投入の音声対話モードを有効化。M5Stackchan K151 + シリアル backend 前提。"
        "応答 TTS と発話は既存の SSE 経路で自動処理される。",
    )
    parser.add_argument(
        "--voice-app-session-id",
        default="",
        help="--voice-conversation 時に xangi に投げる appSessionId。空なら xangi "
        "側で最新 web session が選ばれる。複数 xangi インスタンスを使い分ける時のみ指定。",
    )
    parser.add_argument(
        "--voice-silence-dbfs",
        type=float,
        default=-40.0,
        help="VAD 無音判定の dBFS 閾値 (既定 -40)。静かな室内なら下げる、騒がしいなら上げる。",
    )
    parser.add_argument(
        "--voice-silence-seconds",
        type=float,
        default=1.5,
        help="無音判定後の自動停止までの秒数 (既定 1.5)。",
    )
    parser.add_argument(
        "--voice-max-seconds",
        type=float,
        default=15.0,
        help="最大録音時間 (既定 15)。これを超えたら強制停止。",
    )
    parser.add_argument(
        "--voice-initial-grace-seconds",
        type=float,
        default=5.0,
        help="なでてから最初の発話までの猶予秒数 (既定 5)。この間の無音では止めない。",
    )

    parser.add_argument(
        "--speak-platforms",
        default="",
        help="発話するプラットフォームをカンマ区切りで限定 (例: web)。空なら全部喋る。"
        "接続先 xangi が Discord 等も捌いている時、音声入力 (web) の応答だけ喋らせたい等で使う。",
    )
    parser.add_argument(
        "--lcd-mic-voice",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="LCD 下部のマイクボタン (短くタップ) で音声入力 (録音→STT→xangi) を起動する。"
        "アタマセンサは使わないので、--head-pet-reaction やファームのなで反応と同居できる。"
        "cores3-main-0.17+ 前提。既定で有効。無効化は --no-lcd-mic-voice。",
    )
    parser.add_argument(
        "--head-pet-reaction",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="なでなで反応モード。アタマ touch (head_touch) を触った瞬間にランダムな "
        "セリフを喋る。話しかけ不要で「とにかく反応する」デモ向け。シリアル backend + "
        "head_touch 対応ファーム前提。--voice-conversation とは排他 (voice 優先)。"
        "既定で有効。無効化は --no-head-pet-reaction。",
    )
    parser.add_argument(
        "--head-pet-phrases",
        default="",
        help="なでなで反応のセリフ候補をカンマ区切りで指定。空ならモジュール既定を使う。"
        '例: "なでなでありがとう,えへへ,もっとなでて"',
    )
    parser.add_argument(
        "--head-pet-cooldown-seconds",
        type=float,
        default=2.0,
        help="なでなで反応の発話完了後クールダウン秒数 (既定 2.0)。連打/なで続けで "
        "反応が積み上がらないようにする。",
    )

    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--instance-id",
        default=DEFAULT_INSTANCE_ID,
        help="config namespace inside config.json (default: 'default'). Each "
        "concurrently running stackchan should have its own instance-id.",
    )
    parser.add_argument("--settings-bind", default="127.0.0.1")
    parser.add_argument("--settings-port", type=int, default=DEFAULT_SETTINGS_PORT)
    parser.add_argument(
        "--port-autoshift-tries",
        type=int,
        default=10,
        help="Number of consecutive ports to try when --settings-port is busy "
        "(default 10, i.e. 7897..7906).",
    )
    parser.add_argument(
        "--no-port-autoshift",
        action="store_true",
        help="Disable settings-UI port auto-shift; fail fast on bind error.",
    )
    parser.add_argument("--no-settings-ui", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    return BridgeConfig(
        xangi_url=args.xangi_url,
        thread_id=args.thread_id,
        stackchan=StackchanConfig(
            wifi=args.wifi,
            simulator=args.simulator,
            host=args.host,
            port=args.port,
            baud=args.baud,
            device_profile=args.device_profile,
            max_wav_bytes=args.max_wav_bytes,
            skip_move_during_wav=args.skip_move_during_wav,
        ),
        volume=max(0, min(255, args.volume)),
        tts=args.tts,
        piper_bin=args.piper_bin,
        piper_model=args.piper_model,
        piper_speaker=args.piper_speaker,
        voicevox_url=args.voicevox_url,
        voicevox_speaker=args.voicevox_speaker,
        serial_chunk=args.serial_chunk,
        serial_delay=args.serial_delay,
        stackchan_retry_seconds=args.stackchan_retry_seconds,
        face_idle=args.face_idle,
        face_thinking=args.face_thinking,
        face_talking=args.face_talking,
        face_error=args.face_error,
        face_mode=args.face_mode,
        sprite_sheet=args.sprite_sheet,
        sprite_jpeg_quality=max(1, min(95, args.sprite_jpeg_quality)),
        face_rotation=args.face_rotation,
        stream_timeout=args.stream_timeout,
        retry_seconds=args.retry_seconds,
        max_retry_seconds=args.max_retry_seconds,
        move_enabled=args.move_enabled,
        move_idle_yaw=args.move_idle_yaw,
        move_idle_pitch=args.move_idle_pitch,
        move_thinking_yaw=args.move_thinking_yaw,
        move_thinking_pitch=args.move_thinking_pitch,
        move_error_yaw=args.move_error_yaw,
        move_error_pitch=args.move_error_pitch,
        move_talking_sway_yaw=args.move_talking_sway_yaw,
        move_talking_sway_pitch=args.move_talking_sway_pitch,
        move_talking_sway_interval=args.move_talking_sway_interval,
        puzzle_light_enabled=args.puzzle_light_enabled,
        puzzle_idle=args.puzzle_idle,
        puzzle_thinking=args.puzzle_thinking,
        puzzle_talking=args.puzzle_talking,
        puzzle_error=args.puzzle_error,
        voice_conversation=args.voice_conversation,
        voice_app_session_id=args.voice_app_session_id,
        voice_silence_dbfs=args.voice_silence_dbfs,
        voice_silence_seconds=args.voice_silence_seconds,
        voice_max_seconds=args.voice_max_seconds,
        voice_initial_grace_seconds=args.voice_initial_grace_seconds,
        lcd_mic_voice=args.lcd_mic_voice,
        speak_platforms=[
            p.strip() for p in args.speak_platforms.split(",") if p.strip()
        ],
        head_pet_reaction=args.head_pet_reaction,
        head_pet_phrases=[
            p.strip() for p in args.head_pet_phrases.split(",") if p.strip()
        ],
        head_pet_cooldown_seconds=args.head_pet_cooldown_seconds,
    )


def _ensure_voice_session(args: argparse.Namespace) -> None:
    """voice_conversation 起動時に xangi に専用 web session を作って、
    args.voice_app_session_id と args.thread_id を自動セットする。

    既に voice_app_session_id 指定があるか、--thread-id 指定があれば skip。
    新規 session を作ることで:
      - POST /api/chat は stackchan 専用 session に投入される (他 web セッションを汚さない)
      - SSE event の thread_id が `web:<sid>` で固定 → 他チャンネル
        (Discord 等) の noise が `should_handle_event` で skip されて、
        Mic 録音中のシリアル衝突を回避できる
    """
    if not args.voice_conversation:
        return
    if args.voice_app_session_id and args.thread_id:
        return  # 既に両方指定ある

    try:
        r = requests.post(
            f"{args.xangi_url.rstrip('/')}/api/sessions",
            json={},
            timeout=10,
        )
        if not r.ok:
            log({"voice_session_create_failed": r.status_code, "body": r.text[:200]})
            return
        data = r.json()
        sid = data.get("sessionId") or data.get("id") or ""
        if not sid:
            log({"voice_session_create_failed": "no sessionId in response", "body": data})
            return
        log({"voice_session_created": sid})
        if not args.voice_app_session_id:
            args.voice_app_session_id = sid
        if not args.thread_id:
            args.thread_id = f"web:{sid}"
    except Exception as exc:
        log({"voice_session_create_failed": str(exc)})


def _align_voice_thread(config: BridgeConfig) -> BridgeConfig:
    """Route voice conversation replies back to the Stack-chan bridge.

    Persisted settings can contain a Discord thread filter from a previous
    bridge run plus a web appSessionId for voice input. In that split-brain
    state, voice POSTs go to `web:<session>` while the bridge only accepts
    `discord:<channel>` events, so it discards its own reply.
    """
    if not config.voice_conversation or not config.voice_app_session_id:
        return config
    voice_thread_id = f"web:{config.voice_app_session_id}"
    if config.thread_id == voice_thread_id:
        return config
    log({
        "voice_thread_aligned": voice_thread_id,
        "previous_thread_id": config.thread_id,
    })
    return replace(config, thread_id=voice_thread_id)


def _clear_stale_voice_thread_filter(config: BridgeConfig) -> BridgeConfig:
    """Drop an auto-created voice session thread filter outside voice mode.

    `--voice-conversation` creates a dedicated web session and stores both
    voice_app_session_id and thread_id.  When the same config namespace is later
    used for the normal LCD mic mode, that thread filter would make the bridge
    ignore browser input from any other web session.  Keep explicit thread
    filters intact; only clear the exact auto-created voice thread.
    """
    if config.voice_conversation or not config.voice_app_session_id:
        return config
    voice_thread_id = f"web:{config.voice_app_session_id}"
    if config.thread_id != voice_thread_id:
        return config
    log({
        "stale_voice_thread_cleared": voice_thread_id,
    })
    return replace(config, thread_id=None)


def _install_faulthandler() -> None:
    """SIGUSR1 で全スレッドの Python スタックを stderr (= ログ) に dump する。

    ptrace_scope=1 の環境では py-spy が同ユーザでもアタッチできず、ハング時の
    スタックが取れない。faulthandler.register で SIGUSR1 を捕まえておけば、
    `kill -USR1 <pid>` で全スレッドのスタックを非破壊で吸い出せる (本番でも無害)。
    main thread でしか register できないのでここで呼ぶ。"""
    try:
        faulthandler.enable()
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:
        pass


def main(argv: list[str] | None = None):
    _install_faulthandler()
    parser = build_parser()
    args = parser.parse_args(argv)
    _ensure_voice_session(args)
    ensured_voice_app_session_id = args.voice_app_session_id
    if args.voice_conversation and not args.voice_app_session_id:
        # _ensure_voice_session が失敗 (xangi が起動してない / /api/sessions 未実装)
        # した状態。このまま起動すると POST /api/chat が「最新 web session」を選んで
        # 意図しない既存 session を汚染する。明示警告 + 起動継続。
        print(
            "[xangi-stackchan] WARNING: voice_conversation enabled but no dedicated "
            "web session created (xangi /api/sessions failed). POST /api/chat will "
            "fall back to the latest web session — verify --xangi-url is reachable "
            "or pass --voice-app-session-id <id> manually.",
            file=sys.stderr,
        )
    config_path = Path(args.config).expanduser()
    instance_id = args.instance_id.strip() or DEFAULT_INSTANCE_ID
    config = merge_config(
        config_from_args(args), load_instance_dict(config_path, instance_id)
    )
    if args.voice_conversation and ensured_voice_app_session_id:
        config = replace(config, voice_app_session_id=ensured_voice_app_session_id)
    config = _align_voice_thread(config)
    config = _clear_stale_voice_thread_filter(config)
    if config.tts == "piper" and not config.piper_model:
        parser.error("--piper-model is required when using --tts piper")
    state = RuntimeState(config, config_path, instance_id=instance_id)

    serial_target = (
        config.stackchan.host if config.stackchan.wifi else (config.stackchan.port or "")
    )
    bound_config_port: int | None = None
    if not args.no_settings_ui:
        autoshift = 1 if args.no_port_autoshift else max(1, args.port_autoshift_tries)
        _, bound_config_port = start_settings_server(
            state,
            args.settings_bind,
            args.settings_port,
            autoshift_tries=autoshift,
        )
        log({"settings_ui": f"http://{args.settings_bind}:{bound_config_port}/"})

    log(
        {
            "boot": True,
            "instance_id": instance_id,
            "serial_port": serial_target,
            "wifi": config.stackchan.wifi,
            "bound_config_port": bound_config_port,
            "thread_id": config.thread_id,
            "xangi_url": config.xangi_url,
        }
    )
    run_bridge(state)
