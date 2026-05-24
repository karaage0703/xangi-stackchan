// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/basic/main:
//   アールティ Ver.β (M5Stack Basic + Feetech SCS0009 ×2) 用の XangiBridge 互換
//   受信ファーム。XangiBridge との違い:
//     - 対象: M5Stack Basic (ESP32 旧世代, PSRAM 無し, 内蔵カメラ無し)
//     - WAV バッファ: PSRAM 8MB → 内部 DRAM 64KB チャンク受信に変更
//     - CAPTURE: unavailable 固定 (カメラ無し)
//     - サーボ: SCServo (SCS0009 ×2) は別 PR で統合、本ファームでは unavailable 応答
//     - シリアル baudrate: 921600 → 115200 (Basic CP2104 安定値、要実機検証)
//
// 本ファイルの実装範囲:
//   - STATUS / VOLUME / FACE 動作 (Avatar 統合、Speaker 音量変更)
//   - WAV 受信 + 再生 + 口パク連動 (PSRAM 無し前提、内部 DRAM 96KB 上限の同期再生)
//   - MOVE は unsupported スタブ、CAPTURE は unavailable
//
// XangiBridge との WAV 設計差分:
//   - WAV バッファは PSRAM 上 ps_malloc 4MB → 内部 DRAM malloc 96KB 上限
//     (M5Stack Basic 320KB SRAM 制約。16kHz/mono/16bit で約 3 秒分、xangi TTS の
//      1 chunk として現実的)。それより長い音声はホスト側で分割推奨
//   - WAV キュー (4 slot) → 1 WAV 受信完了でその場で同期 playWav (Basic の
//     SRAM 余裕が無くキュー化メリット薄、シリアル排他が host 側 _lock で取れ
//     ているので直列でも host 側 send_wav 単位の応答性は変わらない)
//   - wavPlayTask 別 core 廃止 → loop 内で再生完了まで block (口パクは block
//     中の vTaskDelay 合間で Avatar 更新)
//
// 別 PR の予定: SCServo SCS0009 統合 (PORT C 16/17、アールティ PCB の配線実機確認)、
// ホスト側 device profile rt_beta 追加。
//
// プロトコル (XangiBridge 互換、docs/xangi_bridge_protocol.md 参照):
//   STATUS / VOLUME:<0-255> / WAV:<size> / FACE:<expr> / MOVE:<yaw,pitch> / CAPTURE

#include <Avatar.h>
#include <M5Unified.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "SCServo.h"

using m5avatar::Avatar;
using m5avatar::Expression;
using namespace scservo;

constexpr uint32_t SERIAL_BAUD  = 115200;
constexpr size_t   MAX_LINE_LEN = 64;
constexpr size_t   MAX_WAV_BYTES = 96 * 1024;   // 96KB (Basic SRAM 320KB の安全上限)
constexpr uint32_t WAV_CHUNK_TIMEOUT_MS  = 2000;
constexpr uint32_t MOUTH_UPDATE_MS       = 80;
constexpr uint32_t PLAY_POLL_INTERVAL_MS = 50;
constexpr const char* FW_VERSION = "basic-main-0.4";

// M5Stack Basic + アールティ Stack-chan PCB のサーボ配線。robo8080/AI_StackChan2_RT
// の `#else` ブランチ (Core2/CoreS3 以外 = Basic 該当) が GPIO16/17 を使用、
// baudrate 1Mbps、SCS0009 半二重バス。
//   ソース: <https://github.com/robo8080/AI_StackChan2_RT/blob/master/M5Unified_AI_StackChan_RT/src/main.cpp>
constexpr int8_t   SERVO_RX_PIN = 16;
constexpr int8_t   SERVO_TX_PIN = 17;
// アールティ Ver.β は工場で原点調整済 (公式ブログ「サーボモータSCS0009 ×2、原点
// 調整済み」明記)、HomeCalibration 不要。scservo ライブラリの DEFAULT_ZERO_RAW
// (= 512、SCS0009 step 0-1023 の中央) を zero とし、setAngleYaw(0) でホーム位置に
// 来る前提。実機ずれが大きければ手動で個別調整 (将来 NVS 保存対応も視野)。
// SCS0009 への移動時間。長くするほど加速度が抑えられ、サーボ突入電流が下がる。
// M5Stack Basic の USB 給電 (5V/500mA) で Stack-chan PCB のサーボ電源を共有する
// 構成だと、500ms (元値) でブラウンアウト → USB 瞬断していたので 2500ms (2.5秒)
// に抑える。応答性は下がるが安定優先。
constexpr uint16_t MOVE_GOAL_TIME_MS = 2500;

