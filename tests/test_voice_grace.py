"""なでてから最初の発話までのグレース期間の検証。

録音開始直後の無音 (なで → 考える時間) では止めず、最初の発話が来てから
silence_seconds の通常無音判定に切り替わる。猶予内に発話が無ければ誤タップ
として stop する。
"""

from __future__ import annotations

import struct
import time
from unittest.mock import MagicMock

from xangi_stackchan.voice_conversation import VoiceConversation


def _make_backend():
    backend = MagicMock()
    backend.start_mic_recording.return_value = {"status": "ok", "mic": "on"}
    backend.stop_mic_recording.return_value = {
        "status": "ok",
        "pcm": b"\x00\x00" * 100,
        "wav": b"RIFF1234",
        "frames": 100,
        "duration_seconds": 0.5,
    }
    return backend


def _silent_chunk(n: int = 512) -> bytes:
    return b"\x00\x00" * n


def _loud_chunk(n: int = 512) -> bytes:
    return struct.pack(f"<{n}h", *([20000] * n))


def _make_vc(backend, **kw):
    params = dict(
        xangi_base_url="",
        silence_dbfs=-40.0,
        silence_seconds=1.5,
        max_record_seconds=15.0,
        initial_grace_seconds=5.0,
        pcm_stall_seconds=99.0,  # この test では stall watchdog を無効化
    )
    params.update(kw)
    return VoiceConversation(backend=backend, **params)


def test_silence_during_grace_does_not_stop():
    """発話前の無音 (グレース内) では録音を止めない。"""
    backend = _make_backend()
    vc = _make_vc(backend, initial_grace_seconds=5.0)
    vc._on_head_touch({"gesture": "press"})
    assert vc._recording is True

    # グレース内の無音 chunk を複数投入しても止まらない
    for _ in range(10):
        vc._on_pcm_chunk(_silent_chunk())
    assert vc._recording is True
    assert vc._speech_started is False


def test_speech_after_grace_then_silence_stops():
    """発話 → 無音 silence_seconds で stop。発話開始フラグが立つこと。"""
    backend = _make_backend()
    vc = _make_vc(backend, silence_seconds=0.3, initial_grace_seconds=5.0)
    vc._on_head_touch({"gesture": "press"})

    # 考える時間 (無音)
    for _ in range(3):
        vc._on_pcm_chunk(_silent_chunk())
    assert vc._speech_started is False
    assert vc._recording is True

    # 発話
    vc._on_pcm_chunk(_loud_chunk())
    assert vc._speech_started is True

    # 発話後の無音 → silence_seconds 経過で stop
    deadline = time.time() + 2.0
    while vc._recording and time.time() < deadline:
        vc._on_pcm_chunk(_silent_chunk())
        time.sleep(0.05)
    assert vc._recording is False
    backend.stop_mic_recording.assert_called()


def test_no_speech_within_grace_stops_as_mistap():
    """猶予を過ぎても一度も発話が無ければ誤タップとして stop。"""
    backend = _make_backend()
    vc = _make_vc(backend, initial_grace_seconds=0.4)
    vc._on_head_touch({"gesture": "press"})
    assert vc._recording is True

    deadline = time.time() + 2.0
    while vc._recording and time.time() < deadline:
        vc._on_pcm_chunk(_silent_chunk())
        time.sleep(0.05)
    assert vc._recording is False
    assert vc._speech_started is False
    backend.stop_mic_recording.assert_called()
