// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/cores3/speaker-demo:
//   K151 / CoreS3 内蔵スピーカーから WAV を再生する最小ファーム。
//   サーボには触らない (PY32 VM_EN も叩かない)。
//
// 目的:
//   xangi SSE bridge で から `WAV:<size>\n` + バイナリで受信した
//   音声をそのまま `M5.Speaker.playWav(buf, size)` に流す経路を、外部供給
//   なしで先に独立検証する。**実機焼き → 音が鳴ること**だけを確認する。
//
//   そのため:
//     - 起動時に `int16_t` PCM サイン波 (440Hz / 500ms / 16kHz mono) を
//       global バッファに生成し、44 byte の RIFF/WAVE ヘッダを前置きして
//       PROGMEM 不要の 1 byte 配列にまとめる。
//     - トリガごとに `M5.Speaker.playWav(buf, size)` を呼んで再生する。
//   WAV 受信実装ではこの buf をシリアル受信バッファに置き換えれば良い。
//
// 動作:
//   起動時: Speaker init、LCD に「TAP / serial 'p': play」
//   トリガ (2 系統): 画面タップ / Serial 'p' (or 'P')
//     → playWav 開始、LCD に "playing..." 表示、完了で "ready" に戻す
//   音量調整: Serial '-' で -16、Serial '+' で +16 (0..255)
//             デフォルト 128。LCD 上部に常時表示
//   緊急停止: Serial 's' で `M5.Speaker.stop()` (再生中の中断)
//
//   CoreS3 にはハードウェアの BtnA/BtnB/BtnC が無い (BtnPWR のみ)。
//   M5Unified は画面下端のタッチエリアを 3 分割で BtnA/B/C に仮想割り当て
//   する仕様 (`setTouchButtonHeight`) を持つが、SpeakerDemo は最小ファーム
//   として UI を作り込まず、画面全体タップ + シリアルだけに絞る。
//
// 注意:
//   - CoreS3 内蔵スピーカーは I2S 経由 (NS4150 アンプ)。`M5.begin()` で
//     M5Unified が自動初期化する。明示的な begin は不要だが、setVolume と
//     再生前に Speaker.begin() が走っているか確認のため `M5.Speaker.begin()`
//     を呼んでおく (二重 begin は安全)。
//   - `playWav` は WAV ヘッダを内部で parse するが、対応する PCM 形式は
//     M5Unified の実装に依存 (16bit mono は確実、24bit / 32-bit float は
//     端末や M5Unified バージョンで差がある)。本ファームは 16bit / 16kHz /
//     mono の最も保守的な組合せのみ生成する。
//   - **旧 stackchan-atama 試作で piper の 16bit mono 16kHz PCM を
//     CoreS3 内蔵スピーカーで playWav できることは実機確認済み**。本ファーム
//     は 本体ファームの前段としてその経路を K151 firmware 内で再現する。
//   - 周波数 16kHz は piper / VOICEVOX 出力と揃えてある (本体ファームで変更不要)。

#include <M5Unified.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

constexpr uint32_t SAMPLE_RATE_HZ = 16000;     // piper / VOICEVOX と同じ
constexpr uint32_t TONE_FREQ_HZ   = 440;       // A4
constexpr uint32_t TONE_DUR_MS    = 500;
constexpr float    TONE_AMP       = 0.45f;     // 0..1 ピーク振幅 (clip 防止)

constexpr size_t   SAMPLE_COUNT   = (SAMPLE_RATE_HZ * TONE_DUR_MS) / 1000;  // 8000
constexpr size_t   PCM_BYTES      = SAMPLE_COUNT * sizeof(int16_t);          // 16000
constexpr size_t   WAV_HEADER_LEN = 44;
constexpr size_t   WAV_BUF_LEN    = WAV_HEADER_LEN + PCM_BYTES;

// .bss 配置 (~16KB)。CoreS3 RAM 8MB なので余裕あり。
static uint8_t g_wavBuf[WAV_BUF_LEN];

static uint8_t g_volume = 128;  // 0..255

enum class State { Booting, Ready, Playing, Error };
static State g_state = State::Booting;

