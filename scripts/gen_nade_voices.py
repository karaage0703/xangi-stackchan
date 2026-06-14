#!/usr/bin/env python3
"""なでなで反応モード用の埋め込み音声 (firmware/examples/cores3/main/nade_voices.h) を生成する。

piper (tsukuyomi-chan 6lang) で短いセリフを合成 → 11025Hz mono 16-bit WAV に downsample →
C の const uint8_t 配列ヘッダに変換する。セリフを変えたい時はここの phrases を編集して
`uv run python scripts/gen_nade_voices.py` で再生成 → ファーム再ビルド/再書き込み。
"""
import io
import wave
from pathlib import Path

from xangi_stackchan.tts import (
    DEFAULT_PIPER_BIN,
    DEFAULT_PIPER_MODEL,
    PiperProcess,
    downsample_wav,
)

PHRASES = [
    ("nade1", "なでなで、ありがとう！"),
    ("nade2", "えへへ、くすぐったいよ"),
    ("nade3", "もっとなでて！"),
    ("nade4", "わーい、うれしいな"),
    ("nade5", "きもちいい〜"),
]
HEADER = Path("firmware/examples/cores3/main/nade_voices.h")


def emit_array(name, data):
    lines = [f"static const uint8_t {name}[] = {{"]
    row = []
    for b in data:
        row.append(f"0x{b:02x},")
        if len(row) == 16:
            lines.append("  " + "".join(row))
            row = []
    if row:
        lines.append("  " + "".join(row))
    lines.append("};")
    return "\n".join(lines)


def main():
    piper = PiperProcess(DEFAULT_PIPER_BIN, DEFAULT_PIPER_MODEL, 0)
    wavs = piper.synthesize_many([t for _, t in PHRASES])
    piper.close()

    out = [
        "// 自動生成: scripts/gen_nade_voices.py。なでなで反応モードの埋め込み音声。",
        "// piper (tsukuyomi-chan 6lang) で合成 → 11025Hz mono 16-bit WAV に downsample。",
        "// 差し替えたい時は scripts/gen_nade_voices.py の PHRASES を編集して再生成する。",
        "#pragma once",
        "#include <stdint.h>",
        "#include <stddef.h>",
        "",
    ]
    names = []
    for (name, text), wav in zip(PHRASES, wavs):
        wav = downsample_wav(wav, 2)
        with wave.open(io.BytesIO(wav), "rb") as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2
        out.append(f"// {text!r} ({len(wav)} bytes)")
        out.append(emit_array(f"NADE_VOICE_{name.upper()}", wav))
        out.append("")
        names.append(name.upper())

    out += [
        "struct NadeVoiceClip {",
        "    const uint8_t* data;",
        "    size_t         len;",
        "};",
        "",
        "static const NadeVoiceClip NADE_VOICES[] = {",
    ]
    for n in names:
        out.append(f"    {{ NADE_VOICE_{n}, sizeof(NADE_VOICE_{n}) }},")
    out += [
        "};",
        "static const int NADE_VOICE_COUNT = sizeof(NADE_VOICES) / sizeof(NADE_VOICES[0]);",
        "",
    ]
    HEADER.write_text("\n".join(out))
    print(f"wrote {HEADER}")


if __name__ == "__main__":
    main()
