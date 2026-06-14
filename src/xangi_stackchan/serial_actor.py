"""Single-reader/single-writer serial actor for xangi-stackchan host side.

Rationale (2026-05-27 Phase 2.x):
  Phase 2 / 2.1 の実機検証で「send_command/send_wav/READY 待ち/MIC reader/event
  poll が同じ pyserial.Serial をロックなしで読む」設計が race condition を
  量産していた。READY 文字列の先食い、binary frame の途中切れ、ack JSON と
  非同期 event の取り違え等が「修正のたびに別の race で再発」する状況だった。

  本クラスはその根本対策: 唯一の reader thread が serial を `ser.read()` し、
  byte stream を parser に流す。上位は line listener / mic_pcm listener / 進行中
  transaction の expect_line を経由してデータを受け取る。書き込みは write() を
  通って lock 経由でシリアライズ。これで「単一 reader / 単一 writer」が保証され、
  従来の race source が原理的に消える。

Parser state machine:
  - LINE: '\n' まで line_buf に積む → line dispatch
  - 'MIC_PCM:<size>\n' を見たら → BINARY:MIC_PCM <size> へ遷移、size byte 読んだら
    body dispatch、その後 LINE に戻る
  - 'IMG:<size>\n' は IMG body も同じく BINARY:IMG <size> へ遷移
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from queue import Empty, Queue
from typing import Optional

import serial


class SerialActor:
    """pyserial.Serial を排他的に保持する唯一の reader + writer thread。

    使い方:
        actor = SerialActor(ser)
        actor.add_line_listener(handler)         # 全 line を dispatch
        actor.set_mic_pcm_listener(on_pcm_chunk) # MIC_PCM body の dispatch 先
        actor.start()

        actor.write(b"STATUS\\n")
        line = actor.expect_line(lambda l: l.startswith("{"), timeout=2.0)

        actor.stop()
    """

    # MIC_PCM frame size 上限。それを超えるとプロトコル不整合とみなして
    # binary を読まず line dispatch に戻す (security + 防御的)。
    MAX_MIC_PCM_BYTES = 65536
    # IMG (JPEG) は最大 256KB 程度 (320x240 RGB565 → JPEG はせいぜい 30KB)。
    MAX_IMG_BYTES = 512 * 1024

    def __init__(self, ser):
        self.ser = ser
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._listener_thread: threading.Thread | None = None

        # parser state
        self._line_buf = bytearray()
        # binary mode: ("MIC_PCM", size) or ("IMG", size) or None
        self._binary_mode: tuple[str, int] | None = None
        self._binary_buf = bytearray()

        # dispatch targets
        self._line_listeners: list[Callable[[str], None]] = []
        self._mic_pcm_listener: Callable[[bytes], None] | None = None
        self._img_data_box: list[bytes] = []  # 最新 IMG body (1 要素)

        # listener は reader thread と別 thread で実行する。理由: line_listener
        # 内で actor.expect_line を呼ぶ host コード (head_touch event → start_mic_recording)
        # があり、reader thread 上で呼ぶと自分自身を wait させて dead lock になる。
        # listener queue + worker thread で「listener 呼び出しを reader thread から
        # 切り離す」。
        self._listener_queue: Queue = Queue()

        # write は serial.write() 自体が thread-safe じゃないので排他。
        # reader thread (ser.read) と writer (ser.write) は pyserial 上では別 fd
        # 操作で衝突しないが、明示 lock で意図を出しておく。
        self._write_lock = threading.Lock()

        # expect_line 用の pending queue。常時 active で受信 line を積み続ける
        # (start_transaction で clear)。これで「前 transaction の end と次の
        # start の隙間で来た line が drop される」事故を防ぐ (2026-05-27 22:53 で
        # STATUS 応答が消えた事象の真因)。
        self._expect_queue: deque[str] = deque()
        self._expect_max = 200
        self._expect_lock = threading.Lock()
        self._expect_cond = threading.Condition(self._expect_lock)

        # シリアル切断 (USB 再列挙 / デバイス再起動) の検知。write が
        # SerialException/OSError を投げた時、または read 系が連続して例外を
        # 返し続けた時に「接続が死んだ」とみなして on_dead を 1 回だけ呼ぶ。
        # write timeout (SerialTimeoutException) は一時的な詰まりであって
        # 切断ではないので対象外。on_dead は reader/writer thread 上で呼ばれる
        # ので、登録側は重い処理をせずフラグ/イベント通知に留めること。
        self.on_dead: Callable[[str], None] | None = None
        self._dead = False
        self._dead_lock = threading.Lock()
        # read 系例外の連続回数しきい値。_read_loop は例外時 50ms wait なので
        # 20 回 ≈ 1 秒間 read が全滅したら切断と判定する。
        self.read_error_threshold = 20

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="stackchan-serial-actor"
        )
        self._reader_thread.start()
        self._listener_thread = threading.Thread(
            target=self._listener_loop, daemon=True, name="stackchan-serial-listener"
        )
        self._listener_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        if self._listener_thread is not None:
            # listener_queue にダミーを入れて get の block を解く
            try:
                self._listener_queue.put((None, None))
            except Exception:
                pass
            self._listener_thread.join(timeout=2.0)
            self._listener_thread = None

    def _listener_loop(self) -> None:
        """line_listener を reader thread から切り離して実行する。listener 内で
        host コードが actor.expect_line を呼んでも reader thread を block しない。
        """
        while not self._stop_event.is_set():
            try:
                fn, line = self._listener_queue.get(timeout=0.1)
            except Empty:
                continue
            if fn is None:  # stop signal
                break
            try:
                fn(line)
            except Exception:
                # listener 例外で worker を殺さない
                pass

    def write(self, data: bytes) -> bool:
        """シリアライズした write。reader thread とは別経路。

        戻り値: 送信成功なら True、write_timeout に達して送れなかったら False。

        write_timeout (stackchan.connect で設定) に達すると pyserial は
        SerialTimeoutException を投げる。ここで握り潰さず raise すると、呼び出し側の
        `with self._lock:` を抜ける際に lock は解放されるが例外が SSE event loop /
        sprite thread まで伝播してしまう。代わりにここで捕捉して False を返し、
        out buffer を捨てて (partial frame による firmware parser desync を最小化)、
        呼び出し側は後続の expect_line timeout で「ack 無し」として正常に unwind
        できるようにする。狙いは「どのスレッドも serial write で無限ブロックしない」
        こと。"""
        with self._write_lock:
            if self.ser is None:
                return False
            try:
                self.ser.write(data)
                self.ser.flush()
                return True
            except serial.SerialTimeoutException as exc:
                sys.stderr.write(
                    f"[actor] write timeout ({len(data)} bytes): {exc}\n"
                )
                # partial 送信でファーム側 parser がずれるのを最小化。
                try:
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                return False
            except (serial.SerialException, OSError) as exc:
                # USB 再列挙 / デバイス再起動で fd が死んだ ("write failed:
                # [Errno 5] Input/output error" 等)。例外は伝播させず False を
                # 返し、on_dead で上位 (StackchanSerial) に再接続を委ねる。
                sys.stderr.write(f"[actor] write failed (dead?): {exc}\n")
                self._mark_dead(f"write: {exc}")
                return False

    def add_line_listener(self, fn: Callable[[str], None]) -> None:
        """全行 dispatch 先を追加 (非同期 event 検出等)。同一 thread から呼ばれる。"""
        self._line_listeners.append(fn)

    def set_mic_pcm_listener(self, fn: Callable[[bytes], None] | None) -> None:
        """MIC_PCM body の dispatch 先。None で解除。録音中以外は普通 None。"""
        self._mic_pcm_listener = fn

    def pop_img_data(self) -> bytes | None:
        """最新の IMG body を取り出す (1 回限り、取り出したら破棄)。"""
        if self._img_data_box:
            return self._img_data_box.pop()
        return None

    def start_transaction(self) -> None:
        """write → expect_line を使う前に呼ぶ。queue を clear して「これ以降に
        来る line だけを応答とみなす」状態にする。古い line は捨てる。
        """
        with self._expect_cond:
            self._expect_queue.clear()

    def end_transaction(self) -> None:
        """互換のために残す no-op。queue は次の start_transaction まで溜め続けるが、
        bounded (_expect_max) なので memory leak しない。"""
        return

    def expect_line(
        self,
        predicate: Callable[[str], bool],
        timeout: float,
    ) -> str | None:
        """predicate(line) が True になる最初の line を返す。timeout 経過で None。
        queue に積まれた既存 line を先に走査し、合致しない line は queue に積み戻す
        (他 transaction が拾えるように)。これで USB CDC のバッファリングで応答が
        遅延した時の「前 transaction の ack が次 transaction で誤拾い」を防ぐ。
        """
        deadline = time.time() + timeout
        with self._expect_cond:
            while True:
                skipped: list[str] = []
                found: str | None = None
                while self._expect_queue:
                    line = self._expect_queue.popleft()
                    if predicate(line):
                        found = line
                        break
                    skipped.append(line)
                # 合致しなかった line を queue 先頭に積み戻す (順序維持)。
                for line in reversed(skipped):
                    self._expect_queue.appendleft(line)
                if found is not None:
                    return found
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._expect_cond.wait(timeout=remaining)

    # ----------------------------------------------------------------- internal
    def _mark_dead(self, reason: str) -> None:
        """切断検知を 1 回だけ on_dead に通知する (write/read どちらの thread からも安全)。"""
        with self._dead_lock:
            if self._dead:
                return
            self._dead = True
        sys.stderr.write(f"[actor] serial dead: {reason}\n")
        cb = self.on_dead
        if cb is not None:
            try:
                cb(reason)
            except Exception as exc:
                sys.stderr.write(f"[actor] on_dead callback error: {exc}\n")

    @property
    def is_dead(self) -> bool:
        return self._dead

    def _read_loop(self) -> None:
        debug = os.environ.get("STACKCHAN_SERIAL_DEBUG")
        last_dbg = 0.0
        loop_count = 0
        consec_errors = 0
        while not self._stop_event.is_set():
            try:
                avail = self.ser.in_waiting
            except Exception as exc:
                if debug:
                    sys.stderr.write(f"[actor] in_waiting exc: {exc}\n")
                consec_errors += 1
                if consec_errors >= self.read_error_threshold:
                    self._mark_dead(f"read: {exc}")
                    return
                self._stop_event.wait(0.05)
                continue
            if avail <= 0:
                consec_errors = 0  # idle は健全 (エラー無しで応答できている)
                # 30 秒に 1 回 alive ping を出す
                loop_count += 1
                if debug and time.time() - last_dbg > 30:
                    sys.stderr.write(f"[actor] alive loop={loop_count} idle\n")
                    last_dbg = time.time()
                self._stop_event.wait(0.005)
                continue
            try:
                data = self.ser.read(avail)
                consec_errors = 0
            except Exception as exc:
                if debug:
                    sys.stderr.write(f"[actor] read exc: {exc}\n")
                consec_errors += 1
                if consec_errors >= self.read_error_threshold:
                    self._mark_dead(f"read: {exc}")
                    return
                self._stop_event.wait(0.05)
                continue
            if data:
                if debug:
                    sys.stderr.write(f"[actor] read {len(data)} bytes\n")
                self._feed(data)

    def _feed(self, data: bytes) -> None:
        """parser state machine: bytes を line / binary body に分類して dispatch。"""
        i = 0
        n = len(data)
        while i < n:
            if self._binary_mode is not None:
                kind, size = self._binary_mode
                remaining = size - len(self._binary_buf)
                take = min(remaining, n - i)
                self._binary_buf.extend(data[i : i + take])
                i += take
                if len(self._binary_buf) >= size:
                    body = bytes(self._binary_buf)
                    self._binary_buf.clear()
                    self._binary_mode = None
                    self._dispatch_binary(kind, body)
                continue

            # LINE mode: '\n' まで読む
            byte = data[i]
            i += 1
            if byte == 0x0A:  # '\n'
                if self._line_buf:
                    line = (
                        self._line_buf.decode("utf-8", errors="replace")
                        .strip()
                    )
                    self._line_buf.clear()
                    if line:
                        self._dispatch_line(line)
            else:
                self._line_buf.append(byte)

    def _dispatch_line(self, line: str) -> None:
        # debug 用に環境変数で全 line ログ出力 (STACKCHAN_SERIAL_DEBUG=1)
        if os.environ.get("STACKCHAN_SERIAL_DEBUG"):
            sys.stderr.write(f"[actor] line: {line[:200]!r}\n")
        # binary frame ヘッダの検出
        if line.startswith("MIC_PCM:"):
            try:
                size = int(line[len("MIC_PCM:") :])
            except ValueError:
                size = -1
            if 0 < size <= self.MAX_MIC_PCM_BYTES:
                self._binary_mode = ("MIC_PCM", size)
                return
            # サイズ不正は drop + listener にも回さない
            return
        if line.startswith("IMG:"):
            try:
                size = int(line[len("IMG:") :])
            except ValueError:
                size = -1
            if 0 < size <= self.MAX_IMG_BYTES:
                self._binary_mode = ("IMG", size)
                return
            return

        # 通常 line: listener と expect_queue 両方に dispatch。
        # listener は別 thread (_listener_loop) に queue で渡す (reader thread を
        # block しないように)。
        for fn in self._line_listeners:
            self._listener_queue.put((fn, line))
        with self._expect_cond:
            self._expect_queue.append(line)
            while len(self._expect_queue) > self._expect_max:
                self._expect_queue.popleft()
            self._expect_cond.notify_all()

    def _dispatch_binary(self, kind: str, body: bytes) -> None:
        if kind == "MIC_PCM":
            if self._mic_pcm_listener is not None:
                try:
                    self._mic_pcm_listener(body)
                except Exception:
                    pass
        elif kind == "IMG":
            # 1 要素 box に上書き (古いものは捨てる)
            self._img_data_box.clear()
            self._img_data_box.append(body)