static uint8_t g_volume = 128;
static bool    g_wav_playing  = false;
static bool    g_servo_ready  = false;
static bool    g_servo_torque = false;

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

static Avatar avatar;
// M5Stack Basic は Serial2 (GPIO16/17) でサーボに繋がる。Serial1 は Flash と
// ピン競合するため使えない。
static SCServo servo(Serial2, SERVO_RX_PIN, SERVO_TX_PIN);

static void setState(State s) {
    g_state = s;
}

// サーボ起動 (PY32 VM_EN は K151 固有、アールティ PCB では不要)。SCS0009 と
// Serial2 1Mbps で疎通 → torque OFF → zero=512 セット → readPos 確認。失敗時は
// graceful degradation で WAV/FACE のみの ready 状態に降りる。
static bool initServo() {
    if (!servo.begin()) {
        Serial.println("[bridge] servo: Serial2 begin failed");
        return false;
    }
    Serial.printf("[bridge] servo: Serial2 1Mbps opened (RX=G%d, TX=G%d)\n",
                  SERVO_RX_PIN, SERVO_TX_PIN);

    // 念のため torque OFF (起動直後の安全モード)
    for (uint8_t i = 0; i < 3; i++) {
        servo.enableTorque(SERVO_ID_YAW,   false);
        delay(20);
        servo.enableTorque(SERVO_ID_PITCH, false);
        delay(40);
    }
    g_servo_torque = false;

    // アールティ Ver.β は工場原点調整済 → zero=512 (scservo::DEFAULT_ZERO_RAW) 固定
    servo.setZeroYaw(scservo::DEFAULT_ZERO_RAW);
    servo.setZeroPitch(scservo::DEFAULT_ZERO_RAW);
    Serial.printf("[bridge] servo: zero set yaw=%d pitch=%d (factory pre-calibrated)\n",
                  scservo::DEFAULT_ZERO_RAW, scservo::DEFAULT_ZERO_RAW);

    // 通信疎通確認 (リトライ付き)。ping 失敗時は g_servo_ready=false で降ろす
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
    Serial.printf("[bridge] servo: pinged yaw=%d pitch=%d\n", yawPos, pitchPos);

    // 起動時にゆっくりホーム位置 (yaw=0, pitch=0) へ移動して torque ON 状態で安定
    // させる。MOVE 初回受信時に突入電流が一気に来るのを避けるため、起動シーケンス
    // 内で 1 回だけピークを通す設計。USB 給電だけで動かす構成だと、torque ON
    // 直後の急加速で USB が瞬断するため、enableTorque 間に長めの delay + 即
    // ホーム位置 writePos でゆっくり移動させる。
    servo.enableTorque(SERVO_ID_YAW, true);
    delay(150);
    servo.setAngleYaw(0.0f, MOVE_GOAL_TIME_MS);
    delay(150);
    servo.enableTorque(SERVO_ID_PITCH, true);
    delay(150);
    servo.setAnglePitch(0.0f, MOVE_GOAL_TIME_MS);
    g_servo_torque = true;
    Serial.printf("[bridge] servo: torque ON, home positioned (yaw=0, pitch=0)\n");

    return true;
}

static void ensureTorqueOn() {
    if (g_servo_torque) return;
    servo.enableTorque(SERVO_ID_YAW,   true);
    delay(50);
    servo.enableTorque(SERVO_ID_PITCH, true);
    delay(50);
    g_servo_torque = true;
}

