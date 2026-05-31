---
name: xs:xangi-stackchan
description: xangi の SSE イベントを購読してスタックチャン (M5Stack CoreS3 / K151 / K151-R) に喋らせ・表情変更・首振りさせる常駐ブリッジを起動・操作するスキル。TTS は piper-plus / VOICEVOX を選択可。設定 UI 経由でランタイム設定変更、ダンスデモ実行、ファーム単体テストにも対応。「xangi の返答をスタックチャンで読ませて」「xangi-stackchan を立ち上げて」「スタックチャン踊らせて」「xangi-stackchan」で使用。
---

# xangi-stackchan 制御スキル

xangi の `GET /api/events/stream` を購読して、`turn.started` / `message.delta` / `turn.complete` / `agent.error` に応じてスタックチャンに表情・首振り・音声再生をさせる常駐ブリッジ。

USB シリアル経由で M5Stack CoreS3 ベースのデバイスと通信する。
表示 UI は持たず、デバイスの表情変更と音声再生に集中する。

## 対応デバイス

USB シリアル経由で Arduino (PlatformIO) ファームを焼く。

| デバイス | ファーム (PlatformIO env) | baud | フル機能 |
|---------|---------|------|---------|
| K151 / K151-R (CoreS3 + サーボ + Remote) | `cores3-main` (`examples/cores3/main`) | 921600 | WAV / FACE / MOVE / CAPTURE |
| CoreS3 単体 (サーボ無し) | `cores3-main` (`examples/cores3/main`) | 921600 | WAV / FACE / CAPTURE (MOVE は unavailable) |
| AtomS3R + Atomic Voice Base / Echo Base | `atoms3r-main` (`examples/atoms3r/main`) | 115200 | WAV / FACE (MOVE / CAPTURE は unavailable) |
| M5Stack Basic + アールティ Ver.β | `basic-main` (`examples/basic/main`) | 115200 | WAV / FACE / MOVE (CAPTURE は unavailable) |

CoreS3 系はサーボ・カメラ有無を起動時に自動検出、`STATUS` の `servo` / `camera` フィールドで現状取得可。

## Step 0: 初回セットアップ (piper-plus のバイナリ・モデルが無い場合のみ)

```bash
cd [SKILL_DIR] && uv sync && ./scripts/setup_piper.sh
```

OS/ARCH を自動判定し、piper-plus CLI (macOS/Linux arm64/x64) と、つくよみちゃん 6-language モデルをダウンロード、`tools/piper` ラッパーを生成する。

`tools/piper` はモデルカードの推奨どおり `--language ja-en-zh-es-fr-pt` と `--length-scale 1.5` を既定で使う。上書きする場合は `PIPER_LANGUAGE` / `PIPER_LENGTH_SCALE` / `PIPER_NOISE_SCALE` を指定する。

**`piper failed: ... Opset 5 ... opset 3 only` エラーが出たら:** 初回実行で生成された最適化キャッシュが onnxruntime と互換性が無い状態。`rm [SKILL_DIR]/models/*.cpu.opt.onnx*` で解消。

## Step 1: 起動状態の確認

設定 UI が立ち上がっているかで判断するのが速い (常駐起動済みなら `7897` で listen している)。

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7897/api/config
# 200 が返れば常駐中
```

プロセス単位で確認:

```bash
pgrep -fa xangi-stackchan
```

## Step 2: 立ち上げ (常駐起動)

ターミナル終了で落ちないように `setsid -f` で常駐起動する。

```bash
cd [SKILL_DIR]
setsid -f bash -c 'cd '"$PWD"' && exec uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --device-profile cores3_k151 \
  --volume 200 \
  --tts piper \
  --stackchan-retry-seconds 3 \
  --settings-port 7897' </dev/null >>/tmp/xangi-stackchan.log 2>&1
```

音声対話 (アタマセンサをなでて話しかける) も有効にする場合は `--voice-conversation` を足す (K151 専用)。STT モデルは `STACKCHAN_WHISPER_MODEL=medium` を前置すると短い発話を取りこぼしにくい (`small` は VAD でこぼしやすい)。voice 用設定が通常運用と混ざらないよう `--instance-id` を分けると良い:

```bash
cd [SKILL_DIR]
setsid -f bash -c 'cd '"$PWD"' && STACKCHAN_WHISPER_MODEL=medium exec uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --device-profile cores3_k151 \
  --volume 80 \
  --tts piper \
  --settings-port 7897 \
  --voice-conversation \
  --instance-id voice' </dev/null >>/tmp/xangi-stackchan.log 2>&1
