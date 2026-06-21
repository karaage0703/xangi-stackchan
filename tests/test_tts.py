import threading
import time

from xangi_stackchan.tts import split_text, wait_for_complete_file


def test_split_text_keeps_japanese_sentences():
    assert split_text("こんにちは。元気ですか？はい！", max_len=8) == [
        "こんにちは。",
        "元気ですか？",
        "はい！",
    ]


def test_split_text_hard_splits_long_sentence_without_punctuation():
    assert split_text("長い文章が句読点なしで続くケース", max_len=5) == [
        "長い文章が",
        "句読点なし",
        "で続くケー",
        "ス",
    ]


def test_wait_for_complete_file_waits_for_stable_size(tmp_path):
    target = tmp_path / "out.wav"

    def writer():
        target.write_bytes(b"")
        time.sleep(0.05)
        target.write_bytes(b"RIFF" + b"\0" * 60)

    thread = threading.Thread(target=writer)
    thread.start()
    data = wait_for_complete_file(target, time.time() + 2)
    thread.join()
    assert len(data) == 64
