# XangiBridge シリアルプロトコル仕様書

M5Stack CoreS3 系 (K151 / K151-R / CoreS3 単体) ファーム `examples/cores3/main/` / M5Stack AtomS3R + Atomic Voice/Echo Base ファーム `examples/atoms3r/main/` と Python 側
`src/xangi_stackchan/stackchan.py::StackchanSerial` の間で動く USB シリアル
プロトコル。xangi (or 任意ホスト) がデバイスに WAV/コマンドを流して喋らせる
ための共通仕様を定義する。

WAV シリアル受信実装 で **WAV / STATUS / VOLUME** を確定、コマンド枠 `FACE:` / `MOVE:` を予約。
Avatar 統合実装 で `FACE:<expr>` を Avatar 経由で実装 + WAV 再生中の口パク連動。
**サーボ統合 PR で `MOVE:<yaw,pitch>` を SCServo (自前ライブラリ `lib/scservo/`)
経由で実装**、PY32 IO Expander VM_EN ON + NVS zero load + 起動時 torque OFF +
SAFE 範囲 clamp + clamp 透明性 ack (`clamped:true` + `requested_*`)。これで
全コマンド (STATUS/VOLUME/WAV/FACE/MOVE) 実装完了 (version `xangi-bridge-0.3`)。

## 1. シリアル設定

| 項目 | 値 | 備考 |
|------|----|------|
| ボーレート | XangiBridge: `921600` / AtomVoiceBridge: `115200` | Python 側 `--baud` で指定、CoreS3 系 `DEFAULT_BAUD` は 921600 |
| データビット / パリティ / ストップ | 8N1 | Arduino `Serial.begin(baud)` デフォルト |
| フロー制御 | なし | |
| 物理層 | USB-Serial/JTAG (ESP32-S3) | CoreS3 / AtomS3R とも VID `0x303A` |
| 文字エンコード | UTF-8 (テキスト行) | ascii 範囲のみ使う想定 |
| 行末 | `\n` (LF) | `\r\n` (CRLF) も受け付ける (CR 無視) |

Python 側のポート検出は `serial.tools.list_ports` の VID で優先順位付け
(`0x303A` ESP32 > `0x10C4`/`0x1A86` USB-Serial ブリッジ)。

## 2. コマンド全体像

ホスト → デバイス: テキスト行 (\n 終端) のコマンド + 必要なら追従バイナリ。
デバイス → ホスト: JSON ack 1 行 (\n 終端) または `READY\n` (WAV 受信前の合図)。

| コマンド | 引数 | 応答 | 状態 |
|---------|------|------|------|
| `STATUS` | なし | `{"state","volume","version","servo","torque","queued","playing","image_face","battery_level","battery_voltage_mv","charging","reset_reason","uptime_ms","puzzle","puzzle_pattern","stack_led","stack_led_pattern"}` JSON | WAV シリアル受信実装 で初版、サーボ統合 PR で `servo`/`torque`、WAV キュー化 PR で `queued`/`playing` 追加、画像顔/バッテリー表示 PR で `image_face`/battery 追加、状態表示LED対応で `puzzle`/`stack_led` 追加 |
| `VOLUME:<0-255>` | 0..255 整数 | `{"status":"ok","volume":N}` | WAV シリアル受信実装 |
| `WAV:<size>` | size = WAV 全長 byte | `READY\n` → バイナリ受信 → JSON ack | WAV シリアル受信実装 (口パク連動 D-2、**WAV キュー化 PR で受信即 ack 化 + WAV キュー (4 slots)**) |
| `FACE:<expr>` | `neutral`/`happy`/`sad`/`doubt`/`sleepy`/`angry` | `{"status":"ok","face":"..."}` | Avatar 統合実装 (Avatar) |
| `IMAGE:<size>` | size = JPEG 全長 byte | `READY\n` → JPEG バイナリ受信 → `{"status":"ok","image":N,"battery_level":P}` | 画像顔表示 (スプライトを host 側で JPEG 化して送信) |
| `SIMG:<slot>,<size>` | slot = 0..63、size = JPEG 全長 byte | `READY\n` → JPEG バイナリ受信 → `{"status":"ok","sprite_cache":slot,"size":N}` | CoreS3 画像顔キャッシュ (PSRAM に JPEG を保持) |
| `SFRAME:<slot>` | slot = 0..63 | `{"status":"ok","sprite_frame":slot,"size":N,"battery_level":P}` | CoreS3 画像顔キャッシュの表示切替 |
| `MOVE:<yaw,pitch>` | zero ベース角度 (yaw ±100° / pitch ±30° SAFE) | `{"status":"ok","yaw":N,"pitch":N[,"requested_*","clamped":true]}` | サーボ統合 PR (SCServo) |
| `CAPTURE` | なし | `IMG:<size>\n` → JPEG バイナリ → JSON ack `{"status":"ok","size":N,"format":"jpeg","width":W,"height":H,"captured_at":<device ms>}` | カメラ統合 (CoreS3 GC0308) |
| `PUZZLE:<pattern>` | `off`/`red`/`green`/`blue`/`white`/`rainbow`/`thinking`/`talking`/`error` | `{"status":"ok","puzzle":"..."}` | CoreS3 Grove PORT.B の Puzzle Unit WS2812E 64 LED 制御 |
| `STACKLED:<pattern>` | `off`/`red`/`green`/`blue`/`white`/`rainbow`/`thinking`/`talking`/`error` | `{"status":"ok","stack_led":"..."}` | M5Stack 公式 StackChan K151 / K151-R 本体 12 RGB LED 制御 |
| `MIC_START` | なし | `{"status":"ok","mode":"recording","sample_rate":16000,"bits":16,"channels":1,"chunk_bytes":2048}` → 以後 `MIC_PCM:<size>\n<binary>` を繰り返し送出 | マイク統合 (CoreS3 内蔵 PDM、cores3-main-0.9) |
| `MIC_STOP` | なし | `{"status":"ok","mode":"speaker"}` | 録音停止 + Speaker 復帰 |
| `HEADPET_SOUND:<on\|off>` | `on` / `off` | `{"status":"ok","head_pet_sound":true\|false}` | なでなで反応 (スタンドアローン: head_touch で埋め込み音声 + 首振り) の有効/無効。既定 on。host が head_touch を自分で使う時 (voice_conversation / head-pet-reaction) は off にして二重発火を防ぐ。cores3-main-0.16 |

