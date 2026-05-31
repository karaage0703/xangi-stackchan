// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/atoms3r/main:
//   M5Stack AtomS3R + Atomic Voice Base / Atomic Echo Base を xangi のシリアル
//   経由音声出力デバイスとして動かす受信ファーム。XangiBridge (CoreS3 / K151)
//   と同じシリアルプロトコルを採用、サーボ・カメラを持たないので MOVE / CAPTURE
//   は unavailable 応答に降格する graceful degradation 版。
//
// プロトコル (docs/xangi_bridge_protocol.md):
//   STATUS           → JSON ack {"state","volume","version","servo":false,
//                                "torque":false,"camera":false,"queued","playing"}
//   VOLUME:<0-255>   → setVolume (master + channel) → JSON ack
//   WAV:<size>       → "READY\n" 返してバイナリ受信、再生キューに push → 即 ack
//   FACE:<expr>      → setExpression → JSON ack
//   ROTATE:<0-3>     → 顔の向きを時計回りクオータターン (0=自然/1=CW90/2=180/3=CW270)
//                      で切替 → JSON ack。既定は起動時 1 (時計回り90度)
//   MOVE:<yaw,pitch> → {"status":"error","error":"servo not available"}
//   CAPTURE          → {"status":"error","error":"camera not available"}
//
// 参考: karaage0703/stackchan-atama (MIT) の AtomS3R 初期化パターン。M5Unified
// が `cfg.external_speaker.atomic_echo = true` で ES8311 audio codec を自動初期化
// してくれるので、Voice Base / Echo Base 同一系統 (ES8311 + I2S G5/G6/G7/G8) は
// このフラグで両対応。AtomS3R LCD は 128x128 で小さいので Avatar は scale 0.5 +
// オフセット調整。
//
// 設計メモ:
//   - SERIAL_BAUD = 115200 (AtomS3R USB-CDC で安定する保守値、atama リポ準拠)
//     CoreS3 系 XangiBridge は 921600 なので Python 側 `--baud` で切替が必要
//   - PSRAM (opi 8MB) を ps_malloc で WAV バッファに使う
//   - WAV キュー (4 slot ring buffer) + 別 RTOS task `wavPlayTask` で受信即 ack
//     パターンは XangiBridge と同一 (host 側の send_wav 機構をそのまま使える)

#include <Avatar.h>
#include <M5Unified.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

using m5avatar::Avatar;
using m5avatar::Expression;

constexpr uint32_t SERIAL_BAUD       = 115200;
constexpr size_t   MAX_LINE_LEN      = 64;
constexpr size_t   MAX_WAV_BYTES     = 4 * 1024 * 1024;  // 4MB 上限 (PSRAM 8MB)
constexpr uint32_t WAV_CHUNK_TIMEOUT_MS  = 2000;
constexpr uint32_t PLAY_POLL_INTERVAL_MS = 50;
constexpr uint32_t MOUTH_UPDATE_MS   = 80;
constexpr int      WAV_QUEUE_SIZE    = 4;
constexpr uint8_t  ATOMIC_ECHO_VOLUME = 192;  // ES8311 で過変調を避ける保守値

// 顔の向き。M5.begin が決める自然な向き (g_base_rotation) を基準に、時計回りの
// クオータターン数 (g_rotation_step, 0-3) を足して M5.Display.setRotation に渡す。
//   step 0 = 自然な向き / 1 = 時計回り90度 / 2 = 180度 / 3 = 時計回り270度 (反時計90度)
// AtomS3R LCD は 128x128 の正方形なので 90 度単位の回転で歪みは出ない。
constexpr uint8_t  DEFAULT_ROTATION_STEP = 1;  // 既定は時計回り90度

static uint8_t g_volume = ATOMIC_ECHO_VOLUME;
static uint8_t g_base_rotation  = 0;
static uint8_t g_rotation_step  = DEFAULT_ROTATION_STEP;

enum class State { Booting, Ready, Receiving, Playing, Error };
static State g_state = State::Booting;

static const char* stateStr(State s) {
    switch (s) {
        case State::Booting:   return "booting";
        case State::Ready:     return "ready";
        case State::Receiving: return "receiving";
        case State::Playing:   return "playing";
        case State::Error:     return "error";
    }
    return "unknown";
}

