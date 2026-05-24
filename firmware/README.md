# firmware

xangi-stackchan の Arduino (PlatformIO) ファーム群。xangi の SSE イベントを購読する USB シリアルブリッジ (Python 側 `xangi-stackchan` CLI) と連携する。

## 対応ハードウェアとファーム

ディレクトリは **SoC (M5Stack 機種)** で分かれている。各機種ディレクトリの `main/` が本体ファーム、ほかは初回セットアップ用デモ・キャリブレーション。

```
firmware/
├── platformio.ini             # 全 env 集約 (cores3-main / atoms3r-main / basic-main 等)
├── lib/scservo/               # Feetech SCServo Arduino C++ ライブラリ (Apache-2.0)
└── examples/
    ├── cores3/                # CoreS3 系 (K151 / K151-R / CoreS3 単体)
    │   ├── main/                  ← 本体ファーム (cores3-main env)
    │   ├── home-calibration/      ← サーボ EEPROM 中央位置を NVS に焼き込む
    │   ├── set-angle-demo/        ← サーボ ±30° スイープ動作確認
    │   ├── speaker-demo/          ← 内蔵スピーカー単独動作確認
    │   └── safe-startup/          ← サーボ電源・通信疎通の安全起動
    ├── atoms3r/               # AtomS3R + Atomic Voice/Echo Base
    │   └── main/                  ← 本体ファーム (atoms3r-main env)
    └── basic/                 # M5Stack Basic + アールティ Ver.β
        └── main/                  ← 本体ファーム (basic-main env)
```

| ファーム | PlatformIO env | 対象ハード | サーボ | カメラ | Serial baud |
|---|---|---|---|---|---|
| cores3/main | `cores3-main` | CoreS3 + K151 / K151-R / CoreS3 単体 | SCServo SCS (graceful degradation) | GC0308 内蔵 (graceful degradation) | 921600 |
| atoms3r/main | `atoms3r-main` | AtomS3R + Atomic Voice/Echo Base | なし (unavailable) | なし (unavailable) | 115200 |
| basic/main | `basic-main` | M5Stack Basic + アールティ Ver.β | SCServo SCS0009 ×2 | なし (unavailable) | 115200 |

## 共通シリアルプロトコル

`docs/xangi_bridge_protocol.md` 参照。全ファームで以下のコマンドを実装 (ハード未搭載のものは `unavailable` 応答で graceful degradation):

- `STATUS` → ステータス JSON (state / volume / version / servo / torque / camera / queued / playing)
- `VOLUME:<0-255>` → 音量変更
- `WAV:<size>` → `READY\n` 返してバイナリ受信、内蔵スピーカーで再生 + Avatar 口パク連動
- `FACE:<expr>` → 表情変更 (neutral / happy / sad / doubt / sleepy / angry)
- `MOVE:<yaw,pitch>` → サーボ首振り (度ベース、内部 clamp あり)
- `CAPTURE` → JPEG キャプチャ (CoreS3 系のみ)

## 依存ライブラリ

- [m5stack/M5Unified](https://github.com/m5stack/M5Unified) (MIT)
- [m5stack/M5CoreS3](https://github.com/m5stack/M5CoreS3) (MIT) — CoreS3 内蔵カメラ用、`cores3-main` のみ
- [meganetaaan/M5Stack-Avatar](https://github.com/stack-chan/m5stack-avatar) (MIT)
- `lib/scservo/` (Apache-2.0、本リポ同梱、`docs/scservo_protocol.md` のプロトコル仕様に基づく Arduino C++ 実装)

## ビルド・書き込み

```bash
cd firmware
pio run                              # default = cores3-main をビルド
pio run -e cores3-main -t upload     # K151 / K151-R / CoreS3 単体に書き込み
pio run -e atoms3r-main -t upload    # AtomS3R に書き込み
pio run -e basic-main -t upload      # M5Stack Basic (アールティ Ver.β) に書き込み
pio device monitor -e <env>          # シリアルログ確認
```

## 各 example の説明

### cores3/main (CoreS3 系本体ファーム)

xangi シリアル経由で WAV 再生 + Avatar 表情・口パク + サーボ首振り + 内蔵カメラ JPEG キャプチャを実装する本番ファーム。

- サーボ・カメラの有無を起動時に自動検出 (`STATUS` の `servo` / `camera` フィールドで識別)
- K151 / K151-R 構成: SCServo SCS を PORT C (TX=G6, RX=G7) 1Mbps で制御、PY32 IO Expander 経由でサーボバス電源 VM_EN を ON。`cores3-home-calibration` で zero raw を NVS に焼き込んでおく前提
- CoreS3 単体構成: サーボ・PY32 無しで graceful degradation、MOVE は `unavailable` 応答だが WAV / FACE / CAPTURE は動作

Python 側は `src/xangi_stackchan/stackchan.py` の `StackchanSerial` がそのまま使える (baud 921600 一致)。テストは `scripts/test_xangi_bridge.py`。

### cores3/home-calibration

K151 系で「サーボ中央 = 真正面・水平」になっていない個体向け。手で水平に向けた状態で画面タップ or シリアル `c` を送ると、現在の SCServo raw 値を NVS namespace `xstackchan` の `yaw_zero` / `pitch_zero` に保存する。`cores3-main` / `cores3-set-angle-demo` はこの値を読んで zero ベース計算する。

SCSCL シリーズはサーボ側に「物理現在位置を中央として永続記録」する機能を持たないため、ホスト側 (ESP32 NVS) で zero raw を保持する設計 (`docs/scservo_protocol.md` §11 参照)。

### cores3/set-angle-demo

NVS の zero raw を使って yaw を `0° → -30° → 0° → +30° → 0°` スイープする torque ON 系デモ。`home-calibration` を先に焼いて zero を保存しておくこと。画面タップ or シリアル `g` で起動。`s` で torque OFF。

### cores3/speaker-demo

CoreS3 内蔵スピーカー (NS4150 アンプ + I2S) で 440Hz 0.5秒 sine 波 (16kHz / mono / 16bit PCM) を `M5.Speaker.playWav()` 再生する単独デモ。サーボ電源には触らないので `home-calibration` の前後どちらでも単独で焼ける。画面タップ or シリアル `p` で再生、`+/-` で音量、`s` で停止。

### cores3/safe-startup

サーボ電源系統 + SCServo 通信を疎通確認するだけの安全起動ファーム。loop は表情切替だけで何もしない (サーボ触らない)。`home-calibration` の前にハードウェア構成を確認する用途。

### atoms3r/main (AtomS3R + Atomic Voice/Echo Base 本体ファーム)

ES8311 audio codec は `cfg.external_speaker.atomic_echo = true` で M5Unified が自動初期化 (Atomic Voice Base / Echo Base 共通の I2S 配線)。AtomS3R LCD 128×128 で Avatar を縮小表示。サーボ・カメラ非搭載で graceful degradation (`MOVE` / `CAPTURE` は unavailable)。Serial baud 115200。

### basic/main (M5Stack Basic + アールティ Ver.β 本体ファーム)

M5Stack Basic + アールティ Stack-chan PCB + Feetech SCS0009 ×2 構成。詳細は `examples/basic/main/README.md` 参照。

- WAV バッファ: PSRAM 無しのため内部 DRAM 96KB 上限の同期再生 (XangiBridge の 4MB キュー化とは別設計)
- SCServo SCS0009: Serial2 (GPIO16/17) 1Mbps、ID 1=Yaw, 2=Pitch、工場原点調整済 → zero=512 固定
- カメラ無し → CAPTURE は unavailable 固定
