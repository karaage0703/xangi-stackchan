"""シリアル切断検知 + 自動再接続の回帰テスト。

デバイス再起動 / USB 再列挙で ttyACMx が変わると、旧 fd への write が
SerialException ("write failed: [Errno 5] Input/output error") になる。
従来は bridge が死んだ接続を握ったまま全操作が失敗し続け、手動再起動でしか
復旧できなかった。本テストは以下を担保する:

- SerialActor が write 例外 / read 連続例外で on_dead を 1 回だけ呼ぶ
- write timeout (一時的な詰まり) は切断扱いしない
- StackchanSerial が切断後に再接続 thread を回し、成功時 on_reconnected を呼ぶ
- 切断中の send_* は fail-fast でエラー dict を返す (ブロックしない)
- close() で再接続 loop が止まる
"""
from __future__ import annotations

import threading
import time

import serial as pyserial

from xangi_stackchan.serial_actor import SerialActor
from xangi_stackchan.stackchan import StackchanSerial


class _DeadWriteSerial:
    """write が常に SerialException (USB 切断相当) を投げる serial。"""

    def write(self, data):
        raise pyserial.SerialException(
            "write failed: [Errno 5] Input/output error"
        )

    def flush(self):
        pass

    def reset_output_buffer(self):
        pass


class _TimeoutWriteSerial:
    def write(self, data):
        raise pyserial.SerialTimeoutException("Write timeout")

    def flush(self):
        pass

    def reset_output_buffer(self):
        pass


class _DeadReadSerial:
    """in_waiting が常に OSError (fd 消失相当) を投げる serial。"""

    @property
    def in_waiting(self):
        raise OSError(5, "Input/output error")

    def write(self, data):
        return len(data)

    def flush(self):
        pass


class TestSerialActorDeadDetection:
    def test_write_serial_exception_marks_dead_once(self):
        actor = SerialActor(_DeadWriteSerial())
        calls = []
        actor.on_dead = calls.append
        assert actor.write(b"FACE:happy\n") is False
        assert actor.write(b"FACE:neutral\n") is False
        assert calls == ["write: write failed: [Errno 5] Input/output error"]
        assert actor.is_dead

    def test_write_timeout_does_not_mark_dead(self):
        actor = SerialActor(_TimeoutWriteSerial())
        calls = []
        actor.on_dead = calls.append
        assert actor.write(b"FACE:happy\n") is False
        assert calls == []
        assert not actor.is_dead

    def test_read_loop_consecutive_errors_mark_dead(self):
        actor = SerialActor(_DeadReadSerial())
        actor.read_error_threshold = 3
        dead = threading.Event()
        actor.on_dead = lambda reason: dead.set()
        actor.start()
        try:
            assert dead.wait(3.0), "read 連続例外で on_dead が呼ばれること"
            assert actor.is_dead
        finally:
            actor.stop()

    def test_on_dead_callback_exception_is_swallowed(self):
        actor = SerialActor(_DeadWriteSerial())

        def bad_callback(reason):
            raise RuntimeError("callback bug")

        actor.on_dead = bad_callback
        # callback が例外を投げても write は False を返して伝播しない
        assert actor.write(b"X\n") is False
        assert actor.is_dead


class TestStackchanSerialReconnect:
    def _make(self) -> StackchanSerial:
        sc = StackchanSerial("/dev/fake-test-port")
        sc.reconnect_interval = 0.05
        return sc

    def test_reconnect_retries_until_open_succeeds(self, monkeypatch):
        sc = self._make()
        attempts = []

        def fake_open():
            attempts.append(1)
            if len(attempts) < 3:
                raise pyserial.SerialException("could not open port")

        monkeypatch.setattr(sc, "open", fake_open)
        reconnected = threading.Event()
        sc.on_reconnected = reconnected.set

        sc._on_serial_dead("write: [Errno 5] Input/output error")
        assert not sc.is_connected
        assert reconnected.wait(3.0), "open 成功後に on_reconnected が呼ばれること"
        assert len(attempts) == 3
        assert sc.is_connected

    def test_send_fails_fast_while_disconnected(self, monkeypatch):
        sc = self._make()
        sc.reconnect_interval = 10.0  # テスト中は再接続成功させない
        monkeypatch.setattr(
            sc, "open",
            lambda: (_ for _ in ()).throw(pyserial.SerialException("no device")),
        )
        sc._on_serial_dead("read: gone")
        started = time.monotonic()
        result = sc.send_command("STATUS")
        elapsed = time.monotonic() - started
        assert result["status"] == "error"
        assert result.get("disconnected") is True
        assert elapsed < 0.5, "切断中の send_command はブロックしないこと"
        assert sc.send_wav(b"RIFFxxxxWAVE")["disconnected"] is True
        assert sc.send_image(b"\xff\xd8jpeg")["disconnected"] is True

    def test_dead_notification_is_idempotent(self, monkeypatch):
        sc = self._make()
        sc.reconnect_interval = 10.0
        monkeypatch.setattr(
            sc, "open",
            lambda: (_ for _ in ()).throw(pyserial.SerialException("no device")),
        )
        sc._on_serial_dead("first")
        first_thread = sc._reconnect_thread
        sc._on_serial_dead("second")
        assert sc._reconnect_thread is first_thread, "再接続 thread は 1 本だけ"

    def test_close_stops_reconnect_loop(self, monkeypatch):
        sc = self._make()
        sc.reconnect_interval = 0.01
        monkeypatch.setattr(
            sc, "open",
            lambda: (_ for _ in ()).throw(pyserial.SerialException("no device")),
        )
        sc._on_serial_dead("gone")
        t = sc._reconnect_thread
        assert t is not None and t.is_alive()
        sc.close()
        t.join(2.0)
        assert not t.is_alive(), "close() で再接続 loop が終了すること"

    def test_reconnect_disabled_does_nothing(self):
        sc = self._make()
        sc.reconnect_enabled = False
        sc._on_serial_dead("gone")
        assert sc._reconnect_thread is None
        assert sc.is_connected
