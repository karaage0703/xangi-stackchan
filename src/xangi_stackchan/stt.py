"""faster-whisper / openai-whisper による Speech-to-Text。

CoreS3 内蔵マイクから取得した 16kHz/16bit/mono PCM (WAV ラップ済) を text に
変換する。voice_conversation モジュールが head_touch press → 録音 →
無音検出 → STT → xangi `/api/chat` 投入の経路で利用する。

バックエンド自動選択:
- 一部の GPU 環境では faster-whisper の ctranslate2 が CUDA デバイスを認識できない
  (`get_cuda_device_count()==0`) 一方で PyTorch は CUDA を使える
  (`torch.cuda.is_available()==True`) ことがある。そこで device=auto では
    1. ctranslate2 が CUDA 対応 → faster-whisper / cuda
    2. ダメで torch が CUDA 可 → openai-whisper / cuda
    3. どちらも不可 → faster-whisper / cpu
  と選ぶ。これで GPU が使える環境では STT が CPU 比で大幅に高速化する。

設計:
- モデルは singleton ロード。最初の transcribe で load_model が走る。
- 環境変数で動作チューニング可能:
    STACKCHAN_WHISPER_MODEL=small      # tiny / base / small / medium / large-v3
    STACKCHAN_WHISPER_DEVICE=auto      # auto / cpu / cuda
    STACKCHAN_WHISPER_COMPUTE=int8     # faster-whisper の compute_type (cpu 用)
    STACKCHAN_WHISPER_LANGUAGE=ja
    STACKCHAN_WHISPER_BEAM=3           # beam_size、大きいと精度↑速度↓
- 失敗時は例外を上げず {"status":"error", "error":"..."} を返す (呼び出し側の
  ループを止めない)。
"""

from __future__ import annotations

import io
import os
import threading
import time
import wave

DEFAULT_MODEL    = os.environ.get("STACKCHAN_WHISPER_MODEL", "small")
# auto = ctranslate2 が CUDA 非対応でも torch-cuda にフォールバックさせるため既定を auto に。
DEFAULT_DEVICE   = os.environ.get("STACKCHAN_WHISPER_DEVICE", "auto")
DEFAULT_COMPUTE  = os.environ.get("STACKCHAN_WHISPER_COMPUTE", "int8")
DEFAULT_LANGUAGE = os.environ.get("STACKCHAN_WHISPER_LANGUAGE", "ja")
DEFAULT_BEAM     = int(os.environ.get("STACKCHAN_WHISPER_BEAM", "3"))
# Silero VAD で発話区間のみ STT。ノイズ・無音の hallucination
# (「ご視聴ありがとうございました」「エンディング」等) を抑える + 30倍高速。
# faster-whisper 経路のみ有効 (openai-whisper は内蔵 VAD 非対応)。
DEFAULT_VAD      = os.environ.get("STACKCHAN_WHISPER_VAD", "1") not in ("0", "false", "")

# backend: "faster-whisper" | "openai-whisper"
_model = None
_backend: str | None = None
_resolved_device: str | None = None
_model_lock = threading.Lock()
_load_meta: dict = {}


def _detect_backend(requested_device: str) -> tuple[str, str]:
    """(device, backend) を決定する。

    requested_device: "auto" / "cpu" / "cuda"
    backend: "faster-whisper" | "openai-whisper"
    """
    if requested_device == "cpu":
        return "cpu", "faster-whisper"

    # 1. ctranslate2 が CUDA 対応か (faster-whisper を GPU で使えるか)
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0 and ctranslate2.get_supported_compute_types("cuda"):
            return "cuda", "faster-whisper"
    except Exception:
        pass

    # 2. ctranslate2 が CUDA 非対応 → PyTorch (openai-whisper) にフォールバック。
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "openai-whisper"
    except Exception:
        pass

    # 3. CPU フォールバック (faster-whisper)
    return "cpu", "faster-whisper"


def load_model(
    model_size: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE,
):
    """Whisper モデルを singleton でロード。バックエンドは _detect_backend で自動選択。

    一度ロードした後は引数を変更しても返るのは最初のインスタンス (再ロードしたい
    場合は `reset_model()` を先に呼ぶ)。
    """
    global _model, _backend, _resolved_device
    with _model_lock:
        if _model is not None:
            return _model
        t0 = time.time()
        resolved_device, backend = _detect_backend(device)

        if backend == "openai-whisper":
            import whisper  # 遅延 import (依存重い)

            _model = whisper.load_model(model_size, device=resolved_device)
        else:
            from faster_whisper import WhisperModel  # 遅延 import (依存重い)

            # cpu は int8、cuda は float16 が定石。
            ct = compute_type if resolved_device == "cpu" else "float16"
            _model = WhisperModel(model_size, device=resolved_device, compute_type=ct)

        _backend = backend
        _resolved_device = resolved_device
        _load_meta.update(
            {
                "model_size": model_size,
                "device": resolved_device,
                "backend": backend,
                "load_seconds": time.time() - t0,
            }
        )
    return _model


