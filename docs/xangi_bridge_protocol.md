# XangiBridge シリアルプロトコル仕様書

M5Stack CoreS3 系 (K151 / K151-R / CoreS3 単体) ファーム `examples/XangiBridge/` / M5Stack AtomS3R + Atomic Voice/Echo Base ファーム `examples/AtomVoiceBridge/` と Python 側
`src/xangi_stackchan/stackchan.py::StackchanSerial` の間で動く USB シリアル
プロトコル。xangi (or 任意ホスト) がデバイスに WAV/コマンドを流して喋らせる
ための共通仕様を定義する。

Step D-1 で **WAV / STATUS / VOLUME** を確定、コマンド枠 `FACE:` / `MOVE:` を予約。
Step D-2 で `FACE:<expr>` を Avatar 経由で実装 + WAV 再生中の口パク連動。
**Step E で `MOVE:<yaw,pitch>` を SCServo (自前 clean-room lib `lib/scservo/`)
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
| `STATUS` | なし | `{"state","volume","version","servo","torque","queued","playing"}` JSON | Step D-1 で初版、Step E で `servo`/`torque`、Step G で `queued`/`playing` 追加 |
| `VOLUME:<0-255>` | 0..255 整数 | `{"status":"ok","volume":N}` | Step D-1 |
| `WAV:<size>` | size = WAV 全長 byte | `READY\n` → バイナリ受信 → JSON ack | Step D-1 (口パク連動 D-2、**Step G で受信即 ack 化 + WAV キュー (4 slots)**) |
| `FACE:<expr>` | `neutral`/`happy`/`sad`/`doubt`/`sleepy`/`angry` | `{"status":"ok","face":"..."}` | Step D-2 (Avatar) |
| `MOVE:<yaw,pitch>` | zero ベース角度 (yaw ±100° / pitch ±30° SAFE) | `{"status":"ok","yaw":N,"pitch":N[,"requested_*","clamped":true]}` | Step E (SCServo) |
| `CAPTURE` | なし | `IMG:<size>\n` → JPEG バイナリ → JSON ack `{"status":"ok","size":N,"format":"jpeg","width":W,"height":H,"captured_at":<device ms>}` | Phase 1A (camera, CoreS3 GC0308) |

未知のコマンドは `{"status":"error","error":"unknown command","line":"..."}` を返す。
複数行のコマンドは扱わない (1 行 = 1 コマンド = 1 応答)。

## 3. WAV 転送シーケンス

最も重要なフロー。Python 側実装は `src/xangi_stackchan/stackchan.py::StackchanSerial.send_wav`。

Step G (xangi-bridge-0.4) で **受信即 ack + WAV キュー (4 slots ring buffer) + 別 RTOS task `wavPlayTask` 再生** に変更。これにより従来 `isPlaying()` 完了まで ack を待っていたぶん (1 WAV = 数秒〜十数秒) の block が無くなり、ホスト側 `send_wav` は WAV 受信完了 (= 数 ms) で次 chunk に進める。

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

1. `threading.RLock` を取得 (シリアル排他制御、Step F 以降)
2. `drain()` で受信バッファを空にする
3. `WAV:<size>\n` をシリアルに書く + flush
4. **3 秒以内に `READY\n` または JSON エラー応答を待つ**。`{` で始まる行が来た場合は早期エラー応答とみなして即返す
5. `chunk_size=1024` バイトずつバイナリを送信、各 chunk 後に `chunk_delay=5ms` sleep
6. **10 秒以内に JSON ack (1 行、`{` 始まり) を待つ**。Step G 以降のファームは受信完了で即 ack を返すので通常は数 ms 以内。タイムアウトしたら `{"status":"ok","size":N,"note":"no confirmation received"}` を返す
7. ack が `{"status":"error","error":"queue full"}` の場合は Lock を解放して **0.5 秒待ち → 最大 8 回まで retry** (キュー 4 slot なので最悪でも 1 chunk 再生時間 = 数秒で空く)。retry 中も他の send_command (MOVE / FACE / VOLUME) は Lock で直列化される

### 3.2 デバイス側挙動 (`examples/XangiBridge/main.cpp::handleWav` + `wavPlayTask`)

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
| `queue full` | `{"status":"error","error":"queue full"}` | Ready 維持 | コマンド受信時 (Step G 追加、ホスト側 retry) |
| ps_malloc 失敗 | `{"status":"error","error":"ps_malloc failed"}` | Ready 維持 | コマンド受信時 |
| 受信タイムアウト | `{"status":"error","error":"recv timeout"}` | free → 元 state 復帰 | バイナリ受信中 |
| `queue full after recv` | `{"status":"error","error":"queue full after recv"}` | free → 元 state 復帰 | 受信完了時 (キュー間チェック競合保険、通常起きない) |
| `playWav` 失敗 | `wavPlayTask` 内でログ出力 + free、ホストへの ack 無し | キュー残あり Playing / 無し Ready | wavPlayTask 内 (Step G、playWav 開始時) |

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

### 4.2 `VOLUME:<0-255>\n`

0..255 の整数を引数に取り、`M5.Speaker.setVolume()` を呼ぶ。範囲外は clamp。

```json
{"status":"ok","volume":160}
```

## 5. FACE (Step D-2) / MOVE (Step E)

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

