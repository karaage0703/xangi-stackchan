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
    avatarSay(stateStr(s));
    Serial.printf("[bridge] state=%s\n", stateStr(s));
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

static void sendAckUnsupported(const char* cmd) {
    Serial.printf("{\"status\":\"unsupported\",\"cmd\":\"%s\"}\n", cmd);
}

// === コマンド処理 ============================================================

static void handleStatus() {
    Serial.printf("{\"state\":\"%s\",\"volume\":%u,\"version\":\"cores3-main-0.7\","
                  "\"servo\":%s,\"torque\":%s,\"camera\":%s,\"queued\":%d,\"playing\":%s}\n",
                  stateStr(g_state), g_volume,
                  g_servo_ready  ? "true" : "false",
                  g_servo_torque ? "true" : "false",
                  g_camera_ready ? "true" : "false",
                  wavQueueCount(),
                  g_wav_playing ? "true" : "false");
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
    avatar.setExpression(ex);
    char extra[48];
    snprintf(extra, sizeof(extra), "\"face\":\"%s\"", arg);
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
    avatar.setMouthOpenRatio(0.0f);
    avatar.setSpeechText("stopped");
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
            else if (strncmp(g_line, "FACE:", 5) == 0) {
                handleFace(g_line + 5);
            }
            else if (strncmp(g_line, "MOVE:", 5) == 0) {
                handleMove(g_line + 5);
            }
            else if (strcmp(g_line, "CAPTURE") == 0) {
                handleCapture();
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
    Serial.println("[bridge] xangi-stackchan / cores3-main 0.7 (avatar+servo+wavqueue+camera+touchstop)");

    // Avatar 初期化。`init()` で内部スプライトを確保し、表情/口パク用の draw
    // task を起動する。M5.begin() の後で呼ぶ必要あり。
    avatar.init();
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

    // WAV 再生タスクを core 1 (APP_CPU_NUM) に pin。loop task は core 1 で動く
    // ので同 core にすることで M5.Speaker のスレッド親和性を維持する (M5Unified
    // の Speaker は呼び出し core で I2S DMA を回す)。stack 8KB は playWav の
    // 内部 task に必要な余裕を見込んだサイズ。
    xTaskCreatePinnedToCore(wavPlayTask, "wavPlay", 8192, nullptr, 1, nullptr, APP_CPU_NUM);

    resetLine();
    setState(State::Ready);
}

void loop() {
    M5.update();
    pollSerialCommand();
    pollTouchStop();
    delay(2);
}