```

注意: 1 デバイス = 1 プロセス。同じシリアルポートに複数ブリッジを並走させると先発がポートを掴んだまま後発が応答できず「なでても無反応」になる。起動前に `pgrep -fa xangi-stackchan` で重複が無いか確認する。

オプション要点:

- `--xangi-url`: 接続先 xangi (既定 `http://127.0.0.1:18888`)
- `--port`: USB シリアル。`/dev/ttyACMx` は USB 再列挙で番号が変わるので、番号非依存の固定パスを使う。Linux なら `/dev/serial/by-id/...` の安定リンク (`ls /dev/serial/by-id/` で確認) か、udev で `/dev/stackchan` を固定するのが安全。config.json の `port` は CLI `--port` より優先されるので、番号付きパスが保存されていると番号ズレ時に無反応になる (起動ログの `serial_port` で実際に使われたパスを確認)
- `--device-profile`: `cores3_k151` / `cores3_standalone` / `atoms3r` / `rt_beta` (baud / WAV 上限 / capability をまとめて設定)。指定時は `--baud` 不要
- `--baud`: profile 未指定時のみ。`921600` (CoreS3 系) / `115200` (AtomS3R / rt_beta 系)
- `--volume`: 0〜255 (既定 255、AtomS3R + Voice Base は ES8311 過変調防止で 192 以下推奨)
- `--tts`: `piper` / `voicevox` / `none`
- `--face-mode`: `avatar` / `sprite`。`sprite` は `spritesheet.webp` を LCD 画像顔として送り、filled-frame tick でまばたき/表情アニメーションする
- `--sprite-sheet`: `--face-mode sprite` 用の `spritesheet.webp` (既定 `assets/pets/default/spritesheet.webp`)。スプライト本体は `.gitignore` 対象でコミットしない
- `--settings-port`: 設定 UI の port (既定 7897)
- `--settings-bind`: 既定 `127.0.0.1`。同一マシン以外 (LAN / Tailscale 等の別端末) から設定 UI を開きたい場合のみ `0.0.0.0`
- `--voice-conversation`: アタマセンサなで → 録音 → STT → xangi 投入の音声対話モード (K151 専用)
- `--instance-id`: config namespace。用途別 (通常運用 / voice 等) に分けると設定が混ざらない

ログ確認:

```bash
tail -f /tmp/xangi-stackchan.log
```

## Step 3: 設定 UI 経由でランタイム設定変更

ブラウザで <http://127.0.0.1:7897/> (同一マシン) を開くと以下を変更できる。別端末から開く場合は `--settings-bind 0.0.0.0` で起動した上で、そのマシンの LAN / Tailscale アドレスの `:7897` にアクセスする。

- xangi URL / thread filter
- USB 接続先・音量
- TTS 設定 (piper / voicevox / none)
- 状態ごとの表情 (idle / thinking / talking / error)
- 首振り (MOVE) 設定 (サーボあり機のみ)
- カメラスナップショット

保存すると `~/.xangi/xangi-stackchan/config.json` に永続化し、実行中デーモンにも反映する。xangi URL を変更した場合はストリームを張り直す。

API 経由で叩く場合:

```bash
# 現在の設定取得
curl -s http://127.0.0.1:7897/api/config | jq

# 一部更新 (例: 音量)
curl -s -X POST http://127.0.0.1:7897/api/config \
  -H 'Content-Type: application/json' \
  -d '{"volume":180}'
```

## Step 3.5: カメラスナップショット

CoreS3 内蔵 GC0308 カメラから JPEG 1 枚取得し、設定 UI / API で表示する。LLM への
自動添付は将来別 PR で対応予定。カメラ初期化に失敗した機種では `camera not ready` 応答。

設定 UI: <http://127.0.0.1:7897/> 末尾の「camera」パネル → スナップショットボタン。

API:

```bash
# 撮影 + JPEG ダウンロード (キャッシュ更新)
curl -X POST http://127.0.0.1:7897/api/camera/capture
curl -o /tmp/snapshot.jpg http://127.0.0.1:7897/api/camera/snapshot.jpg

# 強制再キャプチャ
curl -o /tmp/snapshot.jpg "http://127.0.0.1:7897/api/camera/snapshot.jpg?force=1"

# メタデータ (age_ms 等)
curl -s http://127.0.0.1:7897/api/camera/status | jq
```

