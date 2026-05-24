#!/usr/bin/env python3
"""RTBridge ファーム (firmware/examples/basic/main) 単体テスト。

アールティ Ver.β スタックチャン (M5Stack Basic + Feetech SCS0009 ×2) で
flash した RTBridge ファームと USB シリアル疎通する。STATUS / VOLUME / FACE /
WAV の動作を確認、WAV は 96KB 上限を考慮した短いトーンを送信。

XangiBridge との差分:
    - default baud = 115200 (M5Stack Basic CP2104 想定、要実機検証)
    - duration default = 1.5 秒 (96KB SRAM 制約、16kHz/mono/16bit で約 48KB)
    - MOVE は unavailable 応答想定 (SCServo 統合は別 PR)
    - CAPTURE は unavailable 固定 (M5Stack Basic にカメラ無し)

使い方:
    uv run python scripts/test_rt_bridge.py --port /dev/ttyACM0
    # or 自動検出
    uv run python scripts/test_rt_bridge.py
"""

import argparse
import math
import struct
import sys
import time

try:
    from xangi_stackchan.stackchan import (
        StackchanSerial,
        detect_serial_port,
    )
except ImportError:
    print(
        "ERROR: src/xangi_stackchan が import できない。`uv sync` 済みか確認。",
        file=sys.stderr,
    )
    sys.exit(1)


RT_DEFAULT_BAUD = 115200
RT_MAX_WAV_BYTES = 96 * 1024  # firmware MAX_WAV_BYTES と一致


def build_sine_wav(
    freq_hz: float = 440.0,
    duration_s: float = 1.5,
    sample_rate: int = 16000,
    amplitude: float = 0.45,
) -> bytes:
    sample_count = int(sample_rate * duration_s)
    pcm_bytes_len = sample_count * 2

    header = b"RIFF"
    header += struct.pack("<I", 36 + pcm_bytes_len)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)
    header += struct.pack("<H", 1)
    header += struct.pack("<H", 1)
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", sample_rate * 2)
    header += struct.pack("<H", 2)
    header += struct.pack("<H", 16)
    header += b"data"
    header += struct.pack("<I", pcm_bytes_len)

    peak = int(32767 * amplitude)
    rad_per_smp = 2 * math.pi * freq_hz / sample_rate
    fade = int(sample_rate * 0.005)
    pcm = bytearray()
    for i in range(sample_count):
        if i < fade:
            gain = i / fade
        elif i >= sample_count - fade:
            gain = (sample_count - 1 - i) / fade
        else:
            gain = 1.0
        s = int(peak * gain * math.sin(rad_per_smp * i))
        pcm += struct.pack("<h", s)

    return bytes(header) + bytes(pcm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=None, help="シリアルポート (省略時は自動検出)")
    parser.add_argument("--baud", type=int, default=RT_DEFAULT_BAUD,
                        help=f"ボーレート (デフォルト {RT_DEFAULT_BAUD}、RT 版 M5Stack Basic 想定)")
    parser.add_argument("--freq", type=float, default=440.0, help="トーン周波数 Hz")
    parser.add_argument("--duration", type=float, default=1.5,
                        help="トーン長 秒 (96KB 上限内、16kHz mono なら最大約 3.0)")
    parser.add_argument("--volume", type=int, default=160, help="音量 0..255")
    parser.add_argument("--skip-move", action="store_true",
                        help="MOVE テストを飛ばす (現 RTBridge は SCServo 未統合、default は実行して unavailable 応答を確認)")
    args = parser.parse_args()

    port = args.port or detect_serial_port()
    print(f"[test] RTBridge: port={port} baud={args.baud}")
    backend = StackchanSerial(port, args.baud)
    backend.open()
    try:
        time.sleep(0.5)
        backend.drain()

        print("[test] -> STATUS (起動状態確認)")
        resp = backend.send_command("STATUS")
        print(f"[test] <- {resp}")
        if "rt-bridge" not in str(resp.get("version", "")):
            print("[test] WARN: version が rt-bridge 系でない。XangiBridge を間違えて flash した可能性")

        print(f"[test] -> VOLUME:{args.volume}")
        resp = backend.send_command(f"VOLUME:{args.volume}")
        print(f"[test] <- {resp}")

        for face in ["happy", "sad", "doubt", "sleepy", "neutral"]:
            print(f"[test] -> FACE:{face}")
            resp = backend.send_command(f"FACE:{face}")
            print(f"[test] <- {resp}")
            time.sleep(0.5)

        if not args.skip_move:
            print("[test] -> MOVE:0,0 (現 RTBridge は SCServo 未統合、unavailable 応答想定)")
            resp = backend.send_command("MOVE:0,0")
            print(f"[test] <- {resp}")

        print("[test] -> CAPTURE (M5Stack Basic はカメラ無し、unavailable 応答想定)")
        resp = backend.send_command("CAPTURE")
        print(f"[test] <- {resp}")

        wav = build_sine_wav(freq_hz=args.freq, duration_s=args.duration)
        if len(wav) > RT_MAX_WAV_BYTES:
            print(f"[test] ERROR: WAV size {len(wav)} > {RT_MAX_WAV_BYTES} (firmware 上限)、--duration 短く")
            return 1
        print(f"[test] -> WAV:{len(wav)} ({args.freq:.0f}Hz {args.duration:.2f}s)")
        t0 = time.time()
        resp = backend.send_wav(wav)
        elapsed = time.time() - t0
        print(f"[test] <- {resp}  (elapsed {elapsed:.2f}s)")

        print("[test] -> STATUS (再生後)")
        resp = backend.send_command("STATUS")
        print(f"[test] <- {resp}")
    finally:
        backend.close()

    print("[test] 完了。Avatar 顔表示・トーン音・口パクが見えたら基本動作 OK。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
