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

### シリアルポートの指定

`/dev/ttyACMx` の番号は再起動・USB 抜き差し・USB の再列挙で変わる。config.json に `/dev/ttyACM0` のような番号付きパスを保存していると、番号がズレた瞬間にデバイスを掴めず喋らなくなる。さらに config.json に保存された `port` は CLI の `--port` より優先されるため、起動時に `--port` で別パスを渡しても効かないことがある (実際に使われたパスは起動ログの `serial_port` フィールドで確認できる)。番号に依存しない固定パスで指定するのが安全。

推奨順:

1. by-id の安定リンク (Linux、最も手軽): `ls /dev/serial/by-id/` に出る `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<シリアル>-if00` を `--port` に渡す (config.json の `port` にも同じ値を書く)。udev rules を書かなくてもチップのシリアル番号で固定されるので番号ドリフトに強い。単体運用ならこれが一番簡単。
2. udev SYMLINK: 下記 udev ルールで `/dev/stackchan` を割り当てる。複数台で名前を付け分けたいときに有効。
3. `/dev/ttyACMx` 直指定: 番号が変動するので非推奨。検証用の一時起動向け。

#### udev ルール (Linux、オプション)

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
  --tts piper \
  --lcd-mic-voice \
  --head-pet-reaction

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
  --device-profile cores3_k151 \
  --volume 200 \
  --tts piper \
  --lcd-mic-voice \
  --head-pet-reaction \
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
- `--face-mode`: `avatar` (既定) / `sprite`。`sprite` はスプライトシートから状態ごとの画像を切り出して LCD に表示し、row/state + filled-frame tick でまばたき/表情アニメーションする
- `--sprite-sheet`: `--face-mode sprite` 時に使う `spritesheet.webp`。既定 `assets/pets/default/spritesheet.webp`。このファイルはローカル資産として `.gitignore` 対象
- `--sprite-jpeg-quality`: `--face-mode sprite` 時にデバイスへ送る JPEG 品質 (1〜95、既定 85)
- `--move-enabled` / `--no-move-enabled`: 首振り (MOVE) 機能 ON/OFF (既定 ON、K151 SCServo 専用)
- `--move-idle-yaw` / `--move-idle-pitch`: idle 時の首ポーズ (既定 `0` / `+5`、少し上向き)
- `--move-thinking-yaw` / `--move-thinking-pitch`: 考え中の首ポーズ (既定 `-8` / `+5`、少し首かしげ)
- `--move-error-yaw` / `--move-error-pitch`: エラー時の首ポーズ (既定 `0` / `-10`、首下げ)
- `--move-talking-sway-yaw` / `--move-talking-sway-pitch`: 喋り中のランダム揺らぎ振り幅 (既定 `±4` / `±2`)
- `--move-talking-sway-interval`: 喋り中のランダム揺らぎ更新間隔 (秒、既定 `1.5`)
- `--puzzle-light-enabled` / `--no-puzzle-light-enabled`: 状態表示LEDを使う。ファーム `STATUS` が `puzzle:true` なら `PUZZLE:<pattern>`、`stack_led:true` なら `STACKLED:<pattern>` を送る。CoreS3 Grove PORT.B の Puzzle Unit WS2812E と K151 / K151-R 本体 12 RGB LED は自動検出
- `--puzzle-idle`, `--puzzle-thinking`, `--puzzle-talking`, `--puzzle-error`: 状態ごとの LED pattern。既定は `off` / `thinking` / `talking` / `error`
- `--stackchan-retry-seconds`: デバイス切断時の再接続間隔 (秒)。起動時だけでなく**稼働中の切断 (デバイス再起動 / USB 再列挙で `ttyACMx` が変わる) も自動検知して再接続**する。再接続成功時は音量・表情・首ポーズ・ファーム設定を自動で再初期化するので、ブリッジの手動再起動は不要。`--port` には番号非依存の固定パス (`/dev/serial/by-id/...` か udev の `/dev/stackchan`) を使うこと
- `--voice-conversation`: アタマセンサ tap で録音 → STT (faster-whisper) → xangi `POST /api/chat` 投入の音声対話モード (M5Stackchan K151 専用、後述「音声対話モード」参照)
- `--lcd-mic-voice` / `--no-lcd-mic-voice`: LCD 下部のマイクボタンで録音 → STT → xangi 投入。K151 通常運用では既定で有効
- `--head-pet-reaction` / `--no-head-pet-reaction`: アタマセンサのなで反応。K151 通常運用では既定で有効
- `--voice-app-session-id`: 音声対話で xangi に投げる appSessionId。空ならアプリ起動時に専用 web session を自動作成
- `--voice-silence-dbfs`: VAD 無音判定の dBFS 閾値 (既定 -40、静かな部屋なら -50、騒がしい環境なら -30)
- `--voice-silence-seconds`: 無音判定後の自動停止までの秒数 (既定 1.5)
- `--voice-max-seconds`: 最大録音時間 (既定 15、これを超えたら強制停止)
- `--voice-initial-grace-seconds`: なでてから最初の発話までの猶予秒数 (既定 5)。この間の無音では録音を止めない (考える時間)。最初の有音で通常の無音判定 (`--voice-silence-seconds`) に切り替わり、猶予内に一度も発話が無ければ誤タップとして停止。env `STACKCHAN_VC_INITIAL_GRACE_SECONDS` / 設定 UI でも調整可。