def reset_model() -> None:
    """テストや動的差し替え用に singleton を破棄。"""
    global _model, _backend, _resolved_device
    with _model_lock:
        _model = None
        _backend = None
        _resolved_device = None
        _load_meta.clear()


def get_load_meta() -> dict:
    """直近のロード情報を返す (デバッグ用)。"""
    return dict(_load_meta)


def _wav_bytes_to_float32(wav_bytes: bytes):
    """16bit PCM WAV bytes を openai-whisper 用の float32 numpy 配列に変換。

    openai-whisper は 16kHz mono float32 ([-1,1]) を受け取る。録音は既に
    16kHz/16bit/mono なので resample 不要。ffmpeg を介さず自前デコードする。
    """
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def transcribe(
    wav_bytes: bytes,
    language: str = DEFAULT_LANGUAGE,
    beam_size: int = DEFAULT_BEAM,
    vad_filter: bool = DEFAULT_VAD,
) -> dict:
    """WAV bytes (16kHz/16bit/mono 推奨) を text に変換。

    返却:
        {
          "status": "ok" | "error",
          "text": str,
          "language": str,
          "language_probability": float,
          "segments": [{"start": float, "end": float, "text": str}, ...],
          "elapsed_seconds": float,
          "backend": str,
          # error 時は "error": str
        }
    """
    if not wav_bytes:
        return {"status": "error", "error": "empty wav", "text": ""}

    try:
        model = load_model()
    except Exception as exc:
        return {"status": "error", "error": f"model load failed: {exc}", "text": ""}

    try:
        t0 = time.time()
        if _backend == "openai-whisper":
            return _transcribe_openai(model, wav_bytes, language, beam_size, t0)
        return _transcribe_faster(model, wav_bytes, language, beam_size, vad_filter, t0)
    except Exception as exc:
        return {"status": "error", "error": str(exc), "text": ""}


def _transcribe_faster(model, wav_bytes, language, beam_size, vad_filter, t0):
    # VAD パラメータを緩めて短い発話の取りこぼしを抑える:
    # threshold 0.2 (敏感)、min_speech_duration_ms 50 (短い発話を拾う)、
    # min_silence_duration_ms 2000 → 500。環境変数で上書き可能。
    vad_params = None
    if vad_filter:
        vad_params = {
            "threshold": float(
                os.environ.get("STACKCHAN_WHISPER_VAD_THRESHOLD", "0.2")
            ),
            "min_speech_duration_ms": int(
                os.environ.get("STACKCHAN_WHISPER_VAD_MIN_SPEECH_MS", "50")
            ),
            "min_silence_duration_ms": 500,
        }
    segments_iter, info = model.transcribe(
        io.BytesIO(wav_bytes),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=vad_params,
    )
    segments_list = []
    text_parts = []
    for s in segments_iter:
        segments_list.append({"start": s.start, "end": s.end, "text": s.text})
        text_parts.append(s.text)
    text = "".join(text_parts).strip()
    return {
        "status": "ok",
        "text": text,
        "language": info.language,
        "language_probability": float(info.language_probability),
        "segments": segments_list,
        "elapsed_seconds": time.time() - t0,
        "backend": "faster-whisper",
    }


def _transcribe_openai(model, wav_bytes, language, beam_size, t0):
    audio = _wav_bytes_to_float32(wav_bytes)
    # openai-whisper は内蔵 silero VAD を持たないが、no_speech_threshold /
    # condition_on_previous_text=False で無音 hallucination をある程度抑える。
    result = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size if beam_size and beam_size > 1 else None,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    segments_list = [
        {"start": s.get("start"), "end": s.get("end"), "text": s.get("text", "")}
        for s in result.get("segments", [])
    ]
    text = (result.get("text") or "").strip()
    return {
        "status": "ok",
        "text": text,
        "language": result.get("language", language),
        "language_probability": 1.0,
        "segments": segments_list,
        "elapsed_seconds": time.time() - t0,
        "backend": "openai-whisper",
    }
