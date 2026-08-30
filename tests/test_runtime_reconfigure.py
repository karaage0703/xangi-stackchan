import threading

from test_settings import _base_config

from xangi_stackchan import app
from xangi_stackchan.settings import RuntimeState


class FakeBackend:
    def __init__(self):
        self.is_connected = True
        self.closed = False

    def open(self):
        return None

    def close(self):
        self.closed = True

    def send_command(self, command):
        if command == "STATUS":
            return {"status": "ok", "mic_recording": False, "head_touch": True}
        return {"status": "ok"}


class FakeDeviceLock:
    def __init__(self, _device):
        pass

    def acquire(self):
        return self

    def release(self):
        return None


def test_runtime_reapplies_config_without_an_sse_event(tmp_path, monkeypatch):
    created = []
    first_stream_closed = threading.Event()
    stream_started = threading.Event()

    def create_backend(config):
        backend = FakeBackend()
        created.append(
            {
                "backend": backend,
                "tailnet": config.tailnet,
                "speak_responses": state.snapshot()[0].speak_responses,
                "completion_notifications": state.snapshot()[
                    0
                ].completion_notifications,
            }
        )
        return backend

    class SilentStream:
        count = 0

        def __init__(self, *_args, **_kwargs):
            type(self).count += 1
            self.number = type(self).count

        def close(self):
            first_stream_closed.set()

        def __iter__(self):
            if self.number == 1:
                stream_started.set()
                assert first_stream_closed.wait(2)
                return
            raise KeyboardInterrupt
            yield

    state = RuntimeState(_base_config(), tmp_path / "config.json")
    state.update({"tts": "none"})
    monkeypatch.setattr(app, "create_backend", create_backend)
    monkeypatch.setattr(app, "DeviceLock", FakeDeviceLock)
    monkeypatch.setattr(app, "XangiEventStream", SilentStream)

    worker = threading.Thread(target=app.run_bridge, args=(state,), daemon=True)
    worker.start()
    assert stream_started.wait(2)

    state.update(
        {
            "tailnet": True,
            "speak_responses": True,
            "completion_notifications": False,
        }
    )
    worker.join(2)

    assert not worker.is_alive()
    assert first_stream_closed.is_set()
    assert created[0]["backend"].closed is True
    assert created[1]["tailnet"] is True
    assert created[1]["speak_responses"] is True
    assert created[1]["completion_notifications"] is False
