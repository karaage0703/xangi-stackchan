// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/cores3/main:
//   K151 / CoreS3 を xangi (or 任意ホスト) のシリアル経由音声出力デバイスとして
//   動かす受信ファーム。本体ファーム。
//
// プロトコル (詳細は docs/xangi_bridge_protocol.md):
//   テキスト行 (\n 終端):
//     STATUS           → JSON ack {"state":"ready|receiving|playing","volume":N,
//                                  "version":"...","servo":bool,"queued":n,"playing":bool}
//     VOLUME:<0-255>   → setVolume → JSON ack {"status":"ok","volume":N}
//     WAV:<size>       → "READY\n" 返してバイナリモード、<size> bytes 受信して
//                        WAV キューに push → 即 ack {"status":"ok","size":N,"queued":n}
//                        (実際の再生は wavPlayTask が別 task で処理)
//     FACE:<expr>      → setExpression → JSON ack {"status":"ok","face":"..."}
//                        expr: neutral / happy / sad / doubt / sleepy / angry
//     IMAGE:<size>     → "READY\n" 返して JPEG を受信し、LCD に画像顔として表示
//                        → JSON ack {"status":"ok","image":N}
//     MOVE:<yaw,pitch> → setAngleYaw + setAnglePitch (zero ベース角度) → JSON ack
//                        {"status":"ok","yaw":N,"pitch":N} (サーボ統合 PR で実装)
//
// 設計:
//   - **Avatar 統合実装 で Avatar 統合**: 顔表示 + 表情変更 + 口パク連動
//   - **サーボ統合 PR でサーボ統合**: PY32 VM_EN ON → SCServo UART1 1Mbps、起動時に
//     NVS から zero raw load (HomeCalibration の出力)、torque OFF で安全側立ち
//     上げ。MOVE 受信で torque ON + setAngle*。サーボ初期化失敗時は
//     `g_servo_ready = false` で MOVE が unavailable になるが WAV/FACE は動く
//     (graceful degradation)。HomeCalibration を先に焼く前提。
//   - **WAV キュー化**: stackchan-atama の WavSlot ring buffer
//     方式を採用。`handleWav` は受信完了で即 ack、`wavPlayTask` (RTOS task on
//     core 1) が dequeue → `M5.Speaker.playWav` → 口パク → free。これでホスト側
//     `send_wav` の ack 待ち block (再生時間ぶん = 数秒〜十数秒) が無くなり、
//     パイプライン化で chunk 間隙ほぼゼロ + TalkingSway の MOVE も自然に
//     合間に入る。Queue full の場合は `{"status":"error","error":"queue full"}`
//     を返してホスト側に retry を促す。
//   - WAV バッファは PSRAM 上に動的確保 (ps_malloc) で WAV ごとに alloc/free。
//     CoreS3 PSRAM 8MB なので、最大 4MB まで受信許可 (`MAX_WAV_BYTES`)。
//   - シリアルは Arduino `Serial` (USB-Serial/JTAG)、921600 baud (Python 側
//     DEFAULT_BAUD と一致)。

#include <Avatar.h>
#include <M5CoreS3.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <esp_camera.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "SCServo.h"
#include "Si12T.h"

using m5avatar::Avatar;
using m5avatar::Expression;
using namespace scservo;

constexpr uint32_t SERIAL_BAUD       = 921600;
constexpr size_t   MAX_LINE_LEN      = 64;       // テキストコマンド最大長
constexpr size_t   MAX_WAV_BYTES     = 4 * 1024 * 1024;  // 4MB 上限 (PSRAM ~8MB)
constexpr uint32_t WAV_CHUNK_TIMEOUT_MS = 2000;  // 1 chunk 到着までの最大空き時間
constexpr uint32_t PLAY_POLL_INTERVAL_MS = 50;
constexpr uint32_t MOUTH_UPDATE_MS   = 80;       // 口パク更新間隔
constexpr int      WAV_QUEUE_SIZE    = 4;        // WAV キュー slot 数 (ring buffer)
constexpr uint8_t  JPEG_QUALITY      = 80;       // CAPTURE で frame2jpg に渡す品質 (0-100)
constexpr size_t   MAX_JPEG_BYTES    = 256 * 1024;  // 想定上限 (320x240 RGB565→JPEG はせいぜい 30KB)
constexpr size_t   MAX_IMAGE_BYTES   = 512 * 1024;  // host から送る LCD 用 JPEG 画像の上限
constexpr size_t   MAX_RECT_BYTES    = 320 * 240 * 2;  // host から送る RGB565 dirty rect 上限
// IMAGE/RECT のバイナリ受信中、1 chunk 到着までの最大空き時間。ホスト側の READY
// 待ち (3s) より十分短くする。長いとホストが READY を諦めて次の IMAGE:<size>\n を
// 送った時、ファームがまだ前フレームの受信ループに居座り、その IMAGE:... テキストを
// JPEG バイナリとして食ってフレーム境界が恒久的にずれる (READY 不達の連鎖)。短く
// すればホストが諦める前に受信ループを抜けてコマンド待ちに復帰でき、自動再同期する。
constexpr uint32_t IMAGE_CHUNK_TIMEOUT_MS = 1500;
constexpr uint32_t BATTERY_UPDATE_MS = 5000;

// === マイク録音 (MIC_START / MIC_STOP / MIC_PCM) ==============================
// CoreS3 内蔵 PDM マイクで 16-bit signed mono PCM @ 16kHz を取得し、64ms 単位
// (1024 sample = 2048 byte) のチャンクで `MIC_PCM:<size>\n<binary>` 形式で host に
// 流す。host 側で蓄積 → silero-vad で無音検出 → faster-whisper STT 想定 (本 PR は
// stream 配信までの最小実装、無音検出と STT は host 側別 PR)。
//
// CoreS3 はスピーカーとマイクが I2S0 を共有するため、Mic.begin() の前に
// Speaker.end() で I2S を解放、MIC_STOP で逆順に Speaker.begin() で復帰する。
// 録音中は WAV 再生不可 (キューに積んでも playWav が音を出せない)。host 側で
// `mic_recording=true` の間 WAV 送信を抑止する想定。
constexpr uint32_t MIC_SAMPLE_RATE    = 16000;
constexpr size_t   MIC_CHUNK_SAMPLES  = 1024;        // 64ms @ 16kHz
constexpr size_t   MIC_CHUNK_BYTES    = MIC_CHUNK_SAMPLES * sizeof(int16_t);
constexpr uint32_t MIC_STOP_WAIT_MS   = 200;         // 録音タスク終了待ち
// 録音開始からの最大経過時間 (ms)。host が SIGKILL 等で死んで MIC_STOP を送れな
// かった場合の保険。これを超えたら自動で MIC モードを抜けて Speaker 復帰する。
// 通常の host 制御では _on_pcm_chunk で 15 秒 (voice_max_seconds) で自動 stop が
// 走るので、ここはより長め (60 秒) に取って正常系を邪魔しない。
constexpr uint32_t MIC_WATCHDOG_MS    = 60000;