未知のコマンドは `{"status":"error","error":"unknown command","line":"..."}` を返す。
複数行のコマンドは扱わない (1 行 = 1 コマンド = 1 応答)。

### 2.1 画像顔キャッシュ (`SIMG` / `SFRAME`)

`IMAGE:<size>` は毎フレーム JPEG を転送するため、スプライトの常時アニメーションで
USB シリアルと LCD 更新を圧迫しやすい。CoreS3 系ファームは `SIMG` / `SFRAME`
で PSRAM 上に JPEG フレームをキャッシュできる。

```
host                                device
  |---- "SIMG:<slot>,<size>\n" ----->|  slot / size チェック、PSRAM 確保
  |<--- "READY\n" -------------------|
  |---- <size> bytes JPEG ---------->|
  |<--- '{"status":"ok",...}' -------|
  |
  |---- "SFRAME:<slot>\n" ---------->|  キャッシュ済み JPEG を LCD に描画
  |<--- '{"status":"ok",...}' -------|
```

ホスト側は `spritesheet.webp` から生成した JPEG を表情・フレーム位相ごとに一度だけ
`SIMG` で送る。以後のアニメーション tick は `SFRAME` の短いテキストコマンドだけを
送る。`spritesheet.webp` はローカルアセットで、リポジトリ管理外に置く。

## 3. WAV 転送シーケンス

最も重要なフロー。Python 側実装は `src/xangi_stackchan/stackchan.py::StackchanSerial.send_wav`。

WAV キュー化 (xangi-bridge-0.4) で **受信即 ack + WAV キュー (4 slots ring buffer) + 別 RTOS task `wavPlayTask` 再生** に変更。これにより従来 `isPlaying()` 完了まで ack を待っていたぶん (1 WAV = 数秒〜十数秒) の block が無くなり、ホスト側 `send_wav` は WAV 受信完了 (= 数 ms) で次 chunk に進める。

```
host                                device
  |                                    |
  |---- "WAV:<size>\n" --------------->|  パース、queue full / size 異常チェック
  |                                    |
  |  (a) queue full / size error 時:   |
  |<--- '{"status":"error",...}' ------|  ホスト側 0.5s 待ちで retry (最大 8 回)
  |                                    |
  |  (b) 正常 path:                    |
  |                              ps_malloc(size)、READY
  |<--- "READY\n" ---------------------|  受信準備 OK の合図
  |                                    |
  |---- <size> bytes binary ---------->|  1024 byte chunk、5ms 間隔
  |     (1024B chunk × N + remainder)  |  受信中 2000ms 空くと timeout
  |                                    |
  |                              wavQueuePush(buf, size) → 即 ack
  |<--- '{"status":"ok","size":N,      |  ack 後 host は次 chunk を送れる
  |       "queued":n}' ----------------|
  |                                    |
  |                          [wavPlayTask が別タスクで dequeue]
  |                          [M5.Speaker.playWav + isPlaying() polling]
  |                          [口パク連動、完了後 free]
```