// === WAV キュー (XangiBridge の WAV キュー実装と同一構造) ============================
struct WavSlot {
    uint8_t* data;
    size_t   len;
};
static WavSlot           g_wav_queue[WAV_QUEUE_SIZE] = {};
static volatile int      g_wav_queue_head = 0;
static volatile int      g_wav_queue_tail = 0;
static volatile bool     g_wav_playing    = false;

static int wavQueueCount() {
    int c = g_wav_queue_tail - g_wav_queue_head;
    if (c < 0) c += WAV_QUEUE_SIZE;
    return c;
}
static bool wavQueueFull()  { return wavQueueCount() >= (WAV_QUEUE_SIZE - 1); }
static bool wavQueueEmpty() { return g_wav_queue_head == g_wav_queue_tail; }

static bool wavQueuePush(uint8_t* data, size_t len) {
    if (wavQueueFull()) return false;
    g_wav_queue[g_wav_queue_tail].data = data;
    g_wav_queue[g_wav_queue_tail].len  = len;
    g_wav_queue_tail = (g_wav_queue_tail + 1) % WAV_QUEUE_SIZE;
    return true;
}

static Avatar avatar;

static void avatarSay(const char* msg) {
    avatar.setSpeechText(msg);
}

// Avatar 描画タスクを止めずに M5.Display.setRotation を叩くと、回転途中のフレームと
// 描画が競合して表示が乱れる。suspend → setRotation → 画面クリア → resume の順で
// 安全に向きを切り替える。corners は起動時と同じく黒で埋める。
static bool g_avatar_started = false;
static void applyRotation() {
    uint8_t r = static_cast<uint8_t>((g_base_rotation + g_rotation_step) & 0x03);
    if (g_avatar_started) avatar.suspend();
    M5.Display.setRotation(r);
    M5.Display.fillScreen(TFT_BLACK);
    if (g_avatar_started) avatar.resume();
    Serial.printf("[bridge] rotation step=%u (display=%u)\n", g_rotation_step, r);
}

static void setState(State s) {
    g_state = s;
    avatarSay(stateStr(s));
    Serial.printf("[bridge] state=%s\n", stateStr(s));
}

static bool exprFromString(const char* s, Expression& out) {
    if (strcmp(s, "neutral") == 0) { out = Expression::Neutral; return true; }
    if (strcmp(s, "happy")   == 0) { out = Expression::Happy;   return true; }
    if (strcmp(s, "sad")     == 0) { out = Expression::Sad;     return true; }
    if (strcmp(s, "doubt")   == 0) { out = Expression::Doubt;   return true; }
    if (strcmp(s, "sleepy")  == 0) { out = Expression::Sleepy;  return true; }
    if (strcmp(s, "angry")   == 0) { out = Expression::Angry;   return true; }
    return false;
}

static void sendAckOk(const char* extra = nullptr) {
    if (extra) {
        Serial.printf("{\"status\":\"ok\",%s}\n", extra);
    } else {
        Serial.println("{\"status\":\"ok\"}");
    }
}

static void sendAckError(const char* err) {
    Serial.printf("{\"status\":\"error\",\"error\":\"%s\"}\n", err);
}

// === コマンド処理 ============================================================

static void handleStatus() {
    Serial.printf("{\"state\":\"%s\",\"volume\":%u,\"version\":\"atoms3r-main-0.3\","
                  "\"servo\":false,\"torque\":false,\"camera\":false,"
                  "\"rotation\":%u,"
                  "\"queued\":%d,\"playing\":%s}\n",
                  stateStr(g_state), g_volume,
                  g_rotation_step,
                  wavQueueCount(),
                  g_wav_playing ? "true" : "false");
}

static void handleVolume(const char* arg) {
    int v = atoi(arg);
    if (v < 0)   v = 0;
    if (v > 255) v = 255;
    g_volume = static_cast<uint8_t>(v);
    M5.Speaker.setVolume(g_volume);
    M5.Speaker.setChannelVolume(0, g_volume);
    char buf[48];
    snprintf(buf, sizeof(buf), "\"volume\":%u", g_volume);
    sendAckOk(buf);
}

static void handleFace(const char* arg) {
    Expression ex;
    if (!exprFromString(arg, ex)) {
        char err[64];
        snprintf(err, sizeof(err), "unknown face: %s", arg);
        sendAckError(err);
        return;
    }
    avatar.setExpression(ex);
    char extra[48];
    snprintf(extra, sizeof(extra), "\"face\":\"%s\"", arg);
    sendAckOk(extra);
}

