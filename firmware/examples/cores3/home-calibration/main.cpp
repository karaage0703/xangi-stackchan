// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// firmware/examples/cores3/home-calibration:
//   K151 のサーボ ゼロ点 (= 真正面・水平の物理姿勢) を ESP32 NVS に保存する。
//
// 設計:
//   SCSCL シリーズは「物理現在位置を中央として永続記録する」キャリブ機能を
//   持たない (元祖 stack-chan/scservo.ts:252 の `@note SCS series does not
//   have zero position calibration function. The offset value should be
//   handled by the application.` および FTServo_Arduino の examples/SCSCL/
//   に CalibrationOfs.ino が無いことが根拠)。よって ホスト側 (ESP32 NVS)
//   で zero raw を保持する D 案を採用する。
//
//   通常運用ファーム (src/main.cpp サーボ通信疎通モード以降) は起動時に NVS から読んで
//   servo.setZero*() に流し、setAngle*() が zero ベースで動くようにする。
//
// 動作:
//   起動時は torque OFF (手で動かせる)。LCD は 200ms ごとに yaw / pitch
//   の生 raw 値と (現状の zero に対する) 換算角度を表示。手で「真正面・
//   水平」の姿勢に向けてから calibrate トリガを引くと、現在の raw を NVS
//   namespace "xstackchan" に "yaw_zero" / "pitch_zero" として保存する。
//
// キャリブ実行のトリガは 3 系統 (CoreS3 では M5.BtnA が無効なため、画面
// タッチ + USB シリアル 'c' コマンドでもトリガできるようにする):
//   1. BtnA (デバイスによっては効く、保険)
//   2. 画面 LCD のどこかをタップ (M5.Touch、CoreS3 のメインルート)
//   3. USB シリアルに "c\n" を送る (PC からのリモートトリガ)
// BtnB は表示の即時リフレッシュ、BtnC は誤爆防止で未割当。
//
// NVS 永続化: ESP32 内蔵 flash の NVS partition に保存される。ファームを
// 別 env に焼き直しても (Partition Table が同じ限り) zero 値は残る。
// 別の物理位置でもう一度 calibrate すれば上書きされる。

#include <M5Unified.h>
#include <Preferences.h>

#include "SCServo.h"

using namespace scservo;

constexpr int8_t SERVO_RX_PIN = 7;  // G7
constexpr int8_t SERVO_TX_PIN = 6;  // G6

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

SCServo servo(Serial1, SERVO_RX_PIN, SERVO_TX_PIN);

// NVS namespace は 15 文字以内 (ESP32 NVS 制約)。"xstackchan" = 10 文字。
// キーは "yaw_zero" / "pitch_zero"。safe-startup / 通常運用ファームと共有する。
constexpr const char* NVS_NAMESPACE = "xstackchan";
constexpr const char* NVS_KEY_YAW_ZERO   = "yaw_zero";
constexpr const char* NVS_KEY_PITCH_ZERO = "pitch_zero";

enum class Status { Idle, Calibrating, Done, Error };
static Status   g_status        = Status::Idle;
static uint32_t g_statusUntilMs = 0;
static const char* g_statusMsg  = "";

void drawHeader() {
    M5.Display.fillRect(0, 0, 320, 24, TFT_NAVY);
    M5.Display.setTextColor(TFT_WHITE, TFT_NAVY);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(8, 4);
    M5.Display.print("home-calibration");
}

void drawFooter() {
    M5.Display.fillRect(0, 200, 320, 40, TFT_DARKGREY);
    M5.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 208);
    M5.Display.print("TAP screen / BtnA / serial 'c': calibrate");
    M5.Display.setCursor(8, 222);
    M5.Display.print("hold pose forward+level then trigger");
}

void drawAngles(int16_t yawRaw, int16_t pitchRaw, float yawDeg, float pitchDeg) {
    M5.Display.fillRect(0, 28, 320, 96, TFT_BLACK);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setTextSize(2);

    M5.Display.setCursor(8, 36);
    if (yawRaw < 0) {
        M5.Display.print("yaw   :   ----  (raw ----)");
    } else {
        M5.Display.printf("yaw   :%+7.2f  (raw %4d)", yawDeg, yawRaw);
    }
    M5.Display.setCursor(8, 64);
    if (pitchRaw < 0) {
        M5.Display.print("pitch :   ----  (raw ----)");
    } else {
        M5.Display.printf("pitch :%+7.2f  (raw %4d)", pitchDeg, pitchRaw);
    }

    M5.Display.setTextSize(1);
    M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    M5.Display.setCursor(8, 100);
    M5.Display.printf("zero now: yaw=%4d  pitch=%4d (NVS)",
                      servo.getZeroYaw(), servo.getZeroPitch());
}

void drawStatus() {
    M5.Display.fillRect(0, 128, 320, 64, TFT_BLACK);
    if (g_status == Status::Idle) return;

    uint16_t color = TFT_WHITE;
    switch (g_status) {
        case Status::Calibrating: color = TFT_YELLOW; break;
        case Status::Done:        color = TFT_GREEN;  break;
        case Status::Error:       color = TFT_RED;    break;
        default: break;
    }
    M5.Display.setTextColor(color, TFT_BLACK);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(8, 144);
    M5.Display.print(g_statusMsg);
}

void setStatus(Status s, const char* msg, uint32_t holdMs = 0) {
    g_status        = s;
    g_statusMsg     = msg;
    g_statusUntilMs = (holdMs == 0) ? 0 : (millis() + holdMs);
    drawStatus();
    Serial.printf("[homecal] status=%d msg=%s\n", static_cast<int>(s), msg);
}