// CoreS3 の UART1 ピン (docs/scservo_protocol.md §1)
constexpr int8_t SERVO_RX_PIN = 7;
constexpr int8_t SERVO_TX_PIN = 6;

// HomeCalibration ファームと共有する NVS namespace / キー (firmware/examples/cores3/safe-startup/main.cpp と一致)
constexpr const char* NVS_NAMESPACE      = "xstackchan";
constexpr const char* NVS_KEY_YAW_ZERO   = "yaw_zero";
constexpr const char* NVS_KEY_PITCH_ZERO = "pitch_zero";

constexpr uint16_t MOVE_GOAL_TIME_MS = 500;  // setAngle 既定の移動時間

static uint8_t g_volume = 128;
static bool    g_servo_ready  = false;
static bool    g_servo_torque = false;
static bool    g_camera_ready = false;

// アタマタッチセンサ (M5Stack 公式 StackChan K151 内蔵 Si12T、3 ch capacitive)。
// K151 / K151-R では本体内に組み込まれている。CoreS3 単体機 (StackChan body 無し)
// では I2C bus に device が存在しないので begin() が false を返し、polling task は
// 起動しない (graceful degradation: head_touch 機能だけ unavailable)。
static Si12T   g_head_touch;
static bool    g_head_touch_ready = false;
// なでなで feedback (Press で Happy + "nade nade!") の有効/無効。voice_conversation
// モードでは「press 直後に MIC_START → Doubt + listening」がすぐ表示されるのを
// 優先して、なでなで feedback を抑制する。host から `HEADTOUCH_AVATAR:on|off` で
// 切替。デフォルト on (従来挙動を維持)。
static bool    g_head_touch_avatar = true;

// マイク録音状態。host 側 `MIC_START` で true、`MIC_STOP` で false。録音中は
// micRecordTask が PDM マイクから 16-bit PCM をチャンクで切り出して Serial に
// stream 配信する。STATUS の "mic_recording" でホスト側 polling 可能。
static volatile bool g_mic_recording = false;
static volatile uint32_t g_mic_start_ms = 0;  // 録音開始時刻 (watchdog 用)
static volatile bool g_mic_watchdog_fired = false;  // watchdog 発火フラグ
static TaskHandle_t  g_mic_task      = nullptr;
static int16_t       g_mic_buffer[MIC_CHUNK_SAMPLES];

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

// === WAV キュー (stackchan-atama 方式) ================================
// 4 slot ring buffer。`handleWav` が push、`wavPlayTask` が pop して再生する。
// volatile + 整数 head/tail だけで RTOS 同期不要 (push は loop task / pop は
// wavPlayTask の片方ずつ進むので、index の単一書き込み者保証で OK)。
struct WavSlot {
    uint8_t* data;
    size_t   len;
};
static WavSlot           g_wav_queue[WAV_QUEUE_SIZE] = {};
static volatile int      g_wav_queue_head = 0;  // 次に取り出すスロット (wavPlayTask が進める)
static volatile int      g_wav_queue_tail = 0;  // 次に書き込むスロット (handleWav が進める)
static volatile bool     g_wav_playing    = false;

static int wavQueueCount() {
    int c = g_wav_queue_tail - g_wav_queue_head;
    if (c < 0) c += WAV_QUEUE_SIZE;
    return c;
}
static bool wavQueueFull()  { return wavQueueCount() >= (WAV_QUEUE_SIZE - 1); }
static bool wavQueueEmpty() { return g_wav_queue_head == g_wav_queue_tail; }

// push: 成功なら true、queue full なら false。`data` の所有権はキューに移る
// (再生完了後に wavPlayTask が free する)。
static bool wavQueuePush(uint8_t* data, size_t len) {
    if (wavQueueFull()) return false;
    g_wav_queue[g_wav_queue_tail].data = data;
    g_wav_queue[g_wav_queue_tail].len  = len;
    g_wav_queue_tail = (g_wav_queue_tail + 1) % WAV_QUEUE_SIZE;
    return true;
}

static Avatar avatar;
static SCServo servo(Serial1, SERVO_RX_PIN, SERVO_TX_PIN);

// スプライトを host 側で JPEG 化して送る画像顔モード。
// IMAGE を受けたら Avatar draw task を suspend し、以後 LCD にはこの JPEG と
// バッテリー overlay を描く。FACE を受けたら Avatar モードに戻る。
static uint8_t* g_image_face_jpeg = nullptr;
static size_t   g_image_face_len = 0;
static bool     g_image_face_active = false;
static bool     g_image_face_dirty = false;
static M5Canvas g_image_face_canvas(&M5.Display);
static bool     g_image_face_canvas_ready = false;
static uint32_t g_last_battery_update_ms = 0;
static int32_t  g_battery_level = -1;
static int16_t  g_battery_voltage_mv = -1;
static m5::Power_Class::is_charging_t g_battery_charging = m5::Power_Class::charge_unknown;

// === PY32 IO Expander 経由でサーボバス電源 (VM_EN) を ON にする =================
// firmware/examples/cores3/safe-startup/main.cpp の py32 namespace と同一仕様 (docs §11.4.1)。
namespace py32 {
constexpr uint8_t  I2C_ADDR         = 0x6F;
constexpr uint32_t I2C_FREQ         = 100000;
constexpr uint8_t  REG_VERSION      = 0x02;
constexpr uint8_t  REG_GPIO_DIR_L   = 0x03;
constexpr uint8_t  REG_GPIO_OUT_L   = 0x05;
constexpr uint8_t  REG_GPIO_PU_L    = 0x09;
constexpr uint8_t  SERVO_VM_EN_PIN  = 0;

static bool waitReady(uint32_t timeoutMs = 1500) {
    const uint32_t start = millis();
    while (millis() - start < timeoutMs) {
        const uint8_t v = M5.In_I2C.readRegister8(I2C_ADDR, REG_VERSION, I2C_FREQ);
        if (v != 0 && v != 0xFF) {
            Serial.printf("[py32] ready, version=0x%02X\n", v);
            return true;
        }
        delay(50);
    }
    Serial.println("[py32] timeout waiting for PY32");
    return false;
}

static bool enableServoPower() {
    if (!waitReady()) return false;
    const uint8_t mask = 1 << SERVO_VM_EN_PIN;
    bool ok = true;
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_DIR_L, mask, I2C_FREQ);
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_PU_L,  mask, I2C_FREQ);
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_OUT_L, mask, I2C_FREQ);
    delay(200);  // VM rail 安定待ち
    Serial.printf("[py32] servo power %s\n", ok ? "ON" : "FAILED");
    return ok;
}
}  // namespace py32

