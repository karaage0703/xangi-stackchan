# xangi-stackchan

![xangi-stackchan](docs/images/cover.jpg)

[xangi](https://github.com/karaage0703/xangi)（AI アシスタントフレームワーク）の `GET /api/events/stream` を購読してスタックチャン系デバイスを物理ペットのように動かす、常駐ブリッジ + Arduino ファームウェア。xangi の応答に合わせて表情・首振り・音声合成を実機側で再生する。

## できること

- xangi が考え始めるとデバイスが `doubt` 顔 + 首をかしげる
- xangi が話し始めると `happy` 顔になる
- `turn.complete` の最終テキストを piper-plus / VOICEVOX で音声化して再生。再生中は首がささやかに揺れる
- 完了後は `neutral` 顔 + idle ポーズに戻る
- `agent.error` では `sad` 顔 + 首を下げる
- **カメラスナップショット (Phase 1A)**: 内蔵 GC0308 カメラで JPEG 撮影 → 設定 UI / API で表示。LLM 連携は Phase 1B 以降

表示 UI は持たず、デバイスの表情変更と音声再生に集中する。サーボの有無は起動時に自動判定され、サーボ無しの CoreS3 単体機では MOVE のみ unavailable 応答 (WAV/FACE/CAPTURE は通常動作) する graceful degradation 設計。

## 対応デバイス

USB シリアル経由で Arduino (PlatformIO) ファームを焼く。CoreS3 系は `firmware/k151/examples/XangiBridge/`、AtomS3R 系は `firmware/k151/examples/AtomVoiceBridge/` を使う。両ファームはシリアルプロトコル互換 (STATUS / VOLUME / WAV / FACE)、機種差は graceful degradation で吸収。

| デバイス | ファーム | baud | MOVE | CAPTURE |
|---------|---------|------|------|---------|
| M5Stack 公式 K151 / K151-R (CoreS3 + サーボ + Remote) | XangiBridge | 921600 | ✅ | ✅ |
| M5Stack CoreS3 単体 (サーボ無し) | XangiBridge | 921600 | 🚫 | ✅ |
| M5Stack AtomS3R + Atomic Voice Base / Echo Base (ES8311) | AtomVoiceBridge | 115200 | 🚫 | 🚫 |

XangiBridge ではサーボ有無は起動時に自動検出、`STATUS` の `servo` フィールドで現状を取得できる。

## 構成

```
xangi (:18888)
  └─ GET /api/events/stream (SSE)
      └─ xangi-stackchan (host bridge, Python)
          ├─ thread_id filter
          ├─ piper-plus persistent process
          └─ device (USB serial 共通プロトコル: STATUS / FACE: / WAV:<size> / VOLUME: / MOVE: / CAPTURE)
              ├─ M5Stack CoreS3 (K151 / K151-R / 単体機、firmware/k151/ XangiBridge、baud 921600)
              └─ M5Stack AtomS3R + Atomic Voice/Echo Base (firmware/k151/ AtomVoiceBridge、baud 115200)
```

## クイックスタート

```bash
git clone https://github.com/karaage0703/xangi-stackchan.git
cd xangi-stackchan
uv sync
./scripts/setup_piper.sh

uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --tts piper
```

起動すると設定 UI が `http://127.0.0.1:7897/` で立ち上がる。xangi URL / 接続先 / 音量 / TTS / 表情をブラウザから変更でき、保存すると `~/.xangi/xangi-stackchan/config.json` に永続化される。

詳細なセットアップ・オプション・常駐起動・トラブルシュートは [`docs/usage.md`](./docs/usage.md) を参照。

## AI エージェント連携

[`SKILL.md`](./SKILL.md) を参照。Claude Code や borot 等の AI エージェントから本ブリッジを起動・操作してスタックチャンを動かすための手順を集約してある。

## ドキュメント

- [`docs/usage.md`](./docs/usage.md): セットアップ・オプション・常駐起動・デモ・トラブルシュート
- [`docs/xangi_bridge_protocol.md`](./docs/xangi_bridge_protocol.md): USB シリアル共通プロトコル仕様
- [`docs/scservo_protocol.md`](./docs/scservo_protocol.md): Feetech SCS シリアルサーボのプロトコル仕様
- [`firmware/k151/README.md`](./firmware/k151/README.md): K151 / K151-R 用 Arduino ファームの開発ガイド

## 参考

- [m5stack/StackChan](https://github.com/m5stack/StackChan): M5Stack 公式 K151 のリポ (xiaozhi-esp32 ベース、本リポでは仕様情報のみ参照)
- [stack-chan/stack-chan](https://github.com/stack-chan/stack-chan): 元祖スタックチャン (Apache-2.0、`firmware/k151/` のサーボ制御ロジックの仕様参照元)

## ライセンス

MIT License
