// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-License-Identifier: MIT

#include "SCServo.h"

#include <math.h>

namespace scservo {

namespace {
inline uint8_t lo(int16_t v) { return static_cast<uint8_t>(v & 0xFF); }
inline uint8_t hi(int16_t v) { return static_cast<uint8_t>((v >> 8) & 0xFF); }
}  // namespace

SCServo::SCServo(HardwareSerial& serial,
                           int8_t rxPin, int8_t txPin,
                           uint32_t baud)
    : _serial(serial), _rxPin(rxPin), _txPin(txPin), _baud(baud) {}

bool SCServo::begin() {
    // CoreS3 の UART1: TX=G6, RX=G7 (docs §1)
    _serial.begin(_baud, SERIAL_8N1, _rxPin, _txPin);
    // 念のため受信バッファをフラッシュ
    while (_serial.available()) {
        _serial.read();
    }
    return true;
}

void SCServo::end() {
    _serial.end();
}

uint8_t SCServo::checksum(const uint8_t* buf, size_t length) {
    // HEADER 2 byte をスキップして ID 以降の和を取り、ビット反転 (docs §6)
    uint16_t sum = 0;
    for (size_t i = 2; i < length; i++) {
        sum += buf[i];
    }
    return static_cast<uint8_t>(~(sum & 0xFF));
}

bool SCServo::sendCommand(uint8_t id, uint8_t command, uint8_t address,
                               const uint8_t* data, size_t dataLen) {
    // パケット最大長: HEADER(2) + ID(1) + LEN(1) + CMD(1) + ADDR(1) + data + CHECKSUM(1)
    if (dataLen + 7 > BUF_SIZE) {
        return false;
    }

    _txBuf[0] = HEADER_BYTE;
    _txBuf[1] = HEADER_BYTE;
    _txBuf[2] = id;
    // LEN = data + 3 (CMD/ADDR/CHECKSUM の 3 byte 分、docs §7.1)
    _txBuf[3] = static_cast<uint8_t>(dataLen + 3);
    _txBuf[4] = command;
    _txBuf[5] = address;
    for (size_t i = 0; i < dataLen; i++) {
        _txBuf[6 + i] = data[i];
    }
    size_t csIdx = 6 + dataLen;
    _txBuf[csIdx] = checksum(_txBuf, csIdx);
    size_t totalLen = csIdx + 1;

    // 送信前に受信バッファをクリア (前回の応答残りを掃く)
    while (_serial.available()) {
        _serial.read();
    }

    size_t written = _serial.write(_txBuf, totalLen);
    _serial.flush();

    if (_debug) {
        Serial.printf("[scservo] TX id=%u cmd=0x%02X addr=%u len=%u: ",
                      id, command, address, (unsigned)totalLen);
        for (size_t i = 0; i < totalLen; i++) {
            Serial.printf("%02X ", _txBuf[i]);
        }
        Serial.println();
    }

    return written == totalLen;
}

bool SCServo::readResponse(uint8_t* out, size_t length, uint32_t timeoutMs) {
    uint32_t start = millis();
    size_t read = 0;
    while (read < length) {
        if (_serial.available()) {
            int c = _serial.read();
            if (c < 0) {
                continue;
            }
            out[read++] = static_cast<uint8_t>(c);
        } else if (millis() - start > timeoutMs) {
            if (_debug) {
                Serial.printf("[scservo] RX timeout (got %u/%u bytes)\n",
                              (unsigned)read, (unsigned)length);
            }
            return false;
        }
    }
    if (_debug) {
        Serial.print("[scservo] RX: ");
        for (size_t i = 0; i < length; i++) {
            Serial.printf("%02X ", out[i]);
        }
        Serial.println();
    }
    return true;
}

bool SCServo::writePos(uint8_t id, int16_t position, uint16_t goalTimeMs) {
    if (position < 0) position = 0;
    if (position > POSITION_MAX) position = POSITION_MAX;

    // SCSCL シリーズは 2 byte レジスタを Big Endian で並べる (low address に
    // High byte、high address に Low byte)。データシートの `*_L` / `*_H` は
    // 「Low/High バイト」ではなく「Low/High アドレス」を意味する。Feetech 公式
    // FTServo_Arduino (MIT) の SCS::Host2SCS で End=1 のとき DataL=High。
    if (goalTimeMs == 0) {
        uint8_t data[2] = { hi(position), lo(position) };
        return sendCommand(id, CMD_WRITE, ADDR_GOAL_POSITION, data, sizeof(data));
    }
    // GOAL_POSITION (2 byte) + GOAL_TIME (2 byte) を連続書き、いずれも BE
    uint8_t data[4] = {
        hi(position), lo(position),
        hi(static_cast<int16_t>(goalTimeMs)), lo(static_cast<int16_t>(goalTimeMs)),
    };
    return sendCommand(id, CMD_WRITE, ADDR_GOAL_POSITION, data, sizeof(data));
}

int16_t SCServo::readPos(uint8_t id) {
    uint8_t reqLen = 2;  // 2 byte だけ読む (docs §5 の最小 read)
    if (!sendCommand(id, CMD_READ, ADDR_PRESENT_POSITION, &reqLen, 1)) {
        return -1;
    }
    // 応答パケット: HEADER(2) + ID(1) + LEN(1) + ERROR(1) + DATA(2) + CHECKSUM(1) = 8 byte
    uint8_t resp[8];
    if (!readResponse(resp, sizeof(resp))) {
        return -1;
    }
    if (resp[0] != HEADER_BYTE || resp[1] != HEADER_BYTE) {
        if (_debug) Serial.println("[scservo] readPos: bad header");
        return -1;
    }
    if (resp[2] != id) {
        if (_debug) Serial.printf("[scservo] readPos: id mismatch %u\n", resp[2]);
        return -1;
    }
    // PRESENT_POSITION 応答もビッグエンディアン (resp[5]=high, resp[6]=low)。
    // SCSCL は読み書き両方 BE (writePos のコメント参照)。
    int16_t pos = (static_cast<int16_t>(resp[5]) << 8) | static_cast<int16_t>(resp[6]);
    return pos;
}

bool SCServo::enableTorque(uint8_t id, bool enable) {
    uint8_t data[1] = { enable ? uint8_t(1) : uint8_t(0) };
    return sendCommand(id, CMD_WRITE, ADDR_TORQUE_ENABLE, data, sizeof(data));
}

bool SCServo::setAngleClamped(uint8_t id, int16_t zeroRaw, float angleDeg,
                                   uint16_t goalTimeMs,
                                   float safeMin, float safeMax,
                                   float physMin, float physMax,
                                   const char* axisName) {
    // 1) 運用安全マージン clamp
    float clamped = constrain(angleDeg, safeMin, safeMax);
    // 2) 物理限界 clamp (二重防御、絶対外れない)
    clamped = constrain(clamped, physMin, physMax);

    if (fabsf(clamped - angleDeg) > 0.01f) {
        Serial.printf("[scservo] %s %.2f -> clamped to %.2f\n",
                      axisName, angleDeg, clamped);
    }

    // angle (degree) → raw step。zero raw が個体差を吸収する。
    int32_t delta = static_cast<int32_t>(roundf(clamped * STEP_PER_DEG));
    int32_t raw   = static_cast<int32_t>(zeroRaw) + delta;
    if (raw < 0) raw = 0;
    if (raw > POSITION_MAX) raw = POSITION_MAX;

    if (_debug) {
        Serial.printf("[scservo] %s setAngle %.2f deg -> raw %ld (zero=%d, goalTime=%u)\n",
                      axisName, clamped, (long)raw, zeroRaw, goalTimeMs);
    }
    return writePos(id, static_cast<int16_t>(raw), goalTimeMs);
}

bool SCServo::setAngleYaw(float angleDeg, uint16_t goalTimeMs) {
    return setAngleClamped(SERVO_ID_YAW, _zeroYaw, angleDeg, goalTimeMs,
                           YAW_SAFE_MIN_DEG, YAW_SAFE_MAX_DEG,
                           YAW_PHYS_MIN_DEG, YAW_PHYS_MAX_DEG,
                           "yaw");
}

bool SCServo::setAnglePitch(float angleDeg, uint16_t goalTimeMs) {
    return setAngleClamped(SERVO_ID_PITCH, _zeroPitch, angleDeg, goalTimeMs,
                           PITCH_SAFE_MIN_DEG, PITCH_SAFE_MAX_DEG,
                           PITCH_PHYS_MIN_DEG, PITCH_PHYS_MAX_DEG,
                           "pitch");
}

float SCServo::getAngleYaw() {
    int16_t pos = readPos(SERVO_ID_YAW);
    if (pos < 0) return NAN;
    return static_cast<float>(pos - _zeroYaw) * DEG_PER_STEP;
}

float SCServo::getAnglePitch() {
    int16_t pos = readPos(SERVO_ID_PITCH);
    if (pos < 0) return NAN;
    return static_cast<float>(pos - _zeroPitch) * DEG_PER_STEP;
}

}  // namespace scservo