## Step 4: ダンスデモ (K151 サーボ機のみ)

テキストを piper TTS で喋らせつつ BPM 駆動で首を振る単発デモ。

プリセット:

- `happy` — BPM 120 / yaw ±20° / pitch ±5° (元気)
- `chill` — BPM 70 / yaw ±10° / pitch ±2° (ゆっくり)
- `wave`  — BPM 100 / yaw ±15° / pitch ±5° (8 の字)

### A. 動作中の xangi-stackchan に POST (推奨、シリアル取り合いなし)

```bash
curl -s -X POST http://127.0.0.1:7897/api/demo \
  -H 'Content-Type: application/json' \
  -d '{"text":"踊るよ","preset":"happy"}'
```

CLI ラッパー版:

```bash
cd [SKILL_DIR]
uv run python scripts/dance_demo.py --text "踊るよ" --preset happy \
    --via-bridge http://127.0.0.1:7897
```

### B. xangi-stackchan 停止時のスタンドアロン

```bash
cd [SKILL_DIR]
uv run python scripts/dance_demo.py --text "踊るよ" --preset happy
uv run python scripts/dance_demo.py --text "TTS だけ確認" --dry-run    # シリアル不要
```

シリアルを直接掴むので、常駐ブリッジが動いていると競合する。先に `pkill -f xangi-stackchan` で停止してから実行する。

## Step 5: ファーム単体テスト (K151 XangiBridge)

STATUS / VOLUME / FACE / MOVE / WAV (440Hz トーン) の往復を 1 ショットで確認する。

```bash
cd [SKILL_DIR]
uv run python scripts/test_xangi_bridge.py --port /dev/stackchan
```

ファーム書き込み直後の動作確認に使う。常駐ブリッジが動いていると競合するので停止してから。

## Step 6: 停止

```bash
pkill -f xangi-stackchan
```

`setsid -f` で起動しているので、Ctrl-C ではなく `pkill` で落とす。

## イベントごとの既定動作

xangi 側から流れてくる SSE イベントに対する振る舞い:

| イベント | 表情 | サーボ (K151) |
|----------|------|---------------|
| `turn.started`  | `doubt`   | thinking ポーズ (yaw -8° / pitch +5°) |
| `message.delta` | `happy`   | (継続) |
| `turn.complete` | 発話中 `happy` → 発話後 `neutral` | 発話中は sway (±4° / ±2° を 1.5 秒間隔)、発話後 idle ポーズ |
| `agent.error`   | `sad`     | エラーポーズ (pitch -10°) |

設定 UI または CLI オプション (`--face-*`, `--move-*`) で個別に変更できる。

## 動作確認・実機テスト

xangi 経由で会話を投げてリアクションを確認:

```bash
curl -sN -X POST http://127.0.0.1:18888/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"接続テストです。「接続テスト成功」とだけ返してください。"}'
```

xangi-stackchan 側のログで `turn.started` → `turn.complete` の流れと `send_wav: queued` が出ていれば OK。

## トラブルシュート

- **常駐ブリッジが xangi に繋がらない**: `--xangi-url` で指定した port が xangi のデフォルト (`18888`) と合っているか / xangi が起動しているか確認
- **デバイスに音声が届かない**: `pgrep -fa xangi-stackchan` で生きているか、`tail -f /tmp/xangi-stackchan.log` で `send_wav` エラーが出ていないか確認
- **シリアルポートが掴まれている**: 常駐ブリッジ自身が掴んでいるケース。`pkill -f xangi-stackchan` で落としてから再起動
- **MOVE が効かない**: CoreS3 単体 (サーボ無し) では無効、STATUS の `servo: false` で判定可能。サーボあり機の場合は `--no-move-enabled` で OFF にしていないか / 設定 UI で MOVE が有効か確認
- **発話が遅い**: piper-plus は常駐プロセスなので、初回だけモデルロード。それ以降は低遅延

## 使用例

```
xangi の返答をスタックチャンで読ませて
xangi-stackchan を立ち上げて
スタックチャンを happy preset で踊らせて
xangi-stackchan の状態確認して
xangi-stackchan を止めて
ファームの単体テストやって
```
