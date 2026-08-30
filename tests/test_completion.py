import json
import time

from xangi_stackchan.completion import (
    TurnTimerStore,
    completion_decision,
    completion_text,
    summarize_completion,
)


def test_completion_diagnostics_cover_notification_outcomes():
    common = {
        "text": "完了です。",
        "speak_responses": False,
        "after_seconds": 30,
        "max_chars": 100,
    }
    assert (
        completion_decision(
            **common, completion_notifications=True, elapsed_seconds=30
        )["reason"]
        == "notified"
    )
    assert (
        completion_decision(
            **common, completion_notifications=False, elapsed_seconds=60
        )["reason"]
        == "notifications_disabled"
    )
    assert (
        completion_decision(
            **common, completion_notifications=True, elapsed_seconds=29.9
        )["reason"]
        == "below_threshold"
    )
    assert completion_decision(
        **common,
        completion_notifications=True,
        elapsed_seconds=60,
        duplicate=True,
    ) == {"text": None, "notified": False, "reason": "duplicate"}


def test_summarize_completion_removes_discord_suggestions_and_markdown():
    text = '## 結果\n- [設定](https://example.com)を更新しました。\n<xangi_reply_suggestions>["次へ"]</xangi_reply_suggestions>'
    assert (
        summarize_completion(text, 80)
        == "作業が完了しました。結果 設定を更新しました。"
    )


def test_completion_mode_only_speaks_after_threshold():
    assert (
        completion_text(
            "完了です。",
            speak_responses=False,
            completion_notifications=True,
            elapsed_seconds=29.9,
            after_seconds=30,
            max_chars=100,
        )
        is None
    )
    assert (
        completion_text(
            "完了です。",
            speak_responses=False,
            completion_notifications=True,
            elapsed_seconds=30,
            after_seconds=30,
            max_chars=100,
        )
        == "作業が完了しました。完了です。"
    )


def test_response_and_notification_switches_are_independent():
    assert (
        completion_text(
            "全文",
            speak_responses=True,
            completion_notifications=False,
            elapsed_seconds=None,
            after_seconds=30,
            max_chars=100,
        )
        == "全文"
    )
    assert (
        completion_text(
            "全文",
            speak_responses=False,
            completion_notifications=False,
            elapsed_seconds=60,
            after_seconds=30,
            max_chars=100,
        )
        is None
    )


def test_lcd_voice_response_can_bypass_normal_response_switch():
    assert (
        completion_text(
            "音声入力への返答",
            speak_responses=False,
            force_response=True,
            completion_notifications=False,
            elapsed_seconds=1,
            after_seconds=30,
            max_chars=100,
        )
        == "音声入力への返答"
    )


def test_turn_timer_survives_process_restart(tmp_path):
    path = tmp_path / "turn-timers.json"
    started_at = time.time() - 60
    TurnTimerStore(path).start("turn-1", started_at)

    restored = TurnTimerStore(path)

    assert restored.pop("turn-1") == started_at
    assert json.loads(path.read_text()) == {}


def test_turn_timer_discards_stale_entries(tmp_path):
    path = tmp_path / "turn-timers.json"
    path.write_text(json.dumps({"stale": time.time() - 90000}))

    assert TurnTimerStore(path).pop("stale") is None
