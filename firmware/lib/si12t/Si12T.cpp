// SPDX-FileCopyrightText: 2026 karaage0703
// SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
// SPDX-License-Identifier: MIT

#include "Si12T.h"

#include <M5Unified.h>

namespace {
// Si12T register map (M5Stack 公式 BSP の hal/drivers/Si12T/Si12T.h 由来)
constexpr uint8_t REG_SENS1   = 0x02;
constexpr uint8_t REG_SENS2   = 0x03;
constexpr uint8_t REG_SENS3   = 0x04;
constexpr uint8_t REG_SENS4   = 0x05;
constexpr uint8_t REG_SENS5   = 0x06;
constexpr uint8_t REG_CTRL1   = 0x08;
constexpr uint8_t REG_CTRL2   = 0x09;
constexpr uint8_t REG_REF_RST1 = 0x0A;
constexpr uint8_t REG_REF_RST2 = 0x0B;
constexpr uint8_t REG_CH_HOLD1 = 0x0C;
constexpr uint8_t REG_CH_HOLD2 = 0x0D;
constexpr uint8_t REG_CAL_HOLD1 = 0x0E;
constexpr uint8_t REG_CAL_HOLD2 = 0x0F;
constexpr uint8_t REG_OUTPUT1   = 0x10;

constexpr uint8_t TOUCH_INTENSITY_THRESHOLD = 1;
}  // namespace

Si12T::Si12T(uint8_t i2c_addr) : addr_(i2c_addr) {}

bool Si12T::writeReg(uint8_t reg, uint8_t value) {
    // M5.In_I2C は M5Unified が CoreS3 内部 I2C (SDA=12/SCL=11) を提供する経路。
    return M5.In_I2C.writeRegister8(addr_, reg, value, 100000);
}

bool Si12T::readReg(uint8_t reg, uint8_t* out) {
    if (out == nullptr) return false;
    uint8_t v = M5.In_I2C.readRegister8(addr_, reg, 100000);
    // readRegister8 は失敗時も 0 を返すので、bus 上で device が応答するかを別途 ping
    // で確認する (begin 時のみ)。runtime polling では値だけ採用。
    *out = v;
    return true;
}

bool Si12T::enableChannel() {
    bool ok = true;
    ok &= writeReg(REG_REF_RST1, 0x00);
    ok &= writeReg(REG_REF_RST2, 0x00);
    ok &= writeReg(REG_CH_HOLD1, 0x00);
    ok &= writeReg(REG_CH_HOLD2, 0x00);
    ok &= writeReg(REG_CAL_HOLD1, 0x00);
    ok &= writeReg(REG_CAL_HOLD2, 0x00);
    return ok;
}

bool Si12T::setCtrl1() {
    // Auto Mode, FTC=01, Interrupt(Middle,High), Response 4 (2+2)
    return writeReg(REG_CTRL1, 0x22);
}

bool Si12T::setCtrl2() {
    // begin() で先に呼び出されているので外部からは未使用。互換のため残す。
    bool ok = true;
    ok &= writeReg(REG_CTRL2, 0x0F);
    ok &= writeReg(REG_CTRL2, 0x07);
    return ok;
}

bool Si12T::setSensitivity(SensitivityType type, SensitivityLevel level) {
    uint8_t value = 0x00;
    if (type == SensitivityType::High) {
        switch (level) {
            case SensitivityLevel::L0: value = 0x88; break;
            case SensitivityLevel::L1: value = 0x99; break;
            case SensitivityLevel::L2: value = 0xAA; break;
            case SensitivityLevel::L3: value = 0xBB; break;
            case SensitivityLevel::L4: value = 0xCC; break;
            case SensitivityLevel::L5: value = 0xDD; break;
            case SensitivityLevel::L6: value = 0xEE; break;
            case SensitivityLevel::L7: value = 0xFF; break;
            default: return false;
        }
    } else {
        switch (level) {
            case SensitivityLevel::L0: value = 0x00; break;
            case SensitivityLevel::L1: value = 0x11; break;
            case SensitivityLevel::L2: value = 0x22; break;
            case SensitivityLevel::L3: value = 0x33; break;
            case SensitivityLevel::L4: value = 0x44; break;
            case SensitivityLevel::L5: value = 0x55; break;
            case SensitivityLevel::L6: value = 0x66; break;
            case SensitivityLevel::L7: value = 0x77; break;
            default: return false;
        }
    }

    bool ok = true;
    ok &= writeReg(REG_SENS1, value);
    ok &= writeReg(REG_SENS2, value);
    ok &= writeReg(REG_SENS3, value);
    ok &= writeReg(REG_SENS4, value);
    ok &= writeReg(REG_SENS5, value);
    return ok;
}