## 音声対話モード

M5Stackchan K151 のアタマセンサ (Si12T 容量タッチ、cores3-main 0.8+) を tap →
内蔵 PDM マイクで録音 → 無音 1.5 秒で自動停止 → faster-whisper STT (Silero VAD
フィルタ ON) → xangi `POST /api/chat` 投入。xangi 応答 (turn.complete) は piper
TTS で発話される (既存経路)。

### 起動例

```bash
uv run xangi-stackchan \
  --voice-conversation \
  --xangi-url http://127.0.0.1:18888 \
  --device-profile cores3_k151 \
  --port /dev/ttyACM1 \
  --volume 30
```

`--voice-app-session-id` と `--thread-id` を両方指定しなければ、起動時に xangi
で stackchan 専用の新規 web session を作成して両方に自動セット。これで:

- POST /api/chat は stackchan 専用 session に投入 (他 web セッションを汚さない)
- SSE event は `thread_id` フィルタで stackchan 専用 thread のみ反応 (Discord/
  Slack 等の他チャンネルからの message は届かない → Mic 録音中のシリアル衝突を回避)

### 環境変数チューニング

主要パラメータは CLI / 設定 UI (`--voice-silence-dbfs` 等) 経由で変更するのが
推奨。env はそれら CLI 引数のデフォルト値として使う場合のみ。

| env | 既定 | 内容 |
|---|---|---|
| `STACKCHAN_WHISPER_MODEL` | `small` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `STACKCHAN_WHISPER_DEVICE` | `cpu` | `cpu` / `cuda` (DGX Spark ARM64 の ctranslate2 は CUDA 未対応、CPU のみ) |
| `STACKCHAN_WHISPER_COMPUTE` | `int8` | `int8` / `float16` / `float32` |
| `STACKCHAN_WHISPER_LANGUAGE` | `ja` | ISO 言語コード |
| `STACKCHAN_WHISPER_BEAM` | `1` | beam_size。大きいと精度↑速度↓ |
| `STACKCHAN_WHISPER_VAD` | `1` | Silero VAD フィルタ ON (`0` で OFF) |
| `STACKCHAN_VC_SILENCE_DBFS` | `-40.0` | VoiceConversation 直接生成時の無音 dBFS フォールバック |
| `STACKCHAN_VC_SILENCE_SECONDS` | `1.5` | 同上 (秒数) |
| `STACKCHAN_VC_MAX_SECONDS` | `15.0` | 同上 (最大録音秒) |
| `STACKCHAN_VC_MIN_PCM_BYTES` | `8192` | 短すぎる録音 (誤タップ) を捨てる閾値 |
| `STACKCHAN_VC_HISTORY_MAX` | `10` | 設定 UI に表示する発話履歴の保持件数 |

