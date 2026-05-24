// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/cores3/safe-startup: サーボ電源系統と SCServo 通信を疎通確認するだけの安全起動。
//   1. PY32 IO Expander 経由でサーボバス電源 (VM) を ON
//   2. UART1 (1Mbps, TX=G6, RX=G7) を開く
//   3. 念のため torque OFF を投げて、サーボを手で動かせる状態にする
//   4. NVS (namespace "xstackchan") から zero raw を読んで servo に反映
//      (HomeCalibration ファームで保存される。未設定なら DEFAULT_ZERO_RAW=512)
//   5. readPos で現在位置を取得 (BE 並びで読む)、zero との差を Serial にログ
//   6. loop は表情切替だけで何もしない (サーボ touch なし、安全モード)
// 中央移動デモは別 PR で setAngle*() を呼ぶ形に進める (zero ベース計算)。

#include <M5Unified.h>
#include <Avatar.h>
#include <Preferences.h>

#include "SCServo.h"

using namespace m5avatar;
using namespace scservo;

// CoreS3 の UART1 ピン (docs §1)
constexpr int8_t SERVO_RX_PIN = 7;  // G7
constexpr int8_t SERVO_TX_PIN = 6;  // G6

// PY32 IO Expander 経由で K151 のサーボバス電源 (VM_EN, pin 0) を ON にする。
// レジスタ仕様は m5stack/StackChan-BSP の PY32IOExpander.cpp 参照。
namespace py32 {
constexpr uint8_t  I2C_ADDR         = 0x6F;
constexpr uint32_t I2C_FREQ         = 100000;
constexpr uint8_t  REG_VERSION      = 0x02;
constexpr uint8_t  REG_GPIO_DIR_L   = 0x03;  // direction (0=input, 1=output)
constexpr uint8_t  REG_GPIO_OUT_L   = 0x05;  // output level
constexpr uint8_t  REG_GPIO_PU_L    = 0x09;  // pull-up enable
constexpr uint8_t  SERVO_VM_EN_PIN  = 0;

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
    delay(200);  // VM rail 安定待ち
    Serial.printf("[py32] servo power %s\n", ok ? "ON" : "FAILED");
    return ok;
}
}  // namespace py32

// readPos リトライ回数とインターバル (サーボ電源 ON 直後は応答が安定するまでかかる)
constexpr uint8_t READ_POS_RETRIES = 8;
constexpr uint16_t READ_POS_RETRY_DELAY_MS = 80;

// HomeCalibration ファームと共有する NVS namespace / キー
constexpr const char* NVS_NAMESPACE      = "xstackchan";
constexpr const char* NVS_KEY_YAW_ZERO   = "yaw_zero";
constexpr const char* NVS_KEY_PITCH_ZERO = "pitch_zero";

Avatar avatar;
SCServo servo(Serial1, SERVO_RX_PIN, SERVO_TX_PIN);

// NVS から zero raw を読んで servo に反映する。未設定なら DEFAULT_ZERO_RAW (=512)
// のまま安全側で動く。
void loadZeroFromNvs() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true /* RO */)) {
        Serial.println("[main] NVS namespace not yet exists, zero=512 (run HomeCalibration first)");
        return;
    }
    int16_t yawZero   = prefs.getShort(NVS_KEY_YAW_ZERO,   DEFAULT_ZERO_RAW);
    int16_t pitchZero = prefs.getShort(NVS_KEY_PITCH_ZERO, DEFAULT_ZERO_RAW);
    prefs.end();

    servo.setZeroYaw(yawZero);
    servo.setZeroPitch(pitchZero);
    Serial.printf("[main] loaded zero from NVS: yaw=%d pitch=%d\n", yawZero, pitchZero);
}

const Expression EXPRESSIONS[] = {
    Expression::Neutral,
    Expression::Happy,
    Expression::Sad,
    Expression::Doubt,
    Expression::Sleepy,
};
constexpr size_t NUM_EXPR = sizeof(EXPRESSIONS) / sizeof(EXPRESSIONS[0]);

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);

    Serial.begin(115200);
    delay(100);
    Serial.println();
    Serial.println("[main] xangi-stackchan / cores3 safe-startup");

    avatar.init();
    avatar.setExpression(Expression::Neutral);
    avatar.setSpeechText("boot");

    if (!py32::enableServoPower()) {
        Serial.println("[main] FATAL: servo power not enabled");
        avatar.setSpeechText("vm fail");
        return;
    }

    if (!servo.begin()) {
        Serial.println("[main] FATAL: servo.begin failed");
        avatar.setSpeechText("uart fail");
        return;
    }
    Serial.println("[main] UART1 1Mbps opened (TX=G6, RX=G7)");
    servo.setDebug(true);  // setup 中だけ TX/RX を Serial に dump

    // 起動直後は念のため torque OFF にして手戻し可能状態にする (キャリブ前の安全モード)
    for (uint8_t i = 0; i < 3; i++) {
        servo.enableTorque(SERVO_ID_YAW, false);
        delay(20);
        servo.enableTorque(SERVO_ID_PITCH, false);
        delay(40);
    }

    // HomeCalibration で保存した zero を読み込み (未設定なら 512 デフォルト)
    loadZeroFromNvs();

    // 通信疎通確認 (BE で読む)
    int16_t yawPos = -1;
    int16_t pitchPos = -1;
    for (uint8_t i = 0; i < READ_POS_RETRIES; i++) {
        if (yawPos < 0)   yawPos   = servo.readPos(SERVO_ID_YAW);
        if (pitchPos < 0) pitchPos = servo.readPos(SERVO_ID_PITCH);
        if (yawPos >= 0 && pitchPos >= 0) break;
        delay(READ_POS_RETRY_DELAY_MS);
    }

    if (yawPos < 0 || pitchPos < 0) {
        Serial.printf("[main] readPos failed: yaw=%d pitch=%d\n", yawPos, pitchPos);
        avatar.setSpeechText("read fail");
        return;
    }

    // zero ベースの角度も併記 (現在の物理姿勢が NVS の zero からどれだけずれているか)
    float yawDeg   = (yawPos   - servo.getZeroYaw())   * DEG_PER_STEP;
    float pitchDeg = (pitchPos - servo.getZeroPitch()) * DEG_PER_STEP;
    Serial.printf("[main] ready, yaw=%d (%.2f deg) pitch=%d (%.2f deg) torque OFF\n",
                  yawPos, yawDeg, pitchPos, pitchDeg);
    avatar.setSpeechText("ready");
    servo.setDebug(false);
}

void loop() {
    // サーボには触らず Avatar 表情だけ巡回する (torque OFF のまま放置)
    static size_t exprIdx = 0;
    static uint32_t lastTick = 0;
    const uint32_t now = millis();

    if (now - lastTick >= 5000) {
        lastTick = now;
        avatar.setExpression(EXPRESSIONS[exprIdx]);
        exprIdx = (exprIdx + 1) % NUM_EXPR;
    }
    delay(50);
}
