# 使い方

xangi-stackchan のセットアップから常駐起動・デモ・トラブルシュートまで。

## セットアップ

```bash
git clone https://github.com/karaage0703/xangi-stackchan.git
cd xangi-stackchan
uv sync
```

### piper-plus のセットアップ

piper-plus を使う場合は、このリポジトリ内でセットアップする。

```bash
./scripts/setup_piper.sh
```

セットアップ後は、以下の相対パスが既定値として使われる。

- piper bin: `tools/piper`
- piper model: `models/tsukuyomi-chan-6lang-fp16.onnx`
- piper config: `models/config.json`

`PIPER_BIN` / `PIPER_MODEL` 環境変数や CLI オプションで上書きできるが、通常は指定不要。

### udev ルール (Linux、オプション)

`/dev/stackchan` を固定 SYMLINK で割り当てたい場合 (`/dev/ttyACMx` の番号変動を避ける):

```bash
sudo cp udev/99-xangi-stackchan.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger
```

対応デバイス: ESP32-S3 (CoreS3 / K151) と CP2104 (Core / Core2)。Mac / Windows ではこの手順は不要 (Python 側で自動検出)。

## 起動

### 最小起動

USB 接続したデバイスを動かす最小例。`--device-profile` でプリセット (baud / WAV サイズ上限 / capability) をまとめて指定できる:

| デバイス | `--device-profile` | baud | WAV 上限 |
|---|---|---|---|
| M5Stack 公式 K151 / K151-R (CoreS3 + サーボ + カメラ) | `cores3_k151` | 921600 | 無制限 (PSRAM 4MB) |
| M5Stack CoreS3 単体 (サーボ無し、カメラあり) | `cores3_standalone` | 921600 | 無制限 |
| M5Stack AtomS3R + Atomic Voice / Echo Base | `atoms3r` | 115200 | 256KB |
| アールティ Ver.β (M5Stack Basic + Feetech SCS0009 ×2) | `rt_beta` | 115200 | 96KB (内部 DRAM 制約) |

```bash
# CoreS3 系 (K151 / K151-R / CoreS3 単体)
uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --device-profile cores3_k151 \
  --volume 200 \
  --tts piper

# AtomS3R + Voice/Echo Base
uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --device-profile atoms3r \
  --volume 192 \
  --tts piper

# アールティ Ver.β (M5Stack Basic + SCS0009)
uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --device-profile rt_beta \
  --volume 200 \
  --tts piper
```

`--baud` / `--max-wav-bytes` で個別上書きも可能。`--device-profile` を指定しない場合は `--baud` (既定 921600) と `--max-wav-bytes` (既定 0 = 無制限) を直接指定する。

`--xangi-url` で接続先の xangi を選ぶ。複数 xangi を建てている場合は、対象インスタンスの URL を指定する。

起動すると設定 UI も立ち上がる。

```text
http://127.0.0.1:7897/
```

UI から以下を変更できる。

- xangi URL
- thread filter
- USB 接続先
- 音量
- TTS 設定
- 状態ごとの表情
- 首振り (MOVE) 設定 (サーボあり機のみ)
- カメラスナップショット

保存すると `~/.xangi/xangi-stackchan/config.json` に永続化し、実行中デーモンにも反映する。xangi URL を変更した場合はストリームを張り直す。

設定 UI を LAN / Tailscale 経由でも開きたい場合は `--settings-bind 0.0.0.0` を付ける (既定は `127.0.0.1`)。

### 設定 UI を無効化

```bash
uv run xangi-stackchan --no-settings-ui ...
```

### 常駐起動

チャット連携で使う場合は、ターミナル終了で落ちないように常駐起動する。

```bash
setsid -f bash -c 'cd /path/to/xangi-stackchan && exec uv run xangi-stackchan \
  --xangi-url http://127.0.0.1:18888 \
  --port /dev/stackchan \
  --baud 921600 \
  --volume 200 \
  --tts piper \
  --stackchan-retry-seconds 3 \
  --settings-port 7897' </dev/null >>/tmp/xangi-stackchan.log 2>&1
```

ログ確認:

```bash
tail -f /tmp/xangi-stackchan.log
```

## piper-plus の推奨設定

つくよみちゃん 6-language モデルでは以下を既定値にしている。

- `PIPER_LANGUAGE=ja-en-zh-es-fr-pt`
- `PIPER_LENGTH_SCALE=1.5`
- `PIPER_NOISE_SCALE=0.667`

`PiperPlus.Cli --json-input` を常駐させるため、初回以外はモデルロードを避けられる。出力ファイルはサイズが安定してから読み込むため、0 byte WAV を送る race を避ける。