// === サーボ起動 ===============================================================
// 成功なら g_servo_ready=true、失敗なら false (WAV/FACE は引き続き動く)。
// HomeCalibration で保存した zero raw を NVS から読み、setZero*() で反映する。
static bool initServo() {
    if (!py32::enableServoPower()) {
        Serial.println("[bridge] servo: VM_EN ON failed");
        return false;
    }
    if (!servo.begin()) {
        Serial.println("[bridge] servo: UART1 begin failed");
        return false;
    }
    Serial.println("[bridge] servo: UART1 1Mbps opened (TX=G6, RX=G7)");

    // 起動直後は念のため torque OFF (手戻し可能、キャリブ前の安全モード)
    for (uint8_t i = 0; i < 3; i++) {
        servo.enableTorque(SERVO_ID_YAW,   false);
        delay(20);
        servo.enableTorque(SERVO_ID_PITCH, false);
        delay(40);
    }
    g_servo_torque = false;

    // NVS から zero raw を load
    Preferences prefs;
    if (prefs.begin(NVS_NAMESPACE, true /* RO */)) {
        int16_t yawZero   = prefs.getShort(NVS_KEY_YAW_ZERO,   DEFAULT_ZERO_RAW);
        int16_t pitchZero = prefs.getShort(NVS_KEY_PITCH_ZERO, DEFAULT_ZERO_RAW);
        prefs.end();
        servo.setZeroYaw(yawZero);
        servo.setZeroPitch(pitchZero);
        Serial.printf("[bridge] servo: zero loaded yaw=%d pitch=%d\n", yawZero, pitchZero);
    } else {
        Serial.println("[bridge] servo: NVS namespace not found, zero=512 (run HomeCalibration first)");
    }

    // 通信疎通確認 (リトライ付き)。失敗してもファームは続行するが MOVE は飛ばない
    // 環境を作る (g_servo_ready のフラグは下げる)。
    int16_t yawPos = -1, pitchPos = -1;
    for (uint8_t i = 0; i < 8; i++) {
        if (yawPos   < 0) yawPos   = servo.readPos(SERVO_ID_YAW);
        if (pitchPos < 0) pitchPos = servo.readPos(SERVO_ID_PITCH);
        if (yawPos >= 0 && pitchPos >= 0) break;
        delay(80);
    }
    if (yawPos < 0 || pitchPos < 0) {
        Serial.printf("[bridge] servo: readPos failed yaw=%d pitch=%d\n", yawPos, pitchPos);
        return false;
    }
    Serial.printf("[bridge] servo: ready yaw=%d pitch=%d (torque OFF)\n", yawPos, pitchPos);
    return true;
}

static void ensureTorqueOn() {
    if (g_servo_torque) return;
    servo.enableTorque(SERVO_ID_YAW,   true);
    delay(5);
    servo.enableTorque(SERVO_ID_PITCH, true);
    delay(5);
    g_servo_torque = true;
    Serial.println("[bridge] servo: torque ON");
}

// === Avatar ヘルパ ===========================================================
static void avatarSay(const char* msg) {
    avatar.setSpeechText(msg);
}

static void setState(State s) {
    g_state = s;
    if (!g_image_face_active) {
        avatarSay(stateStr(s));
    }
    Serial.printf("[bridge] state=%s\n", stateStr(s));
}

static void updateBatteryInfo(bool force = false) {
    uint32_t now = millis();
    if (!force && now - g_last_battery_update_ms < BATTERY_UPDATE_MS) return;
    g_last_battery_update_ms = now;
    g_battery_level = M5.Power.getBatteryLevel();
    g_battery_voltage_mv = M5.Power.getBatteryVoltage();
    g_battery_charging = M5.Power.isCharging();

    if (!g_image_face_active && avatar.isDrawing()) {
        bool charging = (g_battery_charging == m5::Power_Class::is_charging);
        avatar.setBatteryStatus(charging, g_battery_level);
    }
}

template <typename Gfx>
static void drawBatteryOverlayOn(Gfx& gfx) {
    updateBatteryInfo();
    int32_t level = g_battery_level;
    if (level < 0) level = 0;
    if (level > 100) level = 100;
    bool charging = (g_battery_charging == m5::Power_Class::is_charging);

    constexpr int x = 260;
    constexpr int y = 8;
    constexpr int w = 44;
    constexpr int h = 16;
    uint16_t fg = (level <= 20 && !charging) ? TFT_RED : TFT_WHITE;

    gfx.fillRoundRect(x - 6, y - 4, 58, 28, 4, TFT_BLACK);
    gfx.drawRect(x, y + 3, 4, 8, fg);
    gfx.drawRect(x + 4, y, w, h, fg);
    int fill = (w - 4) * level / 100;
    gfx.fillRect(x + 6 + (w - 4 - fill), y + 2, fill, h - 4, fg);
    if (charging) {
        gfx.setTextColor(TFT_YELLOW, TFT_BLACK);
        gfx.drawString("+", x - 3, y - 1);
    }
    gfx.setTextColor(fg, TFT_BLACK);
    gfx.setTextSize(1);
    gfx.drawRightString(String(level) + "%", x + w, y + h + 2);
}

static void drawBatteryOverlay() {
    drawBatteryOverlayOn(M5.Display);
}

static void ensureImageFaceCanvas() {
    if (g_image_face_canvas_ready) return;
    g_image_face_canvas.setColorDepth(16);
    g_image_face_canvas.createSprite(320, 240);
    g_image_face_canvas_ready = true;
}

static void drawImageFace(bool force = false) {
    if (!g_image_face_active || !g_image_face_jpeg || g_image_face_len == 0) return;
    if (!force && !g_image_face_dirty) return;
    ensureImageFaceCanvas();
    g_image_face_canvas.fillScreen(TFT_BLACK);
    bool ok = g_image_face_canvas.drawJpg(g_image_face_jpeg, g_image_face_len, 0, 0, 320, 240, 0, 0);
    drawBatteryOverlayOn(g_image_face_canvas);
    g_image_face_canvas.pushSprite(0, 0);
    g_image_face_dirty = false;
    Serial.printf("[bridge] image face draw: %s size=%u\n",
                  ok ? "ok" : "failed", static_cast<unsigned>(g_image_face_len));
}

static void activateAvatarFace() {
    if (g_image_face_active) {
        g_image_face_active = false;
        avatar.resume();
        avatar.setBatteryIcon(true);
        updateBatteryInfo(true);
    }
}

// FACE 文字列 → Expression 変換。未知は false 返す。
static bool exprFromString(const char* s, Expression& out) {
    if (strcmp(s, "neutral") == 0) { out = Expression::Neutral; return true; }
    if (strcmp(s, "happy")   == 0) { out = Expression::Happy;   return true; }
    if (strcmp(s, "sad")     == 0) { out = Expression::Sad;     return true; }
    if (strcmp(s, "doubt")   == 0) { out = Expression::Doubt;   return true; }
    if (strcmp(s, "sleepy")  == 0) { out = Expression::Sleepy;  return true; }
    if (strcmp(s, "angry")   == 0) { out = Expression::Angry;   return true; }
    return false;
}

// === シリアル送信 ============================================================
// ESP32 Arduino の Serial.printf / println は内部 TX buffer に書くだけで自動 flush
// しない。応答が「次の応答までまとめて batch 送信」されると host 側 send_command の
// expect_line が timeout して順序がズレる (2026-05-27 23:27 で発覚)。各 ack 関数の
// 末尾で Serial.flush() を呼んで即時送信する。
static void sendAckOk(const char* extra = nullptr) {
    if (extra) {
        Serial.printf("{\"status\":\"ok\",%s}\n", extra);
    } else {
        Serial.println("{\"status\":\"ok\"}");
    }
    Serial.flush();
}