### 3.1 ホスト側挙動 (Python `StackchanSerial.send_wav`)

1. `threading.RLock` を取得 (シリアル排他制御、TalkingSway PR 以降)
2. `drain()` で受信バッファを空にする
3. `WAV:<size>\n` をシリアルに書く + flush
4. **3 秒以内に `READY\n` または JSON エラー応答を待つ**。`{` で始まる行が来た場合は早期エラー応答とみなして即返す
5. `chunk_size=1024` バイトずつバイナリを送信、各 chunk 後に `chunk_delay=5ms` sleep
6. **10 秒以内に JSON ack (1 行、`{` 始まり) を待つ**。WAV キュー化以降のファームは受信完了で即 ack を返すので通常は数 ms 以内。タイムアウトしたら `{"status":"ok","size":N,"note":"no confirmation received"}` を返す
7. ack が `{"status":"error","error":"queue full"}` の場合は Lock を解放して **0.5 秒待ち → 最大 8 回まで retry** (キュー 4 slot なので最悪でも 1 chunk 再生時間 = 数秒で空く)。retry 中も他の send_command (MOVE / FACE / VOLUME) は Lock で直列化される

### 3.2 デバイス側挙動 (`examples/cores3/main/main.cpp::handleWav` + `wavPlayTask`)

**`handleWav` (loop task で実行、即 ack を目指す)**:

1. `WAV:<size>` パース、`size == 0` または `size > MAX_WAV_BYTES (4MB)` ならエラー応答
2. **キュー満杯チェック**: `wavQueueFull()` ならエラー応答 `{"status":"error","error":"queue full"}`、ホスト側 retry に任せる
3. `ps_malloc(size)` で PSRAM 上に受信バッファ確保、失敗時はエラー応答
4. `"READY\n"` 送信 + flush、state = Receiving
5. `Serial.readBytes` で chunk 受信、**最後の到着から 2000ms 空いたら timeout** で `free(buf)` してエラー応答
6. 受信完了で **`wavQueuePush(buf, size)` → 即 JSON ack** `{"status":"ok","size":N,"queued":n}` を送信。バッファ所有権は `wavPlayTask` に移る (loop task は free しない)

**`wavPlayTask` (core 1 で実行、再生を担当)**:

1. `wavQueueEmpty()` ならば `vTaskDelay(10ms)` でスピン
2. dequeue で head の WavSlot を取り出し、`g_wav_playing = true` + state = Playing
3. `M5.Speaker.playWav(buf, size, 1, 0, true)` 呼ぶ
4. `M5.Speaker.isPlaying()` をポーリング (50ms 間隔) して完了待ち、その間 `MOUTH_UPDATE_MS = 80ms` ごとに `avatar.setMouthOpenRatio` でランダム口パク
5. 完了後 `avatar.setMouthOpenRatio(0.0f)` + `free(buf)` + `g_wav_playing = false` + state = キュー残あれば Playing 維持 / 空なら Ready
6. 次の dequeue へ

これで「`isPlaying()` 完了まで ack を待たない」設計が実現し、ホスト側パイプライン送信が可能になる。`g_wav_playing` フラグは STATUS で公開、デバッグ時に再生状況を確認可能。

### 3.3 WAV フォーマット

| 項目 | 値 |
|------|----|
| コンテナ | RIFF/WAVE |
| サンプリングレート | 16000 Hz |
| ビット深度 | 16-bit signed |
| チャンネル数 | 1 (mono) |
| ヘッダサイズ | 44 byte (標準 fmt chunk + data chunk) |

`M5.Speaker.playWav` は内部で WAV ヘッダを parse する。16bit/mono は M5Unified
で確実に動作、24bit/float は端末や M5Unified バージョン依存で不安定なので、
**piper / VOICEVOX とも 16bit/mono/16kHz を出力させる**こと。M5Unified の `playWav`
で実機再生実績がある最も保守的な組合せ。

### 3.4 受信中のエラー

| エラー | デバイス側応答 | 状態遷移 | 発生タイミング |
|--------|---------------|----------|---------------|
| `size == 0` | `{"status":"error","error":"size=0"}` | Ready 維持 | コマンド受信時 |
| `size > MAX_WAV_BYTES` | `{"status":"error","error":"size exceeds MAX_WAV_BYTES"}` | Ready 維持 | コマンド受信時 |
| `queue full` | `{"status":"error","error":"queue full"}` | Ready 維持 | コマンド受信時 (WAV キュー化 PR で追加、ホスト側 retry) |
| ps_malloc 失敗 | `{"status":"error","error":"ps_malloc failed"}` | Ready 維持 | コマンド受信時 |
| 受信タイムアウト | `{"status":"error","error":"recv timeout"}` | free → 元 state 復帰 | バイナリ受信中 |
| `queue full after recv` | `{"status":"error","error":"queue full after recv"}` | free → 元 state 復帰 | 受信完了時 (キュー間チェック競合保険、通常起きない) |
| `playWav` 失敗 | `wavPlayTask` 内でログ出力 + free、ホストへの ack 無し | キュー残あり Playing / 無し Ready | wavPlayTask 内 (WAV キュー化、playWav 開始時) |

