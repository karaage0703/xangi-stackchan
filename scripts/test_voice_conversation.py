#!/usr/bin/env python3
"""voice_conversation 単体テスト (xangi 連携無し)。

M5Stackchan K151 をつないで以下を確認する:
  1. STATUS で head_touch=true / mic_recording=false / version cores3-main-0.9+
  2. アタマセンサを N 回 tap → そのつど録音 → STT → 文字を表示 + WAV 保存
  3. xangi `POST /api/chat` は叩かない (--xangi-post で有効化)
  4. 録音中の press toggle / 無音自動停止 / 最大時間で強制停止 を確認

使い方:
    # USB シリアル経由、3 回 tap して録音 → STT 比較
    uv run python scripts/test_voice_conversation.py --port /dev/ttyACM1 --rounds 3

    # 既存の voice_conversation と同じく xangi に POST する
    uv run python scripts/test_voice_conversation.py --port /dev/ttyACM1 --xangi-post \
        --xangi-url http://127.0.0.1:18888

    # whisper モデル切り替え (env でも可)
    STACKCHAN_WHISPER_MODEL=medium uv run python scripts/test_voice_conversation.py ...

このスクリプトは `xangi_stackchan.voice_conversation.VoiceConversation` を
直接呼んで動かす。app.py の SSE event loop は経由しないので、Discord/Slack の
noise や TTS 再生・FACE/MOVE 連動は発生しない。Phase 2 / 2.1 で実装した
録音 → STT → POST 経路だけを切り出して単体検証するための補助スクリプト。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:
    from xangi_stackchan import stt as stt_module
    from xangi_stackchan.stackchan import (
        DEFAULT_BAUD,
        StackchanSerial,
        detect_serial_port,
    )
    from xangi_stackchan.voice_conversation import VoiceConversation
except ImportError as exc:
    print(f"ERROR: import 失敗: {exc}. `uv sync` 済みか確認。", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default="", help="USB シリアル port (省略時は自動検出)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="tap → 録音 → STT を何回繰り返すか (既定 3、Ctrl-C で中断)",
    )
    parser.add_argument(
        "--silence-dbfs",
        type=float,
        default=-40.0,
        help="無音判定 dBFS 閾値 (既定 -40)",
    )
    parser.add_argument(
        "--silence-seconds",
        type=float,
        default=1.5,
        help="この秒数無音が続いたら自動停止 (既定 1.5)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=15.0,
        help="最大録音時間 (既定 15)",
    )
    parser.add_argument(
        "--xangi-post",
        action="store_true",
        help="STT 結果を xangi `POST /api/chat` に投入する (既定 OFF、STT 出力だけ確認)",
    )
    parser.add_argument(
        "--xangi-url",
        default="http://127.0.0.1:18888",
        help="--xangi-post 有効時の base URL",
    )
    parser.add_argument(
        "--save-wav",
        default="/tmp/voice_test_round_{n}.wav",
        help="録音 WAV 保存先テンプレート ({n} に round 番号)",
    )
    args = parser.parse_args()

    port = args.port or detect_serial_port()
    print(f"[test] connecting to {port} @ {args.baud}")
    backend = StackchanSerial(port, args.baud)
    backend.open()

    # STATUS で前提確認
    status = backend.send_command("STATUS")
    print(f"[test] STATUS = {status}")
    if not status.get("head_touch"):
        print("[test] WARN: head_touch=false. M5Stackchan K151 以外かも", file=sys.stderr)
    version = str(status.get("version", ""))
    if "0.9" not in version and "0.10" not in version and "0.11" not in version:
        print(
            f"[test] WARN: ファーム version={version} (0.9+ が必要)。"
            " MIC_START / MIC_STOP に未対応の可能性",
            file=sys.stderr,
        )

    # STT モデル warm-up (初回 DL を事前に走らせる)
    print("[test] warming up Whisper model...")
    t0 = time.time()
    stt_module.load_model()
    print(f"  loaded in {time.time()-t0:.1f}s  meta={stt_module.get_load_meta()}")

    # VoiceConversation を `xangi-post` の有無で挙動切替
    rounds_done = {"n": 0}
    done_event = threading.Event()
    results: list[dict] = []

    last_wav: dict = {"path": None}

    def on_stop(stop_result: dict) -> None:
        # STT 前に呼ばれる (録音停止 + WAV 構築直後)。WAV を保存。
        n = rounds_done["n"] + 1
        wav = stop_result.get("wav", b"")
        path = args.save_wav.format(n=n)
        try:
            with open(path, "wb") as f:
                f.write(wav)
            last_wav["path"] = path
        except Exception as exc:
            print(f"[round {n}] WAV save failed: {exc}", file=sys.stderr)

    def on_transcribed(text: str, r: dict) -> None:
        n = rounds_done["n"] + 1
        rounds_done["n"] = n
        print(
            f"\n[round {n}/{args.rounds}] STT result:"
            f"\n  text       = {text!r}"
            f"\n  language   = {r.get('language')}  prob={r.get('language_probability'):.2f}"
            f"\n  segments   = {len(r.get('segments', []))}"
            f"\n  elapsed_s  = {r.get('elapsed_seconds'):.2f}"
            f"\n  wav_saved  = {last_wav['path']}"
        )
        results.append({"n": n, "text": text, "stt": r, "wav": last_wav["path"]})
        if n >= args.rounds:
            done_event.set()

    def on_sent(text: str, info: dict) -> None:
        print(f"  → xangi POST: status={info.get('status_code')} url={info.get('url')}")

    vc = VoiceConversation(
        backend,
        xangi_base_url=args.xangi_url if args.xangi_post else "",
        silence_dbfs=args.silence_dbfs,
        silence_seconds=args.silence_seconds,
        max_record_seconds=args.max_seconds,
        on_transcribed=on_transcribed,
        on_sent=on_sent if args.xangi_post else None,
        on_stop=on_stop,
    )

    # xangi_post=False 時は VoiceConversation._stt_and_send 内の POST 経路を黙らせる。
    # シンプルに base_url を空にしておけば POST は失敗するが log_on_sent で見える。
    if not args.xangi_post:
        # POST を無効化 (空 base_url なら requests.post が "/api/chat" 単独で失敗)
        vc.xangi_base_url = ""

    vc.start()
    print(
        f"\n[test] ready. M5Stackchan のアタマセンサを {args.rounds} 回 tap して喋ってください。"
        f"\n  silence_dbfs={args.silence_dbfs}  silence_seconds={args.silence_seconds}"
        f"\n  max_seconds={args.max_seconds}  xangi_post={args.xangi_post}"
    )

    try:
        done_event.wait(timeout=300)  # 最大 5 分待ち
        if not done_event.is_set():
            print("[test] timeout (5 分)")
    except KeyboardInterrupt:
        print("\n[test] aborted by user")

    vc.stop()
    print("\n[test] summary:")
    for r in results:
        print(f"  round {r['n']}: {r['text']!r}")

    backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