static void sendAckError(const char* err) {
    Serial.printf("{\"status\":\"error\",\"error\":\"%s\"}\n", err);
    Serial.flush();
}

static void sendAckUnsupported(const char* cmd) {
    Serial.printf("{\"status\":\"unsupported\",\"cmd\":\"%s\"}\n", cmd);
    Serial.flush();
}

// === コマンド処理 ============================================================

static void handleStatus() {
    updateBatteryInfo(true);
    Serial.printf("{\"state\":\"%s\",\"volume\":%u,\"version\":\"cores3-main-0.15\","
                  "\"servo\":%s,\"torque\":%s,\"camera\":%s,\"head_touch\":%s,"
                  "\"mic_recording\":%s,\"queued\":%d,\"playing\":%s,"
                  "\"image_face\":%s,\"battery_level\":%ld,"
                  "\"battery_voltage_mv\":%d,\"charging\":\"%s\"}\n",
                  stateStr(g_state), g_volume,
                  g_servo_ready  ? "true" : "false",
                  g_servo_torque ? "true" : "false",
                  g_camera_ready ? "true" : "false",
                  g_head_touch_ready ? "true" : "false",
                  g_mic_recording ? "true" : "false",
                  wavQueueCount(),
                  g_wav_playing ? "true" : "false",
                  g_image_face_active ? "true" : "false",
                  static_cast<long>(g_battery_level),
                  g_battery_voltage_mv,
                  g_battery_charging == m5::Power_Class::is_charging ? "charging" :
                    (g_battery_charging == m5::Power_Class::is_discharging ? "discharging" : "unknown"));
    Serial.flush();
}

// CAPTURE\n
// CoreS3 内蔵 GC0308 カメラで 1 フレーム取得 → frame2jpg で JPEG 化 → シリアル送信。
// プロトコル (詳細は docs/xangi_bridge_protocol.md):
//   ホスト送信:  CAPTURE\n
//   ファーム応答 (成功):
//     IMG:<size>\n
//     <size bytes JPEG binary>
//     {"status":"ok","size":N,"format":"jpeg","width":W,"height":H,"captured_at":<ms>}\n
//   ファーム応答 (失敗):
//     {"status":"error","error":"..."}\n
//
// 注意: GC0308 のデフォルト pixformat は M5CoreS3 ライブラリ実装で RGB565 設定。
// 320x240 RGB565 = 153600 bytes、JPEG 80 で 5-30KB に圧縮。frame2jpg は ESP-IDF
// esp32-camera 内蔵のヘルパー。921600bps シリアルなら 30KB JPEG は ~330ms で送信。
static void handleCapture() {
    if (g_mic_recording) {
        // Mic と Speaker / Camera は I2S/I2C を共有してないが、Mic 録音中は
        // ホスト側で動かすべき経路ではないので二重防御。
        sendAckError("mic recording active");
        return;
    }
    if (!g_camera_ready) {
        sendAckError("camera not ready");
        return;
    }

    avatar.setSpeechText("capturing");

    if (!CoreS3.Camera.get()) {
        avatar.setSpeechText("");
        sendAckError("Camera.get failed");
        return;
    }
    camera_fb_t* fb = CoreS3.Camera.fb;
    if (!fb || !fb->buf || fb->len == 0) {
        CoreS3.Camera.free();
        avatar.setSpeechText("");
        sendAckError("empty frame");
        return;
    }

    uint8_t* out_jpg     = nullptr;
    size_t   out_jpg_len = 0;
    bool ok = frame2jpg(fb, JPEG_QUALITY, &out_jpg, &out_jpg_len);
    int width  = fb->width;
    int height = fb->height;
    uint32_t captured_at_ms = millis();
    CoreS3.Camera.free();

    if (!ok || !out_jpg || out_jpg_len == 0) {
        if (out_jpg) free(out_jpg);
        avatar.setSpeechText("");
        sendAckError("frame2jpg failed");
        return;
    }
    if (out_jpg_len > MAX_JPEG_BYTES) {
        free(out_jpg);
        avatar.setSpeechText("");
        sendAckError("jpeg too large");
        return;
    }

    // バイナリ送信ヘッダ → JPEG 本体 → ack。ホスト側 (StackchanSerial.capture)
    // は "IMG:<size>\n" 受けたら <size> bytes バイナリ読み → 行頭が `{` の ack を待つ。
    Serial.printf("IMG:%u\n", static_cast<unsigned>(out_jpg_len));
    Serial.flush();
    // 1 chunk で全部書き出す (256KB 上限なので 921600bps でも数百 ms)。
    Serial.write(out_jpg, out_jpg_len);
    Serial.flush();
    free(out_jpg);

    char extra[160];
    snprintf(extra, sizeof(extra),
             "\"size\":%u,\"format\":\"jpeg\",\"width\":%d,\"height\":%d,\"captured_at\":%lu",
             static_cast<unsigned>(out_jpg_len), width, height,
             static_cast<unsigned long>(captured_at_ms));
    sendAckOk(extra);

    avatar.setSpeechText("");
}

static void handleVolume(const char* arg) {
    int v = atoi(arg);
    if (v < 0)   v = 0;
    if (v > 255) v = 255;
    g_volume = static_cast<uint8_t>(v);
    M5.Speaker.setVolume(g_volume);
    char buf[48];
    snprintf(buf, sizeof(buf), "\"volume\":%u", g_volume);
    sendAckOk(buf);
}

