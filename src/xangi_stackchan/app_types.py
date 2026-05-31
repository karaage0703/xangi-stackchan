from dataclasses import dataclass

from .stackchan import StackchanConfig


@dataclass
class BridgeConfig:
    xangi_url: str
    thread_id: str | None
    stackchan: StackchanConfig
    volume: int
    tts: str
    piper_bin: str
    piper_model: str
    piper_speaker: int
    voicevox_url: str
    voicevox_speaker: int
    serial_chunk: int
    serial_delay: float
    stackchan_retry_seconds: float
    face_idle: str
    face_thinking: str
    face_talking: str
    face_error: str
    face_mode: str
    sprite_sheet: str
    sprite_jpeg_quality: int
    stream_timeout: int
    retry_seconds: float
    max_retry_seconds: float
    # AtomS3R 等の顔の向き。時計回りクオータターン数 (0=自然/1=CW90/2=180/3=CW270)。
    # None ならファーム既定 (atoms3r-main は起動時 CW90) のまま、何も送らない。
    face_rotation: int | None = None
    move_enabled: bool = True
    move_idle_yaw: float = 0.0
    move_idle_pitch: float = 5.0
    move_thinking_yaw: float = -8.0
    move_thinking_pitch: float = 5.0
    move_error_yaw: float = 0.0
    move_error_pitch: float = -10.0
    move_talking_sway_yaw: float = 4.0
    move_talking_sway_pitch: float = 2.0
    move_talking_sway_interval: float = 1.5
    # head_touch press → 録音 → STT → xangi `/api/chat` 投入の音声対話モード。
    # シリアル backend + M5Stack 公式 StackChan K151 のアタマセンサ前提 (CoreS3
    # 内蔵 PDM マイク経由のため、WiFi backend では使えない)。デフォルト無効。
    voice_conversation: bool = False
    # xangi 投入時に使う appSessionId。空なら xangi 側で「最新の web session」を
    # 使う (POST /api/chat の挙動)。複数 xangi インスタンスに分ける時のみ指定。
    voice_app_session_id: str = ""
    # VAD 閾値: dBFS 未満が voice_silence_seconds 秒続いたら自動 stop。
    # 静かな室内 -40 / 多少騒がしいリビング -30 / マスク越し 等で調整。
    voice_silence_dbfs: float = -40.0
    voice_silence_seconds: float = 1.5
    voice_max_seconds: float = 15.0
    # なでてから最初の発話までの猶予 (秒)。この間の無音では録音を止めない (考える
    # 時間)。最初の有音で通常 voice_silence_seconds 判定に切替。猶予内に一度も発話が
    # 無ければ誤タップとして stop。
    voice_initial_grace_seconds: float = 5.0
