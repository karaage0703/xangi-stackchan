"""head_touch (アタマセンサ) を触った瞬間に「なでなで反応」を返すモード。

voice_conversation (press → 録音 → STT → xangi) と違って、話しかけなくても
なでた瞬間にランダムなセリフを喋る。デモで「とにかく反応する」用途のための
ホスト側だけで完結する機構 (ファーム改修不要)。

ファーム側 (cores3-main の applyHeadTouchAvatar) が press で即 Happy 顔 + 吹き出しを
出すので、それと併用すると「触った瞬間に顔が変わる → 少し遅れて喋り出す」になる。

press と swipe を「なでた」とみなして発火する。連打やなで続けで反応が積み上がらない
ように、発話中は無視し、発話完了後 cooldown_seconds の間は再発火しない。
"""

import random
import threading
import time
from typing import Callable

# セリフは --head-pet-phrases / 設定 UI で差し替え可能。空指定時はこの既定を使う。
DEFAULT_PHRASES: list[str] = [
    "なでなで、ありがとう！",
    "わー、くすぐったいよー",
    "えへへ、うれしいな",
    "もっとなでてー！",
    "やったー、なでてくれた！",
    "ふふっ、気持ちいい",
    "だいすき！",
    "こんにちは！元気だよ",
]

# 「なでた」とみなすジェスチャ。press は単発タッチ、swipe は前後のなで動作。
PET_GESTURES = frozenset({"press", "swipe_forward", "swipe_backward"})


class HeadPetReaction:
    """backend.on_head_touch にバインドして、なでた瞬間に speak を駆動する。

    speak は「テキストを 1 つ受け取って同期的に喋り終えるまでブロックする」callable。
    app.py 側で speak_text(...) をラップして渡す想定。発話は専用 daemon thread で行い、
    serial reader thread (SerialActor) をブロックしない。
    """

    def __init__(
        self,
        backend,
        *,
        speak: Callable[[str], None],
        on_react: Callable[[str, dict], None] | None = None,
        phrases: list[str] | None = None,
        cooldown_seconds: float = 2.0,
        react_on_swipe: bool = True,
    ) -> None:
        self.backend = backend
        self._speak = speak
        self.on_react = on_react
        self.phrases = list(phrases) if phrases else list(DEFAULT_PHRASES)
        self.cooldown_seconds = cooldown_seconds
        self.react_on_swipe = react_on_swipe

        self._lock = threading.Lock()
        self._busy = False
        self._last_react = 0.0
        # bound method は参照のたびに別オブジェクトになり identity 比較が効かないので、
        # bind/unbind 用に 1 つに固定しておく。
        self._handler = self._on_head_touch

    def start(self) -> None:
        self.backend.on_head_touch = self._handler

    def stop(self) -> None:
        if getattr(self.backend, "on_head_touch", None) is self._handler:
            self.backend.on_head_touch = None

    def _is_trigger(self, gesture: str | None) -> bool:
        if gesture == "press":
            return True
        if self.react_on_swipe and gesture in ("swipe_forward", "swipe_backward"):
            return True
        return False

    def _on_head_touch(self, event: dict) -> None:
        """SerialActor の reader thread から呼ばれる。重い処理はしない。"""
        if not self._is_trigger(event.get("gesture")):
            return
        now = time.time()
        with self._lock:
            if self._busy:
                return  # 発話中は無視 (なで続けで積み上がらない)
            if now - self._last_react < self.cooldown_seconds:
                return  # cooldown 中
            self._busy = True
            self._last_react = now
        threading.Thread(target=self._react, args=(event,), daemon=True).start()

    def _react(self, event: dict) -> None:
        try:
            phrase = random.choice(self.phrases) if self.phrases else ""
            if self.on_react is not None:
                try:
                    self.on_react(phrase, event)
                except Exception:
                    pass
            if phrase:
                self._speak(phrase)
        finally:
            with self._lock:
                # cooldown は「発話完了後」から数える (喋り終わってすぐ連発しない)。
                self._last_react = time.time()
                self._busy = False