// little-endian 書き込みヘルパ
static void wrLE16(uint8_t* p, uint16_t v) {
    p[0] = static_cast<uint8_t>(v & 0xFF);
    p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
}
static void wrLE32(uint8_t* p, uint32_t v) {
    p[0] = static_cast<uint8_t>(v & 0xFF);
    p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
    p[2] = static_cast<uint8_t>((v >> 16) & 0xFF);
    p[3] = static_cast<uint8_t>((v >> 24) & 0xFF);
}

// 16bit mono / 指定 sample_rate の RIFF WAVE ヘッダを書き込む。
// 戻り値: 書き込み終了位置 (= WAV_HEADER_LEN)
static size_t writeWavHeader(uint8_t* dst, uint32_t sample_rate, uint32_t pcm_bytes) {
    const uint16_t num_channels    = 1;
    const uint16_t bits_per_sample = 16;
    const uint16_t block_align     = num_channels * (bits_per_sample / 8);
    const uint32_t byte_rate       = sample_rate * block_align;

    memcpy(dst + 0, "RIFF", 4);
    wrLE32(dst + 4, 36 + pcm_bytes);   // chunk size = file size - 8
    memcpy(dst + 8, "WAVE", 4);

    memcpy(dst + 12, "fmt ", 4);
    wrLE32(dst + 16, 16);              // fmt chunk size (PCM)
    wrLE16(dst + 20, 1);               // audio format = 1 (PCM)
    wrLE16(dst + 22, num_channels);
    wrLE32(dst + 24, sample_rate);
    wrLE32(dst + 28, byte_rate);
    wrLE16(dst + 32, block_align);
    wrLE16(dst + 34, bits_per_sample);

    memcpy(dst + 36, "data", 4);
    wrLE32(dst + 40, pcm_bytes);

    return WAV_HEADER_LEN;
}

// 起動時に PCM サイン波を生成して g_wavBuf に詰める。
static void buildToneWav() {
    size_t pos = writeWavHeader(g_wavBuf, SAMPLE_RATE_HZ, PCM_BYTES);

    const float two_pi    = 6.283185307179586f;
    const float radPerSmp = two_pi * static_cast<float>(TONE_FREQ_HZ)
                          / static_cast<float>(SAMPLE_RATE_HZ);
    const int16_t peak    = static_cast<int16_t>(32767.0f * TONE_AMP);

    for (size_t i = 0; i < SAMPLE_COUNT; i++) {
        // 端のクリック音を抑えるため、頭尾 5ms (80 sample) を線形フェード
        constexpr size_t fade = (16000 * 5) / 1000;
        float gain = 1.0f;
        if (i < fade)                          gain = static_cast<float>(i) / fade;
        else if (i >= SAMPLE_COUNT - fade)     gain = static_cast<float>(SAMPLE_COUNT - 1 - i) / fade;

        int16_t s = static_cast<int16_t>(peak * gain * sinf(radPerSmp * i));
        wrLE16(g_wavBuf + pos, static_cast<uint16_t>(s));
        pos += 2;
    }
}

// === 表示 =====================================================================
void drawHeader() {
    M5.Display.fillRect(0, 0, 320, 24, TFT_NAVY);
    M5.Display.setTextColor(TFT_WHITE, TFT_NAVY);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(8, 4);
    M5.Display.print("SpeakerDemo");
}

void drawFooter() {
    M5.Display.fillRect(0, 200, 320, 40, TFT_DARKGREY);
    M5.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 208);
    M5.Display.print("TAP screen / serial 'p': play 440Hz 0.5s");
    M5.Display.setCursor(8, 222);
    M5.Display.print("serial '+' vol+  '-' vol-  's' stop");
}

void drawVolume() {
    M5.Display.fillRect(0, 28, 320, 40, TFT_BLACK);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(8, 36);
    M5.Display.printf("volume: %3u / 255", g_volume);
}

void drawState() {
    M5.Display.fillRect(0, 76, 320, 100, TFT_BLACK);
    uint16_t color = TFT_WHITE;
    const char* msg = "";
    switch (g_state) {
        case State::Booting: color = TFT_DARKGREY; msg = "booting...";  break;
        case State::Ready:   color = TFT_GREEN;    msg = "ready";        break;
        case State::Playing: color = TFT_YELLOW;   msg = "playing...";   break;
        case State::Error:   color = TFT_RED;      msg = "speaker fail"; break;
    }
    M5.Display.setTextColor(color, TFT_BLACK);
    M5.Display.setTextSize(3);
    M5.Display.setCursor(8, 100);
    M5.Display.print(msg);
}