// MOVE:<yaw_deg>,<pitch_deg>\n
// zero ベース角度 (HomeCalibration の zero=0° として相対)。SAFE 範囲は scservo lib
// が内部 clamp する (yaw ±100°、pitch ±30°、`SCServo.h:YAW_SAFE_MIN_DEG` 等)。
// g_servo_ready=false の場合は service unavailable 応答 (HomeCalibration 未焼き
// or サーボ電源失敗等)。
static void handleMove(const char* arg) {
    if (!g_servo_ready) {
        sendAckError("servo not ready (HomeCalibration required?)");
        return;
    }
    // "yaw,pitch" parse
    const char* comma = strchr(arg, ',');
    if (!comma) {
        sendAckError("MOVE syntax: yaw,pitch");
        return;
    }
    char yaw_buf[16];
    size_t yaw_len = static_cast<size_t>(comma - arg);
    if (yaw_len == 0 || yaw_len >= sizeof(yaw_buf)) {
        sendAckError("MOVE yaw parse error");
        return;
    }
    memcpy(yaw_buf, arg, yaw_len);
    yaw_buf[yaw_len] = '\0';
    const char* pitch_str = comma + 1;

    float yawDeg   = atof(yaw_buf);
    float pitchDeg = atof(pitch_str);

    // SAFE 範囲で自前 clamp してから setAngle に渡す (scservo lib も内部 clamp
    // するが、ack に "実際に適用された値" を入れたいので明示する)。要求値が
    // SAFE 範囲外の場合は ack に `requested_*` と `clamped:true` を入れて、
    // ホスト側 (xangi) が「指定通り動かなかった」を判定できるようにする。
    float yawClamped   = constrain(yawDeg,   YAW_SAFE_MIN_DEG,   YAW_SAFE_MAX_DEG);
    float pitchClamped = constrain(pitchDeg, PITCH_SAFE_MIN_DEG, PITCH_SAFE_MAX_DEG);
    bool  clamped      = (yawClamped != yawDeg || pitchClamped != pitchDeg);

    ensureTorqueOn();

    bool ok_yaw   = servo.setAngleYaw(yawClamped,   MOVE_GOAL_TIME_MS);
    bool ok_pitch = servo.setAnglePitch(pitchClamped, MOVE_GOAL_TIME_MS);
    if (!ok_yaw || !ok_pitch) {
        Serial.printf("[bridge] MOVE failed: yaw_ok=%d pitch_ok=%d\n", ok_yaw, ok_pitch);
        sendAckError("setAngle failed");
        return;
    }

    char extra[160];
    if (clamped) {
        snprintf(extra, sizeof(extra),
                 "\"yaw\":%.2f,\"pitch\":%.2f,"
                 "\"requested_yaw\":%.2f,\"requested_pitch\":%.2f,\"clamped\":true",
                 yawClamped, pitchClamped, yawDeg, pitchDeg);
    } else {
        snprintf(extra, sizeof(extra), "\"yaw\":%.2f,\"pitch\":%.2f",
                 yawClamped, pitchClamped);
    }
    sendAckOk(extra);
}

// FACE:<expr>\n
static void handleFace(const char* arg) {
    Expression ex;
    if (!exprFromString(arg, ex)) {
        char err[64];
        snprintf(err, sizeof(err), "unknown face: %s", arg);
        sendAckError(err);
        return;
    }
    activateAvatarFace();
    avatar.setExpression(ex);
    char extra[48];
    snprintf(extra, sizeof(extra), "\"face\":\"%s\"", arg);
    sendAckOk(extra);
}

// IMAGE:<size>\n
// host 側で 320x240 JPEG に変換済みの画像顔を受信して LCD に描画する。
static void handleImage(size_t size) {
    if (g_mic_recording) {
        sendAckError("mic recording active");
        return;
    }
    if (size == 0) {
        sendAckError("size=0");
        return;
    }
    if (size > MAX_IMAGE_BYTES) {
        sendAckError("size exceeds MAX_IMAGE_BYTES");
        return;
    }

    uint8_t* buf = static_cast<uint8_t*>(ps_malloc(size));
    if (!buf) {
        sendAckError("ps_malloc failed");
        return;
    }

    Serial.printf("[bridge] image recv start, expect=%u\n", static_cast<unsigned>(size));
    Serial.println("READY");
    Serial.flush();

    size_t received = 0;
    uint32_t last_byte_ms = millis();
    while (received < size) {
        int avail = Serial.available();
        if (avail > 0) {
            size_t want = static_cast<size_t>(avail);
            if (want > size - received) want = size - received;
            int got = Serial.readBytes(buf + received, want);
            if (got > 0) {
                received += static_cast<size_t>(got);
                last_byte_ms = millis();
            }
        } else {
            if (millis() - last_byte_ms > IMAGE_CHUNK_TIMEOUT_MS) {
                free(buf);
                sendAckError("recv timeout");
                return;
            }
            M5.update();
            delay(1);
        }
    }

    if (g_image_face_jpeg) {
        free(g_image_face_jpeg);
    }
    g_image_face_jpeg = buf;
    g_image_face_len = received;
    g_image_face_active = true;
    g_image_face_dirty = true;
    avatar.suspend();
    updateBatteryInfo(true);

    char extra[96];
    snprintf(extra, sizeof(extra), "\"image\":%u,\"battery_level\":%ld",
             static_cast<unsigned>(received), static_cast<long>(g_battery_level));
    sendAckOk(extra);
    drawImageFace(true);
}

// RECT:<x>,<y>,<w>,<h>,<size>\n
// host 側で前フレームとの差分 bbox を RGB565 little-endian に変換済みの矩形。
// JPEG 全画面再描画ではなく pushImage で変化した矩形だけ更新する。
static void handleRect(const char* arg) {
    if (g_mic_recording) {
        sendAckError("mic recording active");
        return;
    }

    int x = 0, y = 0, w = 0, h = 0;
    long n = 0;
    if (sscanf(arg, "%d,%d,%d,%d,%ld", &x, &y, &w, &h, &n) != 5) {
        sendAckError("RECT syntax: x,y,w,h,size");
        return;
    }
    if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > 320 || y + h > 240) {
        sendAckError("rect out of bounds");
        return;
    }
    if (n <= 0 || static_cast<size_t>(n) > MAX_RECT_BYTES) {
        sendAckError("rect size out of range");
        return;
    }
    size_t size = static_cast<size_t>(n);
    if (size != static_cast<size_t>(w) * static_cast<size_t>(h) * 2) {
        sendAckError("rect size mismatch");
        return;
    }

    uint8_t* buf = static_cast<uint8_t*>(ps_malloc(size));
    if (!buf) {
        sendAckError("ps_malloc failed");
        return;
    }

    Serial.printf("[bridge] rect recv start x=%d y=%d w=%d h=%d size=%u\n",
                  x, y, w, h, static_cast<unsigned>(size));
    Serial.println("READY");
    Serial.flush();

    size_t received = 0;
    uint32_t last_byte_ms = millis();
    while (received < size) {
        int avail = Serial.available();
        if (avail > 0) {
            size_t want = static_cast<size_t>(avail);
            if (want > size - received) want = size - received;
            int got = Serial.readBytes(buf + received, want);
            if (got > 0) {
                received += static_cast<size_t>(got);
                last_byte_ms = millis();
            }
        } else {
            if (millis() - last_byte_ms > IMAGE_CHUNK_TIMEOUT_MS) {
                free(buf);
                sendAckError("recv timeout");
                return;
            }
            M5.update();
            delay(1);
        }
    }

    if (!g_image_face_active) {
        if (g_image_face_jpeg) {
            free(g_image_face_jpeg);
            g_image_face_jpeg = nullptr;
            g_image_face_len = 0;
        }
        g_image_face_active = true;
        avatar.suspend();
        M5.Display.fillScreen(TFT_BLACK);
        updateBatteryInfo(true);
    }

    M5.Display.pushImage(x, y, w, h, reinterpret_cast<uint16_t*>(buf));
    free(buf);
    drawBatteryOverlay();
    g_image_face_dirty = false;

    char extra[128];
    snprintf(extra, sizeof(extra),
             "\"rect\":[%d,%d,%d,%d],\"bytes\":%u,\"battery_level\":%ld",
             x, y, w, h, static_cast<unsigned>(size), static_cast<long>(g_battery_level));
    sendAckOk(extra);
}

