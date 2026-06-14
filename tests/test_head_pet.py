"""HeadPetReaction (なでなで反応モード) の単体テスト。"""

import time

from xangi_stackchan.head_pet import DEFAULT_PHRASES, HeadPetReaction


class FakeBackend:
    def __init__(self):
        self.on_head_touch = None


def _wait_until(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _make(**kwargs):
    backend = FakeBackend()
    spoken: list[str] = []
    hp = HeadPetReaction(
        backend,
        speak=lambda text: spoken.append(text),
        phrases=kwargs.pop("phrases", ["ぽよ"]),
        cooldown_seconds=kwargs.pop("cooldown_seconds", 0.05),
        **kwargs,
    )
    return backend, hp, spoken


def test_start_binds_callback():
    backend, hp, _ = _make()
    hp.start()
    assert backend.on_head_touch is hp._handler


def test_stop_unbinds_callback():
    backend, hp, _ = _make()
    hp.start()
    hp.stop()
    assert backend.on_head_touch is None


def test_press_triggers_speak():
    backend, hp, spoken = _make(phrases=["なでなで"])
    hp.start()
    backend.on_head_touch({"gesture": "press"})
    assert _wait_until(lambda: spoken == ["なでなで"])


def test_release_and_none_ignored():
    backend, hp, spoken = _make()
    hp.start()
    backend.on_head_touch({"gesture": "release"})
    backend.on_head_touch({"gesture": "none"})
    time.sleep(0.1)
    assert spoken == []


def test_swipe_triggers_when_enabled():
    backend, hp, spoken = _make(react_on_swipe=True)
    hp.start()
    backend.on_head_touch({"gesture": "swipe_forward"})
    assert _wait_until(lambda: len(spoken) == 1)


def test_swipe_ignored_when_disabled():
    backend, hp, spoken = _make(react_on_swipe=False)
    hp.start()
    backend.on_head_touch({"gesture": "swipe_forward"})
    time.sleep(0.1)
    assert spoken == []


def test_cooldown_blocks_rapid_repeat():
    backend, hp, spoken = _make(phrases=["ぽよ"], cooldown_seconds=10.0)
    hp.start()
    backend.on_head_touch({"gesture": "press"})
    assert _wait_until(lambda: len(spoken) == 1)
    # cooldown 中の 2 回目は無視される。
    backend.on_head_touch({"gesture": "press"})
    time.sleep(0.1)
    assert len(spoken) == 1


def test_reaction_repeats_after_cooldown():
    backend, hp, spoken = _make(phrases=["ぽよ"], cooldown_seconds=0.05)
    hp.start()
    backend.on_head_touch({"gesture": "press"})
    assert _wait_until(lambda: len(spoken) == 1)
    time.sleep(0.1)  # cooldown 経過
    backend.on_head_touch({"gesture": "press"})
    assert _wait_until(lambda: len(spoken) == 2)


def test_busy_guard_during_speak():
    backend = FakeBackend()
    spoken: list[str] = []
    release = {"go": False}

    def slow_speak(text):
        while not release["go"]:
            time.sleep(0.005)
        spoken.append(text)

    hp = HeadPetReaction(backend, speak=slow_speak, phrases=["ぽよ"], cooldown_seconds=0.0)
    hp.start()
    backend.on_head_touch({"gesture": "press"})  # 発話開始 (slow_speak でブロック)
    time.sleep(0.05)
    backend.on_head_touch({"gesture": "press"})  # busy 中なので無視
    release["go"] = True
    assert _wait_until(lambda: len(spoken) == 1)
    time.sleep(0.1)
    assert len(spoken) == 1


def test_empty_phrases_falls_back_to_default():
    backend = FakeBackend()
    hp = HeadPetReaction(backend, speak=lambda t: None, phrases=None)
    assert hp.phrases == DEFAULT_PHRASES


def test_on_react_called_with_phrase_and_event():
    backend = FakeBackend()
    seen: list[tuple] = []
    hp = HeadPetReaction(
        backend,
        speak=lambda t: None,
        on_react=lambda phrase, event: seen.append((phrase, event.get("gesture"))),
        phrases=["やあ"],
        cooldown_seconds=0.0,
    )
    hp.start()
    backend.on_head_touch({"gesture": "press"})
    assert _wait_until(lambda: seen == [("やあ", "press")])
