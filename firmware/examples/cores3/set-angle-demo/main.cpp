// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/cores3/set-angle-demo:
//   K151 で **初の torque ON 系ファーム**。HomeCalibration が NVS に保存した
//   zero raw を読み込み、`servo.setAngleYaw()` で yaw を中央 → ±30° → 中央に
//   スイープさせる。setAngle*() の zero ベース角度計算が実機で意図通り動くか
//   を確認するためのデモ。
//
// pitch を動かさない理由:
//   `SCServo.h` の `PITCH_SAFE_MIN_DEG = +20.0f` / `PITCH_SAFE_MAX_DEG = +70.0f`
//   は物理角度ベースで定義されているが、`setAngleClamped()` はその範囲を
//   zero ベース角度として処理している。HomeCalibration で水平姿勢を保存した
//   後に `setAnglePitch(0.0f)` を呼ぶと SAFE_MIN=20° に clamp されて 20° 上を
//   向いてしまう。pitch SAFE 範囲の意味論修正は次 PR (仕様書 §11 反映) で
//   対応するため、本デモでは pitch は torque OFF のまま yaw のみ動かす。
//
// 表示方針:
//   StackChan-Avatar が M5.Display を全画面占有するため、生 raw / deg は
//   USB シリアルにログ出力する。LCD は Avatar の SpeechText で状態を表示し、
//   Expression で状態遷移を表す (Neutral=待機、Happy=デモ実行中、
//   Sleepy=完了 idle、Sad=error、Doubt=e-stop)。
//
// 動作:
//   起動: torque OFF (両軸)、NVS から zero 読み込み、Serial に 200ms ごと
//         live yaw raw/deg を出力
//   トリガ: 画面タッチ / BtnA / Serial 'g' → yaw torque ON、デモ実行
//     1. setAngleYaw(0)   ホーム
//     2. setAngleYaw(-30) 左
//     3. setAngleYaw(0)   ホーム
//     4. setAngleYaw(+30) 右
//     5. setAngleYaw(0)   ホーム
//     完了後は yaw torque ON のまま idle (再トリガで再実行)
//   緊急停止: BtnB / Serial 's' → yaw torque OFF (手で動かせる状態に戻す)
//
// 注意:
//   - HomeCalibration ファームを先に焼いて zero を NVS に保存しておくこと。
//     未保存だと zero=512 デフォルトで動くため、物理姿勢によっては setAngleYaw(0)
//     で大きく回転して機械干渉する可能性がある。
//   - 起動直後の initial readPos が失敗したら VM rail / UART 疎通の問題。

#include <M5Unified.h>
#include <Avatar.h>
#include <Preferences.h>

#include "SCServo.h"

using namespace m5avatar;
using namespace scservo;

constexpr int8_t SERVO_RX_PIN = 7;  // G7
constexpr int8_t SERVO_TX_PIN = 6;  // G6

// home-calibration / safe-startup と共有する NVS namespace / キー
constexpr const char* NVS_NAMESPACE      = "xstackchan";
constexpr const char* NVS_KEY_YAW_ZERO   = "yaw_zero";
constexpr const char* NVS_KEY_PITCH_ZERO = "pitch_zero";

// PY32 IO Expander 経由で K151 のサーボバス電源 (VM_EN, pin 0) を ON。
// home-calibration / safe-startup と同じ手順 (docs/scservo_protocol.md §1)。
namespace py32 {
constexpr uint8_t  I2C_ADDR        = 0x6F;
constexpr uint32_t I2C_FREQ        = 100000;
constexpr uint8_t  REG_VERSION     = 0x02;
constexpr uint8_t  REG_GPIO_DIR_L  = 0x03;
constexpr uint8_t  REG_GPIO_OUT_L  = 0x05;
constexpr uint8_t  REG_GPIO_PU_L   = 0x09;
constexpr uint8_t  SERVO_VM_EN_PIN = 0;

bool waitReady(uint32_t timeoutMs = 1500) {
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

bool enableServoPower() {
    if (!waitReady()) return false;
    const uint8_t mask = 1 << SERVO_VM_EN_PIN;
    bool ok = true;
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_DIR_L, mask, I2C_FREQ);
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_PU_L,  mask, I2C_FREQ);
    ok &= M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_OUT_L, mask, I2C_FREQ);
    delay(200);
    Serial.printf("[py32] servo power %s\n", ok ? "ON" : "FAILED");
    return ok;
}
}  // namespace py32

