import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCAL_PIPER_BIN = "tools/piper"
DEFAULT_LOCAL_MODEL = "models/tsukuyomi-chan-6lang-fp16.onnx"
LOCAL_MODELS = sorted(
    p.relative_to(ROOT) for p in (ROOT / "models").glob("*.onnx") if ".cpu.opt" not in p.name
)

DEFAULT_TTS = os.environ.get("STACKCHAN_TTS", "piper")
DEFAULT_VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
DEFAULT_VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "1"))
DEFAULT_SAMPLE_RATE = int(os.environ.get("STACKCHAN_SAMPLE_RATE", "16000"))
DEFAULT_PIPER_BIN = os.environ.get(
    "PIPER_BIN",
    DEFAULT_LOCAL_PIPER_BIN,
)
DEFAULT_PIPER_MODEL = os.environ.get(
    "PIPER_MODEL",
    str(LOCAL_MODELS[0]) if LOCAL_MODELS else DEFAULT_LOCAL_MODEL,
)
DEFAULT_PIPER_LANGUAGE = os.environ.get("PIPER_LANGUAGE", "ja-en-zh-es-fr-pt")
DEFAULT_PIPER_LENGTH_SCALE = os.environ.get("PIPER_LENGTH_SCALE", "1.5")
DEFAULT_PIPER_NOISE_SCALE = os.environ.get("PIPER_NOISE_SCALE", "0.667")


def downsample_wav(wav_bytes: bytes, factor: int = 2) -> bytes:
    """16bit PCM WAV のサンプルレートを 1/factor に落として bytes を返す。

    piper(PiperPlus) は 22050Hz 固定出力で、これを USB シリアルに丸ごと流すと長文応答
    で送信に時間がかかり会話レイテンシのボトルネックになる。発話用途では 11025Hz 程度
    でも十分聞き取れるので、送信前にダウンサンプルして転送量を減らす。audioop.ratecv は
    内蔵ローパス付きでエイリアスを抑える。factor<=1 や解析失敗時は原 bytes をそのまま
    返す (安全側)。
    """
    if factor <= 1 or not wav_bytes:
        return wav_bytes
    try:
        import audioop
        import io
        import wave

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            n_ch = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
        new_rate = max(8000, rate // factor)
        converted, _ = audioop.ratecv(frames, width, n_ch, rate, new_rate, None)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(n_ch)
            w.setsampwidth(width)
            w.setframerate(new_rate)
            w.writeframes(converted)
        return out.getvalue()
    except Exception:
        return wav_bytes


def split_text(text: str, max_len: int = 80) -> list[str]:
    parts = re.findall(r"[^。！？!?\.]+[。！？!?\.]?", text.strip())
    chunks: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part[:max_len])
            part = part[max_len:]
        if not part:
            continue
        if current and len(current) + len(part) > max_len:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks or [text.strip()]


def resolve_repo_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return ROOT / candidate


def piper_cli_bin(piper_bin: str) -> str:
    path = Path(piper_bin)
    if path.is_absolute() or "/" in piper_bin:
        path = resolve_repo_path(piper_bin)
    if path.name == "piper" and path.exists():
        cli = path.parent.parent / "_piper" / "PiperPlus.Cli"
        if cli.exists():
            return str(cli)
        return str(path)
    return str(path) if path.exists() else piper_bin


def piper_config_args(model: str) -> list[str]:
    model_path = resolve_repo_path(model)
    candidates = [
        model_path.with_suffix(".onnx.json"),
        model_path.with_name("config.json"),
        ROOT / "models" / "config.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return ["--config", str(candidate)]
    return []


def wait_for_complete_file(path: Path, deadline: float, min_size: int = 44) -> bytes:
    last_size = -1
    stable_count = 0
    while time.time() < deadline:
        if not path.exists():
            time.sleep(0.01)
            continue
        size = path.stat().st_size
        if size >= min_size and size == last_size:
            stable_count += 1
            if stable_count >= 2:
                return path.read_bytes()
        else:
            stable_count = 0
        last_size = size
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for complete file: {path.name}")


class PiperProcess:
    """Persistent piper-plus JSONL process for low-latency repeated synthesis."""

    def __init__(
        self,
        piper_bin: str = DEFAULT_PIPER_BIN,
        model: str = DEFAULT_PIPER_MODEL,
        speaker: int = 0,
    ):
        if not model:
            raise RuntimeError("--piper-model is required when using --tts piper")
        model_path = resolve_repo_path(model)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock = threading.Lock()
        self.counter = 0
        cmd = [
            piper_cli_bin(piper_bin),
            "--model",
            str(model_path),
            "--json-input",
            "--output-dir",
            self.tmpdir.name,
            "--language",
            DEFAULT_PIPER_LANGUAGE,
            "--length-scale",
            DEFAULT_PIPER_LENGTH_SCALE,
            "--noise-scale",
            DEFAULT_PIPER_NOISE_SCALE,
            "--quiet",
        ] + piper_config_args(str(model_path))
        if speaker:
            cmd += ["--speaker", str(speaker)]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def synthesize_many(self, texts: list[str], timeout: float | None = None) -> list[bytes]:
        texts = [text for text in texts if text.strip()]
        if not texts:
            return []
        timeout = timeout or max(30, 10 * len(texts))
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError(f"piper process exited with code {self.process.returncode}")
            filenames: list[str] = []
            for text in texts:
                self.counter += 1
                filename = f"live_{self.counter:06}.wav"
                filenames.append(filename)
                line = json.dumps({"text": text, "output_file": filename}, ensure_ascii=False)
                self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

            deadline = time.time() + timeout
            wavs: list[bytes] = []
            for filename in filenames:
                path = Path(self.tmpdir.name) / filename
                while not path.exists() and time.time() <= deadline:
                    if self.process.poll() is not None:
                        raise RuntimeError(f"piper process exited with code {self.process.returncode}")
                    time.sleep(0.01)
                if self.process.poll() is not None:
                    raise RuntimeError(f"piper process exited with code {self.process.returncode}")
                wavs.append(wait_for_complete_file(path, deadline))
                try:
                    path.unlink()
                except OSError:
                    pass
            return wavs

    def close(self):
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.tmpdir.cleanup()


def voicevox_synthesize(
    text: str,
    voicevox_url: str = DEFAULT_VOICEVOX_URL,
    speaker: int = DEFAULT_VOICEVOX_SPEAKER,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    query = requests.post(
        f"{voicevox_url}/audio_query",
        params={"text": text, "speaker": speaker},
        timeout=10,
    )
    query.raise_for_status()
    payload = query.json()
    payload["outputSamplingRate"] = sample_rate
    payload["outputStereo"] = False
    response = requests.post(
        f"{voicevox_url}/synthesis",
        params={"speaker": speaker},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.content
