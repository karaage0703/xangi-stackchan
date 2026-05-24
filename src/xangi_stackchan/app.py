import argparse
import json
import random
import threading
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

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
from .stackchan import DEFAULT_BAUD, DEFAULT_WIFI_HOST, StackchanConfig, apply_profile_defaults, create_backend
from .tts import (
    DEFAULT_PIPER_BIN,
    DEFAULT_PIPER_MODEL,
    DEFAULT_TTS,
    DEFAULT_VOICEVOX_SPEAKER,
    DEFAULT_VOICEVOX_URL,
    PiperProcess,
    split_text,
    voicevox_synthesize,
)


DEFAULT_XANGI_URL = "http://127.0.0.1:18888"


class ConfigChanged(Exception):
    pass


def log(payload: dict):
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


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
    return True


def close_runtime(backend, piper_process, current_face, current_move, config):
    try:
        if backend:
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
    current_face: list[str | None] = [None]
    current_move: list[float | None] = [None, None]
    active_version = -1
    active_turn = None

    try:
        while True:
            config, version = state.snapshot()
            if version != active_version:
                close_runtime(backend, piper_process, current_face, current_move, config)
                state.set_runtime(None, None)
                backend = open_backend_with_retry(config)
                piper_process = None
                if config.tts == "piper":
                    piper_process = PiperProcess(config.piper_bin, config.piper_model, config.piper_speaker)
                current_face = [None]
                current_move = [None, None]
                active_turn = None
                active_version = version
                set_volume(backend, config.volume)
                set_face_if_needed(backend, config.face_idle, current_face)
                if config.move_enabled:
                    set_move_if_needed(
                        backend, config.move_idle_yaw, config.move_idle_pitch, current_move
                    )
                state.set_runtime(backend, piper_process)
                log({"config_applied": version})

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
                        log(event)

                        if event_type == "turn.started":
                            active_turn = event.get("turn_id")
                            # ユーザがファーム LCD 長押しで前 turn を止めた状態
                            # (user_stopped=True) を新 turn 開始でリセット。これで
                            # 次の send_wav から通常動作復帰する。
                            if getattr(backend, "user_stopped", False):
                                backend.user_stopped = False
                                log({"user_stopped": "cleared", "reason": "turn.started"})
                            set_face_if_needed(backend, config.face_thinking, current_face)
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_thinking_yaw,
                                    config.move_thinking_pitch,
                                    current_move,
                                )
                        elif event_type == "message.delta":
                            if active_turn == event.get("turn_id"):
                                set_face_if_needed(backend, config.face_talking, current_face)
                        elif event_type == "turn.complete":
                            active_turn = None
                            set_face_if_needed(backend, config.face_talking, current_face)
                            if config.move_enabled:
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
                            else:
                                speak_text(backend, event.get("text", ""), config, piper_process)
                            set_face_if_needed(backend, config.face_idle, current_face)
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_idle_yaw,
                                    config.move_idle_pitch,
                                    current_move,
                                )
                        elif event_type == "turn.aborted":
                            active_turn = None
                            set_face_if_needed(backend, config.face_idle, current_face)
                            if config.move_enabled:
                                set_move_if_needed(
                                    backend,
                                    config.move_idle_yaw,
                                    config.move_idle_pitch,
                                    current_move,
                                )
                        elif event_type == "agent.error":
                            active_turn = None
                            set_face_if_needed(backend, config.face_error, current_face)
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
        close_runtime(backend, piper_process, current_face, current_move, config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Physical xangi pet bridge for stackchan family devices (K151 / stackchan-atama)")
    parser.add_argument("--xangi-url", default=DEFAULT_XANGI_URL)
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--stream-timeout", type=int, default=65)
    parser.add_argument("--retry-seconds", type=float, default=1.0)
    parser.add_argument("--max-retry-seconds", type=float, default=30.0)

    parser.add_argument("--wifi", action="store_true")
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

    parser.add_argument("--face-idle", default="neutral")
    parser.add_argument("--face-thinking", default="doubt")
    parser.add_argument("--face-talking", default="happy")
    parser.add_argument("--face-error", default="sad")

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
    )


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser()
    instance_id = args.instance_id.strip() or DEFAULT_INSTANCE_ID
    config = merge_config(
        config_from_args(args), load_instance_dict(config_path, instance_id)
    )
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