`playWav` 失敗は loop task の ack シーケンスを抜けてから起きるため、ホストへは個別 ack を返さない (受信完了の `{"status":"ok",...}` は既に返している)。再生失敗のシグナルが必要な場合は STATUS の `queued` / `playing` を polling するか、`wavPlayTask` 内のログ (`[bridge] playWav failed`) を見る。

## 4. STATUS / VOLUME

### 4.1 `STATUS\n`

引数なし。デバイスは次の JSON 1 行を返す:

```json
{"state":"ready","volume":128,"version":"xangi-bridge-0.5","servo":true,"torque":false,"camera":true,"queued":0,"playing":false}
```

| フィールド | 型 | 値 | 追加バージョン |
|-----------|----|----|----------------|
| `state` | string | `booting` / `ready` / `receiving` / `playing` / `error` | 0.1 |
| `volume` | int | 0..255 | 0.1 |
| `version` | string | `xangi-bridge-<MAJOR>.<MINOR>` | 0.1 |
| `servo` | bool | サーボ初期化に成功して MOVE が使えるか | 0.3 |
| `torque` | bool | サーボ torque が現在 ON か (初回 MOVE で true に) | 0.3 |
| `camera` | bool | GC0308 カメラ初期化に成功して CAPTURE が使えるか | 0.5 |
| `queued` | int | WAV 再生キューに積まれた未再生 slot 数 (0..3) | 0.4 |
| `playing` | bool | `wavPlayTask` が現在 1 つを再生中か | 0.4 |
| `puzzle` | bool | Puzzle Unit WS2812E 制御が使えるか | 0.19 |
| `puzzle_pattern` | string | 現在の Puzzle Unit pattern | 0.19 |
| `stack_led` | bool | K151 / K151-R 本体 12 RGB LED 制御が使えるか | 0.19 |
| `stack_led_pattern` | string | 現在の本体 LED pattern | 0.19 |

### 4.2 `VOLUME:<0-255>\n`

0..255 の整数を引数に取り、`M5.Speaker.setVolume()` を呼ぶ。範囲外は clamp。

```json
{"status":"ok","volume":160}
```

## 5. FACE / IMAGE / MOVE

### 5.1 `FACE:<expr>\n`

M5Stack-Avatar の `setExpression(Expression)` を呼ぶ。引数と enum の対応:

| 引数 | Expression |
|------|-----------|
| `neutral` | `Expression::Neutral` (デフォルト、起動時の表情) |
| `happy`   | `Expression::Happy` |
| `sad`     | `Expression::Sad` |
| `doubt`   | `Expression::Doubt` |
| `sleepy`  | `Expression::Sleepy` |
| `angry`   | `Expression::Angry` |

成功時応答:
```json
{"status":"ok","face":"happy"}
```

未知の引数:
```json
{"status":"error","error":"unknown face: <arg>"}
```

`FACE:` は単独で表情を切り替えるだけ。WAV 再生と独立に呼べる。再生中に呼んでも
口パクは中断されない (`setMouthOpenRatio` を口パクループが優先する)。

### 5.2 `IMAGE:<size>\n`

host 側で `spritesheet.webp` から状態ごとのセルを切り出し、320x240 JPEG に変換して送る。`--face-mode sprite` では row/state + filled-frame tick で `IMAGE` を周期送信し、まばたき/表情アニメーションする。ファームは `READY\n` 後に JPEG バイナリを受信し、Avatar draw task を停止して LCD に画像を表示する。右上には `M5.Power` 由来のバッテリー残量を overlay する。

成功時応答:
```json
{"status":"ok","image":11390,"battery_level":87}
```

`FACE:<expr>` を受けると Avatar draw task を再開し、通常の Avatar 顔に戻る。

### 5.3 WAV 再生中の口パク連動 (Avatar 統合実装)

`WAV:<size>` 受信完了後、`M5.Speaker.playWav` 実行中に `isPlaying()` ポーリング
ループから `avatar.setMouthOpenRatio(0.2..0.9)` を 80ms ごとに駆動する。再生終了で
`setMouthOpenRatio(0.0)` で口を閉じる。

ランダム開口で「喋ってる感」を出す簡易実装。将来 I2S サンプル振幅
ベースの精緻な口パクに置き換える余地あり。

### 5.4 `MOVE:<yaw_deg>,<pitch_deg>\n` (サーボ統合)