// 現在の readPos を NVS に zero として保存する (D 案、SCSCL は EEPROM 焼き
// 込みできないのでホスト側 flash で持つ)。
void runCalibration() {
    setStatus(Status::Calibrating, "reading current pos...");
    int16_t yawRaw   = servo.readPos(SERVO_ID_YAW);
    int16_t pitchRaw = servo.readPos(SERVO_ID_PITCH);
    if (yawRaw < 0 || pitchRaw < 0) {
        Serial.printf("[homecal] readPos failed: yaw=%d pitch=%d\n", yawRaw, pitchRaw);
        setStatus(Status::Error, "readPos failed", 4000);
        return;
    }

    setStatus(Status::Calibrating, "writing NVS...");
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false /* RW */)) {
        setStatus(Status::Error, "NVS begin failed", 4000);
        return;
    }
    bool ok = true;
    ok &= (prefs.putShort(NVS_KEY_YAW_ZERO,   yawRaw)   == sizeof(int16_t));
    ok &= (prefs.putShort(NVS_KEY_PITCH_ZERO, pitchRaw) == sizeof(int16_t));
    prefs.end();
    if (!ok) {
        setStatus(Status::Error, "NVS write failed", 4000);
        return;
    }

    // メモリ上の servo にも即時反映 (再起動なしで角度計算が新 zero に追従)
    servo.setZeroYaw(yawRaw);
    servo.setZeroPitch(pitchRaw);

    Serial.printf("[homecal] saved to NVS: yaw_zero=%d pitch_zero=%d\n",
                  yawRaw, pitchRaw);
    setStatus(Status::Done, "saved to NVS!", 4000);
}

// 起動時に NVS から zero を読んで servo に反映する。未設定なら DEFAULT_ZERO_RAW
// (= 512) のまま安全側で動く。
void loadZeroFromNvs() {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true /* RO */)) {
        Serial.println("[homecal] NVS namespace not yet exists, using default zero=512");
        return;
    }
    int16_t yawZero   = prefs.getShort(NVS_KEY_YAW_ZERO,   DEFAULT_ZERO_RAW);
    int16_t pitchZero = prefs.getShort(NVS_KEY_PITCH_ZERO, DEFAULT_ZERO_RAW);
    prefs.end();

    servo.setZeroYaw(yawZero);
    servo.setZeroPitch(pitchZero);
    Serial.printf("[homecal] loaded zero from NVS: yaw=%d pitch=%d\n", yawZero, pitchZero);
}

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(128);
    M5.Display.fillScreen(TFT_BLACK);

    Serial.begin(115200);
    delay(100);
    Serial.println();
    Serial.println("[homecal] xangi-stackchan-dev / k151 home-calibration");

    drawHeader();
    drawFooter();
    drawAngles(-1, -1, NAN, NAN);

    if (!py32::enableServoPower()) {
        setStatus(Status::Error, "VM rail OFF", 0);
        return;
    }
    if (!servo.begin()) {
        setStatus(Status::Error, "uart fail", 0);
        return;
    }

    // 起動直後は torque OFF (手で動かせる状態)
    for (uint8_t i = 0; i < 3; i++) {
        servo.enableTorque(SERVO_ID_YAW, false);
        delay(20);
        servo.enableTorque(SERVO_ID_PITCH, false);
        delay(40);
    }

    // 既存の zero (前回キャリブ結果) を NVS から復元。表示と再キャリブ判断に使う
    loadZeroFromNvs();

    Serial.println("[homecal] ready, torque OFF, free to move");
}

// Serial に 'c' (or 'C') が来たらトリガ。改行は捨てる。
bool serialCalibrateRequested() {
    bool req = false;
    while (Serial.available()) {
        int c = Serial.read();
        if (c == 'c' || c == 'C') req = true;
    }
    return req;
}

void loop() {
    M5.update();

    static uint32_t lastReadMs   = 0;
    static uint32_t lastTriggerMs = 0;  // 連打防止
    const uint32_t now = millis();

    auto touch = M5.Touch.getDetail();
    bool triggered =
        M5.BtnA.wasPressed()
        || touch.wasPressed()
        || serialCalibrateRequested();

    if (triggered && (now - lastTriggerMs > 4000) && g_status != Status::Calibrating) {
        lastTriggerMs = now;
        runCalibration();
    }
    if (M5.BtnB.wasPressed()) {
        lastReadMs = 0;
    }

    if (g_statusUntilMs != 0 && now >= g_statusUntilMs) {
        g_statusUntilMs = 0;
        g_status        = Status::Idle;
        drawStatus();
    }

    if (now - lastReadMs >= 200) {
        lastReadMs = now;
        int16_t yawRaw   = servo.readPos(SERVO_ID_YAW);
        int16_t pitchRaw = servo.readPos(SERVO_ID_PITCH);
        // 表示の deg は **現在 NVS に保存されている zero** を基準にした角度。
        // calibrate 直後はこれが ~0 になるはず (ドリフトがない限り)
        float yawDeg   = (yawRaw   >= 0) ? ((yawRaw   - servo.getZeroYaw())   * DEG_PER_STEP) : NAN;
        float pitchDeg = (pitchRaw >= 0) ? ((pitchRaw - servo.getZeroPitch()) * DEG_PER_STEP) : NAN;
        drawAngles(yawRaw, pitchRaw, yawDeg, pitchDeg);
    }

    delay(20);
}