// ROTATE:<0-3>\n  顔の向きを時計回りクオータターン数で指定 (0=自然/1=CW90/2=180/3=CW270)
static void handleRotate(const char* arg) {
    char* end = nullptr;
    long n = strtol(arg, &end, 10);
    if (end == arg || n < 0 || n > 3) {
        sendAckError("rotation must be 0-3");
        return;
    }
    g_rotation_step = static_cast<uint8_t>(n);
    applyRotation();
    char extra[32];
    snprintf(extra, sizeof(extra), "\"rotation\":%u", g_rotation_step);
    sendAckOk(extra);
}

// WAV:<size>\n (XangiBridge と同一プロトコル)
static void handleWav(size_t size) {
    if (size == 0) {
        sendAckError("size=0");
        return;
    }
    if (size > MAX_WAV_BYTES) {
        sendAckError("size exceeds MAX_WAV_BYTES");
        return;
    }
    if (wavQueueFull()) {
        sendAckError("queue full");
        return;
    }

    uint8_t* buf = static_cast<uint8_t*>(ps_malloc(size));
    if (!buf) {
        sendAckError("ps_malloc failed");
        return;
    }

    setState(State::Receiving);
    Serial.printf("[bridge] wav recv start, expect=%u\n", static_cast<unsigned>(size));
    Serial.println("READY");
    Serial.flush();

    size_t received = 0;
    uint32_t last_byte_ms = millis();
    uint32_t last_log_ms  = millis();
    while (received < size) {
        int avail = Serial.available();
        if (avail > 0) {
            size_t want = static_cast<size_t>(avail);
            if (want > size - received) want = size - received;
            int got = Serial.readBytes(buf + received, want);
            if (got > 0) {
                received += static_cast<size_t>(got);
                last_byte_ms = millis();
                if (millis() - last_log_ms > 200) {
                    Serial.printf("[bridge] recv progress=%u/%u\n",
                                  static_cast<unsigned>(received),
                                  static_cast<unsigned>(size));
                    last_log_ms = millis();
                }
            }
        } else {
            if (millis() - last_byte_ms > WAV_CHUNK_TIMEOUT_MS) {
                Serial.printf("[bridge] recv timeout at %u/%u (idle=%lums)\n",
                              static_cast<unsigned>(received),
                              static_cast<unsigned>(size),
                              static_cast<unsigned long>(millis() - last_byte_ms));
                free(buf);
                setState(g_wav_playing ? State::Playing : State::Ready);
                sendAckError("recv timeout");
                return;
            }
            M5.update();
            delay(1);
        }
    }

    if (!wavQueuePush(buf, received)) {
        free(buf);
        setState(g_wav_playing ? State::Playing : State::Ready);
        sendAckError("queue full after recv");
        return;
    }
    setState(g_wav_playing ? State::Playing : State::Ready);
    char extra[80];
    snprintf(extra, sizeof(extra), "\"size\":%u,\"queued\":%d",
             static_cast<unsigned>(received), wavQueueCount());
    sendAckOk(extra);
}

static void wavPlayTask(void* /*param*/) {
    for (;;) {
        if (wavQueueEmpty()) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        WavSlot& slot = g_wav_queue[g_wav_queue_head];
        uint8_t* data = slot.data;
        size_t   len  = slot.len;
        slot.data = nullptr;
        slot.len  = 0;
        g_wav_queue_head = (g_wav_queue_head + 1) % WAV_QUEUE_SIZE;

        g_wav_playing = true;
        setState(State::Playing);
        Serial.printf("[bridge] wav play start, size=%u, remaining=%d\n",
                      static_cast<unsigned>(len), wavQueueCount());

        bool ok = M5.Speaker.playWav(data, len, 1, 0, true);
        if (!ok) {
            Serial.println("[bridge] playWav failed");
            free(data);
            g_wav_playing = false;
            setState(wavQueueEmpty() ? State::Ready : State::Playing);
            continue;
        }

        uint32_t last_mouth_ms = 0;
        while (M5.Speaker.isPlaying()) {
            if (millis() - last_mouth_ms > MOUTH_UPDATE_MS) {
                float ratio = 0.2f + (static_cast<float>(esp_random() % 700) / 1000.0f);
                avatar.setMouthOpenRatio(ratio);
                last_mouth_ms = millis();
            }
            vTaskDelay(pdMS_TO_TICKS(PLAY_POLL_INTERVAL_MS));
        }
        avatar.setMouthOpenRatio(0.0f);

        free(data);
        g_wav_playing = false;
        setState(wavQueueEmpty() ? State::Ready : State::Playing);
    }
}