zero ベース角度 (home-calibration の zero raw を 0° とした相対値)。SAFE 範囲は
**yaw `-100..+100°` / pitch `-30..+30°`** (`firmware/lib/scservo/SCServo.h`
の `YAW_SAFE_MIN/MAX_DEG`, `PITCH_SAFE_MIN/MAX_DEG`、詳細は
`docs/scservo_protocol.md` §11)。

例: `MOVE:10,-5\n` で yaw +10° / pitch -5° に移動 (500ms `goalTimeMs` 既定)。

#### 5.3.1 起動シーケンス

XangiBridge は setup で:
1. `py32::enableServoPower()` で K151 ベースの VM_EN を ON
2. `servo.begin()` で UART1 (TX=G6/RX=G7) 1Mbps 開く
3. 安全側 `enableTorque(false)` 両軸 (手戻し可能)
4. NVS (`xstackchan` namespace、home-calibration が書き込む `yaw_zero` / `pitch_zero`) から zero raw load
5. `readPos` で疎通確認

いずれか失敗すると `g_servo_ready = false` でフォールバック (WAV/FACE は引き続き
動く、`MOVE:` だけが `{"status":"error","error":"servo not ready"}` で応答)。
home-calibration を先に焼いておくのが運用上の前提。

#### 5.3.2 応答フォーマット

通常 (SAFE 範囲内):
```json
{"status":"ok","yaw":10.00,"pitch":-5.00}
```

SAFE 範囲外で clamp された場合 (透明性のため `requested_*` と `clamped:true` を併記):
```json
{"status":"ok","yaw":100.00,"pitch":30.00,
 "requested_yaw":200.00,"requested_pitch":100.00,"clamped":true}
```

エラー (サーボ未準備 / setAngle 失敗 / syntax):
```json
{"status":"error","error":"servo not ready (home-calibration required?)"}
{"status":"error","error":"setAngle failed"}
{"status":"error","error":"MOVE syntax: yaw,pitch"}
```

#### 5.3.3 torque 管理

初回 `MOVE:` 受信時に `ensureTorqueOn()` で両軸 torque ON、以後 torque ON のまま。
`STATUS` 応答の `torque` フィールドで現状を取得できる。
手戻ししたい場合は再起動 (起動直後は torque OFF)。

## 5.4 CAPTURE (カメラ)

`CAPTURE\n` を受信すると、CoreS3 内蔵 GC0308 カメラ (320×240 RGB565 デフォルト) で
1 フレーム取得 → `frame2jpg(fb, 80, ...)` で JPEG エンコード → シリアル経由でホスト
に返す。サーボ非依存、`g_camera_ready=false` の場合は `{"status":"error","error":"camera not ready"}` を返す。

### 5.4.1 シーケンス

```
host                                device
  |                                    |
  |---- "CAPTURE\n" ------------------>|  パース、g_camera_ready チェック
  |                                    |
  |  (a) camera not ready 時:           |
  |<--- '{"status":"error",...}' ------|
  |                                    |
  |  (b) 正常 path:                    |
  |                            CoreS3.Camera.get() + frame2jpg(quality=80)
  |<--- "IMG:<size>\n" ----------------|  JPEG サイズ (bytes) ヘッダ
  |<--- <size> bytes JPEG binary ------|  1 chunk で全部書き出し
  |<--- '{"status":"ok","size":N,      |  ack JSON (メタデータ)
  |       "format":"jpeg","width":320, |
  |       "height":240,                |
  |       "captured_at":<device ms>}' -|
  |                                    |
  |                            CoreS3.Camera.free() + JPEG buf free()
```

### 5.4.2 ホスト側挙動 (Python `StackchanSerial.capture`)

1. `threading.RLock` を取得 (他コマンドと直列化)
2. `drain()` で受信バッファを空にする
3. `CAPTURE\n` をシリアルに書く + flush
4. **5 秒以内に `IMG:<size>\n` または JSON エラー応答を待つ**
5. JPEG バイナリ <size> bytes 読み (max 10 秒)、不足分は `Serial.read` で追加取得
6. **5 秒以内に JSON ack 行を待つ**。ack が来なくても JPEG は取れているので画像だけ返す (note: `ack missing`)
7. ack の `captured_at` (device millis) は `captured_at_device_ms` に rename して保持、`captured_at` は host epoch sec で上書き (鮮度判定用)

### 5.4.3 デバイス側挙動 (`examples/cores3/main/main.cpp::handleCapture`)

