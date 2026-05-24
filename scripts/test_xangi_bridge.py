#!/usr/bin/env python3
"""XangiBridge ファーム (firmware/examples/cores3/main) 単体テスト。

STATUS / VOLUME / WAV の往復を確認する。WAV は 440Hz の sine tone を 1 秒分
組み立てて送信、デバイス側で実音が鳴ることを耳で確認する。

使い方:
    uv run python scripts/test_xangi_bridge.py --port /dev/ttyACM0
"""

import argparse
import math
import struct
import sys
import time

try:
    from xangi_stackchan.stackchan import (
        DEFAULT_BAUD,
        StackchanSerial,
        detect_serial_port,
    )
except ImportError:
    print(
        "ERROR: src/xangi_stackchan が import できない。`uv sync` 済みか確認。",
        file=sys.stderr,
    )
    sys.exit(1)


def build_sine_wav(
    freq_hz: float = 440.0,
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    amplitude: float = 0.45,
) -> bytes:
    """16bit/mono/sample_rate Hz の RIFF/WAVE PCM を組み立てて bytes で返す。

    XangiBridge の `examples/cores3/speaker-demo/main.cpp::buildToneWav()` と
    同じ形式 (44 byte header + sint16 LE PCM)。
    """
    sample_count = int(sample_rate * duration_s)
    pcm_bytes_len = sample_count * 2  # int16

    # 44 byte RIFF/WAVE/fmt /data ヘッダ
    header = b"RIFF"
    header += struct.pack("<I", 36 + pcm_bytes_len)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)  # fmt chunk size (PCM)
    header += struct.pack("<H", 1)  # audio format = PCM
    header += struct.pack("<H", 1)  # mono
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", sample_rate * 2)  # byte rate
    header += struct.pack("<H", 2)  # block align
    header += struct.pack("<H", 16)  # bits per sample
    header += b"data"
    header += struct.pack("<I", pcm_bytes_len)

    # PCM body (頭尾 5ms フェード)
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
    parser.add_argument(
        "--port",
        default=None,
        help="シリアルポート (省略時は detect_serial_port で自動検出)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"ボーレート (デフォルト {DEFAULT_BAUD})",
    )
    parser.add_argument("--freq", type=float, default=440.0, help="トーン周波数 Hz")
    parser.add_argument("--duration", type=float, default=1.0, help="トーン長 秒")
    parser.add_argument("--volume", type=int, default=160, help="音量 0..255")
    args = parser.parse_args()

    port = args.port or detect_serial_port()
    print(f"[test] port={port} baud={args.baud}")
    backend = StackchanSerial(port, args.baud)
    backend.open()
    try:
        time.sleep(0.5)
        backend.drain()

        print("[test] -> STATUS")
        resp = backend.send_command("STATUS")
        print(f"[test] <- {resp}")

        print(f"[test] -> VOLUME:{args.volume}")
        resp = backend.send_command(f"VOLUME:{args.volume}")
        print(f"[test] <- {resp}")

        # FACE 巡回 (PR #14)。デバイスが Avatar 統合版でない場合は unsupported 応答。
        for face in ["happy", "sad", "doubt", "sleepy", "neutral"]:
            print(f"[test] -> FACE:{face}")
            resp = backend.send_command(f"FACE:{face}")
            print(f"[test] <- {resp}")
            time.sleep(0.5)

        # MOVE 巡回 (PR #15)。サーボ未準備なら error 応答。
        # SAFE 範囲 (yaw ±100°、pitch ±30°) と clamp 透明 ack を確認。
        for yaw, pitch, note in [
            (0, 0, "center"),
            (30, -10, "right-down"),
            (-30, 10, "left-up"),
            (200, 100, "out-of-range (should clamp)"),
            (0, 0, "back to center"),
        ]:
            print(f"[test] -> MOVE:{yaw},{pitch}  ({note})")
            resp = backend.send_command(f"MOVE:{yaw},{pitch}")
            print(f"[test] <- {resp}")
            time.sleep(1.0)

        wav = build_sine_wav(
            freq_hz=args.freq,
            duration_s=args.duration,
        )
        print(
            f"[test] -> WAV:{len(wav)} ({args.freq:.0f}Hz {args.duration:.2f}s 16bit/mono/16kHz)"
        )
        t0 = time.time()
        resp = backend.send_wav(wav)
        elapsed = time.time() - t0
        print(f"[test] <- {resp}  (elapsed {elapsed:.2f}s)")

        print("[test] -> STATUS")
        resp = backend.send_command("STATUS")
        print(f"[test] <- {resp}")
    finally:
        backend.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
