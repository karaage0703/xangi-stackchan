import json
import socket
import threading

from xangi_stackchan.stackchan import StackchanTailnet


def test_tailnet_reverse_connection_and_commands():
    backend = StackchanTailnet("127.0.0.1", 0)
    backend.open()

    received = []

    def device():
        with socket.create_connection(("127.0.0.1", backend.port), timeout=2) as sock:
            sock.sendall(
                json.dumps(
                    {
                        "event": "stackchan_connected",
                        "device": "xangi-stackchan-test",
                        "ip": "100.64.0.2",
                        "protocol": 1,
                    }
                ).encode()
                + b"\n"
            )
            stream = sock.makefile("rb")
            for response in (
                {"state": "ready", "servo": True},
                {"status": "ok", "face": "happy"},
                {"status": "ok", "yaw": 4.0, "pitch": 2.0},
            ):
                command = stream.readline().decode().strip()
                received.append(command)
                sock.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=device)
    thread.start()
    try:
        assert backend.send_command("STATUS")["state"] == "ready"
        assert backend.send_command("FACE:happy")["face"] == "happy"
        assert backend.send_command("MOVE:4,2")["yaw"] == 4.0
        assert backend.device["device"] == "xangi-stackchan-test"
    finally:
        thread.join(timeout=2)
        backend.close()

    assert received == ["STATUS", "FACE:happy", "MOVE:4,2"]


def test_tailnet_runs_connected_callback_after_each_handshake():
    backend = StackchanTailnet("127.0.0.1", 0)
    backend.open()
    callback_results = []
    callback_done = threading.Event()

    def on_connected():
        callback_results.append(backend.send_command("HEADPET_SOUND:off"))
        callback_done.set()

    backend.on_connected = on_connected

    def device():
        with socket.create_connection(("127.0.0.1", backend.port), timeout=2) as sock:
            sock.sendall(
                b'{"event":"stackchan_connected","device":"test","protocol":3}\n'
            )
            stream = sock.makefile("rb")
            assert stream.readline().decode().strip() == "HEADPET_SOUND:off"
            sock.sendall(b'{"status":"ok","head_pet_sound":false}\n')

    thread = threading.Thread(target=device)
    thread.start()
    try:
        assert callback_done.wait(timeout=2)
        assert callback_results == [{"status": "ok", "head_pet_sound": False}]
    finally:
        thread.join(timeout=2)
        backend.close()


def test_tailnet_wav_and_lcd_mic_round_trip():
    backend = StackchanTailnet("127.0.0.1", 0)
    backend.open()
    wav_received = bytearray()
    mic_events = []

    def device():
        with socket.create_connection(("127.0.0.1", backend.port), timeout=2) as sock:
            sock.sendall(
                json.dumps(
                    {
                        "event": "stackchan_connected",
                        "device": "xangi-stackchan-test",
                        "ip": "100.64.0.2",
                        "protocol": 2,
                    }
                ).encode()
                + b"\n"
            )
            stream = sock.makefile("rb")

            wav_header = stream.readline().decode().strip()
            wav_size = int(wav_header.split(":", 1)[1])
            sock.sendall(b'{"status":"ready"}\n')
            wav_received.extend(stream.read(wav_size))
            sock.sendall(
                json.dumps({"status": "ok", "size": wav_size, "queued": 1}).encode()
                + b"\n"
            )

            sock.sendall(b'{"event":"mic_button","action":"tap","at":123}\n')
            assert stream.readline().decode().strip() == "MIC_START"
            sock.sendall(b'{"status":"ok","mode":"recording"}\n')
            sock.sendall(b"MIC_PCM:4\n\x01\x00\x02\x00")
            assert stream.readline().decode().strip() == "MIC_STOP"
            sock.sendall(b'{"status":"ok","mode":"speaker"}\n')

    backend.on_mic_button = mic_events.append
    thread = threading.Thread(target=device)
    thread.start()
    try:
        assert (
            backend.send_wav(b"RIFFtest", chunk_size=3, chunk_delay=0)["status"] == "ok"
        )
        for _ in range(100):
            if mic_events:
                break
            threading.Event().wait(0.01)
        assert mic_events[0]["action"] == "tap"
        assert backend.start_mic_recording()["status"] == "ok"
        for _ in range(100):
            if len(backend._mic_pcm_buffer) == 4:
                break
            threading.Event().wait(0.01)
        result = backend.stop_mic_recording()
        assert result["status"] == "ok"
        assert result["pcm"] == b"\x01\x00\x02\x00"
        assert result["frames"] == 2
        assert result["wav"].startswith(b"RIFF")
    finally:
        thread.join(timeout=2)
        backend.close()

    assert wav_received == b"RIFFtest"