1. `g_camera_ready=false` の場合は即エラー応答
2. `avatar.setSpeechText("capturing")` で撮影中表示 (UX)
3. `CoreS3.Camera.get()` でフレーム取得 (camera_fb_t*)、`fb`/`fb->buf` チェック
4. `frame2jpg(fb, 80, &out_jpg, &out_jpg_len)` で JPEG エンコード
5. `CoreS3.Camera.free()` でフレーム解放 (JPEG バッファは別途保持)
6. `MAX_JPEG_BYTES` (256KB) 超過チェック
7. `IMG:<size>\n` 書き出し + `Serial.flush()` → JPEG 本体 1 chunk write
8. ack JSON 書き出し (`size`/`format`/`width`/`height`/`captured_at`)
9. JPEG バッファ `free()`、`setSpeechText("")` で撮影中表示クリア

### 5.4.4 カメラ仕様 (CoreS3 GC0308)

| 項目 | 値 | 備考 |
|------|----|------|
| センサ | Sony GC0308 0.3MP | CoreS3 ハードウェア統合 |
| 解像度 | 320×240 (デフォルト) | M5CoreS3 ライブラリ既定 |
| pixformat | RGB565 (デフォルト) | `frame2jpg` で JPEG 化 |
| JPEG quality | 80 | `JPEG_QUALITY` 定数、ファーム編集で変更可 |
| 期待 JPEG サイズ | 5〜30 KB | 写すものによる |
| シリアル送信時間 | ~50-300 ms | 921600bps / 80% 効率 / 30KB |

## 6. タイミングまとめ

| イベント | タイムアウト | 出所 |
|---------|------------|------|
| READY 待ち (ホスト側) | 3000 ms | `stackchan.py:82` |
| chunk 間の空き (デバイス側) | 500 ms | `main.cpp:WAV_CHUNK_TIMEOUT_MS` |
| 再生完了 ack 待ち (ホスト側) | 10000 ms | `stackchan.py:102` |
| chunk 送信間隔 | 5 ms | `stackchan.py::send_wav` 引数 `chunk_delay` |
| chunk サイズ | 1024 byte | `stackchan.py::send_wav` 引数 `chunk_size` |
| 再生完了ポーリング間隔 | 50 ms | `main.cpp:PLAY_POLL_INTERVAL_MS` |

## 5.5 MIC_START / MIC_STOP / MIC_PCM (マイク録音)

CoreS3 内蔵 PDM マイクで 16-bit signed mono PCM @ 16kHz を取得し、64ms 単位
(1024 sample = 2048 byte) のチャンクで `MIC_PCM:<size>\n<binary>` 形式で host に
流す。host 側で蓄積 → silero-vad で無音検出 → faster-whisper STT 想定。

### プロトコル

```
host                                device
  |---- "MIC_START\n" --------------->|  Speaker.end() → Mic.begin() → 録音タスク起動
  |<--- '{"status":"ok","mode":      |  Mic 初期化失敗時は Speaker 復帰 + error 応答
  |       "recording","sample_rate":  |
  |       16000,...}' ----------------|
  |                                    |
  |  以後、64ms ごとに繰り返し:      |  micRecordTask (priority 3)
  |<--- "MIC_PCM:2048\n" -------------|  ヘッダ (改行終端 ASCII)
  |<---  <2048 bytes int16 LE> ------|  バイナリ本体 (改行・delimiter 無し)
  |                                    |
  |---- "MIC_STOP\n" ---------------->|  録音タスク終了 → Mic.end() → Speaker.begin()
  |<--- '{"status":"ok",              |
  |       "mode":"speaker"}' ---------|  Speaker 復活で WAV 再生再開可能
```

### 制約

- CoreS3 は Mic と Speaker が I2S を共有するため、録音中は WAV 再生不可。host
  側は `mic_recording` フィールドで polling、または MIC_START ack 後の挙動として
  「録音中は send_wav しない」を実装する想定。
- MIC_STOP 受信前にファームが送る最後の MIC_PCM チャンク (~64ms 分) がある。
  host 側は MIC_STOP ack 待ちループで MIC_PCM ヘッダを見たら binary を吸い続ける
  実装が必要 (`StackchanSerial.stop_mic_recording` は対応済み)。
- ファーム再起動 (フラッシュ書き換え後等) で `g_volume` が初期値 128 にリセット。
  host 側は必要に応じて MIC_STOP 直後 (= Speaker 復帰直後) に `VOLUME:<N>` を
  送り直す。

### サンプル (Python)

```python
from xangi_stackchan.stackchan import StackchanSerial
s = StackchanSerial("/dev/ttyACM0", 921600)
s.open()
s.start_mic_recording()           # → ack {"status":"ok","mode":"recording",...}
time.sleep(2.0)                   # 2 秒録音
result = s.stop_mic_recording()   # → {"status":"ok","mode":"speaker", "pcm":bytes, "wav":bytes, ...}
with open("/tmp/recorded.wav", "wb") as f:
    f.write(result["wav"])
```

