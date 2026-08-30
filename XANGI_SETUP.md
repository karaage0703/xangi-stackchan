# xangi-stack-chan Extension セットアップ

このsetupは説明だけで終えず、AIが現在の環境を診断し、不足物の導入、設定、起動、実機検証まで行う。既に動いている項目は再実行しない。

## 完了条件

- Extensionがlinkedかつ`running / healthy / ready = true`
- 対象機種、USB serial port、baud、firmware `STATUS`を確認済み
- `STATUS`のversionと、実機に存在するcamera・head_touch・servo等のcapabilityを記録
- TTSが`piper`なら実行ファイル・model・configが存在し、デモ発話が成功
- camera対応機ではsnapshot取得、head_touch対応機では利用者によるタッチ確認が成功
- 設定が対象xangi instanceのnamespaceへ保存され、再起動後もstatus/doctor成功

未決の機種選択、ファーム書き込み許可、実機での聴取・タッチ確認が残る間は「セットアップ完了」と報告しない。

## 1. 現状を読み取る

1. manifest、README、`docs/usage.md`、`firmware/README.md`、`skills/xs-xangi-stackchan/SKILL.md`を読む。
2. extension rootで`uv sync`を実行する。
3. `command -v pio`、`tools/piper`、`models/tsukuyomi-chan-6lang-fp16.onnx`、`models/config.json`の有無を確認する。
4. `/dev/serial/by-id/`を優先してserial deviceを列挙する。設定済みconfigがあれば、接続中の実deviceと一致するか確認する。
5. 既存のstandalone版や別Extensionが同じUSBを保持していないか、PIDと`/proc/<pid>/cwd`で特定する。勝手に全processを停止せず、重複する対象だけを利用者へ示す。

## 2. Piperを自動準備する

TTSがPiperで、実行ファイル・model・configのいずれかが無い場合は、extension rootで`./scripts/setup_piper.sh`を実行する。対応OS/CPU、download、展開、実行権限、model sizeを確認する。既存ファイルが揃っていれば再downloadしない。絶対pathを対象instance設定へ保存する。VOICEVOXまたは`tts=none`を利用者が明示した場合はPiperを導入しない。

## 3. firmwareを診断する

まず現在のfirmwareを破壊せずに確認する。Extensionが起動済みなら次のmanaged APIを使う。

```bash
xangi tool extension_request --id xangi-stackchan --capability device.stackchan --path /api/device/status
```

起動前でUSBが空いている場合は、CoreS3系は921600 baud、AtomS3R/Basicは115200 baudを候補として`STATUS`を試し、応答した値だけを採用する。

- CoreS3 / K151 / K151-R: `cores3-main`, 921600 baud
- AtomS3R + Atomic Voice/Echo Base: `atoms3r-main`, 115200 baud
- M5Stack Basic + アールティ Ver.β: `basic-main`, 115200 baud

USB識別だけで機種を一意に決められない場合は、検出結果と候補を示して利用者に1回だけ質問する。`STATUS`が応答し、必要なcommandとcapabilityを備える場合はflashしない。応答しない、versionが非対応、または必要capabilityが欠ける場合だけ、対象機種、PlatformIO env、serial port、現在分かっている影響を提示し、firmware書き込みの明示許可を待つ。

## 4. 許可された場合だけbuild・flashする

`pio`が無ければ`uv tool install platformio`で導入する。Extensionを停止してUSB解放を確認後、まず`cd firmware && pio run -e <確認済みenv>`でbuildする。成功後だけ、同じenvと確認済みportへ`pio run -e <確認済みenv> -t upload --upload-port <確認済みport>`でuploadする。

K151/K151-Rのhome calibrationはサーボ原点を書き換える別操作なので、本体firmwareと一緒に自動実行しない。必要性を説明して別途許可を得る。flash後はUSB再enumerationを待ち、stable `/dev/serial/by-id/...`を再取得して`STATUS`を確認する。

## 5. Extensionを登録・設定・起動する

manifestをxangiへlinkし、対象xangi instanceのnamespaceにstable serial port、baud/device profile、TTS種別、Piperの絶対path、利用者が選んだ各機能を保存する。managed起動ではxangiがイベントURL、instance ID、認証tokenを渡すため、これらを設定ファイルへ固定保存しない。standalone版がある場合は設定をバックアップしてから、同じUSBを掴むprocessだけを停止する。

## 6. 実機で検証する

1. Extensionのstatus/doctorを確認し、固定portではなくmanaged APIで`/api/health`を確認する。
   ```bash
   xangi tool extension_request --id xangi-stackchan --capability device.stackchan --path /api/health
   ```
   `service_running`と`device_connected`を分け、現在の`transport`が期待値と一致することを確認する。serviceが起動していても実機未接続なら`ready=false`が正しい。
2. `/api/device/status`でfirmware versionとcapabilityを再確認する。
3. Piper利用時は`/api/demo`で短い日本語を1回発話させ、API送信成功だけでなく利用者の聴取を確認する。
4. cameraがtrueなら`POST /api/camera/capture`とsnapshot JPEGを確認する。
5. head_touchがtrueでなでなでを有効にした場合は、設定のversion更新とruntime反映を確認し、利用者へ頭部タッチを依頼して反応を確認する。反映失敗時だけExtensionをrestartする。
6. 完了通知を有効にした場合は、閾値以上の実turnで`turn.started`から`turn.complete`までの本番経路を確認する。手動demoを完了通知の確認として扱わない。
7. 通知されない場合は`/api/diagnostics`を取得し、最終イベント、完了通知の判定理由、TTS/device送信errorを確認する。認証tokenや発話本文は診断へ保存しない。

設定画面または`/api/config`で保存した変更はruntimeへ即時通知される。USB/Tailnet切替、通常応答、完了通知、LCDマイク、本体内蔵なでなで音声の変更で、SSEイベントやstream timeoutを待たず、Extension再起動も要求しない。保存後はconfig versionの増加とhealthのtransport・接続状態を確認する。

## 7. 利用スキルの追加・更新を提案する

初回setupまたはExtension更新の確認後、同梱スキル`skills/xs-xangi-stackchan/SKILL.md`をworkspaceへ追加・更新する必要があるか確認する。

1. workspaceに`skills/`、`.agents/skills/`、`.claude/skills/`のどれがあるか確認し、既存の配置規則を優先する。規則がなければ`skills/xs-xangi-stackchan/`を提案する。
2. 同名スキルがある場合は、固定port、standalone起動、process停止、Managed Extension、`extension_request`、health/diagnosticsの手順を比較する。実質的な差分がある場合だけ理由と対象pathを示す。
3. Extensionのsetupまたは更新承認はworkspace側のファイル変更承認を兼ねない。次の選択肢を提示し、選択されるまでworkspaceを変更しない。

- 推奨: スキルを追加・更新
- workspaceは変更せず、Extensionだけ利用

更新後は、配置path、差分、`extension_request`によるhealth取得結果を報告する。子processのportや認証tokenは取得・記録しない。

失敗した場合は、Piper、USB、firmware、Extension proxy、設定namespaceのどこまで成功したかを分けて報告する。既存設定のバックアップと復旧方法を保持する。
