"""head_touch press → 録音 → 無音検出 → STT → xangi `/api/chat` 投入のフロー。

xangi 応答 (turn.complete event) → TTS → 発話は既存の ConsumerLoop / TtsPlayer
経路に任せる。本モジュールは「ユーザ発話 → xangi にテキスト投入」のみを担当。

state machine:
    Idle ──press──▶ Recording ──silence_seconds 経過──▶ Idle
                       │
                       └──press (toggle stop) or max_record_seconds──▶ Idle

無音判定は RMS 簡易方式 (silero-vad 等は依存が重いので将来差し替え余地あり)。
各 64ms PCM chunk の RMS を dBFS に変換、`silence_dbfs` 未満が
`silence_seconds` 秒続いたら自動 MIC_STOP。
"""

from __future__ import annotations

import atexit
import math
import os
import signal
import struct
import sys
import threading
import time
from typing import TYPE_CHECKING

import requests

from . import stt as stt_module
from .stackchan import StackchanSerial

if TYPE_CHECKING:
    pass


DEFAULT_SILENCE_DBFS    = float(os.environ.get("STACKCHAN_VC_SILENCE_DBFS", "-40.0"))
DEFAULT_SILENCE_SECONDS = float(os.environ.get("STACKCHAN_VC_SILENCE_SECONDS", "1.5"))
DEFAULT_MAX_SECONDS     = float(os.environ.get("STACKCHAN_VC_MAX_SECONDS", "15.0"))
# なでてから最初の発話までの猶予 (秒)。この間の無音では止めない (考える時間)。
# 猶予内に一度も発話が無ければ誤タップとみなして stop。発話開始後は silence_seconds
# の通常無音判定に切り替わる。
DEFAULT_INITIAL_GRACE_SECONDS = float(os.environ.get("STACKCHAN_VC_INITIAL_GRACE_SECONDS", "5.0"))
DEFAULT_MIN_PCM_BYTES   = int(os.environ.get("STACKCHAN_VC_MIN_PCM_BYTES", "8192"))
HISTORY_MAX             = int(os.environ.get("STACKCHAN_VC_HISTORY_MAX", "10"))
# PCM stream が止まってから強制 stop するまでの猶予 (秒)。無音判定は「chunk が
# 流れ続けること」前提なので、ファームが MIC mode に入れず PCM を吐かない / 途中で
# stream が死ぬと chunk-based 停止が一切走らず録音状態に固まる。これを壁時計
# watchdog で救う安全網。連続なで / 発話直後なで でも固まらなくなる (2026-05-30)。
DEFAULT_PCM_STALL_SECONDS = float(os.environ.get("STACKCHAN_VC_PCM_STALL_SECONDS", "3.0"))
# watchdog thread のポーリング間隔 (秒)。
WATCHDOG_POLL_SECONDS = 0.25


def chunk_dbfs(chunk: bytes) -> float:
    """16-bit signed LE mono PCM チャンクの RMS を dBFS に変換。空 / 完全無音は
    -inf を返す。"""
    if not chunk or len(chunk) < 2:
        return -math.inf
    sample_count = len(chunk) // 2
    samples = struct.unpack(f"<{sample_count}h", chunk[: sample_count * 2])
    if not samples:
        return -math.inf
    acc = 0.0
    for s in samples:
        acc += s * s
    rms = math.sqrt(acc / sample_count)
    if rms <= 0:
        return -math.inf
    return 20.0 * math.log10(rms / 32768.0)