## 6.5 デバイス → ホスト 非同期イベント行

`pollSerialCommand` のコマンド応答とは別に、デバイス側から自発的に流れる JSON 行。
ホスト側は `SerialReader` (別 thread) で逐次読み取り、`event` フィールドで dispatch する。

| event | フィールド | 出所 | 用途 |
|------|----------|------|------|
| `audio_stopped` | `{"reason":"touch","at":<ms>}` | `cores3-main 0.7` / `pollTouchStop` (LCD 長押し 1s) | host 側で現 turn の後続 WAV chunk を skip + FACE idle + MOVE ホーム復帰 |
| `head_touch` | `{"gesture":"press"\|"release"\|"swipe_forward"\|"swipe_backward","at":<ms>}` | `cores3-main 0.8` / `headTouchPollTask` (Si12T 50ms polling) | M5Stack 公式 StackChan K151 のアタマタッチセンサ。`press` を音声入力開始トリガに使う想定 |
| `head_pet_sound` | `{"played":true\|false,"at":<ms>}` | `cores3-main 0.16` / `playNadeReaction` | なでなで反応が発火した通知。`played` は埋め込み音声を鳴らしたか (発話中=他WAV再生中は false で首振りのみ)。首振り + Happy 顔は発話中でも常に反応。host 連携時の観測/デバッグ用 (host は無視してよい) |

注意:

- 行頭は必ず `{` で始まる JSON。コマンド応答と区別がつくよう、ホスト側 `SerialReader` は `event` フィールドの有無で dispatch する。
- `head_touch` の `press` 後に `release` または `swipe_*` が続く (state machine: Idle → Touched → [Swiping] → Idle)。
- K151 / K151-R 以外 (CoreS3 単体 / Basic / AtomS3R) は I2C bus 上に Si12T が存在しないので `head_touch_ready=false`、本イベントは流れない。`STATUS` の `head_touch` フィールドで判定可能。

## 7. テスト

### 7.1 デバイス側単体テスト

`scripts/test_xangi_bridge.py` (WAV シリアル受信実装 同梱):

```bash
uv run python scripts/test_xangi_bridge.py --port /dev/ttyACM0
```

シーケンス:
1. `STATUS` 送信 → `ready` 応答
2. `VOLUME:160` 送信 → ok 応答
3. 440Hz 1.0s の sine tone WAV を組み立てて `send_wav` で送信 → 実機で音を聞く

### 7.2 Python 統合テスト

`src/xangi_stackchan` の高位 API (`StackchanConfig`, `create_backend`) を通して、
xangi 連携の本番経路で再生できることを確認。

## 8. 変更履歴

