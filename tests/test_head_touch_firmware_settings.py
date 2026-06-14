from xangi_stackchan.app import _apply_head_touch_firmware_settings


class FakeBackend:
    def __init__(self):
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        return {"status": "ok"}


def test_voice_mode_suppresses_head_touch_feedback():
    backend = FakeBackend()

    _apply_head_touch_firmware_settings(
        backend,
        suppress_head_touch_avatar=True,
        suppress_head_pet_sound=True,
    )

    assert backend.commands == [
        "HEADTOUCH_AVATAR:off",
        "HEADPET_SOUND:off",
    ]


def test_lcd_mic_mode_restores_head_touch_feedback():
    backend = FakeBackend()

    _apply_head_touch_firmware_settings(
        backend,
        suppress_head_touch_avatar=False,
        suppress_head_pet_sound=False,
    )

    assert backend.commands == [
        "HEADTOUCH_AVATAR:on",
        "HEADPET_SOUND:on",
    ]


def test_host_head_pet_mode_keeps_avatar_and_suppresses_builtin_sound():
    backend = FakeBackend()

    _apply_head_touch_firmware_settings(
        backend,
        suppress_head_touch_avatar=False,
        suppress_head_pet_sound=True,
    )

    assert backend.commands == [
        "HEADTOUCH_AVATAR:on",
        "HEADPET_SOUND:off",
    ]