### 操作

- アタマセンサ tap (Press) → 録音開始、Avatar が listening 顔 (doubt)
- 喋る (録音中は Speaker 切断、音声 feedback なし)
- 1.5 秒無音 (RMS が `-40 dBFS` 未満が継続) で自動 MIC_STOP → STT → POST
- 録音中の再 tap (Press) で toggle 即停止
- 15 秒で強制停止 (`--max-record-seconds` 相当の env で調整可)

### 設定 UI から確認・調整

`http://127.0.0.1:7897/` の voice fieldset で以下を実行時に変更可能:

- 音声対話モード ON/OFF (checkbox)
- appSessionId (空のままなら起動時に自動作成された専用 web session を継続使用)
- silence threshold (dBFS) / silence seconds / max record seconds の動的調整
- 直近 10 件の発話履歴 (5 秒ごと自動更新、各 entry は `[時刻] rec=録音秒 stt=処理秒 →status_code "STT結果"`)

履歴は POST 失敗時や STT 空文字でも残るので、「マイクは録れたが xangi に届いて
ない」「ノイズが拾われて hallucination が出てる」等のトラブルシュートが画面だけで完結する。

### 制約

- M5Stackchan K151 (Si12T 搭載) + シリアル backend (`--wifi` 不可) のみ
- 録音中は WAV 再生不可 (I2S 共有のため)、ファーム側で Speaker を end → Mic
  へ切り替え
- faster-whisper モデル初回 DL (small ≈ 462MB) は最初の transcribe で実行、
  初回 tap は遅延あり。事前に `uv run python -c "from xangi_stackchan.stt import load_model; load_model()"`
  で warm-up しておくと初回 STT が速い
- DGX Spark ARM64 では ctranslate2 の CUDA wheel が未対応で CPU のみ。
  small/int8 で 2 秒 WAV STT が 1.7 秒、VAD ON で無音 WAV は 0.04 秒。
  リアル会話に十分な速度
- 同じ USB serial port を複数プロセスから開こうとすると `fcntl.flock` で即 abort。
  pkill 等で wrapper だけ殺すと子プロセスが取り残されるバグの再発を防止
  (`STACKCHAN_NO_SERIAL_LOCK=1` で無効化可)

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
- **再起動・USB 抜き差し後に喋らなくなった (Linux)**: `/dev/ttyACMx` の番号が変わり、config.json の保存値とズレた可能性。config.json の `port` は CLI の `--port` より優先されるので、`~/.xangi/xangi-stackchan/config.json` の `instances.<id>.port` を `/dev/serial/by-id/...` の安定リンクに書き換える (「シリアルポートの指定」参照)。実際に使われたパスは起動ログの `serial_port` で確認できる
- **音が鳴らない**: `--volume 255` で最大音量
- **発話開始が遅い**: piper-plus は JSONL 入力の常駐プロセスとして保持する設計のため、初回だけモデルロードが入る。2 回目以降のレスポンスは低遅延になる
- **piper-plus の発話速度を変えたい**: `PIPER_LENGTH_SCALE=1.2` のように小さくすると速くなる (既定はモデルカード推奨の `1.5`)。発音品質とのトレードオフ
- **シリアルポートが掴まれている**: `lsof /dev/ttyACM0` で確認、`fuser -k /dev/ttyACM0` で解放。常駐起動した自分自身が掴んでる場合は kill してから再起動
- **`piper failed: ... Opset 5 ... opset 3 only` エラー**: 初回実行で生成された最適化キャッシュが onnxruntime と互換性が無い状態。`rm models/*.cpu.opt.onnx*` で解消
- **VOICEVOX 接続エラー**: `docker ps` でコンテナ起動確認、または `curl http://localhost:50021/version` で疎通確認