static bool exprFromString(const char* s, Expression& out) {
    if (strcmp(s, "neutral") == 0) { out = Expression::Neutral; return true; }
    if (strcmp(s, "happy") == 0)   { out = Expression::Happy;   return true; }
    if (strcmp(s, "sad") == 0)     { out = Expression::Sad;     return true; }
    if (strcmp(s, "doubt") == 0)   { out = Expression::Doubt;   return true; }
    if (strcmp(s, "sleepy") == 0)  { out = Expression::Sleepy;  return true; }
    if (strcmp(s, "angry") == 0)   { out = Expression::Angry;   return true; }
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

static void sendAckUnavailable(const char* cmd, const char* reason) {
    Serial.printf("{\"status\":\"unavailable\",\"cmd\":\"%s\",\"reason\":\"%s\"}\n", cmd, reason);
}

static void handleStatus() {
    Serial.printf("{\"state\":\"%s\",\"volume\":%u,\"version\":\"%s\","
                  "\"servo\":%s,\"torque\":%s,\"camera\":false,"
                  "\"queued\":0,\"playing\":%s}\n",
                  stateStr(g_state), g_volume, FW_VERSION,
                  g_servo_ready  ? "true" : "false",
                  g_servo_torque ? "true" : "false",
                  g_wav_playing  ? "true" : "false");
}

static void handleVolume(const char* arg) {
    int v = atoi(arg);
    if (v < 0 || v > 255) {
        sendAckError("volume out of range");
        return;
    }
    g_volume = static_cast<uint8_t>(v);
    M5.Speaker.setVolume(g_volume);
    Serial.printf("{\"status\":\"ok\",\"volume\":%u}\n", g_volume);
}

static void handleFace(const char* arg) {
    Expression expr;
    if (!exprFromString(arg, expr)) {
        sendAckError("unknown expression");
        return;
    }
    avatar.setExpression(expr);
    Serial.printf("{\"status\":\"ok\",\"face\":\"%s\"}\n", arg);
}

// WAV 受信 → 同期再生 + 口パク連動。XangiBridge とほぼ同じプロトコルだが、
// PSRAM 無しのため (1) 96KB 上限 (2) キュー無し同期再生、にシンプル化。
//
// シーケンス:
//   1. WAV:<size>\n を受信、size チェック (0 < size <= MAX_WAV_BYTES)
//   2. malloc(size) → READY\n をホストに返してバイナリ受信開始
//   3. Serial.available() ベースのバイト単位受信 (Stream timeout 回避)
//   4. 受信完了 → 即 ack ok 返す
//   5. M5.Speaker.playWav(data, size, 1, 0, true) で再生開始
//   6. isPlaying() ポーリングで完了待ち、合間に Avatar.setMouthOpenRatio 更新
//   7. 再生完了 → mouth リセット → free → state Ready
static void handleWav(size_t size) {
    if (size == 0) {
        sendAckError("size=0");
        return;
    }
    if (size > MAX_WAV_BYTES) {
        sendAckError("size exceeds MAX_WAV_BYTES (96KB on M5Stack Basic)");
        return;
    }

    uint8_t* buf = static_cast<uint8_t*>(malloc(size));
    if (!buf) {
        sendAckError("malloc failed (out of SRAM)");
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
                Serial.printf("[bridge] recv timeout at %u/%u\n",
                              static_cast<unsigned>(received),
                              static_cast<unsigned>(size));
                free(buf);
                setState(State::Ready);
                sendAckError("recv timeout");
                return;
            }
            delay(1);
        }
    }

    // 受信完了 → host に ack
    char extra[64];
    snprintf(extra, sizeof(extra), "\"size\":%u,\"queued\":0", static_cast<unsigned>(received));
    sendAckOk(extra);

    // 再生 (同期、完了まで block)
    g_wav_playing = true;
    setState(State::Playing);
    Serial.printf("[bridge] wav play start, size=%u\n", static_cast<unsigned>(received));

    bool ok = M5.Speaker.playWav(buf, received, 1, 0, true);
    if (!ok) {
        Serial.println("[bridge] playWav failed");
        free(buf);
        g_wav_playing = false;
        setState(State::Ready);
        return;
    }

    uint32_t last_mouth_ms = 0;
    while (M5.Speaker.isPlaying()) {
        if (millis() - last_mouth_ms > MOUTH_UPDATE_MS) {
            float ratio = 0.2f + (static_cast<float>(esp_random() % 700) / 1000.0f);
            avatar.setMouthOpenRatio(ratio);
            last_mouth_ms = millis();
        }
        delay(PLAY_POLL_INTERVAL_MS);
    }
    avatar.setMouthOpenRatio(0.0f);

    free(buf);
    g_wav_playing = false;
    setState(State::Ready);
    Serial.println("[bridge] wav play done");
}

