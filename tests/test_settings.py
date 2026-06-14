import json
from pathlib import Path

from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.settings import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_INSTANCE_ID,
    RuntimeState,
    load_config_file,
    load_instance_dict,
    merge_config,
    migrate_to_v2,
    save_instance_dict,
)
from xangi_stackchan.stackchan import StackchanConfig


def _base_config() -> BridgeConfig:
    return BridgeConfig(
        xangi_url="http://127.0.0.1:18890",
        thread_id=None,
        stackchan=StackchanConfig(wifi=False, host="192.168.1.100", port="/dev/stackchan", baud=115200),
        volume=255,
        tts="piper",
        piper_bin="piper",
        piper_model="model.onnx",
        piper_speaker=0,
        voicevox_url="http://127.0.0.1:50021",
        voicevox_speaker=1,
        serial_chunk=1024,
        serial_delay=0.005,
        stackchan_retry_seconds=3.0,
        face_idle="neutral",
        face_thinking="doubt",
        face_talking="happy",
        face_error="sad",
        face_mode="avatar",
        sprite_sheet="assets/pets/default/spritesheet.webp",
        sprite_jpeg_quality=85,
        stream_timeout=65,
        retry_seconds=1.0,
        max_retry_seconds=30.0,
    )


def test_merge_config_keeps_numeric_defaults_for_blank_form_values():
    config = merge_config(
        _base_config(),
        {
            "baud": "",
            "volume": "",
            "piper_speaker": "",
            "voicevox_speaker": "",
            "serial_chunk": "",
            "serial_delay": "",
            "stackchan_retry_seconds": "",
            "stream_timeout": "",
            "retry_seconds": "",
            "max_retry_seconds": "",
        },
    )

    assert config.stackchan.baud == 115200
    assert config.volume == 255
    assert config.piper_speaker == 0
    assert config.voicevox_speaker == 1
    assert config.serial_chunk == 1024
    assert config.serial_delay == 0.005
    assert config.stackchan_retry_seconds == 3.0
    assert config.stream_timeout == 65
    assert config.retry_seconds == 1.0
    assert config.max_retry_seconds == 30.0


def test_merge_config_clamps_volume():
    assert merge_config(_base_config(), {"volume": "-1"}).volume == 0
    assert merge_config(_base_config(), {"volume": "999"}).volume == 255
    assert merge_config(_base_config(), {"volume": "128"}).volume == 128


def test_merge_config_keeps_move_defaults_for_blank_form_values():
    config = merge_config(
        _base_config(),
        {
            "move_idle_yaw": "",
            "move_idle_pitch": "",
            "move_thinking_yaw": "",
            "move_thinking_pitch": "",
            "move_error_yaw": "",
            "move_error_pitch": "",
            "move_talking_sway_yaw": "",
            "move_talking_sway_pitch": "",
            "move_talking_sway_interval": "",
        },
    )

    assert config.move_enabled is True
    assert config.move_idle_pitch == 5.0
    assert config.move_thinking_yaw == -8.0
    assert config.move_error_pitch == -10.0
    assert config.move_talking_sway_yaw == 4.0
    assert config.move_talking_sway_interval == 1.5


def test_merge_config_disables_move_with_form_checkbox_off():
    # HTML form checkbox: not sent when off, so _flatten_form sets move_enabled=False
    config = merge_config(_base_config(), {"move_enabled": False})
    assert config.move_enabled is False


def test_merge_config_accepts_move_overrides():
    config = merge_config(
        _base_config(),
        {
            "move_idle_pitch": "10",
            "move_talking_sway_yaw": "2.5",
            "move_talking_sway_interval": "0.8",
        },
    )
    assert config.move_idle_pitch == 10.0
    assert config.move_talking_sway_yaw == 2.5
    assert config.move_talking_sway_interval == 0.8


def test_merge_config_accepts_puzzle_light_overrides():
    config = merge_config(
        _base_config(),
        {
            "puzzle_light_enabled": False,
            "puzzle_idle": "blue",
            "puzzle_thinking": "rainbow",
            "puzzle_talking": "green",
            "puzzle_error": "red",
        },
    )

    assert config.puzzle_light_enabled is False
    assert config.puzzle_idle == "blue"
    assert config.puzzle_thinking == "rainbow"
    assert config.puzzle_talking == "green"
    assert config.puzzle_error == "red"


def test_merge_config_accepts_sprite_face_options():
    config = merge_config(
        _base_config(),
        {
            "face_mode": "sprite",
            "sprite_sheet": "assets/pets/default/spritesheet.webp",
            "sprite_jpeg_quality": "120",
        },
    )
    assert config.face_mode == "sprite"
    assert config.sprite_sheet == "assets/pets/default/spritesheet.webp"
    assert config.sprite_jpeg_quality == 95


def test_cli_defaults_enable_lcd_mic_and_head_pet_reaction():
    from xangi_stackchan.app import build_parser, config_from_args

    config = config_from_args(build_parser().parse_args([]))

    assert config.voice_conversation is False
    assert config.lcd_mic_voice is True
    assert config.head_pet_reaction is True
    assert config.puzzle_light_enabled is True
    assert config.puzzle_idle == "off"
    assert config.puzzle_thinking == "thinking"
    assert config.puzzle_talking == "talking"
    assert config.puzzle_error == "error"


