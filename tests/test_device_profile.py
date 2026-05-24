"""device_profile (rt_beta / cores3_k151 等) のテスト。"""

from xangi_stackchan.stackchan import (
    DEFAULT_BAUD,
    DEVICE_PROFILES,
    StackchanConfig,
    apply_profile_defaults,
    resolve_profile,
)


def test_resolve_profile_known():
    p = resolve_profile("rt_beta")
    assert p is not None
    assert p["baud"] == 115200
    assert p["max_wav_bytes"] == 96 * 1024
    assert p["capabilities"]["servo"] is True
    assert p["capabilities"]["camera"] is False


def test_resolve_profile_unknown_returns_none():
    assert resolve_profile("unknown") is None
    assert resolve_profile("") is None


def test_apply_profile_rt_beta_fills_defaults():
    cfg = StackchanConfig(device_profile="rt_beta")
    apply_profile_defaults(cfg)
    assert cfg.baud == 115200  # rt_beta の既定が埋まる
    assert cfg.max_wav_bytes == 96 * 1024


def test_apply_profile_does_not_override_explicit_baud():
    # CLI で baud を明示指定 (DEFAULT_BAUD 以外) した場合は profile より優先
    cfg = StackchanConfig(device_profile="rt_beta", baud=500000)
    apply_profile_defaults(cfg)
    assert cfg.baud == 500000  # 上書きされない


def test_apply_profile_does_not_override_explicit_max_wav():
    cfg = StackchanConfig(device_profile="rt_beta", max_wav_bytes=128 * 1024)
    apply_profile_defaults(cfg)
    assert cfg.max_wav_bytes == 128 * 1024


def test_apply_profile_noop_when_unset():
    cfg = StackchanConfig()
    apply_profile_defaults(cfg)
    assert cfg.baud == DEFAULT_BAUD
    assert cfg.max_wav_bytes == 0


def test_all_profiles_have_required_keys():
    for name, p in DEVICE_PROFILES.items():
        assert "baud" in p, f"{name} missing baud"
        assert "max_wav_bytes" in p, f"{name} missing max_wav_bytes"
        assert "capabilities" in p, f"{name} missing capabilities"
        assert "description" in p, f"{name} missing description"
        caps = p["capabilities"]
        assert {"servo", "camera", "mic"} <= set(caps.keys()), f"{name} caps incomplete"


def test_cores3_k151_profile():
    p = resolve_profile("cores3_k151")
    assert p["baud"] == 921600
    assert p["max_wav_bytes"] == 0  # 無制限 (PSRAM 4MB)
    assert p["capabilities"]["servo"] is True
    assert p["capabilities"]["camera"] is True


def test_atoms3r_profile():
    p = resolve_profile("atoms3r")
    assert p["baud"] == 115200
    assert p["capabilities"]["servo"] is False


def test_rt_beta_has_skip_move_during_wav():
    p = resolve_profile("rt_beta")
    assert p.get("skip_move_during_wav") is True


def test_other_profiles_do_not_skip_move():
    for name in ("cores3_k151", "cores3_standalone", "atoms3r"):
        p = resolve_profile(name)
        assert p.get("skip_move_during_wav", False) is False, f"{name} should not skip move"


def test_apply_profile_rt_beta_sets_skip_move_flag():
    cfg = StackchanConfig(device_profile="rt_beta")
    apply_profile_defaults(cfg)
    assert cfg.skip_move_during_wav is True


def test_estimate_wav_duration_16khz_mono_16bit():
    import struct
    from xangi_stackchan.stackchan import estimate_wav_duration_seconds

    sample_rate = 16000
    pcm = b"\x00\x00" * sample_rate  # 1 秒分の 16-bit mono PCM
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<I", 16) + struct.pack("<H", 1)
    header += struct.pack("<H", 1)
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", sample_rate * 2)
    header += struct.pack("<H", 2) + struct.pack("<H", 16)
    header += b"data" + struct.pack("<I", len(pcm))
    duration = estimate_wav_duration_seconds(header + pcm)
    assert 0.99 < duration < 1.01


def test_estimate_wav_duration_invalid_returns_zero():
    from xangi_stackchan.stackchan import estimate_wav_duration_seconds

    assert estimate_wav_duration_seconds(b"") == 0.0
    assert estimate_wav_duration_seconds(b"INVALID HEADER") == 0.0


def test_send_command_move_skipped_when_wav_active():
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.skip_move_during_wav = True
    s._wav_active = True
    result = s.send_command("MOVE:10,5")
    assert result == {"status": "skipped", "cmd": "MOVE:10,5", "reason": "wav playing"}


def test_send_command_move_not_skipped_when_flag_off():
    import threading

    import pytest

    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.skip_move_during_wav = False
    s._wav_active = True
    s.ser = None
    s._lock = threading.RLock()
    with pytest.raises(AttributeError):
        s.send_command("MOVE:10,5")


def test_detect_async_event_audio_stopped_sets_flag():
    """ファームからの `{"event":"audio_stopped",...}` 行で user_stopped が立つ。"""
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False
    assert s._detect_async_event('{"event":"audio_stopped","reason":"touch","at":12345}') is True
    assert s.user_stopped is True


def test_detect_async_event_normal_line_does_not_set_flag():
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False
    assert s._detect_async_event('{"status":"ok","face":"happy"}') is False
    assert s.user_stopped is False


def test_send_wav_skips_when_user_stopped():
    """user_stopped=True なら send_wav はファームに送らず skipped 応答を返す。"""
    import threading

    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = True
    s.max_wav_bytes = 0
    s.skip_move_during_wav = False
    s._wav_active = False
    s._lock = threading.RLock()
    s.ser = None  # 触らないはず

    result = s.send_wav(b"RIFF" + b"\x00" * 100)
    assert result["status"] == "skipped"
    assert result["reason"] == "user_stopped"
    assert result["size"] == 104