void setState(State s) {
    g_state = s;
    drawState();
    Serial.printf("[speaker] state=%d\n", static_cast<int>(s));
}

// === 入力 =====================================================================
struct Cmd { bool play; bool stop; int8_t volDelta; };

Cmd readSerialCmd() {
    Cmd cmd = { false, false, 0 };
    while (Serial.available()) {
        int c = Serial.read();
        if (c == 'p' || c == 'P') cmd.play = true;
        if (c == 's' || c == 'S') cmd.stop = true;
        if (c == '+')             cmd.volDelta += 16;
        if (c == '-')             cmd.volDelta -= 16;
    }
    return cmd;
}

void changeVolume(int delta) {
    int v = static_cast<int>(g_volume) + delta;
    if (v < 0)   v = 0;
    if (v > 255) v = 255;
    g_volume = static_cast<uint8_t>(v);
    M5.Speaker.setVolume(g_volume);
    drawVolume();
    Serial.printf("[speaker] volume=%u\n", g_volume);
}

void playTone() {
    if (g_state == State::Error) {
        Serial.println("[speaker] error state, ignore play");
        return;
    }
    setState(State::Playing);
    // playWav は非同期 (内部 task で再生)。stop_current_sound=true で前の再生を切る。
    bool ok = M5.Speaker.playWav(g_wavBuf, WAV_BUF_LEN, 1, 0, true);
    if (!ok) {
        Serial.println("[speaker] playWav failed");
        setState(State::Error);
        return;
    }
    Serial.printf("[speaker] playWav started, bytes=%u\n",
                  static_cast<unsigned>(WAV_BUF_LEN));
}

// === setup / loop =============================================================
void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);
    M5.Display.fillScreen(TFT_BLACK);

    Serial.begin(115200);
    delay(100);
    Serial.println();
    Serial.println("[speaker] xangi-stackchan-dev / k151 SpeakerDemo");

    drawHeader();
    drawFooter();
    drawVolume();
    setState(State::Booting);

    // Speaker は M5.begin() で auto init されるが、二重 begin は M5Unified では
    // 安全 (既存セッションを破壊しない)。明示的に呼ぶことで「サーボ電源系統に
    // 触らずスピーカーだけ立ち上げる」意図がコード上で明確になる。
    if (!M5.Speaker.begin()) {
        Serial.println("[speaker] M5.Speaker.begin() failed");
        setState(State::Error);
        return;
    }
    M5.Speaker.setVolume(g_volume);

    buildToneWav();
    Serial.printf("[speaker] tone wav built: %u byte (%.1fs @%uHz mono16)\n",
                  static_cast<unsigned>(WAV_BUF_LEN),
                  TONE_DUR_MS / 1000.0f,
                  static_cast<unsigned>(SAMPLE_RATE_HZ));

    setState(State::Ready);
}

void loop() {
    M5.update();
    const uint32_t now = millis();
    static uint32_t lastTriggerMs = 0;

    auto touch = M5.Touch.getDetail();
    Cmd cmd    = readSerialCmd();

    // CoreS3 にはハード BtnA/B/C は無い (BtnPWR のみ)。M5Unified が仮想で
    // 提供する「画面下端タッチ 3 分割ボタン」は setTouchButtonHeight() で
    // 有効化しないと反応しない仕様。SpeakerDemo では UI を最小に保つため
    // 仮想ボタンを使わず、画面全体タッチ + シリアル `+`/`-` のみで操作する。
    bool playTrigger =
        touch.wasPressed()
        || cmd.play;

    if (playTrigger && (now - lastTriggerMs > 200)) {
        lastTriggerMs = now;
        playTone();
    }

    if (cmd.stop) {
        M5.Speaker.stop();
        Serial.println("[speaker] stop");
        if (g_state == State::Playing) setState(State::Ready);
    }

    if (cmd.volDelta < 0) {
        changeVolume(cmd.volDelta);
    }
    if (cmd.volDelta > 0) {
        changeVolume(cmd.volDelta);
    }

    // 再生完了の自動検知 (M5.Speaker.isPlaying()): Playing → Ready 戻し
    if (g_state == State::Playing && !M5.Speaker.isPlaying()) {
        setState(State::Ready);
    }

    delay(20);
}
