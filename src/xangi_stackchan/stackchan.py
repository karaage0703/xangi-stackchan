import glob
import json
import os
import platform
import struct
import threading
import time
from dataclasses import dataclass

import requests
import serial
import serial.tools.list_ports


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
        # シリアルバスは USB 1 本の共有資源。WAV 転送中に MOVE/FACE/VOLUME などの
        # テキストコマンドが割り込むと WAV データに ASCII バイト列が混入し、
        # playWav 失敗・no READY response・ノイズ再生を引き起こす。RLock で
        # send_command / send_wav 全体を直列化する。
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

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=5)
        time.sleep(0.5)
        self.drain()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def drain(self):
        # 単に捨てるのではなく、行単位で読んで非同期 event 行 (audio_stopped 等) を
        # フラグに反映してから捨てる。これで send_wav 前の drain でユーザ stop を
        # 取りこぼさない。
        while self.ser and self.ser.in_waiting:
            try:
                raw = self.ser.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._detect_async_event(line)

    def _detect_async_event(self, line: str) -> bool:
        """ファームからの非同期 event 行 (`{"event":"audio_stopped",...}`) を検知して
        backend 内部フラグを立てる。検知した場合 True を返す (応答行としては使わない)。
        """
        if '"event"' in line and "audio_stopped" in line:
            self.user_stopped = True
            return True
        return False

    def send_command(self, cmd: str) -> dict:
        # WAV 再生中の MOVE は skip_move_during_wav=True 時にスキップ。電流ラッシュ
        # 由来の USB シリアル切断を避けるための rt_beta 向け mutual exclusion。
        if self.skip_move_during_wav and self._wav_active and cmd.startswith("MOVE:"):
            return {"status": "skipped", "cmd": cmd, "reason": "wav playing"}
        with self._lock:
            self.ser.write(f"{cmd}\n".encode())
            self.ser.flush()
            time.sleep(0.2)
            response = ""
            while self.ser.in_waiting:
                line = self.ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if self._detect_async_event(line):
                    continue  # 非同期 event はフラグだけ立てて応答とは別扱い
                response = line
            try:
                return json.loads(response)
            except json.JSONDecodeError:
                return {"raw": response}

    def send_wav(self, wav_data: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        if not wav_data:
            return {"status": "error", "error": "empty WAV"}

        # ユーザがファーム LCD を長押しで stop した状態 (audio_stopped event 受信済)。
        # 次の turn.started が来るまでホスト側でこのフラグを True に保持して、
        # 後続 chunk の WAV 送信を全てスキップする (黙る挙動)。
        if self.user_stopped:
            return {"status": "skipped", "reason": "user_stopped", "size": len(wav_data)}

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
        self.drain()
        self.ser.write(f"WAV:{len(wav_data)}\n".encode())
        self.ser.flush()

        # READY 待ち。READY が来ればバイナリ送信フェーズに進む。`{` で始まる行が
        # 来た場合はファームが事前エラー (queue full / size=0 / size exceeds /
        # ps_malloc failed) を返したと見なして JSON を即返す (Step G で追加された
        # 早期エラーパス)。
        deadline = time.time() + 3
        ready = False
        while time.time() < deadline:
            if self.ser.in_waiting:
                line = self.ser.readline().decode("utf-8", errors="replace").strip()
                if line == "READY":
                    ready = True
                    break
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        pass
            time.sleep(0.05)
        if not ready:
            return {"status": "error", "error": "no READY response"}

        sent = 0
        while sent < len(wav_data):
            end = min(sent + chunk_size, len(wav_data))
            self.ser.write(wav_data[sent:end])
            self.ser.flush()
            sent = end
            time.sleep(chunk_delay)

        # ack 待ち。`readline()` は self.ser のグローバル timeout (=5s) で
        # \n まで block するので、in_waiting で来た分だけ読んで自前で行分割
        # する (デバイス側が大量のデバッグログを流す場合に readline ブロックで
        # deadline を越えてしまう問題への対策)。
        deadline = time.time() + 10
        buf = b""
        while time.time() < deadline:
            avail = self.ser.in_waiting
            if avail > 0:
                buf += self.ser.read(avail)
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # WAV ack のシグネチャ: ファーム xangi-bridge-0.4+ は
                    # `{"status":"ok","size":N,"queued":n}` を返す。前 MOVE
                    # ack や FACE ack (yaw/pitch/face フィールドを持つ) が
                    # シリアル TX の遅延ではぐれてここに紛れ込むことがある
                    # ので、`size` キーが無い ack は捨てて次の行を読む。
                    # エラー ack (`error` キー有り) は WAV 起因なのでそのまま
                    # 返す (size 無くても識別可能)。
                    if "size" in parsed or "error" in parsed:
                        return parsed
                    # それ以外 (他コマンドの ack のはぐれ) は捨てて継続
                    continue
            else:
                time.sleep(0.05)
        return {"status": "ok", "size": len(wav_data), "note": "no confirmation received"}

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
        host_capture_start = time.time()
        self.drain()
        self.ser.write(b"CAPTURE\n")
        self.ser.flush()

        # 1 行目を待つ: "IMG:<size>" or error JSON
        # in_waiting で来た分だけ読んで自前で行分割 (readline の timeout block 回避)。
        deadline = time.time() + timeout
        buf = b""
        header_size: int | None = None
        while time.time() < deadline:
            avail = self.ser.in_waiting
            if avail > 0:
                buf += self.ser.read(avail)
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("IMG:"):
                        try:
                            header_size = int(line[4:])
                        except ValueError:
                            return {"status": "error", "error": f"invalid IMG header: {line}"}
                        break
                    if line.startswith("{"):
                        try:
                            return json.loads(line)
                        except json.JSONDecodeError:
                            return {"status": "error", "error": f"non-json line: {line}"}
                    # ログ行 (`[bridge] ...`) などは捨てて継続
                if header_size is not None:
                    break
            else:
                time.sleep(0.02)
        if header_size is None:
            return {"status": "error", "error": "no IMG header (timeout)"}
        if header_size <= 0:
            return {"status": "error", "error": f"non-positive size: {header_size}"}

        # JPEG 本体 <header_size> bytes 読む。buf に既に取り込み済の余剰があれば
        # それを使い、不足分のみ追加で read する。
        # `ser.read(N)` は serial の global timeout (=5s) で block するので、
        # `in_waiting` で来た分だけ読んで自前で進める (CAPTURE は数百 KB 規模、
        # ack を待つだけで 5s 消費するのを避ける)。
        jpeg = buf[:header_size]
        buf = buf[header_size:]
        remaining = header_size - len(jpeg)
        deadline = time.time() + max(timeout, 10.0)
        while remaining > 0 and time.time() < deadline:
            avail = self.ser.in_waiting
            if avail > 0:
                chunk = self.ser.read(min(remaining, avail, 4096))
                if chunk:
                    jpeg += chunk
                    remaining -= len(chunk)
            else:
                time.sleep(0.005)
        if remaining > 0:
            return {"status": "error", "error": f"binary recv timeout ({remaining}/{header_size} remaining)"}

        # ack JSON 待ち
        deadline = time.time() + timeout
        ack: dict | None = None
        while ack is None and time.time() < deadline:
            avail = self.ser.in_waiting
            if avail > 0:
                buf += self.ser.read(avail)
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ack = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                time.sleep(0.02)
        if ack is None:
            # ack が来ない場合でも JPEG は取れているので、画像だけ返す (ack の欠落は
            # 致命ではない)
            return {
                "status": "ok",
                "image_jpeg": jpeg,
                "size": len(jpeg),
                "format": "jpeg",
                "captured_at": host_capture_start,
                "note": "ack missing",
            }
        if ack.get("status") == "error":
            return ack
        # 成功 ack に画像本体と host 時刻を載せて返す。ファームが返す `captured_at`
        # は device millis (起動からの相対時刻) なので、`captured_at_device_ms` に
        # rename して保持し、`captured_at` は host 側の epoch sec で上書きする。
        device_ms = ack.pop("captured_at", None)
        if isinstance(device_ms, (int, float)):
            ack["captured_at_device_ms"] = int(device_ms)
        ack["image_jpeg"] = jpeg
        ack["captured_at"] = host_capture_start
        return ack


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