def test_cli_can_disable_lcd_mic_and_head_pet_reaction():
    from xangi_stackchan.app import build_parser, config_from_args

    config = config_from_args(
        build_parser().parse_args(["--no-lcd-mic-voice", "--no-head-pet-reaction"])
    )

    assert config.lcd_mic_voice is False
    assert config.head_pet_reaction is False


def test_voice_thread_alignment_only_in_voice_mode():
    from dataclasses import replace

    from xangi_stackchan.app import _align_voice_thread, _clear_stale_voice_thread_filter

    base = _base_config()
    voice = replace(
        base,
        voice_conversation=True,
        voice_app_session_id="sid123",
        thread_id="discord:old",
    )
    aligned = _align_voice_thread(voice)
    assert aligned.thread_id == "web:sid123"

    normal = replace(
        aligned,
        voice_conversation=False,
        lcd_mic_voice=True,
    )
    cleared = _clear_stale_voice_thread_filter(normal)
    assert cleared.thread_id is None


def test_clear_stale_voice_thread_filter_keeps_explicit_thread():
    from dataclasses import replace

    from xangi_stackchan.app import _clear_stale_voice_thread_filter

    config = replace(
        _base_config(),
        voice_conversation=False,
        lcd_mic_voice=True,
        voice_app_session_id="sid123",
        thread_id="web:manual",
    )

    assert _clear_stale_voice_thread_filter(config).thread_id == "web:manual"


# --- v1 -> v2 migration --------------------------------------------------


def test_migrate_v1_flat_wraps_into_default_instance():
    raw = {"xangi_url": "http://x", "volume": 128, "tts": "piper"}
    doc = migrate_to_v2(raw)
    assert doc["version"] == CONFIG_SCHEMA_VERSION
    assert DEFAULT_INSTANCE_ID in doc["instances"]
    assert doc["instances"][DEFAULT_INSTANCE_ID]["volume"] == 128
    assert doc["instances"][DEFAULT_INSTANCE_ID]["tts"] == "piper"


def test_migrate_v2_passthrough():
    raw = {"version": 2, "instances": {"left": {"volume": 200}}}
    doc = migrate_to_v2(raw)
    assert doc["version"] == 2
    assert doc["instances"] == {"left": {"volume": 200}}


def test_load_config_file_missing_returns_empty_v2(tmp_path: Path):
    doc = load_config_file(tmp_path / "absent.json")
    assert doc == {"version": CONFIG_SCHEMA_VERSION, "instances": {}}


def test_load_config_file_migrates_v1_on_disk(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"xangi_url": "http://x", "volume": 50}))
    doc = load_config_file(p)
    assert doc["version"] == CONFIG_SCHEMA_VERSION
    assert doc["instances"][DEFAULT_INSTANCE_ID]["xangi_url"] == "http://x"
    assert load_instance_dict(p, DEFAULT_INSTANCE_ID)["volume"] == 50
    assert load_instance_dict(p, "missing") == {}


def test_save_instance_dict_promotes_v1_file(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"xangi_url": "http://x", "volume": 100}))
    save_instance_dict(p, "right", {"xangi_url": "http://right", "volume": 200})
    raw = json.loads(p.read_text())
    assert raw["version"] == CONFIG_SCHEMA_VERSION
    assert set(raw["instances"]) == {DEFAULT_INSTANCE_ID, "right"}
    assert raw["instances"][DEFAULT_INSTANCE_ID]["volume"] == 100
    assert raw["instances"]["right"]["volume"] == 200


def test_save_instance_dict_creates_v2_when_absent(tmp_path: Path):
    p = tmp_path / "cfg.json"
    save_instance_dict(p, "left", {"xangi_url": "http://left"})
    raw = json.loads(p.read_text())
    assert raw == {
        "version": CONFIG_SCHEMA_VERSION,
        "instances": {"left": {"xangi_url": "http://left"}},
    }


def test_runtime_state_persists_to_instance_namespace(tmp_path: Path):
    p = tmp_path / "cfg.json"
    state = RuntimeState(_base_config(), p, instance_id="left")
    state.update({"volume": "200"})

    raw = json.loads(p.read_text())
    assert raw["version"] == CONFIG_SCHEMA_VERSION
    assert raw["instances"]["left"]["volume"] == 200
    # snapshot_dict surfaces the instance id and persisted path
    snapshot = state.snapshot_dict()
    assert snapshot["instance_id"] == "left"
    assert snapshot["config_path"] == str(p)


def test_two_instances_isolated_on_disk(tmp_path: Path):
    p = tmp_path / "cfg.json"
    left = RuntimeState(_base_config(), p, instance_id="left")
    right = RuntimeState(_base_config(), p, instance_id="right")
    left.update({"thread_id": "left-thread"})
    right.update({"thread_id": "right-thread"})

    assert load_instance_dict(p, "left")["thread_id"] == "left-thread"
    assert load_instance_dict(p, "right")["thread_id"] == "right-thread"
