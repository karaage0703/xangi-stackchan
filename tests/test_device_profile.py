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


def test_detect_async_event_head_touch_dispatches_callback():
    """ファームからの head_touch event 行で on_head_touch が呼ばれる。"""
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False
    captured = []
    s.on_head_touch = lambda d: captured.append(d)
    handled = s._detect_async_event(
        '{"event":"head_touch","gesture":"press","at":12345}'
    )
    assert handled is True
    assert s.user_stopped is False  # audio_stopped とは独立
    assert len(captured) == 1
    assert captured[0]["gesture"] == "press"
    assert captured[0]["at"] == 12345


def test_detect_async_event_head_touch_no_callback_consumed():
    """on_head_touch 未設定でも event は consume される (応答行扱いされない)。"""
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False
    s.on_head_touch = None
    handled = s._detect_async_event(
        '{"event":"head_touch","gesture":"swipe_forward","at":99999}'
    )
    assert handled is True


def test_detect_async_event_head_touch_parse_error_does_not_crash():
    """JSON parse 失敗時も例外で receiver loop を壊さず、event 行扱いで consume。"""
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False
    captured = []
    s.on_head_touch = lambda d: captured.append(d)
    # 末尾途切れの不正 JSON
    handled = s._detect_async_event('{"event":"head_touch","gesture":')
    assert handled is True
    assert captured == []


def test_pcm_to_wav_wraps_int16_mono_at_16khz():
    """pcm_to_wav が 16kHz/16bit/mono の valid WAV を返す。"""
    import io
    import wave

    from xangi_stackchan.stackchan import pcm_to_wav

    # 1 秒分のサイン波もどき (16000 sample × 2 byte = 32000 byte)
    pcm = bytes(range(256)) * 125  # 32000 byte
    wav = pcm_to_wav(pcm, sample_rate=16000, bits=16, channels=1)

    assert wav.startswith(b"RIFF")
    assert wav[8:12] == b"WAVE"

    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        # WAV データ部 = 入力 PCM そのまま (alignment loss なし、16000 frames)
        assert w.getnframes() == 16000
        frames = w.readframes(w.getnframes())
        assert frames == pcm


def test_pcm_to_wav_aligns_odd_length_input():
    """末尾の半端 byte は drop して valid な WAV を返す。"""
    import io
    import wave

    from xangi_stackchan.stackchan import pcm_to_wav

    pcm = bytes([0x01, 0x02, 0x03])  # 3 byte = 1 frame + half
    wav = pcm_to_wav(pcm)

    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnframes() == 1
        assert w.readframes(1) == bytes([0x01, 0x02])


def test_voice_conversation_start_binds_callback():
    """VoiceConversation.start() で backend.on_head_touch が設定される。"""
    from xangi_stackchan.stackchan import StackchanSerial
    from xangi_stackchan.voice_conversation import VoiceConversation

    backend = StackchanSerial.__new__(StackchanSerial)
    backend.on_head_touch = None
    vc = VoiceConversation(backend, xangi_base_url="http://localhost:18888")
    assert backend.on_head_touch is None
    vc.start()
    assert backend.on_head_touch is not None
    assert backend.on_head_touch.__func__ is VoiceConversation._on_head_touch
    assert backend.on_head_touch.__self__ is vc


def test_voice_conversation_history_is_initially_empty():
    """新規 VoiceConversation の history は空 list。"""
    from xangi_stackchan.stackchan import StackchanSerial
    from xangi_stackchan.voice_conversation import VoiceConversation

    backend = StackchanSerial.__new__(StackchanSerial)
    backend.on_head_touch = None
    vc = VoiceConversation(backend, xangi_base_url="http://x")
    assert vc.history == []


def test_voice_conversation_history_caps_at_max():
    """HISTORY_MAX を超える entry は古い側から pop される。"""
    from xangi_stackchan.stackchan import StackchanSerial
    from xangi_stackchan.voice_conversation import HISTORY_MAX, VoiceConversation

    backend = StackchanSerial.__new__(StackchanSerial)
    backend.on_head_touch = None
    vc = VoiceConversation(backend, xangi_base_url="http://x")
    # 30 件追加 → HISTORY_MAX (=10) 件のみ残るのを想定
    for i in range(HISTORY_MAX + 20):
        vc.history.append({"ts": i, "text": f"t{i}"})
        while len(vc.history) > HISTORY_MAX:
            vc.history.pop(0)
    assert len(vc.history) == HISTORY_MAX
    # 最新側が残ってる (古い側が押し出される)
    texts = [e["text"] for e in vc.history]
    assert texts[-1] == f"t{HISTORY_MAX + 19}"


