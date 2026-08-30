import json
import re
import time
from pathlib import Path

_SUGGESTIONS_RE = re.compile(
    r"<xangi_reply_suggestions>.*?</xangi_reply_suggestions>", re.DOTALL
)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"\[([^]]+)]\([^)]+\)")
_TURN_TIMER_MAX_AGE_SECONDS = 24 * 60 * 60


class TurnTimerStore:
    """Persist turn start times so completion notices survive Extension restarts."""

    def __init__(self, path: Path):
        self.path = path
        self._started_at = self._load()

    def _load(self) -> dict[str, float]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        now = time.time()
        return {
            str(turn_id): float(started_at)
            for turn_id, started_at in raw.items()
            if isinstance(started_at, (int, float))
            and 0 <= now - float(started_at) <= _TURN_TIMER_MAX_AGE_SECONDS
        }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._started_at, separators=(",", ":")), encoding="utf-8"
            )
            temporary.replace(self.path)
        except OSError:
            pass

    def start(self, turn_id: str, started_at: float) -> None:
        self._started_at[str(turn_id)] = float(started_at)
        self._save()

    def pop(self, turn_id: str) -> float | None:
        started_at = self._started_at.pop(str(turn_id), None)
        self._save()
        return started_at


def summarize_completion(text: str, max_chars: int = 100) -> str:
    """Turn a final response into a short, deterministic spoken notice."""
    cleaned = _SUGGESTIONS_RE.sub("", text or "")
    cleaned = _FENCE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|[-*+] |\d+[.)] )\s*", "", cleaned)
    cleaned = re.sub(r"[*_`~]", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "作業が完了しました。詳しくはxangiを確認してください。"

    limit = max(20, int(max_chars))
    if len(cleaned) > limit:
        sentence = re.search(r"^.{1,%d}?[。！？!?](?:\s|$)" % limit, cleaned)
        cleaned = sentence.group(0).strip() if sentence else cleaned[:limit].rstrip()
        if cleaned[-1:] not in "。！？!?":
            cleaned += "。"
    return f"作業が完了しました。{cleaned}"


def completion_text(
    text: str,
    *,
    speak_responses: bool,
    force_response: bool = False,
    completion_notifications: bool,
    elapsed_seconds: float | None,
    after_seconds: float,
    max_chars: int,
) -> str | None:
    if (
        completion_notifications
        and elapsed_seconds is not None
        and elapsed_seconds >= max(0.0, after_seconds)
    ):
        return summarize_completion(text, max_chars)
    return text if speak_responses or force_response else None


def completion_decision(
    text: str,
    *,
    speak_responses: bool,
    force_response: bool = False,
    completion_notifications: bool,
    elapsed_seconds: float | None,
    after_seconds: float,
    max_chars: int,
    duplicate: bool = False,
) -> dict[str, object]:
    """Return spoken text plus a stable, secret-free diagnostic reason."""
    if duplicate:
        return {"text": None, "notified": False, "reason": "duplicate"}
    if completion_notifications and elapsed_seconds is not None:
        if elapsed_seconds >= max(0.0, after_seconds):
            return {
                "text": summarize_completion(text, max_chars),
                "notified": True,
                "reason": "notified",
            }
        reason = "below_threshold"
    elif not completion_notifications:
        reason = "notifications_disabled"
    else:
        reason = "start_time_missing"
    response_text = text if speak_responses or force_response else None
    return {"text": response_text, "notified": False, "reason": reason}
