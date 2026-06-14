"""録音開始後に PCM が来ない / 来なくなったケース。

chunk-based の無音/最長判定は「chunk が流れ続けること」前提なので、PCM が
1 つも来ない / 途中で止まると停止判定が走らず録音状態に固まる。これを壁時計
watchdog (max_record_seconds / pcm_stall_seconds) が救うことを検証する。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from xangi_stackchan.voice_conversation import VoiceConversation


def _make_backend():
    backend = MagicMock()
    backend.start_mic_recording.return_value = {"status": "ok", "mic": "on"}
    backend.stop_mic_recording.return_value = {
        "status": "ok",
        "pcm": b"",
        "wav": b"",
        "frames": 0,
        "duration_seconds": 0.0,
    }
    return backend


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_no_pcm_chunk_after_start_auto_stops():
    """press したが PCM chunk が 1 つも来ない → watchdog が pcm_stall_seconds で
    自動 stop し、録音状態に固まらないこと。"""
    backend = _make_backend()
    vc = VoiceConversation(
        backend=backend,
        xangi_base_url="",
        silence_dbfs=-40.0,
        silence_seconds=1.5,
        max_record_seconds=15.0,
        pcm_stall_seconds=0.3,  # chunk が来ないのですぐ stall 判定
    )
    vc._on_head_touch({"gesture": "press"})
    assert vc._recording is True

    # PCM が来なくても watchdog が stall で stop する
    assert _wait_until(lambda: vc._recording is False, timeout=3.0)
    backend.stop_mic_recording.assert_called()


def test_mic_start_failure_calls_on_stop():
    """MIC_START が失敗しても on_stop を呼び、listening UI を戻せること。"""
    backend = _make_backend()
    backend.start_mic_recording.return_value = {
        "status": "error",
        "error": "serial disconnected",
        "disconnected": True,
    }
    on_stop = MagicMock()
    vc = VoiceConversation(
        backend=backend,
        xangi_base_url="",
        on_stop=on_stop,
        pcm_stall_seconds=0.3,
    )

    vc._on_head_touch({"gesture": "press"})

    assert vc._recording is False
    backend.stop_mic_recording.assert_not_called()
    on_stop.assert_called_once()
    result = on_stop.call_args.args[0]
    assert result["status"] == "error"
    assert result["frames"] == 0
    assert result["duration_seconds"] == 0.0


def test_pcm_stream_stops_midway_auto_stops():
    """途中まで chunk が来て、その後 stream が死ぬ → watchdog が stall で stop。"""
    backend = _make_backend()
    backend.stop_mic_recording.return_value = {
        "status": "ok",
        "pcm": b"\x00\x00" * 100,
        "wav": b"RIFF1234",
        "frames": 100,
        "duration_seconds": 0.5,
    }
    vc = VoiceConversation(
        backend=backend,
        xangi_base_url="",
        silence_dbfs=-200.0,  # 無音判定では止まらない (有音扱い)
        silence_seconds=99.0,
        max_record_seconds=15.0,
        pcm_stall_seconds=0.3,
    )
    vc._on_head_touch({"gesture": "press"})
    assert vc._recording is True

    # 数 chunk 流す (有音 = silence では止まらない)
    loud = (b"\xff\x7f" * 512)
    for _ in range(3):
        vc._on_pcm_chunk(loud)
        time.sleep(0.02)
    assert vc._recording is True

    # ここで stream が死ぬ (chunk を送るのをやめる) → watchdog が stall で stop
    assert _wait_until(lambda: vc._recording is False, timeout=3.0)
    backend.stop_mic_recording.assert_called()