// WAV:<size>\n
// WAV キュー実装の核: 受信完了 → WAV キューに push → 即 ack 返す。再生は別 RTOS task
// `wavPlayTask` が core 1 で処理するので、loop task はブロックしない。
//
// シーケンス:
//   (1) サイズ妥当性チェック、queue full チェック
//   (2) PSRAM に size bytes alloc
//   (3) "READY\n" 返してバイナリ受信
//   (4) wavQueuePush(buf, size) (失敗時は free)
//   (5) JSON ack {"status":"ok","size":N,"queued":n} 返す
// failure path で必ず free + ack 整合性を保つこと (push 失敗時に free 漏れすると
// PSRAM が無くなる)。push 成功後の所有権は wavPlayTask へ移る。
static void handleWav(size_t size) {
    if (g_mic_recording) {
        // Mic と Speaker は I2S 共有。録音中の playWav は不可。host 側で skip
        // すべきだが、二重防御として拒否応答 (chunk binary が流れ込む前にエラー
        // を返すことでシリアル汚染も防ぐ)。
        sendAckError("mic recording active");
        return;
    }
    if (size == 0) {
        sendAckError("size=0");
        return;
    }
    if (size > MAX_WAV_BYTES) {
        sendAckError("size exceeds MAX_WAV_BYTES");
        return;
    }
    if (wavQueueFull()) {
        // ホスト側の retry に任せる。queue が空くまで受信を始めない (READY を
        // 出さない) ことで、ホストの送信 chunk バッファをそのまま保持してもらう。
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
            // available 分だけ要求する。readBytes(buf, N) は内部の Stream タイム
            // アウトでブロックしうるので、必ず available <= 要求 にして即時 return
            // させる (これを守らないと last_byte_ms 計測が破綻して recv timeout)。
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

    // 受信完了。WAV キューに push。push が成功したらバッファ所有権は wavPlayTask
    // へ移るので handleWav 側で free しない。失敗 (= queue が満杯化、handleWav の
    // 冒頭チェックと受信中の間に他の path で push されるはずはないので通常起き
    // ない、念のための保険) は free + error。
    if (!wavQueuePush(buf, received)) {
        free(buf);
        setState(g_wav_playing ? State::Playing : State::Ready);
        sendAckError("queue full after recv");
        return;
    }
    // 即 ack 返す。実際の再生は wavPlayTask が非同期で行う。これで host 側
    // send_wav は次 chunk をすぐ送れる。
    setState(g_wav_playing ? State::Playing : State::Ready);
    char extra[80];
    snprintf(extra, sizeof(extra), "\"size\":%u,\"queued\":%d",
             static_cast<unsigned>(received), wavQueueCount());
    sendAckOk(extra);
}

// === WAV 再生タスク (core 1) =========================================
// loop task と分離して動く。キューが空でなければ dequeue → playWav → 口パク →
// free を順次処理。`g_wav_playing` フラグで現在再生中かを公開、STATUS で参照可。
static void wavPlayTask(void* /*param*/) {
    for (;;) {
        if (wavQueueEmpty()) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        // dequeue
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

        // 再生完了まで isPlaying() ポーリング + 口パク連動。
        // playWav 内部 task が data を参照するので、isPlaying() が false に
        // なるまで free してはいけない。
        uint32_t last_mouth_ms = 0;
        while (M5.Speaker.isPlaying()) {
            if (millis() - last_mouth_ms > MOUTH_UPDATE_MS) {
                // 0.2..0.9 のランダム開口で「喋ってる感」を出す。
                float ratio = 0.2f + (static_cast<float>(esp_random() % 700) / 1000.0f);
                if (!g_image_face_active) {
                    avatar.setMouthOpenRatio(ratio);
                }
                last_mouth_ms = millis();
            }
            vTaskDelay(pdMS_TO_TICKS(PLAY_POLL_INTERVAL_MS));
        }
        if (!g_image_face_active) {
            avatar.setMouthOpenRatio(0.0f);
        }

        free(data);
        g_wav_playing = false;
        setState(wavQueueEmpty() ? State::Ready : State::Playing);
    }
}

// === タッチによる発話停止 (Phase 1: stop のみ) ================================
// LCD 長押し (TOUCH_HOLD_MS) で M5.Speaker.stop + WAV キュー全クリア。stop した
// 旨を Serial に `{"event":"audio_stopped","reason":"touch",...}` で通知。host 側
// (xangi-stackchan) は SerialReader で受け取って、現 turn の後続 chunk を skip +
// FACE idle + MOVE ホーム復帰する。pause/resume は M5Unified に API が無いため
// 別 PR (現状未実装)。
constexpr uint32_t TOUCH_HOLD_MS = 1000;  // 1 秒長押しで stop (誤タッチ防止)
static uint32_t g_touch_press_start_ms = 0;
static bool     g_touch_stop_armed     = false;  // この press で stop を出したか

static void emitAudioStopped(const char* reason) {
    // ホスト向け非同期イベント行。`pollSerialCommand` のコマンド応答とは別の
    // 行で、host 側は SerialReader thread で逐次読み取って _user_stopped を立てる。
    Serial.printf("{\"event\":\"audio_stopped\",\"reason\":\"%s\",\"at\":%lu}\n",
                  reason, static_cast<unsigned long>(millis()));
}

// アタマタッチセンサ (Si12T) のジェスチャ通知。M5Stack 公式 StackChan K151 の
// 頭部 3 ch capacitive touch (前/中/後ろ) を Press / Release / SwipeForward /
// SwipeBackward の 4 ジェスチャに集約。host 側は音声入力トリガ等として利用する。
static void emitHeadTouch(const char* gesture) {
    Serial.printf("{\"event\":\"head_touch\",\"gesture\":\"%s\",\"at\":%lu}\n",
                  gesture, static_cast<unsigned long>(millis()));
}

static const char* headTouchGestureName(Si12T::Gesture g) {
    switch (g) {
        case Si12T::Gesture::Press:         return "press";
        case Si12T::Gesture::Release:       return "release";
        case Si12T::Gesture::SwipeForward:  return "swipe_forward";
        case Si12T::Gesture::SwipeBackward: return "swipe_backward";
        default:                            return "none";
    }
}

// アタマなでなで時の Avatar feedback。press で Happy 顔 + "nade nade!" 表示、
// release で Neutral 戻し。録音中 (g_mic_recording = listening 顔) と再生中
// (g_wav_playing = talking 顔) は触らず、それぞれの UX feedback を保つ。
static void applyHeadTouchAvatar(Si12T::Gesture g) {
    if (!g_head_touch_avatar) return;  // host から抑制指示あり (voice_conversation 中)
    if (g_mic_recording || g_wav_playing) return;
    switch (g) {
        case Si12T::Gesture::Press:
            avatar.setExpression(Expression::Happy);
            avatar.setSpeechText("nade nade!");
            break;
        case Si12T::Gesture::Release:
            avatar.setExpression(Expression::Neutral);
            avatar.setSpeechText("");
            break;
        case Si12T::Gesture::SwipeForward:
        case Si12T::Gesture::SwipeBackward:
            // スワイプ中も Happy のまま (Release で戻す)
            break;
        default:
            break;
    }
}

static void headTouchPollTask(void* /*param*/) {
    for (;;) {
        if (g_head_touch_ready) {
            Si12T::Gesture g = g_head_touch.poll();
            if (g != Si12T::Gesture::None) {
                emitHeadTouch(headTouchGestureName(g));
                applyHeadTouchAvatar(g);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

// === マイク PCM stream =======================================================
// 録音タスク本体。`M5.Mic.record` は非同期 DMA キャプチャを request して即返る。
// `M5.Mic.isRecording()` が 0 になるのを polling して完了確認 → buffer を Serial に
// `MIC_PCM:<size>\n<binary>` で送信。1024 sample = 64ms @ 16kHz、毎チャンク 2048
// byte なので 921600 baud (= 92KB/s) 上で 32KB/s 占有、十分余裕。
static void micRecordTask(void* /*param*/) {
    while (g_mic_recording) {
        // host 死亡 watchdog: MIC_START から MIC_WATCHDOG_MS 経過したら強制 stop。
        // host が SIGKILL されたり USB disconnect された場合、ファームが MIC モード
        // のまま PCM stream を吐き続けて次回の host 起動時シリアル汚染が発生する
        // のを防ぐ (2026-05-27 21:01 事故の再発防止)。
        if (g_mic_start_ms != 0 && millis() - g_mic_start_ms > MIC_WATCHDOG_MS) {
            Serial.printf("{\"event\":\"mic_watchdog_timeout\",\"at\":%lu,"
                          "\"elapsed_ms\":%lu}\n",
                          static_cast<unsigned long>(millis()),
                          static_cast<unsigned long>(millis() - g_mic_start_ms));
            g_mic_recording = false;
            g_mic_watchdog_fired = true;  // loop() で Speaker 復帰させる
            break;
        }
        if (!M5.Mic.record(g_mic_buffer, MIC_CHUNK_SAMPLES, MIC_SAMPLE_RATE)) {
            // record() 失敗 (queue full 等)。少し休んで retry。
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        // 録音完了待ち (DMA キャプチャ終了 = isRecording() が 0 になる)。
        while (g_mic_recording && M5.Mic.isRecording()) {
            vTaskDelay(pdMS_TO_TICKS(2));
        }
        if (!g_mic_recording) break;

        // ヘッダ + バイナリで送信。改行は付けない (host 側 reader が binary body の
        // 直後にコマンド応答や次の MIC_PCM ヘッダを受け取れるよう、binary は素直に
        // <size> byte ぴったりで終わる)。
        Serial.printf("MIC_PCM:%u\n", static_cast<unsigned>(MIC_CHUNK_BYTES));
        Serial.write(reinterpret_cast<uint8_t*>(g_mic_buffer), MIC_CHUNK_BYTES);
    }
    g_mic_task = nullptr;
    vTaskDelete(nullptr);
}

static void handleMicStart() {
    if (g_mic_recording) {
        sendAckError("already recording");
        return;
    }
    // Speaker と Mic は I2S を共有するので、Speaker.end() → Mic.begin() の順で
    // 切り替える。Speaker が止まると WAV 再生不可になるが、録音中はその想定。
    M5.Speaker.end();
    if (!M5.Mic.begin()) {
        Serial.println("[bridge] Mic.begin() failed");
        // 失敗時は Speaker を戻して error 応答
        M5.Speaker.begin();
        M5.Speaker.setVolume(g_volume);
        sendAckError("mic begin failed");
        return;
    }
    g_mic_recording = true;
    g_mic_start_ms = millis();  // watchdog の起点
    xTaskCreatePinnedToCore(micRecordTask, "micRec", 4096, nullptr, 3, &g_mic_task,
                            APP_CPU_NUM);
    if (!g_image_face_active) {
        avatar.setExpression(Expression::Doubt);  // listening 顔 (doubt 流用)
        avatar.setSpeechText("listening...");
    }
    Serial.printf("{\"status\":\"ok\",\"mode\":\"recording\",\"sample_rate\":%u,"
                  "\"bits\":16,\"channels\":1,\"chunk_bytes\":%u}\n",
                  static_cast<unsigned>(MIC_SAMPLE_RATE),
                  static_cast<unsigned>(MIC_CHUNK_BYTES));
    Serial.flush();
}

static void handleMicStop() {
    if (!g_mic_recording) {
        sendAckError("not recording");
        return;
    }
    g_mic_recording = false;
    // タスクが MIC_PCM 送信の途中なら最後の chunk が出るまで待つ。最悪 1 chunk
    // ぶん (64ms 録音 + DMA 完了 polling) なので 200ms で十分。
    uint32_t waited = 0;
    while (g_mic_task != nullptr && waited < MIC_STOP_WAIT_MS) {
        vTaskDelay(pdMS_TO_TICKS(10));
        waited += 10;
    }
    M5.Mic.end();
    if (!M5.Speaker.begin()) {
        Serial.println("[bridge] Speaker.begin() failed after mic stop");
        sendAckError("speaker resume failed");
        return;
    }
    M5.Speaker.setVolume(g_volume);
    if (!g_image_face_active) {
        avatar.setExpression(Expression::Neutral);
        avatar.setSpeechText("");
    }
    Serial.println("{\"status\":\"ok\",\"mode\":\"speaker\"}");
    Serial.flush();
}

static void clearWavQueueAndStop() {
    // 1. 現再生中の WAV を即停止
    M5.Speaker.stop();
    // 2. キューに残ってる全 slot を free + クリア
    for (int i = 0; i < WAV_QUEUE_SIZE; i++) {
        if (g_wav_queue[i].data != nullptr) {
            free(g_wav_queue[i].data);
            g_wav_queue[i].data = nullptr;
            g_wav_queue[i].len  = 0;
        }
    }
    g_wav_queue_head = 0;
    g_wav_queue_tail = 0;
    g_wav_playing    = false;
    // 3. Avatar 口パクリセット
    if (!g_image_face_active) {
        avatar.setMouthOpenRatio(0.0f);
        avatar.setSpeechText("stopped");
    }
    // 4. state を Ready に戻す (表情は host からの FACE が来るまで触らない)
    setState(State::Ready);
    Serial.println("[bridge] audio stopped by user touch");
}

static void pollTouchStop() {
    auto t = M5.Touch.getDetail();
    if (t.isPressed()) {
        if (g_touch_press_start_ms == 0) {
            // 押し始め
            g_touch_press_start_ms = millis();
            g_touch_stop_armed     = false;
            Serial.printf("[touch] pressed x=%d y=%d\n", t.x, t.y);
        } else if (!g_touch_stop_armed && millis() - g_touch_press_start_ms >= TOUCH_HOLD_MS) {
            // 1 秒経過 → 1 回だけ stop を発火 (連続発火を armed フラグで防ぐ)
            g_touch_stop_armed = true;
            Serial.println("[touch] long-press detected, stopping audio");
            clearWavQueueAndStop();
            emitAudioStopped("touch");
        }
    } else {
        // タッチ離れた → 状態リセット
        if (g_touch_press_start_ms != 0) {
            Serial.printf("[touch] released after %lu ms\n",
                          static_cast<unsigned long>(millis() - g_touch_press_start_ms));
        }
        g_touch_press_start_ms = 0;
        g_touch_stop_armed     = false;
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
            else if (strncmp(g_line, "IMAGE:", 6) == 0) {
                long n = atol(g_line + 6);
                if (n < 0) {
                    sendAckError("negative size");
                } else {
                    handleImage(static_cast<size_t>(n));
                }
            }
            else if (strncmp(g_line, "RECT:", 5) == 0) {
                handleRect(g_line + 5);
            }
            else if (strncmp(g_line, "FACE:", 5) == 0) {
                handleFace(g_line + 5);
            }
            else if (strncmp(g_line, "MOVE:", 5) == 0) {
                handleMove(g_line + 5);
            }
            else if (strcmp(g_line, "CAPTURE") == 0) {
                handleCapture();
            }
            else if (strcmp(g_line, "MIC_START") == 0) {
                handleMicStart();
            }
            else if (strcmp(g_line, "MIC_STOP") == 0) {
                handleMicStop();
            }
            else if (strncmp(g_line, "HEADTOUCH_AVATAR:", 17) == 0) {
                const char* arg = g_line + 17;
                g_head_touch_avatar = (strcmp(arg, "on") == 0);
                Serial.printf("{\"status\":\"ok\",\"head_touch_avatar\":%s}\n",
                              g_head_touch_avatar ? "true" : "false");
                Serial.flush();
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
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);

    // ESP32 Arduino の Serial RX バッファはデフォルト 256 byte。Python 側の
    // 1024 byte chunk を取りこぼさないため、Serial.begin の前に十分大きく確保。
    // 8192 byte = 8 chunk 分のバッファリング。
    Serial.setRxBufferSize(8192);
    Serial.begin(SERIAL_BAUD);
    delay(100);
    Serial.println();
    Serial.println("[bridge] xangi-stackchan / cores3-main 0.15 (avatar+spriteface+battery+servo+wavqueue+camera+touchstop+headtouch+headavatar+mic+micguard+micwatchdog+avtoggle+ackflush)");

    // Avatar 初期化。`init()` で内部スプライトを確保し、表情/口パク用の draw
    // task を起動する。M5.begin() の後で呼ぶ必要あり。
    avatar.init();
    avatar.setBatteryIcon(true);
    updateBatteryInfo(true);
    avatar.setExpression(Expression::Neutral);
    avatar.setSpeechText("booting");
    g_state = State::Booting;
    Serial.println("[bridge] state=booting");

    if (!M5.Speaker.begin()) {
        Serial.println("[bridge] M5.Speaker.begin() failed");
        setState(State::Error);
        return;
    }
    M5.Speaker.setVolume(g_volume);

    // サーボ初期化 (PY32 VM_EN ON → SCServo UART1 → NVS zero load → torque OFF)。
    // 失敗してもファームは続行する (graceful degradation: WAV/FACE は動く、MOVE
    // だけが unavailable 応答になる)。HomeCalibration を先に焼くのが推奨。
    g_servo_ready = initServo();
    if (!g_servo_ready) {
        avatar.setSpeechText("no servo");
        Serial.println("[bridge] servo init failed, MOVE will return error");
        delay(800);  // ユーザに状態を見せる
    }

    // CoreS3 内蔵カメラ (GC0308) 初期化。失敗しても WAV/FACE/MOVE は引き続き
    // 動く (graceful degradation: CAPTURE のみ unavailable 応答)。M5CoreS3
    // ライブラリが GC0308 のデフォルト pixformat (RGB565) / 解像度 / I2C 初期化
    // を内部で行う。Camera.begin() は esp_camera_init() を呼び失敗時 false を返す。
    g_camera_ready = CoreS3.Camera.begin();
    Serial.printf("[bridge] camera: %s\n", g_camera_ready ? "ready (GC0308)" : "INIT FAILED");
    if (!g_camera_ready) {
        avatar.setSpeechText("no camera");
        delay(500);
    }

    // アタマタッチセンサ (Si12T) を内部 I2C bus 上で探索 → 見つかれば初期化 +
    // 50ms polling task 起動。K151 / K151-R のみ搭載、CoreS3 単体 / Basic では
    // bus 上に device が無いので begin() が false を返して graceful degradation。
    g_head_touch_ready = g_head_touch.begin();
    Serial.printf("[bridge] head_touch: %s\n",
                  g_head_touch_ready ? "ready (Si12T@0x68)" : "not present");
    if (g_head_touch_ready) {
        // priority 2 / core APP_CPU は loop と同じ。50ms 周期 polling なので CPU
        // 負荷はほぼ無視できる。stack 4KB は Si12T::poll() の局所変数 + Serial.printf
        // フォーマット用 buffer 分の余裕を見込んだサイズ。
        xTaskCreatePinnedToCore(headTouchPollTask, "headTouch", 4096, nullptr, 2,
                                nullptr, APP_CPU_NUM);
    }

    // WAV 再生タスクを core 1 (APP_CPU_NUM) に pin。loop task は core 1 で動く
    // ので同 core にすることで M5.Speaker のスレッド親和性を維持する (M5Unified
    // の Speaker は呼び出し core で I2S DMA を回す)。stack 8KB は playWav の
    // 内部 task に必要な余裕を見込んだサイズ。
    xTaskCreatePinnedToCore(wavPlayTask, "wavPlay", 8192, nullptr, 1, nullptr, APP_CPU_NUM);

    resetLine();
    setState(State::Ready);
}

// watchdog 発火時の Speaker 復帰処理。micRecordTask 内から Mic.end / Speaker.begin
// を呼ぶと M5Unified の I2S 状態が別 task からの操作で壊れる可能性があるので、
// main loop で実行する。Mic 録音タスクは既に g_mic_recording=false で抜けてる。
static void recoverFromMicWatchdog() {
    if (!g_mic_watchdog_fired) return;
    g_mic_watchdog_fired = false;
    // micRecordTask が break して終了するまで少し待つ。
    uint32_t waited = 0;
    while (g_mic_task != nullptr && waited < MIC_STOP_WAIT_MS) {
        delay(10);
        waited += 10;
    }
    M5.Mic.end();
    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(g_volume);
    } else {
        Serial.println("[bridge] Speaker.begin() failed after mic watchdog");
    }
    avatar.setExpression(Expression::Neutral);
    avatar.setSpeechText("");
    Serial.println("[bridge] mic watchdog: speaker resumed");
}

void loop() {
    M5.update();
    pollSerialCommand();
    pollTouchStop();
    recoverFromMicWatchdog();
    updateBatteryInfo();
    if (g_image_face_active && millis() - g_last_battery_update_ms < 5) {
        g_image_face_dirty = true;
    }
    drawImageFace();
    delay(2);
}