`piper speaker` はマルチスピーカーモデル用の話者 ID。つくよみちゃん 6-language モデルでは通常 `0` のままでよいので、設定 UI には出していない。必要な場合だけ `--piper-speaker <id>` で指定する。

## 主なオプション

- `--xangi-url`: xangi base URL または `/api/events/stream` の完全 URL (既定 `http://127.0.0.1:18888`)
- `--thread-id`: 対象 thread のみ処理 (複数台運用時の喋り分けに使う、後述「複数台で動かす」参照)
- `--instance-id`: config.json の namespace 名 (既定 `default`、複数台で別 instance を持たせる)
- `--settings-port`: 設定 UI の port (既定 `7897`、衝突したら +1 ずつ auto-shift)
- `--port-autoshift-tries`: 設定 UI port の試行回数 (既定 `10`、`7897..7906`)
- `--no-port-autoshift`: 設定 UI port を auto-shift せず最初の bind 失敗で終了
- `--settings-bind`: 設定 UI の listen アドレス (既定 `127.0.0.1`、LAN/Tailscale 公開時は `0.0.0.0`)
- `--no-settings-ui`: 設定 UI を起動しない
- `--port --baud`: USB serial のポートと baudrate (XangiBridge ファーム既定値 `921600`)
- `--volume`: デバイスの音量 (`0`〜`255`、既定 `255`)
- `--tts`: `piper`, `voicevox`, `none`
- `--piper-bin`: piper-plus 実行ファイル (既定 `tools/piper`)
- `--piper-model`: piper-plus モデル (既定 `models/tsukuyomi-chan-6lang-fp16.onnx`)
- `--piper-speaker`: マルチスピーカーモデル用の話者 ID (通常不要)
- `--face-idle`, `--face-thinking`, `--face-talking`, `--face-error`: 状態ごとの表情
- `--move-enabled` / `--no-move-enabled`: 首振り (MOVE) 機能 ON/OFF (既定 ON、K151 SCServo 専用)
- `--move-idle-yaw` / `--move-idle-pitch`: idle 時の首ポーズ (既定 `0` / `+5`、少し上向き)
- `--move-thinking-yaw` / `--move-thinking-pitch`: 考え中の首ポーズ (既定 `-8` / `+5`、少し首かしげ)
- `--move-error-yaw` / `--move-error-pitch`: エラー時の首ポーズ (既定 `0` / `-10`、首下げ)
- `--move-talking-sway-yaw` / `--move-talking-sway-pitch`: 喋り中のランダム揺らぎ振り幅 (既定 `±4` / `±2`)
- `--move-talking-sway-interval`: 喋り中のランダム揺らぎ更新間隔 (秒、既定 `1.5`)
- `--stackchan-retry-seconds`: デバイス切断時の再接続間隔 (秒)

## 複数台で動かす

xangi-stackchan は同じマシン上で複数プロセスを並列起動できる。1 プロセス = 1 スタックチャンが基本で、台数分だけ起動する。xangi 側は固定 1 URL、各 xangi-stackchan プロセスが個別に SSE を購読する。

### 1. udev で物理デバイスを固定名にする (Linux)

デフォルトの `udev/99-xangi-stackchan.rules` は 2 台以上挿すと `/dev/stackchan` を奪い合うので、シリアル番号ベースで個別 SYMLINK を付ける。

```udev
# /etc/udev/rules.d/99-xangi-stackchan.rules

# 1 台目 (左)
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", \
  ATTRS{serial}=="AABBCCDDEEFF", SYMLINK+="stackchan-left", MODE="0666"

# 2 台目 (右)
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", \
  ATTRS{serial}=="112233445566", SYMLINK+="stackchan-right", MODE="0666"
```

シリアル番号は `udevadm info -a -n /dev/ttyACMx | grep ATTRS{serial}` で取れる。

`ATTRS{serial}` が空 (CP2104 系の一部チップ) な場合は `ENV{ID_PATH}` の物理 USB ポート位置を fallback に使う:

```udev
SUBSYSTEM=="tty", ENV{ID_PATH}=="pci-0000:00:14.0-usb-0:1:1.0", \
  SYMLINK+="stackchan-left", MODE="0666"
```

どちらも取れないチップ・組み合わせなら、3rd fallback として `--port /dev/ttyACM0` 等を起動時に明示するしかない (ただしホットプラグで順序が変わるので運用しにくい)。

反映:

```bash
sudo udevadm control --reload && sudo udevadm trigger
```