// === テキストコマンド受信 ====================================================
static char g_line[MAX_LINE_LEN];
static size_t g_line_len = 0;

static void resetLine() {
    g_line_len = 0;
    g_line[0]  = '\0';
}

static void pollSerialCommand() {
    while (Serial.available()) {
        int c = Serial.read();
        if (c < 0) break;

        if (c == '\r') continue;
        if (c == '\n') {
            if (g_line_len == 0) continue;
            g_line[g_line_len] = '\0';

            if (strcmp(g_line, "STATUS") == 0) {
                handleStatus();
            }
            else if (strncmp(g_line, "VOLUME:", 7) == 0) {
                handleVolume(g_line + 7);
            }
            else if (strncmp(g_line, "WAV:", 4) == 0) {
                long n = atol(g_line + 4);
                if (n < 0) {
                    sendAckError("negative size");
                } else {
                    handleWav(static_cast<size_t>(n));
                }
            }
            else if (strncmp(g_line, "FACE:", 5) == 0) {
                handleFace(g_line + 5);
            }
            else if (strncmp(g_line, "ROTATE:", 7) == 0) {
                handleRotate(g_line + 7);
            }
            else if (strncmp(g_line, "MOVE:", 5) == 0) {
                sendAckError("servo not available");
            }
            else if (strcmp(g_line, "CAPTURE") == 0) {
                sendAckError("camera not available");
            }
            else {
                Serial.printf("{\"status\":\"error\",\"error\":\"unknown command\",\"line\":\"%s\"}\n",
                              g_line);
            }
            resetLine();
            continue;
        }

        if (g_line_len < MAX_LINE_LEN - 1) {
            g_line[g_line_len++] = static_cast<char>(c);
        } else {
            resetLine();
            sendAckError("line too long");
        }
    }
}

// === setup / loop ============================================================
void setup() {
    auto cfg = M5.config();
    // Atomic Voice Base / Atomic Echo Base 共通の ES8311 codec を有効化。
    // M5Unified が I2S 設定 (G5/G6/G7/G8) を自動で行う。
    cfg.external_speaker.atomic_echo = true;
    M5.begin(cfg);

    // ESP32-S3 USB-CDC の RX バッファを大きく取る (4MB WAV を受けるため)
    Serial.setRxBufferSize(32768);
    Serial.begin(SERIAL_BAUD);
    delay(300);
    Serial.println();
    Serial.println("[bridge] xangi-stackchan / atoms3r-main 0.2 (AtomS3R + Voice/Echo Base)");

    // Speaker config (atama リポ準拠、ES8311 で過変調を避ける)
    auto spk_cfg = M5.Speaker.config();
    spk_cfg.sample_rate  = 96000;
    spk_cfg.dma_buf_count = 20;
    spk_cfg.dma_buf_len  = 512;
    M5.Speaker.config(spk_cfg);
    M5.Speaker.begin();
    M5.Speaker.setVolume(g_volume);
    M5.Speaker.setChannelVolume(0, g_volume);

    // 顔の向き: M5.begin が決めた自然な向きを基準に、起動時に既定の回転を適用してから
    // Avatar を init する (init 前に向きを確定させておくと回転途中フレームが出ない)。
    g_base_rotation = static_cast<uint8_t>(M5.Display.getRotation() & 0x03);
    applyRotation();

    // Avatar: AtomS3R LCD は 128x128 で小さいので scale 0.5 + position 調整
    avatar.setScale(0.5);
    avatar.setPosition(-56, -96);
    avatar.init();
    g_avatar_started = true;
    avatar.setExpression(Expression::Neutral);
    avatar.setSpeechText("booting");
    g_state = State::Booting;
    Serial.println("[bridge] state=booting");

    xTaskCreatePinnedToCore(wavPlayTask, "wavPlay", 8192, nullptr, 1, nullptr, APP_CPU_NUM);

    resetLine();
    setState(State::Ready);
}

void loop() {
    M5.update();
    pollSerialCommand();
    delay(2);
}