Avatar avatar;
SCServo servo(Serial1, SERVO_RX_PIN, SERVO_TX_PIN);

// スイープ定義 (yaw 度数 / 移動指令を出してから次ステップを発火するまでの待機 ms)。
// hold > MOVE_TIME_MS にして動作完了の余韻を残す。
struct SweepStep {
    float    yawDeg;
    uint32_t holdMs;
};
constexpr SweepStep SWEEP[] = {
    {   0.0f, 1800 },
    { -30.0f, 1800 },
    {   0.0f, 1800 },
    { +30.0f, 1800 },
    {   0.0f, 1800 },
};
constexpr size_t   SWEEP_LEN     = sizeof(SWEEP) / sizeof(SWEEP[0]);
constexpr uint16_t MOVE_TIME_MS  = 1500;

enum class State { Booting, ReadyTorqueOff, Demo, Idle, Error, EStop };
static State    g_state         = State::Booting;
static bool     g_yawTorqueOn   = false;
static int      g_demoStep      = -1;       // -1 = not running、0..SWEEP_LEN-1 = 実行中
static uint32_t g_demoNextMs    = 0;        // 次ステップを発火する時刻

void setSpeech(const char* msg) {
    avatar.setSpeechText(msg);
    Serial.printf("[setangle] state=%d msg=%s\n", static_cast<int>(g_state), msg);
}

void setExpr(Expression e) {
    avatar.setExpression(e);
}

void enableYawTorque() {
    for (uint8_t i = 0; i < 2; i++) {
        servo.enableTorque(SERVO_ID_YAW, true);
        delay(20);
    }
    g_yawTorqueOn = true;
    Serial.println("[setangle] yaw torque ON");
}

void disableYawTorque() {
    for (uint8_t i = 0; i < 2; i++) {
        servo.enableTorque(SERVO_ID_YAW, false);
        delay(20);
    }
    g_yawTorqueOn = false;
    Serial.println("[setangle] yaw torque OFF");
}

void emergencyOff() {
    disableYawTorque();
    g_demoStep = -1;
    g_state    = State::EStop;
    setExpr(Expression::Doubt);
    setSpeech("e-stop");
}

void startDemo() {
    if (g_state == State::Error) {
        Serial.println("[setangle] error state, ignore trigger");
        return;
    }
    if (g_demoStep >= 0) {
        Serial.println("[setangle] demo already running, ignore");
        return;
    }
    enableYawTorque();
    delay(50);

    g_state      = State::Demo;
    g_demoStep   = 0;
    g_demoNextMs = 0;  // 次の loop で即座に最初のステップへ
    setExpr(Expression::Happy);
    setSpeech("demo: start");
}