### 2. instance-id ごとに別プロセスで起動

`~/.xangi/xangi-stackchan/config.json` は v2 schema で `instances.<id>` namespace を持つ。`--instance-id` を分けると、設定 UI からの保存・読み込みも instance 単位で分離される。

```bash
# 左スタックチャン (--thread-id は xangi 側の thread 識別子)
setsid -f bash -c 'cd /path/to/xangi-stackchan && exec uv run xangi-stackchan \
  --instance-id left \
  --port /dev/stackchan-left \
  --thread-id left \
  --settings-port 7897 \
  --tts piper' </dev/null >>/tmp/xangi-stackchan-left.log 2>&1

# 右スタックチャン (settings UI port は auto-shift で 7898 になる)
setsid -f bash -c 'cd /path/to/xangi-stackchan && exec uv run xangi-stackchan \
  --instance-id right \
  --port /dev/stackchan-right \
  --thread-id right \
  --settings-port 7897 \
  --tts piper' </dev/null >>/tmp/xangi-stackchan-right.log 2>&1
```

`--settings-port 7897` は両方とも同じ起点で良い。後発プロセスは bind 失敗で `+1` 試行を最大 `--port-autoshift-tries` 回繰り返し、空いた port を採用する。実際に bind した port は起動ログの `bound_config_port` フィールドと `settings_ui` 行に出る:

```json
{"settings_ui": "http://127.0.0.1:7898/"}
{"boot": true, "instance_id": "right", "serial_port": "/dev/stackchan-right",
 "wifi": false, "bound_config_port": 7898, "thread_id": "right", ...}
```

設定 UI は instance ごとに別 URL になるので、ブラウザのタブ 2 つで個別に編集できる。

### 3. 喋り分けの考え方 (SSE event routing)

デフォルトは「全員一斉に喋る」 (broadcast)。`--thread-id` を指定しない複数プロセスは、全員が同じ xangi の `turn.complete` を受けて同じ発話をする。

個別に喋り分けたいときは、xangi 側で thread を分けて、各 xangi-stackchan に `--thread-id` を渡す。xangi 側がどう thread を分けるかは xangi の運用次第 (Discord channel ID / Slack channel / Web thread 等)。

将来的に xangi 本体に役割ベース routing が入る予定はあるが、現状は thread-id 分離が唯一の手段。

### 4. config.json の構造 (v2)

```json
{
  "version": 2,
  "instances": {
    "default": {
      "xangi_url": "http://127.0.0.1:18888",
      "port": "/dev/stackchan",
      "thread_id": "",
      "volume": 200,
      "tts": "piper"
    },
    "left": {
      "xangi_url": "http://127.0.0.1:18888",
      "port": "/dev/stackchan-left",
      "thread_id": "left",
      "volume": 192,
      "tts": "piper"
    }
  }
}
```

v1 (フラット 1 ファイル) で運用していた既存ユーザは、初回保存時に自動的に `instances.default` の下に packing される。CLI で `--instance-id` を指定しなければ常に `default` を読むので、シングル運用は変更なしで動く。

## 動作確認

```bash
uv run python -m py_compile src/xangi_stackchan/*.py
uv run pytest
```

実機テスト用 (xangi に向かって投げる):

```bash
curl -sN -X POST http://127.0.0.1:18888/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"接続テストです。「接続テスト成功」とだけ返してください。"}'
```

## デモスクリプト

### ダンスデモ (K151 サーボ機専用)

テキストを piper TTS で喋らせつつ BPM 駆動の連続パターンで首を振る単発デモ。
動作中の xangi-stackchan の内側で実行できるので、USB シリアルの取り合いなしで動かせる。

プリセット (yaw / pitch はファーム SAFE 範囲 yaw ±100° / pitch ±30° の内側で抑え):

| preset | BPM | yaw 振幅 | pitch 振幅 | 雰囲気 |
|--------|-----|---------|-----------|--------|
| happy  | 120 | ±20°    | ±5°       | 元気に左右、軽い縦ノリ |
| chill  | 70  | ±10°    | ±2°       | ゆっくり左右 |
| wave   | 100 | ±15°    | ±5°       | 8 の字風 |

#### A. settings UI から (xangi-stackchan が動いてる前提)

ブラウザで <http://127.0.0.1:7897/> を開くと、ページ下部に「dance demo」フォームがある。
text と preset (任意で BPM) を入れて送信すると、xangi-stackchan が現在使っている TTS /
backend / 表情のままダンスデモを実行する。実行中に xangi の実会話 (`turn.complete`)
が来た場合は、`send_wav` のキューで順番待ちになる (デモ全部喋り終わってから次の発話)。

