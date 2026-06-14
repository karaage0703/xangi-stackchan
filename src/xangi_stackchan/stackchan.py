import glob
import json
import os
import platform
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests
import serial
import serial.tools.list_ports

from .serial_actor import SerialActor


DEFAULT_BAUD = 921600
DEFAULT_WIFI_HOST = os.environ.get("STACKCHAN_IP", "192.168.1.100")


# デバイスごとの既定値プリセット。CLI --device-profile / 設定 UI で選択する。
# - baud:           USB シリアル baud rate
# - max_wav_bytes:  ファーム側の WAV 受信上限。超える size の WAV は送信前に
#                   早期 error で弾く。0 は無制限 (CoreS3 PSRAM 4MB クラス)
# - capabilities:   既定の機能可否 (実機 STATUS の servo/camera/torque で上書き)
DEVICE_PROFILES: dict[str, dict] = {
    "cores3_k151": {
        "baud": 921600,
        "max_wav_bytes": 0,
        "capabilities": {"servo": True, "camera": True, "mic": False},
        "description": "M5Stack 公式 K151 / K151-R (CoreS3 + サーボ + カメラ)",
    },
    "cores3_standalone": {
        "baud": 921600,
        "max_wav_bytes": 0,
        "capabilities": {"servo": False, "camera": True, "mic": False},
        "description": "M5Stack CoreS3 単体 (サーボ無し、カメラあり)",
    },
    "atoms3r": {
        "baud": 115200,
        "max_wav_bytes": 256 * 1024,
        "capabilities": {"servo": False, "camera": False, "mic": False},
        "description": "M5Stack AtomS3R + Atomic Voice / Echo Base",
    },
    "rt_beta": {
        "baud": 115200,
        "max_wav_bytes": 96 * 1024,
        "capabilities": {"servo": True, "camera": False, "mic": False},
        "skip_move_during_wav": True,
        "description": "アールティ Ver.β (M5Stack Basic + Feetech SCS0009 ×2)",
    },
}


def resolve_profile(name: str) -> dict | None:
    """device_profile 名 → プリセット dict。未知の名前は None。"""
    if not name:
        return None
    return DEVICE_PROFILES.get(name)


def estimate_wav_duration_seconds(wav_data: bytes) -> float:
    """RIFF/WAVE ヘッダから再生時間を秒で見積もる。

    16-bit PCM の典型的なフォーマットを想定。ヘッダが壊れている場合は 0 を返す
    (呼び出し側は 0 = 不明として扱う)。WAV 再生中の MOVE スキップ用 timer に使う。
    """
    if len(wav_data) < 44 or wav_data[:4] != b"RIFF" or wav_data[8:12] != b"WAVE":
        return 0.0
    try:
        channels = struct.unpack("<H", wav_data[22:24])[0]
        sample_rate = struct.unpack("<I", wav_data[24:28])[0]
        bits = struct.unpack("<H", wav_data[34:36])[0]
    except struct.error:
        return 0.0
    if sample_rate <= 0 or channels <= 0 or bits <= 0:
        return 0.0
    bytes_per_second = sample_rate * channels * (bits // 8)
    if bytes_per_second <= 0:
        return 0.0
    data_bytes = max(0, len(wav_data) - 44)
    return data_bytes / bytes_per_second


def pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int = 16000,
    bits: int = 16,
    channels: int = 1,
) -> bytes:
    """raw PCM (int16 LE mono が既定) を WAV bytes に wrap する。faster-whisper /
    whisper.cpp / wave モジュール / WAV ファイル保存に投入できる形式。

    `len(pcm_data)` が偶数 (bits=16 のとき) でない場合は末尾 1 byte を drop して
    WAV の sample alignment を保つ。空入力でも valid な 0 frame WAV を返す。
    """
    import io
    import wave

    bytes_per_sample = bits // 8
    if bytes_per_sample <= 0:
        raise ValueError(f"unsupported bits: {bits}")
    aligned_len = (len(pcm_data) // (bytes_per_sample * channels)) * (
        bytes_per_sample * channels
    )
    aligned = pcm_data[:aligned_len]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bytes_per_sample)
        w.setframerate(sample_rate)
        w.writeframes(aligned)
    return buf.getvalue()


def _ack_predicate_for(cmd: str) -> Callable[[str], bool]:
    """各コマンドの ack に含まれる特徴的なキー文字列を見て filter する predicate を返す。

    USB CDC のバッファリングで応答が前 transaction の expect_line timeout 後に
    届くと、次 transaction の queue に流れ込む。コマンド固有のシグネチャで
    filter すれば、本来の ack を確実に拾える + 他コマンドの ack は queue に
    積み戻されて (expect_line 内の挙動) 後続 transaction が拾える。
    """
    cmd_upper = cmd.split(":")[0] if ":" in cmd else cmd
    # 各コマンドの ack に必ず含まれるシグネチャ。'"error"' を含む行も常に通す
    # (error 応答は無視せず即返す)。
    signature_map = {
        "STATUS": '"state"',
        "VOLUME": '"volume"',
        "FACE": '"face"',
        "IMAGE": '"image"',
        "MOVE": '"yaw"',
        "PUZZLE": '"puzzle"',
        "STACKLED": '"stack_led"',
        "HEADTOUCH_AVATAR": '"head_touch_avatar"',
        "MIC_START": '"mode":"recording"',
        "MIC_STOP": '"mode":"speaker"',
    }
    signature = signature_map.get(cmd_upper)

    if signature is None:
        # 不明コマンド: generic JSON line (event を除く)
        def pred_generic(l: str) -> bool:
            return l.startswith("{") and '"event"' not in l
        return pred_generic

    sig = signature

    def pred_specific(l: str) -> bool:
        if not l.startswith("{") or '"event"' in l:
            return False
        if sig in l:
            return True
        # error ack は signature 持たないので別経路で通す (例: "queue full",
        # "not recording", "mic recording active" 等)
        if '"error"' in l:
            return True
        return False

    return pred_specific


