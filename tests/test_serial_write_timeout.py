"""SerialActor.write の write_timeout ハンドリング回帰テスト。

write_timeout 未設定だと、デバイスが受信を捌けない瞬間に ser.write/flush が無限
ブロックし、ロックを抱えたまま全スレッドが停止しうる。write() は
SerialTimeoutException を握り潰して False を返し、ロックを必ず解放し、out buffer を
捨てて自己回復できることを担保する。
"""
from __future__ import annotations

import serial

from xangi_stackchan.serial_actor import SerialActor


class _OkSerial:
    def __init__(self):
        self.written = bytearray()
        self.reset_called = 0

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        pass

    def reset_output_buffer(self):
        self.reset_called += 1


class _TimeoutSerial:
    """write が必ず SerialTimeoutException を投げる serial。"""

    def __init__(self):
        self.reset_called = 0

    def write(self, data):
        raise serial.SerialTimeoutException("Write timeout")

    def flush(self):
        pass

    def reset_output_buffer(self):
        self.reset_called += 1


def test_write_success_returns_true():
    ser = _OkSerial()
    actor = SerialActor(ser)
    assert actor.write(b"PING\n") is True
    assert bytes(ser.written) == b"PING\n"
    assert ser.reset_called == 0


def test_write_timeout_returns_false_and_does_not_raise():
    ser = _TimeoutSerial()
    actor = SerialActor(ser)
    # 無限ブロックや例外伝播ではなく、False を返して自己回復すること。
    result = actor.write(b"IMAGE:11656\n")
    assert result is False
    # partial frame による firmware parser desync を最小化するため out buffer を捨てる。
    assert ser.reset_called == 1


def test_write_releases_lock_after_timeout():
    """timeout 後も _write_lock が解放され、後続 write が継続できること
    (lock を握ったまま死ぬと watchdog の MIC_STOP も道連れになる)。"""
    ser = _TimeoutSerial()
    actor = SerialActor(ser)
    assert actor.write(b"first\n") is False
    # 2 回目も deadlock せず即座に False が返る。
    assert actor.write(b"second\n") is False
    assert ser.reset_called == 2


def test_write_none_serial_returns_false():
    actor = SerialActor(None)
    assert actor.write(b"x") is False
