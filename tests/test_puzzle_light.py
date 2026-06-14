from xangi_stackchan.app import (
    detect_puzzle_light_support,
    set_puzzle_light_if_needed,
)
from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.stackchan import StackchanConfig


class FakeBackend:
    def __init__(self, *, puzzle=True, stack_led=False):
        self.puzzle = puzzle
        self.stack_led = stack_led
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        if command == "STATUS":
            return {
                "status": "ok",
                "puzzle": self.puzzle,
                "stack_led": self.stack_led,
            }
        if command.startswith("PUZZLE:"):
            return {"status": "ok", "puzzle": command.split(":", 1)[1]}
        if command.startswith("STACKLED:"):
            return {"status": "ok", "stack_led": command.split(":", 1)[1]}
        return {"status": "ok"}


def _config() -> BridgeConfig:
    return BridgeConfig(
        xangi_url="http://127.0.0.1:18888",
        thread_id=None,
        stackchan=StackchanConfig(wifi=False, host="", port="/dev/null", baud=921600),
        volume=128,
        tts="none",
        piper_bin="",
        piper_model="",
        piper_speaker=0,
        voicevox_url="",
        voicevox_speaker=0,
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


def test_detect_puzzle_light_support_reads_status():
    backend = FakeBackend(puzzle=True, stack_led=True)

    assert detect_puzzle_light_support(backend, _config()) == {
        "PUZZLE": True,
        "STACKLED": True,
    }
    assert backend.commands == ["STATUS"]


def test_set_puzzle_light_skips_unsupported_and_duplicate_patterns():
    backend = FakeBackend(puzzle=True)
    config = _config()
    current = [None]
    supported = [{}]

    assert set_puzzle_light_if_needed(backend, config, "thinking", current, supported)
    assert backend.commands == []

    supported[0] = {"PUZZLE": True, "STACKLED": True}
    assert set_puzzle_light_if_needed(backend, config, "thinking", current, supported)
    assert set_puzzle_light_if_needed(backend, config, "thinking", current, supported)
    assert backend.commands == ["PUZZLE:thinking", "STACKLED:thinking"]
    assert current[0] == {"PUZZLE": "thinking", "STACKLED": "thinking"}