### 5.2 WAV 再生中の口パク連動 (Step D-2)

`WAV:<size>` 受信完了後、`M5.Speaker.playWav` 実行中に `isPlaying()` ポーリング
ループから `avatar.setMouthOpenRatio(0.2..0.9)` を 80ms ごとに駆動する。再生終了で
`setMouthOpenRatio(0.0)` で口を閉じる。

ランダム開口で「喋ってる感」を出す簡易実装。将来 I2S サンプル振幅
ベースの精緻な口パクに置き換える余地あり。

### 5.3 `MOVE:<yaw_deg>,<pitch_deg>\n` (Step E)

zero ベース角度 (HomeCalibration の zero raw を 0° とした相対値)。SAFE 範囲は
**yaw `-100..+100°` / pitch `-30..+30°`** (`firmware/k151/lib/scservo/SCServo.h`
の `YAW_SAFE_MIN/MAX_DEG`, `PITCH_SAFE_MIN/MAX_DEG`、詳細は
`docs/scservo_protocol.md` §11)。

例: `MOVE:10,-5\n` で yaw +10° / pitch -5° に移動 (500ms `goalTimeMs` 既定)。

#### 5.3.1 起動シーケンス

XangiBridge は setup で:
1. `py32::enableServoPower()` で K151 ベースの VM_EN を ON
2. `servo.begin()` で UART1 (TX=G6/RX=G7) 1Mbps 開く
3. 安全側 `enableTorque(false)` 両軸 (手戻し可能)
4. NVS (`xstackchan` namespace、HomeCalibration が書き込む `yaw_zero` / `pitch_zero`) から zero raw load
5. `readPos` で疎通確認

いずれか失敗すると `g_servo_ready = false` でフォールバック (WAV/FACE は引き続き
動く、`MOVE:` だけが `{"status":"error","error":"servo not ready"}` で応答)。
HomeCalibration を先に焼いておくのが運用上の前提。

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
{"status":"error","error":"servo not ready (HomeCalibration required?)"}
{"status":"error","error":"setAngle failed"}
{"status":"error","error":"MOVE syntax: yaw,pitch"}
```

#### 5.3.3 torque 管理

初回 `MOVE:` 受信時に `ensureTorqueOn()` で両軸 torque ON、以後 torque ON のまま。
`STATUS` 応答の `torque` フィールドで現状を取得できる。
手戻ししたい場合は再起動 (起動直後は torque OFF)。

## 5.4 CAPTURE (Phase 1A、カメラ)

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

### 5.4.3 デバイス側挙動 (`examples/XangiBridge/main.cpp::handleCapture`)

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

## 7. テスト

### 7.1 デバイス側単体テスト

`scripts/test_xangi_bridge.py` (Step D-1 同梱):

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
| 2026-05-11 | Step D-1 で初版、STATUS / VOLUME / WAV を確定、FACE / MOVE は予約。version `xangi-bridge-0.1` |
| 2026-05-11 | Step D-2 で `FACE:<expr>` を Avatar 統合で実装、WAV 再生中の口パク連動 (`setMouthOpenRatio`) も追加。version `xangi-bridge-0.2` |
| 2026-05-11 | Step E で `MOVE:<yaw,pitch>` を SCServo 統合で実装、PY32 VM_EN ON + NVS zero load + SAFE clamp 透明 ack。STATUS に `servo`/`torque` フィールド追加。version `xangi-bridge-0.3` |
| 2026-05-12 | Step G で WAV キュー化 (4 slot ring buffer + `wavPlayTask` on core 1)。`handleWav` は受信完了で即 ack を返すように変更、`isPlaying()` 完了待ちは別タスクで実施。これでホスト側 `send_wav` の再生時間ぶん block が解消。エラー `queue full` 追加 + ホスト側 retry 機構。STATUS に `queued`/`playing` フィールド追加。version `xangi-bridge-0.4` |
| 2026-05-19 | Phase 1A で `CAPTURE` コマンド追加 (CoreS3 内蔵 GC0308 カメラ → JPEG → シリアル送信)。デバイス→ホスト方向のバイナリ転送パターンを新規導入 (`IMG:<size>\n` ヘッダ + バイナリ + ack JSON)。STATUS に `camera` フィールド追加。`M5CoreS3` ライブラリ依存追加 (PlatformIO `m5stack/M5CoreS3 @ ^1.0.1`)。Python 側 `StackchanSerial.capture` 実装、ブリッジに `/api/camera/snapshot.jpg` + `/api/camera/status` + `POST /api/camera/capture` 追加、設定 UI にカメラパネル (snapshot ボタン + プレビュー)。version `xangi-bridge-0.5` |
| 2026-05-19 | M5Stack AtomS3R + Atomic Voice Base / Atomic Echo Base 対応の `examples/AtomVoiceBridge/` を新規追加。ES8311 audio codec を M5Unified の `cfg.external_speaker.atomic_echo = true` で自動初期化。サーボ・カメラ非搭載なので `STATUS` の `servo` / `camera` は常に `false`、`MOVE` / `CAPTURE` は unavailable 応答。Serial baud は `115200` (AtomS3R USB-CDC 安定値)。LCD 128x128 で Avatar は scale 0.5 + position 調整。version `atom-voice-bridge-0.1` |