bool Si12T::begin(SensitivityType sens_type, SensitivityLevel sens_level) {
    // 事前に M5.In_I2C.begin() が走っている前提 (M5.begin() で自動)。device 存在
    // チェックは独立した ping API が無いので、最初の CTRL2 リセット書き込みの
    // NACK で判定する (writeRegister8 は ack 失敗で false を返す)。これで
    // CoreS3 単体機 (StackChan body 無し) では bus 上に device が居らず false で
    // 抜けて graceful degradation できる。
    if (!writeReg(REG_CTRL2, 0x0F)) {  // S/W Reset Enable, Sleep Mode Enable
        ready_ = false;
        return false;
    }
    if (!writeReg(REG_CTRL2, 0x07)) {  // Sleep Mode 解除
        ready_ = false;
        return false;
    }

    bool ok = true;
    ok &= enableChannel();
    ok &= setCtrl1();
    ok &= setSensitivity(sens_type, sens_level);
    ready_ = ok;
    return ok;
}

bool Si12T::readTouchResult(uint8_t* out) {
    return readReg(REG_OUTPUT1, out);
}

void Si12T::parseTouchResult(uint8_t raw, uint8_t out[3]) {
    // 下位 6 bit を 2 bit ずつ 3 ch に分解。各 ch は 0..3 の強度。
    int idx = 0;
    for (int j = 0; j < 6; j += 2) {
        out[idx] = (raw >> j) & 0x03;
        ++idx;
    }
}

int16_t Si12T::getPosition() const {
    uint16_t total = intensity_[0] + intensity_[1] + intensity_[2];
    if (total == 0) return 0;
    int32_t weighted = static_cast<int32_t>(intensity_[0]) * (-100) +
                       static_cast<int32_t>(intensity_[1]) * 0 +
                       static_cast<int32_t>(intensity_[2]) * 100;
    return static_cast<int16_t>(weighted / total);
}

void Si12T::getIntensity(uint8_t out[3]) const {
    out[0] = intensity_[0];
    out[1] = intensity_[1];
    out[2] = intensity_[2];
}

Si12T::Gesture Si12T::poll() {
    if (!ready_) return Gesture::None;

    uint8_t raw = 0;
    if (!readTouchResult(&raw)) return Gesture::None;
    parseTouchResult(raw, intensity_);

    uint8_t max_intensity = intensity_[0];
    if (intensity_[1] > max_intensity) max_intensity = intensity_[1];
    if (intensity_[2] > max_intensity) max_intensity = intensity_[2];

    const bool touched = max_intensity >= TOUCH_INTENSITY_THRESHOLD;
    Gesture gesture = Gesture::None;

    switch (state_) {
        case TouchState::Idle:
            if (touched) {
                state_            = TouchState::Touched;
                initial_position_ = getPosition();
                gesture           = Gesture::Press;
            }
            break;
        case TouchState::Touched:
            if (!touched) {
                state_  = TouchState::Idle;
                gesture = Gesture::Release;
            } else {
                int16_t delta = getPosition() - initial_position_;
                if (delta > swipe_threshold_) {
                    state_  = TouchState::Swiping;
                    gesture = Gesture::SwipeForward;
                } else if (delta < -swipe_threshold_) {
                    state_  = TouchState::Swiping;
                    gesture = Gesture::SwipeBackward;
                }
            }
            break;
        case TouchState::Swiping:
            if (!touched) {
                state_  = TouchState::Idle;
                gesture = Gesture::Release;
            }
            break;
    }
    return gesture;
}
