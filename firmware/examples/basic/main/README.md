# RTBridge ファーム

アールティ Ver.β スタックチャン (M5Stack Basic + Feetech SCS0009 ×2、2023年7月発売) 用の
XangiBridge プロトコル互換ファーム。

## 仕様

- 対象ハード: M5Stack Basic (ESP32 旧世代、PSRAM 無し、Flash 4MB、SRAM 320KB)
- サーボ: Feetech SCS0009 ×2 (別 PR で統合予定、現バージョンは MOVE は unavailable)
- カメラ: 非搭載 (CAPTURE は unavailable 固定)
- マイク: M5Stack Basic は内蔵マイク無し → AI 用途は外部 PC (xangi) 連携前提
- 音声出力: M5Stack Basic 内蔵スピーカー (M5Unified の M5.Speaker)
- Serial: 115200 baud (USB-Serial CP2104)

## XangiBridge との差分

| 項目 | XangiBridge (CoreS3) | RTBridge (M5Stack Basic) |
|---|---|---|
| WAV バッファ | PSRAM `ps_malloc`、4MB 上限 | 内部 DRAM `malloc`、96KB 上限 |
| WAV キュー | 4 slot ring buffer + 別 core task | キュー無し同期再生 |
| Serial baud | 921600 | 115200 |
| カメラ | GC0308 内蔵 (CAPTURE 有効) | なし (CAPTURE unavailable 固定) |
| Avatar | M5Stack-Avatar 有効 | M5Stack-Avatar 有効 |
| FACE | 6 表情 | 6 表情 |
| MOVE | SCServo SCS (PORT C、統合済み) | SCS0009 (未統合、別 PR) |

## flash 手順

```bash
cd firmware
pio run -e basic-main -t upload
# シリアルモニター
pio device monitor -e basic-main
```

## 動作確認

ホスト PC で xangi-stackchan のテストスクリプトを実行する。

```bash
# 自動検出
uv run python scripts/test_rt_bridge.py

# 明示ポート指定
uv run python scripts/test_rt_bridge.py --port /dev/ttyACM0 --baud 115200
```

期待する応答:

- STATUS → `version=rt-bridge-0.2.0-wav`, `servo=false`, `camera=false`
- VOLUME → `ok`
- FACE (happy/sad/doubt/sleepy/neutral) → `ok`、LCD の Avatar 表情変化を目視確認
- MOVE → `unavailable` (SCServo 統合は別 PR で対応予定)
- CAPTURE → `unavailable` (カメラ非搭載)
- WAV (440Hz 1.5秒) → `ok`、内蔵スピーカーからトーン音 + Avatar 口パク連動を確認

## xangi 連動

`xangi-stackchan` 経由で xangi の TTS 出力を再生:

```bash
uv run xangi-stackchan --port /dev/ttyACM0 --baud 115200 \
    --xangi-url <xangi の URL> --thread-id <thread>
```

注意: xangi の TTS 出力が **96KB を超える長文を 1 WAV** で送ると RTBridge が
`size exceeds MAX_WAV_BYTES` を返してその WAV は再生されない。
16kHz/mono/16bit で約 3 秒分が上限。長い発話は xangi 側で分割する想定。

## 制約と既知の問題

- 現バージョン: STATUS / VOLUME / FACE / WAV のみ動作。MOVE と CAPTURE は仕様上 unavailable
- 別 PR で SCServo SCS0009 統合 (Stack-chan PCB の配線実機確認後)
- Serial baud 115200 は M5Stack Basic の標準。921600 で安定するなら将来引き上げ検討
- WAV 96KB 制約 = M5Stack Basic 内部 DRAM 320KB の安全上限。さらに長い WAV が必要なら
  ストリーミング再生 (バッファチャンク循環) を別途設計する必要あり