// MOVE:<yaw,pitch> → setAngleYaw + setAnglePitch (zero ベース度)。
// SCServo.h の安全範囲 (YAW: ±100°、PITCH: ±30°) で内部 clamp される。
// 工場原点調整済の SCS0009 + アールティ Stack-chan PCB 構成では、yaw=0,pitch=0
// が機械的に水平正面を向く前提。実機ずれが大きければ DEFAULT_ZERO_RAW を調整。
static void handleMove(const char* arg) {
    if (!g_servo_ready) {
        sendAckUnavailable("MOVE", "servo not ready (init failed?)");
        return;
    }

    float yaw = 0, pitch = 0;
    if (sscanf(arg, "%f,%f", &yaw, &pitch) != 2) {
        sendAckError("invalid MOVE arg, expected yaw,pitch");
        return;
    }

    ensureTorqueOn();
    bool okY = servo.setAngleYaw(yaw,   MOVE_GOAL_TIME_MS);
    bool okP = servo.setAnglePitch(pitch, MOVE_GOAL_TIME_MS);
    if (!okY || !okP) {
        Serial.printf("[bridge] MOVE write failed yaw_ok=%d pitch_ok=%d\n", okY, okP);
        sendAckError("setAngle write failed");
        return;
    }

    // setAngle*() は内部 clamp 後に書き込むので、clamp された実際値を返したい。
    // getAngle*() で読めるが SCS0009 readPos が遅延するため、ここではホスト要求値
    // をそのまま返す (実値検証したい場合は STATUS の torque/servo フラグで判定)。
    char extra[64];
    snprintf(extra, sizeof(extra), "\"yaw\":%.1f,\"pitch\":%.1f", yaw, pitch);
    sendAckOk(extra);
}

static void handleCapture() {
    sendAckUnavailable("CAPTURE", "no camera on M5Stack Basic");
}

static char   g_line[MAX_LINE_LEN];
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
            } else if (strncmp(g_line, "VOLUME:", 7) == 0) {
                handleVolume(g_line + 7);
            } else if (strncmp(g_line, "WAV:", 4) == 0) {
                long n = atol(g_line + 4);
                if (n < 0) {
                    sendAckError("negative size");
                } else {
                    handleWav(static_cast<size_t>(n));
                }
            } else if (strncmp(g_line, "FACE:", 5) == 0) {
                handleFace(g_line + 5);
            } else if (strncmp(g_line, "MOVE:", 5) == 0) {
                handleMove(g_line + 5);
            } else if (strcmp(g_line, "CAPTURE") == 0) {
                handleCapture();
            } else {
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

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);

    Serial.setRxBufferSize(8192);
    Serial.begin(SERIAL_BAUD);
    delay(100);
    Serial.println();
    Serial.printf("[bridge] xangi-stackchan / basic-main %s\n", FW_VERSION);

    avatar.init();
    avatar.setExpression(Expression::Neutral);
    avatar.setSpeechText("booting");
    setState(State::Booting);
    Serial.println("[bridge] state=booting");

    if (!M5.Speaker.begin()) {
        Serial.println("[bridge] M5.Speaker.begin() failed");
        setState(State::Error);
        return;
    }
    M5.Speaker.setVolume(g_volume);

    // SCServo 初期化 (失敗時は graceful degradation: MOVE は unavailable に降りる
    // が WAV/FACE は引き続き動く)
    g_servo_ready = initServo();
    if (!g_servo_ready) {
        avatar.setSpeechText("no servo");
        Serial.println("[bridge] servo init failed, MOVE will return error");
        delay(800);
    }

    avatar.setSpeechText("ready");
    Serial.println("[bridge] camera: not available (M5Stack Basic)");

    resetLine();
    setState(State::Ready);
}

void loop() {
    M5.update();
    pollSerialCommand();
    delay(2);
}