def test_tailnet_mic_button_callback_can_start_recording_without_deadlock():
    backend = StackchanTailnet("127.0.0.1", 0)
    backend.open()
    callback_result = []
    callback_done = threading.Event()

    def on_mic_button(_event):
        callback_result.append(backend.start_mic_recording())
        callback_done.set()

    backend.on_mic_button = on_mic_button

    def device():
        with socket.create_connection(("127.0.0.1", backend.port), timeout=2) as sock:
            sock.sendall(
                b'{"event":"stackchan_connected","device":"test","protocol":3}\n'
            )
            stream = sock.makefile("rb")
            sock.sendall(b'{"event":"mic_button","action":"tap"}\n')
            assert stream.readline().decode().strip() == "MIC_START"
            sock.sendall(b'{"status":"ok","mode":"recording"}\n')

    thread = threading.Thread(target=device)
    thread.start()
    try:
        assert callback_done.wait(timeout=2)
        assert callback_result == [{"status": "ok", "mode": "recording"}]
    finally:
        thread.join(timeout=2)
        backend.close()


def test_tailnet_sprite_image_and_camera_round_trip():
    backend = StackchanTailnet("127.0.0.1", 0)
    backend.open()
    uploaded = bytearray()
    sprite = b"sprite123"
    jpeg = b"\xff\xd8camera-jpeg\xff\xd9"

    def device():
        with socket.create_connection(("127.0.0.1", backend.port), timeout=2) as sock:
            sock.sendall(
                json.dumps(
                    {
                        "event": "stackchan_connected",
                        "device": "xangi-stackchan-test",
                        "ip": "100.64.0.2",
                        "protocol": 3,
                    }
                ).encode()
                + b"\n"
            )
            stream = sock.makefile("rb")

            image_header = stream.readline().decode().strip()
            assert image_header == f"SIMG:7,{len(sprite)}"
            sock.sendall(b'{"status":"ready"}\n')
            uploaded.extend(stream.read(len(sprite)))
            sock.sendall(
                json.dumps(
                    {"status": "ok", "sprite_image": 7, "size": len(sprite)}
                ).encode()
                + b"\n"
            )

            assert stream.readline().decode().strip() == "SFRAME:7"
            sock.sendall(b'{"status":"ok","sprite_frame":7}\n')

            assert stream.readline().decode().strip() == "CAPTURE"
            sock.sendall(f"IMG:{len(jpeg)}\n".encode() + jpeg)
            sock.sendall(
                json.dumps(
                    {
                        "status": "ok",
                        "size": len(jpeg),
                        "format": "jpeg",
                        "width": 320,
                        "height": 240,
                        "captured_at": 1234,
                    }
                ).encode()
                + b"\n"
            )

    thread = threading.Thread(target=device)
    thread.start()
    try:
        assert (
            backend.cache_image_frame(7, sprite, chunk_size=3, chunk_delay=0)["status"]
            == "ok"
        )
        assert backend.show_cached_image(7)["status"] == "ok"
        result = backend.capture()
        assert result["status"] == "ok"
        assert result["image_jpeg"] == jpeg
        assert result["captured_at_device_ms"] == 1234
    finally:
        thread.join(timeout=2)
        backend.close()

    assert uploaded == sprite
