---
name: xs-xangi-stackchan
description: xangi-stack-chan Managed Extensionを設定・診断・操作するスキル。USB/Tailnet接続、音声応答、完了通知、LCDマイク、カメラ、ダンス、health確認に使用する。「スタックチャンの状態を見て」「xangi-stack-chanを設定して」「スタックチャンで喋らせて」で使用。
---

# xangi-stack-chan

xangiのManaged Extensionとして動くxangi-stack-chanを、親xangiの正規経路から操作する。

## 原則

- Managed Extensionを標準運用とし、子processを直接起動・停止しない。
- 子processの固定portを推測して直接curlしない。
- 認証tokenを取得・表示・保存しない。
- API操作は`xangi tool extension_request`を使う。
- 設定画面はxangiのExtensions画面から開く。
- USBを使う別processが疑われても、曖昧なprocess名による一括停止は行わない。PID、command、`/proc/<pid>/cwd`で対象を特定する。
- firmware書き込み、本番xangiの再起動、公開releaseは別の明示許可を必要とする。

## Step 1: Extensionの状態を確認する

```bash
xangi extension status xangi-stackchan
xangi extension doctor xangi-stackchan
```

停止中で、起動が依頼範囲に含まれる場合だけ次を実行する。

```bash
xangi extension start xangi-stackchan
```

`running`はservice processの状態、`ready`は選択中transportのdevice接続状態として分けて読む。serviceが動いていてもUSB切断中またはTailnet接続待ちなら`ready=false`が正常。

## Step 2: healthと診断を確認する

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/health
```

主なフィールド:

- `service_running`: ExtensionのHTTP serviceが稼働しているか
- `transport`: `usb` / `tailnet` / `simulator`
- `device_connected`: 選択中transportでdeviceが接続済みか
- `ready`: deviceを利用できるか
- `config_version`: 保存済み設定のruntime世代
- `diagnostics.last_event`: 最後に受信したxangiイベントの種類と時刻
- `diagnostics.last_completion`: 最後の`turn.complete`を通知したか、除外理由
- `diagnostics.last_error`: 直近のTTSまたはdevice送信error

同じ情報だけを取得したい場合は`/api/diagnostics`を使える。診断には認証tokenや発話本文を含めない。

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/diagnostics
```

完了通知の主な理由:

- `notified`: 閾値を満たし通知した
- `notifications_disabled`: 完了通知がOFF
- `below_threshold`: 実行時間が閾値未満
- `duplicate`: 同じturnの重複eventを除外
- `start_time_missing`: `turn.started`の時刻を復元できない

`last_event`が更新されていなければ、通知設定やTTSより前にxangiイベント受信経路を確認する。`last_completion.notified=true`で音が出ない場合は`last_error`とdevice接続状態を確認する。

## Step 3: 設定を確認・更新する

現在設定:

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/config
```

一部更新の例:

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/config \
  --method POST \
  --body-json '{"speak_responses":false,"completion_notifications":true}'
```

設定保存はSSEイベントやstream timeoutを待たずruntimeへ通知される。USB/Tailnet切替、通常応答、完了通知、LCDマイク、本体内蔵なでなで音声のON/OFFにExtension再起動を要求しない。

更新後は`/api/config`の`version`増加と、`/api/health`のtransport・接続状態を確認する。接続先が変わる設定では、一時的な`ready=false`と再接続を区別する。

## Step 4: deviceを確認する

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/device/status
```

`STATUS`からfirmware version、servo、camera、mic、head touch等の実capabilityを確認する。backendが存在するだけで接続済みと判断しない。

対応transport:

- `usb`: hostからUSB serialで接続
- `tailnet`: CoreS3側からreverse TCPで接続
- `simulator`: browser simulator用のin-memory backend

## Step 5: 機能を操作する

### ダンス・音声

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/demo \
  --method POST \
  --body-json '{"text":"動作確認です","preset":"happy"}'
```

API成功だけで実機発話成功とせず、利用者の聴取も確認する。

### カメラ

```bash
xangi tool extension_request \
  --id xangi-stackchan \
  --capability device.stackchan \
  --path /api/camera/capture \
  --method POST
```

画像本体の取得方法は親xangiの`extension_request`契約に従う。固定portへ切り替えない。

### LCDマイク

`lcd_mic_voice=true`とdevice側mic capabilityを確認する。`/api/voice/history`にはSTTと送信結果の履歴があるため、入力未検知、STT空、xangi送信失敗、返答TTS失敗を順に切り分ける。秘密や音声本文を外部へ転載しない。

## Step 6: 初回導入・firmware更新

初回導入、Piper準備、機種判定、build・flashはrootの`XANGI_SETUP.md`に従う。既存firmwareの`STATUS`を先に確認し、必要なcapabilityが揃っていればflashしない。書き込みが必要な場合は、機種・environment・port・影響を示して明示許可を得る。

## standalone運用

Managed Extensionを利用できない開発・復旧時だけ、rootの`docs/usage.md`にあるstandalone手順を使う。Managed Extensionと同じUSB deviceを同時に開かない。standaloneの存在を理由にManaged Extensionを再起動したり、無関係なprocessを停止したりしない。

## 完了前チェックリスト

- [ ] Managed Extensionのstatusとdoctorを確認した
- [ ] 固定portや認証tokenを使わず`extension_request`で操作した
- [ ] service状態とdevice接続状態を分けて確認した
- [ ] 設定変更後のversionとruntime反映を確認した
- [ ] 実機操作はAPI応答だけでなく利用者の確認も得た
- [ ] firmware書き込みや再起動が必要なら別途許可を得た