#### B. CLI 経由 (xangi-stackchan に POST)

```bash
uv run python scripts/dance_demo.py --text "踊るよ" --preset happy \
    --via-bridge http://127.0.0.1:7897
```

`--via-bridge` を指定すると `POST /api/demo` で動作中の xangi-stackchan にお願いする
モードになる。USB シリアルは xangi-stackchan が掴んだまま、内部でダンスする。

#### C. CLI 直接モード (xangi-stackchan 停止時のスタンドアロン)

```bash
uv run python scripts/dance_demo.py --text "踊るよ" --preset happy
uv run python scripts/dance_demo.py --text "ゆっくり話すよ" --preset chill --bpm 60
uv run python scripts/dance_demo.py --text "TTS だけ確認" --dry-run    # シリアル不要
```

`--via-bridge` を付けない場合は CLI 側で piper を起動し、シリアルを直接掴んで再生する。
xangi-stackchan が動いていると ttyACM0 が競合するので、その場合は停止してから実行する
(または上の A / B を使う)。`--bpm` で各プリセットの BPM を上書き、`--idle-yaw` /
`--idle-pitch` で基準位置を変更可。

### ファーム単体テスト

```bash
uv run python scripts/test_xangi_bridge.py --port /dev/stackchan
```

STATUS / VOLUME / FACE / MOVE / WAV (440Hz トーン) の往復を 1 ショットで確認する。

### カメラ (スナップショット + モニタリング)

CoreS3 内蔵 GC0308 カメラから JPEG 1 枚取得し、設定 UI で表示する機能。
xangi (LLM) への入力連携は別途実装予定 (発話時 1 枚自動添付)。

#### A. 設定 UI から

ブラウザで <http://127.0.0.1:7897/> を開いて末尾の「camera」パネル:

- 「スナップショット」ボタンで 1 枚撮影 → プレビュー画像更新
- メタデータ (`size` / `width` / `height` / `captured_at`) が JSON で表示

#### B. API 経由 (CLI / 自動化向け)

```bash
# 撮影 + JPEG ダウンロード
curl -X POST http://127.0.0.1:7897/api/camera/capture
curl -o /tmp/snapshot.jpg http://127.0.0.1:7897/api/camera/snapshot.jpg

# キャッシュから取得 (撮影せず最新フレームを取る)
curl -o /tmp/snapshot.jpg "http://127.0.0.1:7897/api/camera/snapshot.jpg"

# 強制再キャプチャ
curl -o /tmp/snapshot.jpg "http://127.0.0.1:7897/api/camera/snapshot.jpg?force=1"

# 最終キャプチャのメタ情報 + age_ms
curl http://127.0.0.1:7897/api/camera/status
```

#### 制約

- **USB シリアル接続のみ対応**。WiFi MJPEG ストリームは将来別途実装予定
- **M5Stack CoreS3 系のみ** (K151 / K151-R / CoreS3 単体)。カメラ初期化に失敗した機種では `camera not ready` 応答
- **オンデマンド撮影** (1 リクエスト 1 枚)。常時ストリーミングは将来検討

## トラブルシュート

- **Permission denied (Linux)**: `sudo usermod -aG dialout $USER` して再ログイン
- **ポートが見つからない**: USB ケーブルがデータ転送対応か確認 (充電専用ケーブルは NG)
- **間違ったポートが選ばれる (複数 ESP32 接続時)**: `--port /dev/cu.usbmodem3101` または udev rules で `/dev/stackchan` を固定
- **音が鳴らない**: `--volume 255` で最大音量
- **発話開始が遅い**: piper-plus は JSONL 入力の常駐プロセスとして保持する設計のため、初回だけモデルロードが入る。2 回目以降のレスポンスは低遅延になる
- **piper-plus の発話速度を変えたい**: `PIPER_LENGTH_SCALE=1.2` のように小さくすると速くなる (既定はモデルカード推奨の `1.5`)。発音品質とのトレードオフ
- **シリアルポートが掴まれている**: `lsof /dev/ttyACM0` で確認、`fuser -k /dev/ttyACM0` で解放。常駐起動した自分自身が掴んでる場合は kill してから再起動
- **`piper failed: ... Opset 5 ... opset 3 only` エラー**: 初回実行で生成された最適化キャッシュが onnxruntime と互換性が無い状態。`rm models/*.cpu.opt.onnx*` で解消
- **VOICEVOX 接続エラー**: `docker ps` でコンテナ起動確認、または `curl http://localhost:50021/version` で疎通確認