| 日付 | 内容 |
|------|------|
| 2026-05-11 | WAV シリアル受信実装 で初版、STATUS / VOLUME / WAV を確定、FACE / MOVE は予約。version `xangi-bridge-0.1` |
| 2026-05-11 | Avatar 統合実装 で `FACE:<expr>` を Avatar 統合で実装、WAV 再生中の口パク連動 (`setMouthOpenRatio`) も追加。version `xangi-bridge-0.2` |
| 2026-05-11 | サーボ統合 PR で `MOVE:<yaw,pitch>` を SCServo 統合で実装、PY32 VM_EN ON + NVS zero load + SAFE clamp 透明 ack。STATUS に `servo`/`torque` フィールド追加。version `xangi-bridge-0.3` |
| 2026-05-12 | WAV キュー化 PR で WAV キュー化 (4 slot ring buffer + `wavPlayTask` on core 1)。`handleWav` は受信完了で即 ack を返すように変更、`isPlaying()` 完了待ちは別タスクで実施。これでホスト側 `send_wav` の再生時間ぶん block が解消。エラー `queue full` 追加 + ホスト側 retry 機構。STATUS に `queued`/`playing` フィールド追加。version `xangi-bridge-0.4` |
| 2026-05-19 | カメラ統合 PR で `CAPTURE` コマンド追加 (CoreS3 内蔵 GC0308 カメラ → JPEG → シリアル送信)。デバイス→ホスト方向のバイナリ転送パターンを新規導入 (`IMG:<size>\n` ヘッダ + バイナリ + ack JSON)。STATUS に `camera` フィールド追加。`M5CoreS3` ライブラリ依存追加 (PlatformIO `m5stack/M5CoreS3 @ ^1.0.1`)。Python 側 `StackchanSerial.capture` 実装、ブリッジに `/api/camera/snapshot.jpg` + `/api/camera/status` + `POST /api/camera/capture` 追加、設定 UI にカメラパネル (snapshot ボタン + プレビュー)。version `xangi-bridge-0.5` |
| 2026-05-19 | M5Stack AtomS3R + Atomic Voice Base / Atomic Echo Base 対応の `examples/atoms3r/main/` を新規追加。ES8311 audio codec を M5Unified の `cfg.external_speaker.atomic_echo = true` で自動初期化。サーボ・カメラ非搭載なので `STATUS` の `servo` / `camera` は常に `false`、`MOVE` / `CAPTURE` は unavailable 応答。Serial baud は `115200` (AtomS3R USB-CDC 安定値)。LCD 128x128 で Avatar は scale 0.5 + position 調整。version `atom-voice-bridge-0.1` |
| 2026-05-26 | M5Stack 公式 StackChan K151 アタマタッチセンサ (Si12T、3 ch capacitive、I2C 0x68 on internal bus SDA=12/SCL=11) 対応の `lib/si12t/` + `examples/cores3/main/` 統合追加。50ms polling task で Press / Release / SwipeForward / SwipeBackward の 4 ジェスチャを検出して `{"event":"head_touch","gesture":"...","at":<ms>}` 行で host に通知。bus 上に device が無い CoreS3 単体機では `pingDevice` 失敗で `head_touch_ready=false`、graceful degradation で他機能 (WAV/FACE/MOVE/CAPTURE) は影響なし。STATUS に `head_touch` フィールド追加。version `cores3-main-0.8` |
| 2026-05-27 | マイク録音 PCM stream (`MIC_START` / `MIC_STOP` / `MIC_PCM:<size>\n<binary>`) を CoreS3 内蔵 PDM 経由で実装。16kHz / 16-bit signed mono、64ms (= 1024 sample = 2048 byte) 単位のチャンク配信。Mic と Speaker は I2S を共有するため、`MIC_START` で `M5.Speaker.end()` → `M5.Mic.begin()`、`MIC_STOP` で `M5.Mic.end()` → `M5.Speaker.begin()` + `setVolume(g_volume)` で復帰。録音中は WAV 再生不可。STATUS に `mic_recording` フィールド追加。host 側 `StackchanSerial.start_mic_recording()` / `stop_mic_recording()` で利用、結果の `wav` フィールドが faster-whisper / wave モジュール投入可能な valid WAV bytes。実機検証 K151 で 2 秒録音 → 16kHz mono WAV (34816 frames / 69676 byte) 取得 + 部屋音 -22.6 dBFS 確認 + Speaker 復帰確認。次フェーズで silero-vad 無音検出 + Whisper STT + head_touch 連動 + カメラ snapshot 同梱を別 PR で予定。version `cores3-main-0.9` |
| 2026-05-31 | `IMAGE:<size>` を追加し、host から受信した 320x240 JPEG を LCD に画像顔として表示。Python 側は `--face-mode sprite` で `spritesheet.webp` から状態別セルを切り出して送信する。スプライト資産は `assets/pets/` にローカル配置し `.gitignore` 対象。CoreS3 ファームは Avatar 顔 / 画像顔の両モードでバッテリー残量表示、STATUS に `image_face` / `battery_level` / `battery_voltage_mv` / `charging` を追加。version `cores3-main-0.15` |
| 2026-06-05 | なでなで反応 (スタンドアローン) を追加。head_touch の press / swipe で埋め込み音声 (`nade_voices.h`、piper つくよみちゃん合成 5 種、11025Hz mono) をランダム再生 + Happy 顔 + 首振り (`nadeWiggle`、上向き + 左右ふりふり)。ホスト PC / AI 連携なしで電源 ON だけで反応するデモ用機構。`HEADPET_SOUND:on\|off` コマンドと STATUS `head_pet_sound` フィールド、`head_pet_sound` event を追加。スタンドアローン既定音量を 255 に。servo を host MOVE (`handleMove`) と首振り (`nadeWiggle`) の 2 タスクから安全に共有するため `g_servo_mutex` を導入し、UART transaction を直列化 (xangi 連携 + スタンドアローン両立)。voice_conversation / host head-pet-reaction 時は host が `HEADPET_SOUND:off` を送って二重発火を抑止。version `cores3-main-0.16` |
| 2026-06-13 | 予期しない再起動 (panic / watchdog / brownout) の事後診断のため、boot 時に `{"event":"boot","reset_reason":"..."}` を 1 行出力し、STATUS に `reset_reason` / `uptime_ms` フィールドを追加。再起動の瞬間のログは USB 再列挙で host に届かないため、次回 boot に痕跡を残す方式。version `cores3-main-0.19` |
| 2026-06-21 | 画像顔 / sprite 表示中の `receiving` / `playing` / `ready` 状態遷移で Avatar の状態文字列が一瞬 LCD に漏れないよう、`setState` で Avatar draw task を再 suspend し、保持中の画像顔を即再描画するガードを追加。version `cores3-main-0.20` |