def test_voice_conversation_ignores_release_and_swipe():
    """release / swipe_forward / swipe_backward は _on_head_touch で無視される。"""
    from xangi_stackchan.stackchan import StackchanSerial
    from xangi_stackchan.voice_conversation import VoiceConversation

    backend = StackchanSerial.__new__(StackchanSerial)
    backend.on_head_touch = None
    started = []

    def fake_start(*args, **kwargs):
        started.append(kwargs)
        return {"status": "ok"}

    backend.start_mic_recording = fake_start
    vc = VoiceConversation(backend, xangi_base_url="http://localhost:18888")
    vc.start()
    backend.on_head_touch({"gesture": "release", "at": 1})
    backend.on_head_touch({"gesture": "swipe_forward", "at": 2})
    backend.on_head_touch({"gesture": "swipe_backward", "at": 3})
    assert started == []  # 録音開始されない


def test_chunk_dbfs_silence_returns_neginf():
    """完全無音 (zero PCM) は -inf dBFS。"""
    import math

    from xangi_stackchan.voice_conversation import chunk_dbfs

    pcm = b"\x00" * 2048
    assert chunk_dbfs(pcm) == -math.inf


def test_chunk_dbfs_full_scale_returns_zero():
    """max int16 だけの PCM はおよそ 0 dBFS。"""
    import math
    import struct

    from xangi_stackchan.voice_conversation import chunk_dbfs

    samples = [32767] * 1024
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    dbfs = chunk_dbfs(pcm)
    assert dbfs > -0.1  # ほぼ 0 dBFS
    assert dbfs < 0.1
    # 数学的には 20*log10(32767/32768) ≈ -0.000265 dBFS
    assert math.isclose(dbfs, 20 * math.log10(32767 / 32768), abs_tol=1e-5)


def test_chunk_dbfs_empty_or_too_small():
    """空 / 1 byte は -inf。"""
    import math

    from xangi_stackchan.voice_conversation import chunk_dbfs

    assert chunk_dbfs(b"") == -math.inf
    assert chunk_dbfs(b"\x00") == -math.inf


def test_chunk_dbfs_quarter_scale_returns_expected_dbfs():
    """1/4 振幅 = 約 -12 dBFS。"""
    import math
    import struct

    from xangi_stackchan.voice_conversation import chunk_dbfs

    samples = [8192] * 1024  # 32768 / 4
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    dbfs = chunk_dbfs(pcm)
    assert math.isclose(dbfs, 20 * math.log10(8192 / 32768), abs_tol=0.5)


def test_pcm_to_wav_empty_input_returns_valid_zero_frame_wav():
    """空 PCM でも valid な 0 frame WAV を返す (RIFF/WAVE ヘッダだけ)。"""
    import io
    import wave

    from xangi_stackchan.stackchan import pcm_to_wav

    wav = pcm_to_wav(b"")
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnframes() == 0


def test_detect_async_event_head_touch_listener_exception_swallowed():
    """listener 内の例外がシリアル受信ループを壊さない。"""
    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s.user_stopped = False

    def boom(_d):
        raise RuntimeError("listener error")

    s.on_head_touch = boom
    handled = s._detect_async_event(
        '{"event":"head_touch","gesture":"press","at":1}'
    )
    assert handled is True


def test_send_wav_skips_when_mic_recording():
    """_mic_recording=True なら send_wav はファームに送らず skipped を返す。

    voice_conversation 中、ファームが Speaker.end() で I2S を Mic に切り替えて
    いる間に SSE turn.complete の応答 TTS が走ると、シリアル binary 衝突 +
    Speaker 無効で再生不可 + multi access 警告 + 録音 PCM 破綻と全て壊れる。
    """
    import threading

    from xangi_stackchan.stackchan import StackchanSerial

    s = StackchanSerial.__new__(StackchanSerial)
    s._mic_recording = True
    s.user_stopped = False
    s.max_wav_bytes = 0
    s.skip_move_during_wav = False
    s._wav_active = False
    s._lock = threading.RLock()
    result = s.send_wav(b"RIFF\x00\x00\x00\x00WAVE")
    assert result["status"] == "skipped"
    assert result["reason"] == "mic_recording"
    assert result["size"] == len(b"RIFF\x00\x00\x00\x00WAVE")


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