def detect_serial_port() -> str:
    env_port = os.environ.get("STACKCHAN_PORT")
    if env_port:
        return env_port

    esp_vid = 0x303A
    bridge_vids = {0x10C4, 0x1A86}
    ports = list(serial.tools.list_ports.comports())
    for port in ports:
        if port.vid == esp_vid:
            return port.device
    for port in ports:
        if port.vid in bridge_vids:
            return port.device

    if platform.system() == "Darwin":
        candidates = glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*")
    else:
        candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    return candidates[0] if candidates else "/dev/ttyACM0"


class StackchanSerial:
    """USB serial backend for stackchan family devices (K151 / stackchan-atama)."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self.ser = None
        # Phase 2.x で SerialActor pattern に refactor。`actor` が唯一の serial reader、
        # writer は `actor.write()` を経由してシリアライズ。`_lock` は「transaction
        # (send_command/send_wav/capture) 全体を排他化する」役で、actor 内部の write_lock
        # とは別レイヤー。これで複数 thread が同時に readline する race が原理的に消える。
        self.actor: SerialActor | None = None
        self._lock = threading.RLock()
        # device_profile から渡される WAV サイズ上限 (0 = 無制限)。超過時は
        # send_wav 内で送信前に早期 error を返す。
        self.max_wav_bytes: int = 0
        # device_profile から渡される「WAV 再生中の MOVE スキップ」フラグ。
        # rt_beta (M5Stack Basic + アールティ PCB) では USB 5V/500mA とサーボ
        # 電源を Stack-chan PCB が共有するため、WAV 受信中のサーボ MOVE 連動で
        # 電流ラッシュ → USB ブラウンアウト → シリアル切断が起きる。True なら
        # send_command で MOVE が来た時 _wav_active=True の間スキップする。
        self.skip_move_during_wav: bool = False
        self._wav_active: bool = False
        self._wav_end_timer: threading.Timer | None = None
        # ファームから `{"event":"audio_stopped",...}` を受信したら True に立ち、
        # send_wav 冒頭でその WAV を skip する。app.py 側で turn.started 受信時に
        # False に戻して次 turn から通常動作復帰。
        self.user_stopped: bool = False
        # ファームから `{"event":"head_touch","gesture":"...","at":<ms>}` を受信した
        # 時のコールバック (M5Stackchan K151 のアタマタッチセンサ Si12T)。
        # gesture は press / release / swipe_forward / swipe_backward。app.py で
        # 録音開始トリガとして bind する想定。callback は drain() / send_command
        # 両方の経路から呼ばれるので、登録側は thread-safety を考慮する (現状の
        # SerialReader は drain は main thread、send_command は呼び出し側 thread)。
        self.on_head_touch: Callable[[dict], None] | None = None
        # ファームから `{"event":"mic_button","at":<ms>}` を受信した時のコールバック
        # (LCD 下部のマイクボタンを短くタップした時)。アタマセンサ (on_head_touch) と
        # 別トリガで、音声入力 (録音→STT→xangi) を起動するのに app.py で bind する。
        self.on_mic_button: Callable[[dict], None] | None = None
        # マイク録音状態。start_mic_recording / stop_mic_recording で操作。録音中は
        # _mic_reader_thread が別 thread で `MIC_PCM:<size>\n<binary>` を受信し続け、
        # _mic_pcm_buffer に蓄積 + (設定されていれば) _mic_on_pcm_chunk callback に
        # チャンクを渡す。WAV 再生・他コマンドは録音中は競合する (I2S 共有のため
        # ファームが Speaker を止めている) ので、host 側でも mutual exclusion を取る。
        self._mic_recording: bool = False
        self._mic_reader_thread: threading.Thread | None = None
        self._mic_pcm_buffer: bytearray = bytearray()
        self._mic_on_pcm_chunk: Callable[[bytes], None] | None = None
        # 稼働中のシリアル切断 (デバイス再起動 / USB 再列挙で ttyACMx が変わる) からの
        # 自動再接続。SerialActor.on_dead → _on_serial_dead → 専用 thread が
        # close + open を reconnect_interval 間隔で成功するまで繰り返す。
        # 切断中の send_* は fail-fast でエラー dict を返す (呼び出し側の sprite
        # animation / SSE event loop をブロックしない)。再接続成功時は
        # on_reconnected callback (app.py が音量・表情の再初期化を登録) を呼ぶ。
        self.reconnect_enabled: bool = True
        self.reconnect_interval: float = 3.0
        self.on_reconnected: Callable[[], None] | None = None
        self._disconnected = threading.Event()
        self._reconnect_thread: threading.Thread | None = None
        self._closing = False

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=5)
        time.sleep(0.5)
        self._acquire_exclusive_lock()
        # OS の serial 受信 buffer に前回 session の古い ack / log が残っていると、
        # 次の send_command で expect_line がそれを拾って ack 取り違えが起きる
        # (2026-05-27 22:00 で発生)。actor 起動前にリセットして「これから来る
        # 応答だけを見る」状態にする。
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        # SerialActor が唯一の serial reader として常時 read + parse する。
        # 非同期 event (head_touch / audio_stopped / mic_watchdog_timeout) は
        # add_line_listener 経由で _on_line に流す。
        self.actor = SerialActor(self.ser)
        self.actor.add_line_listener(self._on_line)
        self.actor.on_dead = self._on_serial_dead
        self.actor.start()
        # ファーム reset 直後の boot ログや前回 session の遅延応答が USB CDC TX
        # buffer に残っている。actor を 500ms 動かしてそれらを line listener 側で
        # 消費させ、最後に start_transaction の clear で expect_queue からも除去。
        # これで最初の send_command が前回の残骸 ack を拾う事故を防ぐ
        # (2026-05-27 22:53 で VOLUME ack ズレの真因)。
        time.sleep(0.5)
        self.actor.start_transaction()
        # 前回 session が SIGKILL/USB 切断等で MIC_STOP を送れずに死ぬと、ファームが
        # MIC モードのまま PCM stream を吐き続けて次回起動時のシリアル通信が破綻する。
        # ファーム watchdog (60s) を待つより、open() で強制 MIC_STOP を打って即時復帰。
        # 既に speaker モードなら `{"status":"error","error":"not recording"}` が
        # 返るだけで害は無い。応答内容は気にせず ack だけ受けて drop。
        try:
            self.actor.write(b"MIC_STOP\n")
            self.actor.expect_line(
                lambda l: l.startswith("{") and "event" not in l, timeout=2.0
            )
        except Exception:
            pass
        # MIC_PCM tail (binary) が来てるかもしれないので少し待ってから queue 再 clear。
        time.sleep(0.3)
        self.actor.start_transaction()

    def _acquire_exclusive_lock(self) -> None:
        """同一 USB serial port を複数プロセスが同時に開かないよう排他 lock を取得。

        Linux/Mac は `fcntl.flock(fd, LOCK_EX|LOCK_NB)` で OS レベルの排他制御。
        既に他プロセスが lock を持っていれば即 RuntimeError で abort。

        2026-05-27 実機検証で発覚: nohup 経由で複数プロセスを起動して PID kill が
        親 wrapper だけに当たり、4 プロセスが同 ACM port を同時アクセス →
        multi access on port 警告 + シリアル汚染 + 録音 PCM 破綻と全て壊れた。

        Windows は flock 非対応なので noop (代わりに OS の排他 open が効くはず)。
        環境変数 `STACKCHAN_NO_SERIAL_LOCK=1` で明示的に無効化可能。
        """
        if os.environ.get("STACKCHAN_NO_SERIAL_LOCK", "") in ("1", "true"):
            return
        if platform.system() == "Windows":
            return
        try:
            import fcntl

            fcntl.flock(self.ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            raise RuntimeError(
                f"{self.port} is already in use by another process "
                f"(fcntl.flock LOCK_EX failed: {exc}). "
                "STACKCHAN_NO_SERIAL_LOCK=1 で lock を無効化できる。"
            )

    def close(self):
        self._closing = True
        self._teardown()

    def _teardown(self):
        """actor / serial fd を黙って閉じる (close と reconnect 共用)。"""
        if self.actor is not None:
            try:
                self.actor.stop()
            except Exception:
                pass
            self.actor = None
        if self.ser:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None

    @property
    def is_connected(self) -> bool:
        return not self._disconnected.is_set()

    def _on_serial_dead(self, reason: str) -> None:
        """SerialActor からの切断通知 (reader/writer thread 上で呼ばれる)。

        重い処理はせず、フラグを立てて再接続 thread を起こすだけ。
        """
        if self._closing or not self.reconnect_enabled:
            return
        if self._disconnected.is_set():
            return
        self._disconnected.set()
        print(
            json.dumps({"serial": "disconnected", "reason": reason, "port": self.port}),
            flush=True,
        )
        t = self._reconnect_thread
        if t is not None and t.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="stackchan-serial-reconnect"
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """切断後、成功するまで close + open を繰り返す。

        port には udev 固定パス (/dev/stackchan) や /dev/serial/by-id/... の
        安定パスが入っている前提なので、デバイスが再列挙されても同じパスで
        開き直せる。USB が物理的に抜かれている間は open が失敗し続けるが、
        挿し直された時点で次の試行が成功する (無期限リトライ)。
        """
        attempt = 0
        while not self._closing:
            attempt += 1
            try:
                with self._lock:
                    self._teardown()
                    self.open()
            except Exception as exc:
                if attempt == 1 or attempt % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "serial": "reconnect_failed",
                                "attempt": attempt,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        ),
                        flush=True,
                    )
                time.sleep(self.reconnect_interval)
                continue
            self._disconnected.clear()
            print(
                json.dumps({"serial": "reconnected", "port": self.port, "attempt": attempt}),
                flush=True,
            )
            cb = self.on_reconnected
            if cb is not None:
                try:
                    cb()
                except Exception as exc:
                    print(
                        json.dumps({"serial": "reconnect_callback_error", "error": str(exc)}),
                        flush=True,
                    )
            return

    def drain(self):
        # SerialActor 移行後は no-op。actor が常時 ser.read() で byte stream を
        # 消費し、line / binary を parser で分類するので、外部 drain は不要。
        # 互換のため method 自体は残す (古い callers が呼んでも害無し)。
        return

    def _resync_serial(self) -> None:
        """IMAGE/WAV の READY/ack が来なかった時に呼ぶ再同期処理。

        READY 不達 = ファームがまだ前フレームの受信ループに居座っている可能性が
        高い。ファーム側のチャンク受信タイムアウト (IMAGE_CHUNK_TIMEOUT_MS) を超えて
        待ってからコマンド待ちに復帰させ、その間に溜まった OS RX バッファの遅延
        READY / エラー行を expect_queue ごとクリアして、次フレームをクリーンな状態
        から始める。ファーム側タイムアウト短縮との二段構えでフレーム境界ずれの恒久化
        を防ぐ (host が次の IMAGE:<size> を送る前にファームが必ず受信ループを抜ける)。
        """
        if self.actor is None:
            return
        # ファームの受信ループが必ず抜けるまで待つ (IMAGE_CHUNK_TIMEOUT_MS=1500ms +
        # 余裕)。環境変数で調整可能。
        wait_s = float(os.environ.get("STACKCHAN_RESYNC_WAIT_SECONDS", "1.8"))
        time.sleep(wait_s)
        # 遅延して届いた READY / 中間バイナリ由来のゴミ行を捨てる。
        try:
            self.actor.start_transaction()
        except Exception:
            pass

    def _on_line(self, line: str) -> None:
        """SerialActor から全 line が dispatch される入口。非同期 event を検出して
        フラグ / callback に反映する。transaction の応答行は actor の expect_line
        側で別途取得するので、ここでは event 検出のみ。
        """
        self._detect_async_event(line)

    def _detect_async_event(self, line: str) -> bool:
        """ファームからの非同期 event 行 (`{"event":"audio_stopped",...}` /
        `{"event":"head_touch",...}`) を検知して backend 内部フラグ or callback
        に dispatch する。検知した場合 True を返す (応答行としては使わない)。
        """
        if '"event"' not in line:
            return False
        if "audio_stopped" in line:
            self.user_stopped = True
            return True
        if "head_touch" in line:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # event 風だが parse 失敗。応答行扱いにせず drop。
                return True
            if self.on_head_touch is not None:
                try:
                    self.on_head_touch(data)
                except Exception:
                    # listener 側の例外でシリアル受信ループを壊さない。
                    pass
            return True
        if "mic_button" in line:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return True
            if self.on_mic_button is not None:
                try:
                    self.on_mic_button(data)
                except Exception:
                    pass
            return True
        return False

    # === マイク録音 PCM stream ================================================
    # ファームに `MIC_START\n` → ack → 別 thread で `MIC_PCM:<size>\n<binary>` を
    # 受信し続ける → `MIC_STOP\n` → ack で停止。蓄積した PCM を WAV にして返す。
    # CoreS3 は Mic と Speaker が I2S を共有するため、ファーム側で録音中は
    # Speaker.end() している。録音中の WAV 送信や他コマンドは挙動が壊れるので、
    # host 側でも _mic_recording=True の間は他経路を抑止する想定 (現状は呼び出し
    # 側で気をつける運用、必要なら _lock で強制ガードに昇格)。

    def start_mic_recording(
        self,
        on_pcm_chunk: Callable[[bytes], None] | None = None,
    ) -> dict:
        """`MIC_START` をファームに送り、ack 取得後に MIC_PCM stream 受信 thread を
        起動する。on_pcm_chunk が指定されれば 1 チャンク (64ms 分、2048 byte) 受信
        ごとに呼ばれる。蓄積バッファは stop_mic_recording で返す。
        """
        if self._mic_recording:
            return {"status": "error", "error": "already recording"}

        with self._lock:
            # 録音中の binary 受信は actor の MIC_PCM listener 経由で buffer に積む。
            # mic_reader_thread の自前 readline 経路は廃止 (race の根)。
            self._mic_pcm_buffer = bytearray()
            self._mic_on_pcm_chunk = on_pcm_chunk
            self.actor.set_mic_pcm_listener(self._on_mic_pcm_chunk)

            self.actor.start_transaction()
            try:
                self.actor.write(b"MIC_START\n")
                ack_line = self.actor.expect_line(
                    lambda l: l.startswith("{") and "event" not in l, timeout=2.0
                )
            finally:
                self.actor.end_transaction()

            if ack_line is None:
                self.actor.set_mic_pcm_listener(None)
                return {"status": "error", "error": "no mic start ack"}
            try:
                ack = json.loads(ack_line)
            except json.JSONDecodeError:
                self.actor.set_mic_pcm_listener(None)
                return {"status": "error", "error": "invalid ack json", "raw": ack_line}
            if ack.get("status") != "ok":
                self.actor.set_mic_pcm_listener(None)
                return {"status": "error", "error": "mic start failed", "raw": ack}

            self._mic_recording = True
        return ack

    def _on_mic_pcm_chunk(self, body: bytes) -> None:
        """SerialActor から MIC_PCM body が来た時の dispatch。buffer に積み + callback。
        actor の reader thread から呼ばれる。"""
        if not self._mic_recording:
            return
        self._mic_pcm_buffer.extend(body)
        if self._mic_on_pcm_chunk is not None:
            try:
                self._mic_on_pcm_chunk(body)
            except Exception:
                pass

    def stop_mic_recording(self) -> dict:
        """`MIC_STOP` をファームに送って録音を終了、蓄積した PCM を WAV bytes に
        wrap して返す。返却 dict には `pcm` (raw int16 LE) と `wav` の両方を含む。

        SerialActor 経由: MIC_STOP 送信前から end_transaction せず、ファームが
        停止する前に送ってくる末尾 MIC_PCM frame は actor の binary parser 経由で
        引き続き _mic_pcm_buffer に積まれる。ack JSON `{"status":"ok","mode":"speaker"}`
        を expect_line で待つ。
        """
        if not self._mic_recording:
            return {"status": "error", "error": "not recording"}

        with self._lock:
            self.actor.start_transaction()
            try:
                self.actor.write(b"MIC_STOP\n")
                ack_line = self.actor.expect_line(
                    lambda l: l.startswith("{") and "event" not in l, timeout=3.0
                )
            finally:
                self.actor.end_transaction()

            # 録音終了 → 以後の MIC_PCM body は無視する。
            self._mic_recording = False
            self.actor.set_mic_pcm_listener(None)

        try:
            ack = json.loads(ack_line) if ack_line else None
        except json.JSONDecodeError:
            ack = None

        pcm_bytes = bytes(self._mic_pcm_buffer)
        wav_bytes = pcm_to_wav(pcm_bytes, sample_rate=16000, bits=16, channels=1)
        result = dict(ack) if ack else {"status": "error", "error": "no ack"}
        result.update({
            "pcm": pcm_bytes,
            "wav": wav_bytes,
            "sample_rate": 16000,
            "bits": 16,
            "channels": 1,
            "frames": len(pcm_bytes) // 2,
            "duration_seconds": (len(pcm_bytes) // 2) / 16000.0,
        })
        return result

    def _is_disconnected(self) -> bool:
        # tests は __new__ で部分構築するので getattr で防御 (未初期化 = 接続扱い)
        ev = getattr(self, "_disconnected", None)
        return ev is not None and ev.is_set()

    def _disconnected_error(self) -> dict:
        return {
            "status": "error",
            "error": "serial disconnected (auto-reconnect in progress)",
            "disconnected": True,
        }

    def send_command(self, cmd: str) -> dict:
        if self._is_disconnected():
            return self._disconnected_error()
        # WAV 再生中の MOVE は skip_move_during_wav=True 時にスキップ。電流ラッシュ
        # 由来の USB シリアル切断を避けるための rt_beta 向け mutual exclusion。
        if self.skip_move_during_wav and self._wav_active and cmd.startswith("MOVE:"):
            return {"status": "skipped", "cmd": cmd, "reason": "wav playing"}
        with self._lock:
            self.actor.start_transaction()
            try:
                self.actor.write(f"{cmd}\n".encode())
                # コマンド別の ack シグネチャで filter する。USB CDC バッファリングで
                # 応答が遅延すると、前 transaction の ack がこの transaction の
                # queue に流れ込んで誤拾いする事故 (2026-05-27 23:27 で頻発) を防ぐ。
                pred = _ack_predicate_for(cmd)
                ack_line = self.actor.expect_line(pred, timeout=3.0)
            finally:
                self.actor.end_transaction()
            if ack_line is None:
                return {"raw": ""}
            try:
                return json.loads(ack_line)
            except json.JSONDecodeError:
                return {"raw": ack_line}

    def send_wav(self, wav_data: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        if not wav_data:
            return {"status": "error", "error": "empty WAV"}
        if self._is_disconnected():
            return self._disconnected_error()

        # ユーザがファーム LCD を長押しで stop した状態 (audio_stopped event 受信済)。
        # 次の turn.started が来るまでホスト側でこのフラグを True に保持して、
        # 後続 chunk の WAV 送信を全てスキップする (黙る挙動)。
        if self.user_stopped:
            return {"status": "skipped", "reason": "user_stopped", "size": len(wav_data)}

        # マイク録音中はファームが Speaker.end() 状態 + シリアルに MIC_PCM stream を
        # 流している。この間に WAV 送信を試みるとシリアル binary 衝突 + Speaker
        # 無効で再生不可 + multi access on port 警告 + 録音 PCM 破綻と全て壊れる。
        # voice_conversation モードでは MIC_STOP 後 (Speaker 復帰後) に送るのが
        # 正しい挙動なので、ここでは skip 応答を返して呼び出し側に判断を委ねる。
        if self._mic_recording:
            return {"status": "skipped", "reason": "mic_recording", "size": len(wav_data)}

        # device_profile (rt_beta / atoms3r 等) で渡された WAV サイズ上限の早期
        # チェック。例: basic-main (M5Stack Basic) は内部 DRAM 96KB 制約。超過時は
        # ファーム側でも error が返るが、シリアル送信のロード自体を避けるため
        # ホスト側で先にブロック。0 = 無制限 (CoreS3 PSRAM 4MB クラス)。
        if self.max_wav_bytes > 0 and len(wav_data) > self.max_wav_bytes:
            return {
                "status": "error",
                "error": "exceeds device profile max_wav_bytes",
                "size": len(wav_data),
                "max_wav_bytes": self.max_wav_bytes,
            }

        # WAV 送信開始の直前に _wav_active=True をセット。skip_move_during_wav=True
        # (rt_beta) の場合、これで TalkingSway 等の並列 MOVE 送信を WAV 送信中
        # ずっと skip させる。送信開始**前**にフラグを立てるのが重要 (ack 受信後だと
        # 最初の sway と WAV chunk 1 が race して USB 電源ラッシュで切れる事例あり、
        # 2026-05-24 実機検証で発覚)。再生は WAV ack 後にファーム側で非同期実行
        # されるが、host 側から見える「サーボに静かにしていてほしい期間」は WAV
        # 送信中 + 再生推定時間まで。前者は send_wav 呼び出し中の状態、後者は
        # 推定時間 timer で end する。
        if self.skip_move_during_wav:
            self._begin_wav_active(estimate_wav_duration_seconds(wav_data) or 1.0)

        # WAV キュー実装: ファーム (xangi-bridge-0.4+) は WAV キューが満杯なら受信前に
        # `{"status":"error","error":"queue full"}` を返す。再生中のスロットが
        # 空くまで短く sleep + retry する (キュー 4 slot なので最悪でも 1 chunk
        # 分の再生時間 = 数秒待てば必ず空く)。リトライ中もシリアル排他は維持。
        try:
            for attempt in range(8):
                with self._lock:
                    result = self._send_wav_locked(wav_data, chunk_size, chunk_delay)
                if result.get("status") == "error" and result.get("error") == "queue full":
                    time.sleep(0.5)
                    continue
                return result
            return result
        finally:
            # WAV 送信エラー (USB 切断・recv timeout 等) ですぐ skip 解除すると、
            # 再生中のサーボラッシュを引き続き避けたいケースで早すぎる。timer は
            # _begin_wav_active で既に起動済 (推定再生時間で auto end) なので、
            # ここでは何もしない。次の send_wav 呼び出し時に古い timer は
            # _begin_wav_active 内で cancel されて新タイマーに置き換わる。
            pass

    def send_image(self, image_jpeg: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        """Send a JPEG face image to firmware via IMAGE:<size>."""
        if not image_jpeg:
            return {"status": "error", "error": "empty image"}
        if self._is_disconnected():
            return self._disconnected_error()
        with self._lock:
            self.actor.start_transaction()
            try:
                self.actor.write(f"IMAGE:{len(image_jpeg)}\n".encode())
                ready_or_err = self.actor.expect_line(
                    lambda l: l == "READY"
                    or (
                        l.startswith("{")
                        and ('"image"' in l or '"error"' in l)
                        and '"event"' not in l
                    ),
                    timeout=3.0,
                )
                if ready_or_err is None:
                    # READY が来ない = ファームがまだ前フレームの受信ループに居座って
                    # いるか、コマンド/バイナリの境界がずれた状態。ここで JPEG 本体を
                    # 送らず、入力バッファを掃除して次フレームをクリーンな状態から
                    # 始められるようにする (フレーム境界ずれの恒久化を防ぐ)。
                    self._resync_serial()
                    return {"status": "error", "error": "no READY response"}
                if ready_or_err != "READY":
                    try:
                        return json.loads(ready_or_err)
                    except json.JSONDecodeError:
                        return {"status": "error", "error": "invalid early ack", "raw": ready_or_err}

                sent = 0
                while sent < len(image_jpeg):
                    end = min(sent + chunk_size, len(image_jpeg))
                    self.actor.write(image_jpeg[sent:end])
                    sent = end
                    if chunk_delay > 0:
                        time.sleep(chunk_delay)

                ack_line = self.actor.expect_line(
                    lambda l: l.startswith("{")
                    and ('"image"' in l or '"error"' in l)
                    and '"event"' not in l,
                    timeout=5.0,
                )
                if ack_line is None:
                    return {"status": "error", "error": "no image ack"}
                try:
                    return json.loads(ack_line)
                except json.JSONDecodeError:
                    return {"status": "error", "error": "invalid image ack json", "raw": ack_line}
            finally:
                self.actor.end_transaction()

    def send_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        rgb565: bytes,
        chunk_size: int = 4096,
        chunk_delay: float = 0.0,
    ) -> dict:
        """Send an RGB565 dirty rectangle to firmware via RECT:x,y,w,h,size."""
        if width <= 0 or height <= 0:
            return {"status": "skipped", "reason": "empty rect"}
        if not rgb565:
            return {"status": "error", "error": "empty rect"}
        expected = width * height * 2
        if len(rgb565) != expected:
            return {"status": "error", "error": "rect size mismatch", "size": len(rgb565), "expected": expected}
        if self._is_disconnected():
            return self._disconnected_error()

        with self._lock:
            self.actor.start_transaction()
            try:
                self.actor.write(f"RECT:{x},{y},{width},{height},{len(rgb565)}\n".encode())
                ready_or_err = self.actor.expect_line(
                    lambda l: l == "READY"
                    or (
                        l.startswith("{")
                        and ('"rect"' in l or '"error"' in l)
                        and '"event"' not in l
                    ),
                    timeout=3.0,
                )
                if ready_or_err is None:
                    return {"status": "error", "error": "no READY response"}
                if ready_or_err != "READY":
                    try:
                        return json.loads(ready_or_err)
                    except json.JSONDecodeError:
                        return {"status": "error", "error": "invalid early ack", "raw": ready_or_err}

                sent = 0
                while sent < len(rgb565):
                    end = min(sent + chunk_size, len(rgb565))
                    self.actor.write(rgb565[sent:end])
                    sent = end
                    if chunk_delay > 0:
                        time.sleep(chunk_delay)

                ack_line = self.actor.expect_line(
                    lambda l: l.startswith("{")
                    and ('"rect"' in l or '"error"' in l)
                    and '"event"' not in l,
                    timeout=5.0,
                )
                if ack_line is None:
                    return {"status": "error", "error": "no rect ack"}
                try:
                    return json.loads(ack_line)
                except json.JSONDecodeError:
                    return {"status": "error", "error": "invalid rect ack json", "raw": ack_line}
            finally:
                self.actor.end_transaction()

    def _begin_wav_active(self, duration_seconds: float) -> None:
        """`_wav_active` を True にし、`duration_seconds` 秒後に False へ戻す
        タイマーを仕掛ける。連続 WAV 送信時は古い timer を cancel して新しい
        終了時刻で上書きする (累積で MOVE スキップ期間が伸びる、これは複数
        WAV キューイング時の意図通り)。
        """
        with self._lock:
            self._wav_active = True
            if self._wav_end_timer is not None:
                self._wav_end_timer.cancel()
            self._wav_end_timer = threading.Timer(max(0.1, duration_seconds), self._end_wav_active)
            self._wav_end_timer.daemon = True
            self._wav_end_timer.start()

    def _end_wav_active(self) -> None:
        with self._lock:
            self._wav_active = False
            self._wav_end_timer = None

    def _send_wav_locked(self, wav_data: bytes, chunk_size: int, chunk_delay: float) -> dict:
        # SerialActor 経由: WAV transaction を 1 つの start_transaction 内で完結。
        # 1. WAV:<size>\n を書く
        # 2. READY 行 / JSON ack エラー どちらか早い方を expect_line で待つ
        #    (event 行は actor 側で自動 skip。size キーを持たない他コマンドの
        #     ack はぐれを除外したいので、size または error を含む JSON のみ通す)
        # 3. READY なら chunk 送信 → ack 待ち
        self.actor.start_transaction()
        try:
            self.actor.write(f"WAV:{len(wav_data)}\n".encode())
            ready_or_err = self.actor.expect_line(
                lambda l: l == "READY"
                or (
                    l.startswith("{")
                    and ('"size"' in l or '"error"' in l)
                    and '"event"' not in l
                ),
                timeout=3.0,
            )
            if ready_or_err is None:
                return {"status": "error", "error": "no READY response"}
            if ready_or_err != "READY":
                # 事前エラー (queue full / size=0 / size exceeds / ps_malloc failed)
                try:
                    return json.loads(ready_or_err)
                except json.JSONDecodeError:
                    return {"status": "error", "error": "invalid early ack", "raw": ready_or_err}

            # binary 送信
            sent = 0
            while sent < len(wav_data):
                end = min(sent + chunk_size, len(wav_data))
                self.actor.write(wav_data[sent:end])
                sent = end
                if chunk_delay > 0:
                    time.sleep(chunk_delay)

            # ack 待ち。WAV ack シグネチャ: {"status":"ok","size":N,"queued":n}。
            # MOVE/FACE ack のはぐれ (size を持たない) は actor 側 expect_line の
            # predicate で弾く。error ack も通す。
            ack_line = self.actor.expect_line(
                lambda l: l.startswith("{")
                and ('"size"' in l or '"error"' in l)
                and '"event"' not in l,
                timeout=10.0,
            )
            if ack_line is None:
                return {"status": "ok", "size": len(wav_data), "note": "no confirmation received"}
            try:
                return json.loads(ack_line)
            except json.JSONDecodeError:
                return {"status": "ok", "size": len(wav_data), "note": "invalid ack json", "raw": ack_line}
        finally:
            self.actor.end_transaction()

    def capture(self, timeout: float = 5.0) -> dict:
        """Phase 1A: CAPTURE コマンドで CoreS3 内蔵カメラ (GC0308) から JPEG 1 枚取得。

        プロトコル (詳細は docs/xangi_bridge_protocol.md):
          ホスト送信:  CAPTURE\n
          ファーム応答 (成功):
            IMG:<size>\n
            <size bytes JPEG binary>
            {"status":"ok","size":N,"format":"jpeg","width":W,"height":H,
             "captured_at":<ms>}\n
          ファーム応答 (失敗):
            {"status":"error","error":"..."}\n

        返り値: 成功時 {"status":"ok","image_jpeg":bytes,"size":N,"format":"jpeg",
                       "width":W,"height":H,"captured_at_device_ms":<device millis>,
                       "captured_at":<host epoch sec, float>}
                失敗時 {"status":"error","error":"..."}

        シリアル排他: self._lock で他 WAV/MOVE/FACE と直列化。
        """
        with self._lock:
            return self._capture_locked(timeout)

    def _capture_locked(self, timeout: float) -> dict:
        """SerialActor 経由: CAPTURE → IMG header → JPEG body → ack JSON。
        IMG body は actor の binary parser が拾って pop_img_data で取れる。"""
        host_capture_start = time.time()
        self.actor.start_transaction()
        try:
            # 古い IMG body は破棄
            self.actor.pop_img_data()
            self.actor.write(b"CAPTURE\n")

            # error JSON が先に来るか、IMG header + body 後の ack JSON か。
            # IMG header 自体は actor 内で binary mode に遷移する効果しかなく
            # line listener には出ない (size のサニティチェックも内部)。
            # → ack JSON を待ち、その時点で pop_img_data が body を持っていれば成功。
            ack_line = self.actor.expect_line(
                lambda l: l.startswith("{") and '"event"' not in l,
                timeout=max(timeout, 5.0),
            )
            if ack_line is None:
                return {"status": "error", "error": "no capture ack (timeout)"}
            try:
                ack = json.loads(ack_line)
            except json.JSONDecodeError:
                return {"status": "error", "error": "invalid capture ack json", "raw": ack_line}

            if ack.get("status") == "error":
                return ack

            # 画像本体を pop。binary parser が IMG header 後に拾ってる想定。
            jpeg = self.actor.pop_img_data()
            if jpeg is None:
                return {"status": "error", "error": "ack received but no image body"}

            device_ms = ack.pop("captured_at", None)
            if isinstance(device_ms, (int, float)):
                ack["captured_at_device_ms"] = int(device_ms)
            ack["image_jpeg"] = jpeg
            ack["captured_at"] = host_capture_start
            return ack
        finally:
            self.actor.end_transaction()


class StackchanWifi:
    """WiFi HTTP API backend for stackchan family devices (K151 / stackchan-atama)."""

    def __init__(self, host: str = DEFAULT_WIFI_HOST):
        self.base_url = f"http://{host}"

    def open(self):
        return None

    def close(self):
        return None

    def send_command(self, cmd: str) -> dict:
        if cmd == "STATUS":
            response = requests.get(f"{self.base_url}/status", timeout=5)
        elif cmd.startswith("FACE:"):
            expression = cmd.split(":", 1)[1]
            response = requests.get(f"{self.base_url}/face", params={"expression": expression}, timeout=5)
        elif cmd.startswith("VOLUME:"):
            level = cmd.split(":", 1)[1]
            response = requests.get(f"{self.base_url}/setting", params={"volume": level}, timeout=5)
        else:
            return {"status": "error", "error": f"unsupported WiFi command: {cmd}"}
        response.raise_for_status()
        return response.json()

    def send_wav(self, wav_data: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        if not wav_data:
            return {"status": "error", "error": "empty WAV"}
        response = requests.post(
            f"{self.base_url}/play",
            data=wav_data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def send_image(self, image_jpeg: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        return {"status": "error", "error": "WiFi IMAGE not implemented"}

    def capture(self, timeout: float = 5.0) -> dict:
        # WiFi 経由のカメラ取得は Phase 2 (WiFi MJPEG ストリーム) で実装予定。
        # Phase 1A は USB シリアル経由のみ。
        return {"status": "error", "error": "WiFi capture not implemented (Phase 2)"}


@dataclass
class StackchanConfig:
    wifi: bool = False
    host: str = DEFAULT_WIFI_HOST
    port: str = ""
    baud: int = DEFAULT_BAUD
    device_profile: str = ""
    max_wav_bytes: int = 0  # 0 = 無制限 (ファーム側に任せる)
    skip_move_during_wav: bool = False  # rt_beta 等の電源マージン制約用


def apply_profile_defaults(config: StackchanConfig) -> StackchanConfig:
    """device_profile が指定されていれば、未設定フィールドにプリセット値を埋める。

    明示指定 (CLI / config.json で 0 以外) は常に優先、profile 値で上書きしない。
    profile 未指定 or 未知の名前なら何もしない。
    """
    profile = resolve_profile(config.device_profile)
    if profile is None:
        return config
    if config.baud == DEFAULT_BAUD and profile.get("baud"):
        # CLI で baud を明示してない場合のみ profile の baud を採用
        config.baud = profile["baud"]
    if config.max_wav_bytes == 0:
        config.max_wav_bytes = profile.get("max_wav_bytes", 0)
    if not config.skip_move_during_wav:
        config.skip_move_during_wav = profile.get("skip_move_during_wav", False)
    return config


def create_backend(config: StackchanConfig):
    if config.wifi:
        return StackchanWifi(config.host)
    backend = StackchanSerial(config.port or detect_serial_port(), config.baud)
    backend.max_wav_bytes = config.max_wav_bytes
    backend.skip_move_during_wav = config.skip_move_during_wav
    return backend
