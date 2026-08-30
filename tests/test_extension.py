import json
import urllib.error
import urllib.request
from pathlib import Path

from test_settings import _base_config

from xangi_stackchan.extension import _xangi_url, build_state
from xangi_stackchan.settings import RuntimeState
from xangi_stackchan.settings_server import start_settings_server


def test_xangi_url_from_events_stream():
    assert (
        _xangi_url("http://127.0.0.1:18888/api/events/stream")
        == "http://127.0.0.1:18888"
    )


def test_managed_mode_defaults_to_completion_notifications(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "XANGI_EXTENSION_EVENTS_URL",
        "http://127.0.0.1:18888/api/events/stream",
    )
    monkeypatch.setenv("XANGI_EXTENSION_INSTANCE_ID", "extension-test")
    state = build_state(tmp_path)
    config, _ = state.snapshot()
    assert config.speak_responses is False
    assert config.completion_notifications is True
    assert config.lcd_mic_voice is False
    assert config.firmware_head_pet_sound is False


def test_managed_settings_server_requires_bearer_token(tmp_path: Path):
    state = RuntimeState(_base_config(), tmp_path / "config.json")
    server, port = start_settings_server(state, "127.0.0.1", 0, auth_token="secret")
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        try:
            urllib.request.urlopen(url)
            raise AssertionError("request without token must fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        request = urllib.request.Request(
            url, headers={"Authorization": "Bearer secret"}
        )
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        assert payload["ready"] is False
        assert payload["service"] == "xangi-stackchan"
        assert payload["service_running"] is True
        assert payload["device_connected"] is False
    finally:
        server.shutdown()
        server.server_close()