class VoiceConversation:
    """head_touch press でユーザ発話セッションを駆動する coordinator。

    使い方:
        vc = VoiceConversation(backend, xangi_base_url="http://localhost:5174")
        vc.start()  # backend.on_head_touch にバインド
        # 以後、アタマ tap で録音 → STT → xangi 投入が自動進行
    """

    def __init__(
        self,
        backend: StackchanSerial,
        xangi_base_url: str,
        app_session_id: str = "",
        silence_dbfs: float = DEFAULT_SILENCE_DBFS,
        silence_seconds: float = DEFAULT_SILENCE_SECONDS,
        max_record_seconds: float = DEFAULT_MAX_SECONDS,
        initial_grace_seconds: float = DEFAULT_INITIAL_GRACE_SECONDS,
        pcm_stall_seconds: float = DEFAULT_PCM_STALL_SECONDS,
        min_pcm_bytes: int = DEFAULT_MIN_PCM_BYTES,
        language: str = stt_module.DEFAULT_LANGUAGE,
        on_transcribed: callable = None,
        on_sent: callable = None,
        on_stop: callable = None,
        on_press: callable = None,
    ):
        self.backend = backend
        self.xangi_base_url = xangi_base_url.rstrip("/")
        self.app_session_id = app_session_id
        self.silence_dbfs = silence_dbfs
        self.silence_seconds = silence_seconds
        self.max_record_seconds = max_record_seconds
        self.initial_grace_seconds = initial_grace_seconds
        self.pcm_stall_seconds = pcm_stall_seconds
        self.min_pcm_bytes = min_pcm_bytes
        self.language = language
        self.on_transcribed = on_transcribed  # callback(text: str, stt_result: dict)
        self.on_sent = on_sent                # callback(text: str, response: dict)
        # callback(stop_result: dict)。stop_mic_recording の戻り値 (pcm/wav/frames/
        # duration_seconds) を STT 前に受け取る。テストスクリプトで WAV 保存に使う。
        self.on_stop = on_stop
        # callback(event: dict)。head_touch press 受信時 (録音開始時) に呼ばれる。
        # app.py で「press → 表情変更 + 録音開始ログ出力」に使う想定。
        self.on_press = on_press
        # 直近の STT 履歴 (最大 HISTORY_MAX 件)。設定 UI / デバッグ用に保持。
        # 各 entry は {ts, text, language, elapsed_seconds, stt_status, sent_status}。
        # 録音 PCM やフルセグメントは含まない (メモリ節約)。
        self.history: list[dict] = []

        self._state_lock = threading.Lock()
        self._recording = False
        self._record_start_time = 0.0
        self._last_chunk_time = 0.0  # 直近 PCM chunk 受信時刻 (0 = まだ 1 つも来ていない)
        self._speech_started = False  # 録音開始後に一度でも有音を検知したか
        self._silence_since = 0.0  # 0 = 無音じゃない、>0 = この時刻から無音
        self._stopping = False     # _stop_and_process 多重発火防止
        self._stop_thread: threading.Thread | None = None
        # PCM chunk に依存しない壁時計監視 thread。録音開始ごとに起動し、
        # _recording が False になったら抜ける。
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        # ファームから流れてくる非同期 event (head_touch / audio_stopped) を host 側で
        # 拾うための周期 drain thread。SSE event 待ちの間 serial を読んでいなかった
        # ので、head_touch event が host に届かない潜在バグ (Phase 1 から) があった。
        # signal handler 登録時に保存する前ハンドラ (使わないが将来 chain したい時用)。
        self._prev_sigterm = None

    def start(self) -> None:
        """backend.on_head_touch に bind。録音 stream の callback 経路も
        backend.start_mic_recording 内で on_pcm_chunk として渡す。

        atexit に cleanup を登録して、録音中のプロセス終了 (Ctrl-C / kill SIGTERM
        / 通常 exit) でもファームに MIC_STOP を確実に送る。これを忘れるとファーム
        が MIC モードのまま PCM stream を吐き続け、次回起動時のシリアルが汚染される
        (2026-05-27 実機検証で確認済の既知パターン)。
        """
        self.backend.on_head_touch = self._on_head_touch
        atexit.register(self._atexit_cleanup)
        # SIGTERM (pkill default) で atexit が走らないので、明示的に signal handler を
        # 仕込んで MIC_STOP を保証する。SIGKILL (-9) は trap 不可なので諦め (代わりに
        # ファーム側 watchdog で host 無応答 60s で自動 stop する)。signal は main
        # thread でしか登録できないので main 以外から start された場合は skip。
        if threading.current_thread() is threading.main_thread():
            try:
                self._prev_sigterm = signal.signal(signal.SIGTERM, self._signal_cleanup_handler)
            except (ValueError, OSError):
                self._prev_sigterm = None
        # SerialActor 移行後 (Phase 2.x) は event poll thread 不要。actor が常時
        # ser.read() で byte stream を消費し、line を _detect_async_event → on_head_touch
        # に dispatch する。drain race / READY 先食い等の事故が原理的に消える。

    def stop(self) -> None:
        """bind 解除。録音中なら停止。"""
        self.backend.on_head_touch = None
        self._watchdog_stop.set()  # watchdog thread を畳む
        if self._recording:
            self._schedule_stop()
            if self._stop_thread is not None:
                self._stop_thread.join(timeout=3.0)

    def _signal_cleanup_handler(self, signum, frame):
        """SIGTERM 受信時に MIC_STOP を確実に送ってから exit。pkill default で
        ファームが MIC モードに取り残されるバグの再発防止 (2026-05-27 21:01 で発生)。
        """
        self._atexit_cleanup()
        # SIGTERM 時の慣例的 exit code = 128 + signum。元の handler が SIG_DFL
        # だった場合はそれが exit するが、明示的に sys.exit でファイナライザを走らせる。
        sys.exit(128 + signum)

    def _atexit_cleanup(self) -> None:
        """インタプリタ終了直前のフック。録音中ならファームに MIC_STOP を送って
        Speaker モードに復帰させる。例外は飲み込む (atexit で raise すると別の
        cleanup ハンドラに影響する)。"""
        try:
            if self._recording:
                # backend.stop_mic_recording は reader thread join + ack 待ち含むので
                # 数百 ms ブロックする可能性あり、それでも安全側でやりきる。
                self.backend.stop_mic_recording()
        except Exception:
            pass

    def _on_head_touch(self, event: dict) -> None:
        gesture = event.get("gesture")
        if gesture != "press":
            return  # release / swipe は無視
        with self._state_lock:
            if self._recording:
                # 録音中の press = toggle stop
                self._schedule_stop()
                return
            self._recording = True
            self._stopping = False
            self._record_start_time = time.time()
            self._last_chunk_time = 0.0
            self._speech_started = False
            self._silence_since = 0.0

        # UI feedback must be sent before MIC_START. Once the firmware enters
        # mic mode, IMAGE commands are rejected to keep the PCM stream clean.
        if self.on_press is not None:
            try:
                self.on_press(event)
            except Exception:
                pass

        # ack 取得 + 録音開始。失敗時は state を戻す。
        ack = self.backend.start_mic_recording(on_pcm_chunk=self._on_pcm_chunk)
        if ack.get("status") != "ok":
            with self._state_lock:
                self._recording = False
            return
        # PCM が来なくても max / stall で必ず stop する安全網を起動。
        self._start_watchdog()

    def _on_pcm_chunk(self, chunk: bytes) -> None:
        """各 PCM chunk の RMS で無音判定 → 無音 N 秒で自動 stop。"""
        if not self._recording:
            return
        now = time.time()
        self._last_chunk_time = now  # watchdog の stall 判定用に活性を記録

        # 最長録音時間到達
        if now - self._record_start_time >= self.max_record_seconds:
            self._schedule_stop()
            return

        dbfs = chunk_dbfs(chunk)
        if dbfs >= self.silence_dbfs:
            # 有音: 発話開始フラグを立て、無音タイマーをリセット。
            self._speech_started = True
            self._silence_since = 0.0
            return

        # 無音
        if not self._speech_started:
            # 発話前のグレース期間。なでてから喋り出すまでの「考える時間」は止めない。
            # 猶予を過ぎても一度も有音が来なければ誤タップとみなして stop。
            if now - self._record_start_time >= self.initial_grace_seconds:
                self._schedule_stop()
            return

        # 発話開始後の通常無音判定: silence_seconds 続いたら stop。
        if self._silence_since == 0.0:
            self._silence_since = now
        elif now - self._silence_since >= self.silence_seconds:
            self._schedule_stop()

    def _start_watchdog(self) -> None:
        """PCM chunk に依存しない壁時計監視 thread を起動。

        無音/最長判定は `_on_pcm_chunk` の中だけにあり「chunk が流れ続けること」が
        前提。ファームが MIC mode に入れず PCM を吐かない / 途中で stream が死ぬと、
        その前提が崩れて停止判定が一切走らず録音状態に固まる (2026-05-30 #dev_stackchan_01
        実機で連続なで時に再現)。この watchdog が max_record_seconds / pcm_stall_seconds
        で必ず stop を蹴るので、chunk が来なくても自己回復する。
        """
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="stackchan-vc-watchdog"
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(WATCHDOG_POLL_SECONDS):
            if not self._recording:
                return
            now = time.time()
            # 最長録音到達 (chunk-based 判定が走らなかった場合の保険)。
            if now - self._record_start_time >= self.max_record_seconds:
                self._schedule_stop()
                return
            # PCM stall: chunk が pcm_stall_seconds 来ていない (1 つも来ていない場合は
            # 録音開始時刻を基準にする)。通常録音は 64ms 間隔で chunk が流れ続けるので
            # 発火せず、stream が実際に死んだ時だけ発火する。
            last = self._last_chunk_time or self._record_start_time
            if now - last >= self.pcm_stall_seconds:
                self._schedule_stop()
                return

    def _schedule_stop(self) -> None:
        """別 thread で stop_mic_recording + STT + POST を実行 (PCM callback の
        中で同期実行すると reader thread が join 待ちで dead-lock する)。"""
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
        self._stop_thread = threading.Thread(
            target=self._stop_and_process, daemon=True, name="stackchan-vc-stop"
        )
        self._stop_thread.start()

    def _stop_and_process(self) -> None:
        try:
            result = self.backend.stop_mic_recording()
        finally:
            with self._state_lock:
                self._recording = False
        if self.on_stop is not None:
            try:
                self.on_stop(result)
            except Exception:
                pass
        wav = result.get("wav", b"")
        pcm = result.get("pcm", b"")
        if len(pcm) < self.min_pcm_bytes:
            # 短すぎる (誤タップ or 発話なし)
            return

        # STT
        r = stt_module.transcribe(wav, language=self.language)
        text = r.get("text", "").strip()
        if self.on_transcribed is not None:
            try:
                self.on_transcribed(text, r)
            except Exception:
                pass

        # 履歴に追加 (STT 段階で記録 = 後段 POST 失敗時も text/STT 結果は残る)。
        entry = {
            "ts": time.time(),
            "text": text,
            "language": r.get("language"),
            "language_probability": r.get("language_probability"),
            "elapsed_seconds": r.get("elapsed_seconds"),
            "stt_status": r.get("status"),
            "duration_seconds": result.get("duration_seconds"),
            "frames": result.get("frames"),
            "sent_status": None,
        }
        self.history.append(entry)
        while len(self.history) > HISTORY_MAX:
            self.history.pop(0)

        if not text:
            return

        # xangi POST /api/chat。base_url 空指定なら POST 自体を skip (単体テスト
        # スクリプト / xangi 未起動の状態で STT 動作だけ確認したい場合用)。
        if not self.xangi_base_url:
            sent_info = {"skipped": True, "reason": "no xangi_base_url"}
        else:
            try:
                payload: dict = {"message": text}
                if self.app_session_id:
                    payload["appSessionId"] = self.app_session_id
                response = requests.post(
                    f"{self.xangi_base_url}/api/chat",
                    json=payload,
                    timeout=120,
                    stream=True,
                )
                # SSE 応答は購読側 (events.py) で処理するのでここでは close。
                response.close()
                sent_info = {"status_code": response.status_code, "url": response.url}
            except Exception as exc:
                sent_info = {"error": str(exc)}
        # 履歴に POST 結果を反映 (直前 entry を update)。
        if self.history:
            self.history[-1]["sent_status"] = (
                sent_info.get("status_code")
                or sent_info.get("error")
                or ("skipped" if sent_info.get("skipped") else None)
            )

        if self.on_sent is not None:
            try:
                self.on_sent(text, sent_info)
            except Exception:
                pass