// 起動時に NVS から zero 読み込み。未設定なら DEFAULT_ZERO_RAW (=512) のまま
void loadZeroFromNvs() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true /* RO */)) {
        Serial.println("[setangle] NVS namespace missing, zero=512 (run HomeCalibration first!)");
        return;
    }
    int16_t yawZero   = prefs.getShort(NVS_KEY_YAW_ZERO,   DEFAULT_ZERO_RAW);
    int16_t pitchZero = prefs.getShort(NVS_KEY_PITCH_ZERO, DEFAULT_ZERO_RAW);
    prefs.end();

    servo.setZeroYaw(yawZero);
    servo.setZeroPitch(pitchZero);
    Serial.printf("[setangle] zero from NVS: yaw=%d pitch=%d\n", yawZero, pitchZero);
}

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);

    Serial.begin(115200);
    delay(100);
    Serial.println();
    Serial.println("[setangle] xangi-stackchan-dev / k151 SetAngleDemo");

    avatar.init();
    avatar.setExpression(Expression::Sleepy);
    avatar.setSpeechText("boot...");

    if (!py32::enableServoPower()) {
        g_state = State::Error;
        setExpr(Expression::Sad);
        setSpeech("vm fail");
        return;
    }
    if (!servo.begin()) {
        g_state = State::Error;
        setExpr(Expression::Sad);
        setSpeech("uart fail");
        return;
    }

    // 起動直後は **両軸 torque OFF** (手で動かせる安全状態)。
    // yaw だけはデモトリガで torque ON する。pitch は触らない。
    for (uint8_t i = 0; i < 3; i++) {
        servo.enableTorque(SERVO_ID_YAW,   false);
        delay(20);
        servo.enableTorque(SERVO_ID_PITCH, false);
        delay(40);
    }

    loadZeroFromNvs();

    // 通信疎通確認
    int16_t yawPos = -1;
    for (uint8_t i = 0; i < 8 && yawPos < 0; i++) {
        yawPos = servo.readPos(SERVO_ID_YAW);
        if (yawPos < 0) delay(80);
    }
    if (yawPos < 0) {
        g_state = State::Error;
        setExpr(Expression::Sad);
        setSpeech("read fail");
        return;
    }

    g_state = State::ReadyTorqueOff;
    setExpr(Expression::Neutral);
    setSpeech("tap to start");
    Serial.printf("[setangle] ready, yaw=%d (zero=%d)\n", yawPos, servo.getZeroYaw());
}

// Serial に 'g' / 's' が来たらトリガ。改行は捨てる
struct SerialCmd { bool go; bool stop; };
SerialCmd readSerialCmd() {
    SerialCmd cmd = { false, false };
    while (Serial.available()) {
        int c = Serial.read();
        if (c == 'g' || c == 'G') cmd.go   = true;
        if (c == 's' || c == 'S') cmd.stop = true;
    }
    return cmd;
}

void tickDemo(uint32_t now) {
    if (g_demoStep < 0) return;
    if (now < g_demoNextMs) return;

    const SweepStep& step = SWEEP[g_demoStep];

    char buf[32];
    snprintf(buf, sizeof(buf), "yaw %+.0f", step.yawDeg);
    setSpeech(buf);

    if (!servo.setAngleYaw(step.yawDeg, MOVE_TIME_MS)) {
        Serial.println("[setangle] setAngleYaw failed");
        g_state    = State::Error;
        g_demoStep = -1;
        setExpr(Expression::Sad);
        setSpeech("setAngle fail");
        return;
    }

    g_demoNextMs = now + step.holdMs;
    g_demoStep++;
    if (g_demoStep >= static_cast<int>(SWEEP_LEN)) {
        // 最後のステップを発火し終えた → hold 時間経過後に Idle へ
        g_demoStep = -1;
    }
}

void loop() {
    M5.update();
    const uint32_t now = millis();

    auto touch = M5.Touch.getDetail();
    auto cmd = readSerialCmd();
    bool goTrigger   = M5.BtnA.wasPressed() || touch.wasPressed() || cmd.go;
    bool stopTrigger = M5.BtnB.wasPressed() || cmd.stop;

    if (stopTrigger) {
        emergencyOff();
    } else if (goTrigger && g_state != State::Demo) {
        startDemo();
    }

    tickDemo(now);

    // Demo 終了 (最後の hold が経過) → Idle 遷移
    if (g_state == State::Demo && g_demoStep < 0 && now >= g_demoNextMs) {
        g_state = State::Idle;
        setExpr(Expression::Sleepy);
        setSpeech("demo done");
    }

    // 200ms ごとに live yaw raw/deg を Serial に出力
    static uint32_t lastLogMs = 0;
    if (now - lastLogMs >= 200) {
        lastLogMs = now;
        int16_t yawRaw = servo.readPos(SERVO_ID_YAW);
        if (yawRaw >= 0) {
            float yawDeg = (yawRaw - servo.getZeroYaw()) * DEG_PER_STEP;
            Serial.printf("[setangle] yaw raw=%4d deg=%+7.2f torque=%s state=%d\n",
                          yawRaw, yawDeg, g_yawTorqueOn ? "ON" : "OFF",
                          static_cast<int>(g_state));
        }
    }

    delay(20);
}
