// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT
//
// Feetech SCS シリアルバスサーボ用 Arduino ライブラリ。
// プロトコル仕様は docs/scservo_protocol.md 参照。
//
// 重要: SCSCL シリーズ (K151 のサーボ) は **物理現在位置を中央として永続記録
// するキャリブレーション機能を持たない** (元祖 stack-chan/scservo.ts:252 の
// 開発者コメント `@note SCS series does not have zero position calibration
// function. The offset value should be handled by the application.` および
// FTServo_Arduino の examples/SCSCL/ に CalibrationOfs.ino が無いことが根拠)。
// このため SCServo クラスは **ホスト側 (ESP32 NVS) で zero raw を保持し、
// setAngle*() で zero ベース計算する設計**。サーボ EEPROM の OFFSET / Calib
// 系は SCSCL では未定義/未対応なので一切触らない。

#pragma once

#include <Arduino.h>
#include <HardwareSerial.h>

namespace scservo {

// プロトコル定数 (docs/scservo_protocol.md §3, §4, §5)
constexpr uint8_t HEADER_BYTE      = 0xFF;
constexpr uint8_t BROADCAST_ID     = 0xFE;

constexpr uint8_t CMD_READ         = 0x02;
constexpr uint8_t CMD_WRITE        = 0x03;

// SCSCL のレジスタアドレス (FTServo_Arduino src/SCSCL.h で確認)。
// ADDR_OFFSET (31) は **SCSCL のメモリマップに存在しない** ので定義しない。
constexpr uint8_t ADDR_ID                = 5;
constexpr uint8_t ADDR_TORQUE_ENABLE     = 40;
constexpr uint8_t ADDR_GOAL_ACC          = 41;
constexpr uint8_t ADDR_GOAL_POSITION     = 42;
constexpr uint8_t ADDR_GOAL_TIME         = 44;
constexpr uint8_t ADDR_LOCK              = 48;
constexpr uint8_t ADDR_PRESENT_POSITION  = 56;

// K151 のサーボ ID 割当 (docs/scservo_protocol.md §1)
constexpr uint8_t SERVO_ID_YAW   = 1;
constexpr uint8_t SERVO_ID_PITCH = 2;

// 物理可動域 / 運用既定 clamp は **zero ベース角度** (HomeCalibration で保存した
// zero raw を 0° と見なした相対角度) で定義する。setAngleClamped() がこの範囲で
// constrain した後 raw = zero + delta * STEP_PER_DEG で書き込むため、constexpr 値も
// zero ベースで揃える必要がある (絶対角と混在させると `setAnglePitch(0)` が
// 中央に来ない)。詳細は docs/scservo_protocol.md §11.1〜§11.3 参照。
//
// pitch :
//   M5Stack 公式 K151 の絶対物理可動域は 5°〜85° (推奨)、超過で permanent damage。
//   幅 80° の中央 (≈45° absolute) を「水平」として HomeCalibration で零点に取る前提で、
//   絶対角からの delta = ±40° を物理上限の見立てとし、安全マージン 10° を入れた
//   ±30° を運用既定とする。零点を中央以外に取ると片側で先に物理ストッパに当たるので、
//   その場合は HomeCalibration 取り直しを推奨。
// yaw :
//   BSP utils/motion/motion.h 観測値 (公式未確認の保守値) ±128° を物理上限とし、
//   ±100° を運用既定とする。zero=512 デフォルトでも HomeCalibration 後でも、
//   yaw は機構的に左右対称なので zero ベース ±100° / ±128° で問題ない。
constexpr float YAW_PHYS_MIN_DEG   = -128.0f;
constexpr float YAW_PHYS_MAX_DEG   = +128.0f;
constexpr float PITCH_PHYS_MIN_DEG =  -40.0f;
constexpr float PITCH_PHYS_MAX_DEG =  +40.0f;

constexpr float YAW_SAFE_MIN_DEG   = -100.0f;
constexpr float YAW_SAFE_MAX_DEG   = +100.0f;
constexpr float PITCH_SAFE_MIN_DEG =  -30.0f;
constexpr float PITCH_SAFE_MAX_DEG =  +30.0f;

// 角度⇔生位置の換算 (10-bit 0..1023 で 0..200°、1 step ≒ 0.195°)
constexpr int16_t POSITION_MAX = 0x03FF;
constexpr float DEG_PER_STEP   = 200.0f / 1024.0f;
constexpr float STEP_PER_DEG   = 1024.0f / 200.0f;

// zero raw のデフォルト (NVS 未設定時)。SCSCL の中央 raw 512 を初期値とする
constexpr int16_t DEFAULT_ZERO_RAW = 512;

class SCServo {
public:
    SCServo(HardwareSerial& serial,
                 int8_t rxPin, int8_t txPin,
                 uint32_t baud = 1000000);

    // UART 開始。begin() 後の torque は OFF のまま (docs §11.4 のシーケンス遵守)
    bool begin();
    void end();

    // 低レベル API (docs §7)
    bool writePos(uint8_t id, int16_t position, uint16_t goalTimeMs = 0);
    int16_t readPos(uint8_t id);  // 失敗時は -1
    bool enableTorque(uint8_t id, bool enable);

    // ホスト側で持つ zero raw を設定/取得。NVS に書き込むのは呼び出し側で。
    // setAngle*() / getAngle*() はこの zero を基準に計算する。
    void setZeroYaw(int16_t zeroRaw)   { _zeroYaw   = zeroRaw; }
    void setZeroPitch(int16_t zeroRaw) { _zeroPitch = zeroRaw; }
    int16_t getZeroYaw()   const { return _zeroYaw; }
    int16_t getZeroPitch() const { return _zeroPitch; }

    // 角度ベース API。zero raw を基準に target raw を計算して書き込む。
    bool setAngleYaw(float angleDeg, uint16_t goalTimeMs = 0);
    bool setAnglePitch(float angleDeg, uint16_t goalTimeMs = 0);

    // 現在角度 (-100°..+100° 程度、zero ベース)。失敗時は NAN
    float getAngleYaw();
    float getAnglePitch();

    // デバッグログ ON/OFF (Serial にパケット ASCII 出力)
    void setDebug(bool on) { _debug = on; }

private:
    HardwareSerial& _serial;
    int8_t _rxPin;
    int8_t _txPin;
    uint32_t _baud;
    bool _debug = false;

    int16_t _zeroYaw   = DEFAULT_ZERO_RAW;
    int16_t _zeroPitch = DEFAULT_ZERO_RAW;

    static constexpr size_t BUF_SIZE = 32;
    uint8_t _txBuf[BUF_SIZE];
    uint8_t _rxBuf[BUF_SIZE];

    // パケット組立て & 送信 (docs §6, §7)
    bool sendCommand(uint8_t id, uint8_t command, uint8_t address,
                     const uint8_t* data, size_t dataLen);
    // 応答待ち。timeoutMs 内に length バイト読めれば true
    bool readResponse(uint8_t* out, size_t length, uint32_t timeoutMs = 40);
    static uint8_t checksum(const uint8_t* buf, size_t length);

    // 角度 → raw (zero ベース、二重 clamp)
    bool setAngleClamped(uint8_t id, int16_t zeroRaw, float angleDeg,
                         uint16_t goalTimeMs,
                         float safeMin, float safeMax,
                         float physMin, float physMax,
                         const char* axisName);
};

}  // namespace scservo
