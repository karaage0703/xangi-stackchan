# Feetech SCS シリアルサーボ プロトコル仕様書

`firmware/lib/scservo/` の **元プロトコル仕様書**。M5Stack 公式 K151 / K151-R に搭載されている Feetech SCS0009 系シリアルサーボを Arduino C++ で叩くためのプロトコル仕様をここに集約する。

## 参考にしたソース

このドキュメントは [stack-chan/stack-chan](https://github.com/stack-chan/stack-chan) の `firmware/stackchan/drivers/scservo.ts` (Apache License 2.0、Copyright 2021 Shinya Ishikawa) をプロトコル仕様書として参照して作成した。バイトフォーマット・コマンドコード・ADDRESS マップ・チェックサム計算のロジックを参考にしている。

> Portions of this document and the related Arduino C++ implementation in `firmware/lib/scservo/` are derived from `stack-chan/stack-chan` (Apache License 2.0, Copyright 2021 Shinya Ishikawa). Bytes format, command codes, and register addresses were referenced as protocol specification.

---

## 1. ハードウェア仕様（K151 / K151-R）

| 項目 | 値 |
| --- | --- |
| サーボ型番 | SCS0009 (Feetech 系シリアルバスサーボ) |
| バス通信 | **UART (シリアル、PWM ではない)** |
| ボーレート | **1,000,000 bps (1 Mbps)** |
| 接続 | デイジーチェーン、複数サーボ 1 バス |
| サーボ ID 割当 (K151) | yaw (首振り) = ID 1、pitch (うなずき) = ID 2 |
| GPIO (CoreS3) | TX = G6、RX = G7 (M5Stack 公式 K151 ピンアサイン) |
| 可動域 (物理的限界、超過で破損) | yaw: −128°〜+128° / pitch: 5°〜85° |
| Motion API 角度単位 | 10 = 1°（例: `move(300, 600)` = X 30° / Y 60°） |

> ⚠️ **可動域を超えると物理破損する**。ソフト側で必ず clamp すること。元祖 stack-chan の `scservo-driver.ts` では pitch を `-25..+10°` に制限していたが、これは元祖ハードの値で K151 とは別仕様。**K151 の実機仕様に合わせて clamp 値を決める**こと。

**注**: 元祖 stack-chan の `scservo.ts` ではデフォルト GPIO を `TX=17, RX=16, port=2` としているが、これは ESP32 上で ModdableSDK が動く前提。**M5Stack 公式 K151 (CoreS3) では TX=G6, RX=G7, UART1 を使う**。Arduino C++ 実装ではここを CoreS3 のピンアサインに合わせる。

---

## 2. シリアル通信パラメータ

```
baud rate : 1,000,000 bps
data bits : 8 (Feetech 標準、要実機確認)
stop bits : 1 (Feetech 標準、要実機確認)
parity    : none (Feetech 標準、要実機確認)
flow ctrl : なし
```

元祖 `scservo.ts` の `PacketHandler` 設定:

```ts
new PacketHandler({
  receive: 16,
  transmit: 17,
  baud: 1_000_000,
  port: 2,
})
```

ボーレート以外のシリアルパラメータ (data/stop/parity) は ModdableSDK の `embedded:io/serial` のデフォルトに依存しているため明示記載なし。Feetech SCS の汎用仕様 (8N1) に従う。

---

## 3. パケットフォーマット

### 基本構造

```
+-------+-------+--------+--------+--------+----------+----------+
| 0xFF  | 0xFF  | ID     | LEN    | CMD    | DATA...  | CHECKSUM |
+-------+-------+--------+--------+--------+----------+----------+
  byte0   byte1   byte2    byte3    byte4    byte5..    末尾1byte
```

| フィールド | サイズ | 説明 |
| --- | --- | --- |
| HEADER | 2 byte | 固定値 `0xFF 0xFF` |
| ID | 1 byte | 対象サーボ ID (1〜252)、255 = ブロードキャスト |
| LEN | 1 byte | CMD + ADDR + DATA 部のバイト数 + 1 (checksum を含めない、CMD/ADDR 含む) |
| CMD | 1 byte | 命令コード (下表) |
| DATA | 可変長 | ADDRESS と書き込みデータ等 |
| CHECKSUM | 1 byte | byte2 (ID) 以降の合計値のビット反転下位 8 bit |

### 定数

```c
#define SCS_HEADER_BYTE    0xFF
#define SCS_BROADCAST_ID   0xFE  // 254 (元祖実装値、Feetech 標準は 0xFE)
#define SCS_MAX_ID         0xFC  // 252
#define SCS_END_MARKER     0x00
```

> **注**: 元祖 `scservo.ts` 上では `BROADCAST_ID = 0xfe` と定義されている。Feetech 公式仕様書 (要確認) では `0xFE` が標準のはず。ID 0〜252 は個別、253〜254 は予約・ブロードキャスト、255 は不可。

---

## 4. コマンドコード (CMD)

```c
#define SCS_CMD_RESPONSE      0x00  // ステータス応答（サーボ → ホスト）
#define SCS_CMD_RESPONSE_ALT  0x01  // 一部バージョンの応答コード
#define SCS_CMD_READ          0x02  // メモリ読み出し
#define SCS_CMD_WRITE         0x03  // メモリ書き込み
```

元祖 `scservo.ts` の定義:

```ts
const COMMAND = {
  RESPONSE: 0x00,
  RESPONSE_ALT: 0x01,
  WRITE: 0x03,
  READ: 0x02,
} as const
```

> Feetech SCS は Dynamixel と類似の命令体系だが、PING / SYNC_WRITE / REG_WRITE / ACTION / FACTORY_RESET 等の上位命令は元祖実装には含まれていない。実装は **READ / WRITE の 2 命令のみで首ふり制御を完結している**。

---

## 5. ADDRESS マップ (主要レジスタ)

```c
#define SCS_ADDR_ID                  5    // サーボ ID (1 byte)
#define SCS_ADDR_OFFSET              31   // 角度オフセット (2 byte、ビッグエンディアン)
#define SCS_ADDR_TORQUE_ENABLE       40   // トルク有効 (1 byte: 0 / 1)
#define SCS_ADDR_GOAL_ACC            41   // 加速度 (1 byte)
#define SCS_ADDR_GOAL_POSITION       42   // 目標位置 (2 byte、ビッグエンディアン、0..1023)
#define SCS_ADDR_GOAL_TIME           44   // 移動時間 (2 byte、ビッグエンディアン)
#define SCS_ADDR_LOCK                48   // EEPROM ロック (1 byte: 0 / 1)
#define SCS_ADDR_PRESENT_POSITION    56   // 現在位置 (2 byte、ビッグエンディアン、0..1023)
```

> ⚠️ **エンディアン注意 (重要)**: SCSCL シリーズの 2 byte レジスタは **読み書きとも Big Endian** (低アドレスに High byte、高アドレスに Low byte)。Feetech datasheet の `*_L` / `*_H` サフィックスは「Low/High バイト」ではなく「Low/High **アドレス**」を意味する。Feetech 公式 [FTServo_Arduino](https://github.com/ftservo/FTServo_Arduino) (MIT) の `SCS::Host2SCS` で SCSCL コンストラクタが `End=1` のとき `*DataL = (Data>>8)` (= Low アドレス側に High byte) としているのが根拠。STS / SMS シリーズ (`End=0`) では Little Endian なので注意。

元祖 `scservo.ts` の定義:

```ts
const ADDRESS = {
  ID: 5,
  OFFSET: 31,
  TORQUE_ENABLE: 40,
  GOAL_ACC: 41,
  GOAL_POSITION: 42,
  GOAL_TIME: 44,
  LOCK: 48,
  PRESENT_POSITION: 56,
} as const
```

> **読み出し時の長さ**: `readStatus()` は `ADDRESS.PRESENT_POSITION` から **15 byte** 一括 read している。SCS の状態テーブルが 56〜70 番地あたりに連続していて、`PRESENT_POSITION (2 byte) + その後の温度・速度・電圧・負荷等` を一括取得している。Arduino C++ 実装でも同じ 15 byte read を使うか、最小の 2 byte だけ read するかは実装方針次第。

---

## 6. チェックサム計算

**ロジック**: HEADER 2 byte をスキップし、`buffer[2..length-1]` (= ID 以降、checksum 自身は含まない) の総和を求めて、下位 8 bit のビット反転を取る。

元祖 TS:

```ts
function checksum(buffer: Uint8Array, length: number): number {
  let sum = 0
  for (let i = 2; i < length; i++) {
    sum += buffer[i]
  }
  const cs = ~(sum & 0xff)
  return cs
}
```

Arduino C++ ポート例 (将来サーボ通信モジュール内で実装):

```c
uint8_t scs_checksum(const uint8_t* buf, size_t length) {
  uint16_t sum = 0;
  for (size_t i = 2; i < length; i++) {
    sum += buf[i];
  }
  return (uint8_t)(~(sum & 0xff));
}
```

---

## 7. パケット組み立てロジック

### 7.1 共通の組み立て (writePos / read 共通の枠)

元祖 TS の `_sendCommand(command, address, ...values)`:

```ts
this.#txBuf[0] = 0xff
this.#txBuf[1] = 0xff
this.#txBuf[2] = this.#id
this.#txBuf[3] = values.length + 3   // LEN = data 数 + (CMD 1 + ADDR 1 + checksum 自身を含むバイトカウント調整)
this.#txBuf[4] = command              // 0x02 (READ) or 0x03 (WRITE)
this.#txBuf[5] = address
let idx = 6
for (const v of values) {
  this.#txBuf[idx] = v
  idx++
}
this.#txBuf[idx] = checksum(this.#txBuf, idx)
```

> **LEN の算出**: 元祖実装は `values.length + 3`。Feetech SCS のフォーマットでは LEN = (CMD + ADDR + DATA バイト数) + checksum バイト = `values.length + 1 + 1 + 1 = values.length + 3`。これは「LEN は CMD/ADDR/DATA/checksum 全部のバイト数 - 1」という Feetech 仕様。

### 7.2 トルク有効化

```ts
async setTorque(enable: boolean): Promise<unknown> {
  return this.#sendCommand(COMMAND.WRITE, ADDRESS.TORQUE_ENABLE, Number(enable))
}
```

→ パケット: `FF FF [ID] 04 03 28 [00 or 01] [CHECKSUM]`
（LEN=4 = data1 + 3, ADDRESS=0x28=40）

### 7.3 目標位置書き込み (setAngle)

```ts
async setAngle(angle: number): Promise<unknown> {
  const a = Math.floor(clamp(((angle + this.#offset) * 1024) / 200, 0, 0x03ff))
  return this.#sendCommand(COMMAND.WRITE, ADDRESS.GOAL_POSITION, ...le(a))
}
```

→ パケット: `FF FF [ID] 05 03 2A [POS_HIGH] [POS_LOW] [CHECKSUM]`
（LEN=5 = data2 + 3, ADDRESS=0x2A=42）

`le(value)` という関数名は誤解を招くが、**実装は Big Endian** で `[high, low]` を返す:

```ts
function le(value: number): [number, number] {
  return [(value & 0xff00) >> 8, value & 0xff]   // [high, low]
}
```

→ SCSCL は低アドレスに High byte を置く規約なので、書き込み順は **High → Low**。

### 7.4 現在位置読み出し (readStatus)

```ts
async readStatus(): Promise<Maybe<{ angle: number }>> {
  const values = await this.#sendCommand(COMMAND.READ, ADDRESS.PRESENT_POSITION, 15)
  if (values == null || values.length < 15) {
    return { success: false, reason: 'response corrupted.' }
  }
  const angle = (el(values[0], values[1]) * 200) / 1024
  return { success: true, value: { angle } }
}
```

→ 送信パケット: `FF FF [ID] 04 02 38 0F [CHECKSUM]`
（LEN=4 = data1 + 3, ADDRESS=0x38=56, 読み出しバイト数=15）

応答パース:

```ts
function el(high: number, low: number): number {
  return ((high << 8) & 0xff00) + (low & 0xff)
}
```

> **応答パース (実機検証済)**: `el(values[0], values[1])` = `(values[0] << 8) + values[1]` で **values[0] = High、values[1] = Low**。SCSCL の応答ペイロードも **Big Endian** (低アドレスに High byte)。元祖 TS の関数名 `el` も誤解を招くが、実装は BE 解釈で正しい。書き込み (`le`) と読み出し (`el`) の両方で SCSCL は BE で揃っている。STS / SMS シリーズは LE なので別扱いが要る。

---

## 8. 角度値のエンコーディング

| 項目 | 値 |
| --- | --- |
| 位置値の範囲 | **0..1023** (10-bit) |
| 物理角度範囲 | **0°..200°** (= 1023/1024 × 200°) |
| エンコーディング | ビッグエンディアン (high byte が先 = 低アドレス) |
| 変換式 | `position = floor((angle + offset) × 1024 / 200)` |
| 逆変換 | `angle = position × 200 / 1024` |
| オフセット保存先 | `ADDRESS.OFFSET (31番地)`、2 byte ビッグエンディアン |

サーボ単体の物理可動域は 0°..200° (= 0..1023 の 10-bit エンコーディング)。これに **ハード側のメカ可動域制限** (K151 の場合 yaw -128..128°、pitch 5..85°) が重なる。

> **ロボットの首振り角度制御** は通常「中央 = 100°」をオフセット 0 として扱い、`-100°..+100°` の範囲で角度を指定する。元祖の `(angle + offset) × 1024 / 200` という式はこの規約に従っている。

---

## 9. タイムアウト・順序実行

### タイムアウト

```ts
return this.#waitSlot.wait(40, () => {
  trace('timeout.\n')
})
```

**40 ms** で応答待ちタイムアウト。Arduino C++ 実装でも同等の時間待ちを設定する (`millis()` ベース or `Stream.setTimeout()` 経由)。

### 順序実行

複数の API 呼び出しは **シリアル化** されて発行される (デイジーチェーン上で 1 リクエスト = 1 レスポンスを保証):

```ts
this.#queueTail = run.then(() => undefined, () => undefined)
```

Arduino C++ では FreeRTOS タスクで mutex / semaphore を取って排他にするか、シングルスレッドで明示的に await する設計にする。

---

## 10. SCServo クラス API（元祖 stack-chan の TS 設計、参考）

```ts
class SCServo {
  constructor({ id }: SCServoConstructorParam)
  teardown(): void
  get id(): number

  async readOffsetAngle(): Promise<number>
  async setOffsetAngle(angle: number): Promise<unknown>
  async loadSettings(): Promise<unknown>
  async saveSettings(): Promise<unknown>
  async flashId(id: number): Promise<unknown>
  async setAngle(angle: number): Promise<unknown>
  async setAngleInTime(angle: number, goalTime: number): Promise<unknown>
  async setTorque(enable: boolean): Promise<unknown>
  async readStatus(): Promise<Maybe<{ angle: number }>>
}
```

### Arduino C++ 設計案 (サーボ通信モジュールで実装する `firmware/lib/scservo/SCServoClean.h`)

```cpp
class SCServoClean {
public:
  // 引数: HardwareSerial 参照, RX pin, TX pin, baud rate
  SCServoClean(HardwareSerial& serial, int8_t rxPin, int8_t txPin, uint32_t baud = 1000000);

  bool begin();
  void end();

  bool writePos(uint8_t id, int16_t position, uint16_t goalTime = 0);
  int16_t readPos(uint8_t id);
  bool enableTorque(uint8_t id, bool enable);
  bool setOffset(uint8_t id, int16_t offset);
  int16_t readOffset(uint8_t id);
  bool flashId(uint8_t oldId, uint8_t newId);

  // 角度ベースの便宜 API
  bool setAngle(uint8_t id, float angleDeg, uint16_t goalTimeMs = 0);
  float getAngle(uint8_t id);

private:
  HardwareSerial& _serial;
  int8_t _rxPin, _txPin;
  uint32_t _baud;
  uint8_t _txBuf[32];
  uint8_t _rxBuf[32];

  bool sendCommand(uint8_t id, uint8_t command, uint8_t address,
                   const uint8_t* data, size_t dataLen,
                   uint8_t* response = nullptr, size_t responseLen = 0);
  static uint8_t checksum(const uint8_t* buf, size_t length);
};
```

具体実装は **safe-startup / 通信モジュール PR で別 PR**。

---

## 11. サーボ可動域とソフト側 clamp（必須安全則）

> ⚠️ **守らないと K151 のサーボが物理破損する**。safe-startup ファーム (`firmware/examples/cores3/safe-startup/main.cpp`) 〜 set-angle-demo (`firmware/examples/cores3/set-angle-demo/`) の実機テストで確定した可動域・起動シーケンス・キャリブ方式を集約している。

### 11.1 物理的限界（壊れるライン）

K151 / K151-R の StackChan ジョイント機構と SCS0009 サーボの組み合わせにおける可動域。**出典が確定しているもの**と**出典不明確なもの**を分けて記載する（盲信禁止、実機キャリブで確認）。

#### pitch (Y 軸、うなずき、上下) — 出典確定

| 項目 | 値 | 出典 |
| --- | --- | --- |
| **推奨可動域** | **+5° 〜 +85°** | M5Stack 公式 K151 製品ページ |
| 超過時のリスク | servo stall + permanent damage（公式が明言） | 同上 |

公式原文 (英語、抜粋):

> "The movement angle of the StackChan Y-axis servo (vertical direction) is recommended to be controlled within 5 ~ 85°. **Operating at extreme angles may cause servo stall and permanent damage.**"
>
> — [M5Stack docs / Products / K151](https://docs.m5stack.com/en/products/sku/K151)

→ pitch は **公式が「破損する」と書いてる**ので、これがハード破損ラインとして信頼できる。

#### yaw (X 軸、首振り、左右) — 出典不明確、保守値で運用

| 項目 | 値 | 出典 |
| --- | --- | --- |
| 公式の記載 | **360-degree continuous rotation** | M5Stack 公式 K151 製品ページ + `m5stack/StackChan` README |
| サーボ単体仕様 | mechanical angle **300°** / Limit angle: **No limit** | Feetech SCS0009 公式製品ページ |
| BSP の実装値 | **−128° 〜 +128°** (= 10 倍スケールで -1280..1280) | `m5stack/StackChan-BSP` の `utils/motion/motion.h` 観測値（数値情報のみ MEMORY.md 5/10 12:00 経由） |
| 独立実装での収斂証拠 | **未確認** | robo8080 / mongonta555 / kisaragi-mochi 系の README には数値記載なし |

→ **yaw の ±128° は公式ドキュメントに記述がない**。サーボ単体としては 300° / No limit、StackChan 公式は「360° continuous rotation」と謳っている。**BSP の `motion.h` が実装上 ±128° に制限している**のは、ケーブル機構保護や stall 回避のための **保守的な値の可能性が高い**（破損ラインそのものではないかも）。

**実装方針**: BSP 値（±128°）を **保守的な物理上限の見立て** として採用しつつ、本ファームでは ±100° の運用マージンを入れる。実機キャリブで余裕があれば運用マージンを広げる、というアプローチを取る。

> 出典 URL:
> - M5Stack 公式 K151 製品ページ: <https://docs.m5stack.com/en/products/sku/K151>
> - `m5stack/StackChan` (README 部分のみ参照): <https://github.com/m5stack/StackChan>
> - Feetech SCS0009 製品ページ: <https://www.feetechrc.com/6v-23kg-serial-bus-steering-gear_65522.html>
> - Feetech SCS0009 datasheet (Switch Science 配布、PDF): <https://pages.switch-science.com/comparison/files/feetech/serial-scs/SCS0009_datasheet.pdf>
> - `m5stack/StackChan-BSP` の `utils/motion/motion.h` 観測値（本リポでは数値情報のみ MEMORY.md 経由で取得して参照）

### 11.2 ソフト側 clamp の必須要件

実装の入口で **必ず clamp** する。clamp 漏れがあると一発でサーボが壊れる。

**重要**: 本ファームの clamp は **zero ベース角度** (home-calibration で保存した zero raw を 0° と見なした相対角度) で定義する。SCSCL シリーズには zero を永続記録する公式機能が存在せず (§11.5)、ホスト側 NVS で zero raw を保持する設計のため、絶対角と zero ベース角を混在させると `setAnglePitch(0)` で中央に来なくなる。constexpr の clamp 値も zero ベースで揃える。

**推奨マージン (zero ベース、home-calibration で水平に零点を取った前提)**:

| 軸 | 物理上限の見立て | ソフト clamp（既定） | 安全マージン | 上限の根拠 |
| --- | --- | --- | --- | --- |
| yaw | **±128°** (zero ベース) | **±100°** (zero ベース) | ±28° | BSP 観測値（公式未確認、保守値）。yaw は機構が左右対称 |
| pitch | **±40°** (zero ベース) | **±30°** (zero ベース) | ±10° | M5Stack 公式の絶対 5°〜85° (幅 80°) の中央を水平として zero を取る前提で、delta ±40° を物理上限と見立てる |

物理上限ギリギリは個体差・組み立て公差・サーボの慣性オーバーランで超える可能性があるため、**安全マージンを入れて運用**する。マージン値は K151 個体ごとに `examples/cores3/home-calibration` の零点キャリブ結果（NVS namespace `xstackchan` の `yaw_zero` / `pitch_zero` 〜§11.5）を引いて再判定。

> ⚠️ **yaw の上限値は確定根拠が無い**。実機で「中央から外側に少しずつ」近づけて、stall 異音 / 物理ストッパに当たる音 / readPos が止まる位置を見極めて、yaw のソフト clamp と物理上限を**実機側で再確定**する。pitch は公式記載 5..85° (絶対) を破損ラインとして信頼してよいが、零点を物理可動域中央以外に取った個体では片側で先に物理ストッパに当たる可能性があるので、その場合は home-calibration 取り直しを推奨。

### 11.3 clamp 実装の擬似コード（現行 `firmware/lib/scservo/SCServo.{h,cpp}` のリファレンス）

```cpp
// firmware/lib/scservo/SCServo.h (現行実装)

namespace scservo {
  // ハード物理限界 (zero ベース、二重 clamp の外側、絶対に超えない)
  //   pitch: M5Stack 公式 K151 ストアページに「5..85° (絶対) 推奨、超過で servo stall +
  //          permanent damage」明記。幅 80° の中央 (≈45° absolute) を「水平」として
  //          home-calibration で零点を取る前提で、絶対角からの delta = ±40° を
  //          物理上限の見立てとする。
  //   yaw: 公式根拠なし。出典は BSP utils/motion/motion.h の観測値。
  //        ±128° は BSP の保守的制限値の可能性。yaw は機構が左右対称なので zero ベースでも
  //        ±128° で運用 OK。
  constexpr float YAW_PHYS_MIN_DEG   = -128.0f;  // BSP 観測値、公式未確認
  constexpr float YAW_PHYS_MAX_DEG   = +128.0f;  // BSP 観測値、公式未確認
  constexpr float PITCH_PHYS_MIN_DEG =  -40.0f;  // M5Stack 公式 5..85° (幅 80°) の半分
  constexpr float PITCH_PHYS_MAX_DEG =  +40.0f;  // M5Stack 公式 5..85° (幅 80°) の半分

  // 推奨運用範囲 (zero ベース、安全マージン込み、設定で広げ可)
  constexpr float YAW_SAFE_MIN_DEG   = -100.0f;
  constexpr float YAW_SAFE_MAX_DEG   = +100.0f;
  constexpr float PITCH_SAFE_MIN_DEG =  -30.0f;
  constexpr float PITCH_SAFE_MAX_DEG =  +30.0f;
}

bool SCServo::setAngleYaw(float angleDeg, uint16_t goalTimeMs) {
  // 1. 安全マージン clamp (運用既定)
  float clamped = constrain(angleDeg, YAW_SAFE_MIN_DEG, YAW_SAFE_MAX_DEG);
  // 2. 物理限界 clamp (二重防御、絶対外れない)
  clamped = constrain(clamped, YAW_PHYS_MIN_DEG, YAW_PHYS_MAX_DEG);
  // 3. clamp された場合は警告ログ
  if (fabsf(clamped - angleDeg) > 0.01f) {
    Serial.printf("[scservo] yaw %.2f -> clamped to %.2f\n", angleDeg, clamped);
  }
  // 4. zero ベースで raw 計算: target_raw = zero + delta_step
  //    delta_step = round(clamped * STEP_PER_DEG)
  return setAngleClamped(SERVO_ID_YAW, _zeroYaw, clamped, goalTimeMs, ...);
}

bool SCServo::setAnglePitch(float angleDeg, uint16_t goalTimeMs) {
  float clamped = constrain(angleDeg, PITCH_SAFE_MIN_DEG, PITCH_SAFE_MAX_DEG);
  clamped = constrain(clamped, PITCH_PHYS_MIN_DEG, PITCH_PHYS_MAX_DEG);
  if (fabsf(clamped - angleDeg) > 0.01f) {
    Serial.printf("[scservo] pitch %.2f -> clamped to %.2f\n", angleDeg, clamped);
  }
  return setAngleClamped(SERVO_ID_PITCH, _zeroPitch, clamped, goalTimeMs, ...);
}
```

二重 clamp の理由: 安全マージン clamp は運用上の柔らかい上限（将来広げる可能性あり）、物理限界 clamp は **絶対防御** で外せない constexpr。仮に安全マージンを誤って広げても物理限界 clamp で死守。

> **zero ベース統一の経緯**: 旧版では PITCH_SAFE_MIN/MAX = +20/+70° (絶対角) で書かれていたが、`setAngleClamped()` が同じ値を zero ベース角として処理するため、home-calibration で水平に零点を取った個体で `setAnglePitch(0.0f)` が +20° まで持ち上がるバグがあった (set-angle-demo で yaw のみ動かして検出済み)。仕様書 §11 反映時に SCServo.h と本ドキュメントを zero ベースに統一した。

### 11.4 起動シーケンス（Torque enable の責任分界）

K151 起動時の **正しい順序**:

```
1. M5.begin()                        // M5Unified 初期化 (I2C は M5.In_I2C で内部バス)
2. py32::enableServoPower()          // ★ PY32 IO Expander 経由で VM_EN を ON (§11.4.1 必須)
                                     // この呼び出し前は SCS バスに通電されておらず、
                                     // begin() / readPos() が応答しない or タイムアウトする
3. SCServo::begin()                  // UART1 1Mbps オープン、tx/rx ピン設定
                                     // この時点で torque は OFF のまま (起動直後にバン!と動かない)
4. loadZeroFromNvs()                 // NVS から zero raw を読んで servo に反映 (§11.5)
                                     // 未設定なら DEFAULT_ZERO_RAW=512 のままセーフに動く
5. readPos(SERVO_ID_YAW) / readPos(SERVO_ID_PITCH)
                                     // 通信疎通確認。VM 起動直後は応答が安定するまで時間が
                                     // かかるので READ_POS_RETRIES=8 程度のリトライ推奨
6. 目標位置を中央付近に設定 (torque OFF のままなので物理動作はしない)
   setAngleYaw(0); setAnglePitch(0);   // zero ベースなので 0 = 中央 (home-calibration 済の前提)
7. setTorque(SERVO_ID_YAW, true) / setTorque(SERVO_ID_PITCH, true)
                                     // ここで初めてサーボが目標位置に向けて動く。
                                     // 安全マージン内の中央なので衝突しない
8. 通常運用ループ
```

**禁止パターン**: `M5.begin()` 直後にいきなり `setAngle(180)` (= 範囲外) を投げる、または前回シャットダウン時の目標位置のまま電源 ON する。Torque enable の瞬間にサーボが目標位置に向かって全力で回ろうとして物理停止できない場合がある。

#### 11.4.1 PY32 VM_EN を ON する責任分界 (K151 必須)

K151 / K151-R では、CoreS3 のサーボバス電源 (VM rail) は **直接 GPIO に繋がっていない**。CoreS3 内蔵 I2C バス (M5.In_I2C, addr 0x6F) に接続された **PY32L020 IO Expander** の pin 0 を High にすることで初めて VM rail が立ち上がる仕様。これを忘れると:

- `Serial1.begin()` は成功するが SCS バスに 5V が来ていないため `readPos()` が無応答
- LED 制御 (WS2812) も同経路 (VLED rail) のため点灯しない
- 起動ログ上はエラーが出ないので debug が難しい

**リファレンス実装** (`firmware/examples/cores3/safe-startup/main.cpp` および `examples/cores3/home-calibration/main.cpp` / `examples/cores3/set-angle-demo/main.cpp` で同一):

```cpp
namespace py32 {
constexpr uint8_t I2C_ADDR        = 0x6F;
constexpr uint32_t I2C_FREQ       = 100000;
constexpr uint8_t REG_VERSION     = 0x02;
constexpr uint8_t REG_GPIO_DIR_L  = 0x03;  // direction (0=input, 1=output)
constexpr uint8_t REG_GPIO_OUT_L  = 0x05;  // output level
constexpr uint8_t REG_GPIO_PU_L   = 0x09;  // pull-up enable
constexpr uint8_t SERVO_VM_EN_PIN = 0;

bool waitReady(uint32_t timeoutMs = 1500) {
  // I2C 経由で REG_VERSION を読み、0/0xFF 以外が返るまで待つ
  // PY32 自身が起動完了するまで応答しない
}

bool enableServoPower() {
  if (!waitReady()) return false;
  const uint8_t mask = 1 << SERVO_VM_EN_PIN;
  M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_DIR_L, mask, I2C_FREQ);  // pin0 を出力に
  M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_PU_L,  mask, I2C_FREQ);  // pull-up 有効
  M5.In_I2C.bitOn(I2C_ADDR, REG_GPIO_OUT_L, mask, I2C_FREQ);  // pin0 を High に
  delay(200);  // VM rail 安定待ち
  return true;
}
}
```

レジスタアドレスと初期化シーケンスは [m5stack/StackChan-BSP](https://github.com/m5stack/StackChan-BSP) の `PY32IOExpander.cpp` 参照。**`SCServo::begin()` より前にこの呼び出しが必須**。

> 出典: K151 / K151-R は CoreS3 ベース M5Stack-Avatar スタックチャンキット。サーボバス電源と LED 電源は PY32L020 経由でゲートされている。本仕様は safe-startup ファーム実機テスト (2026-05-10) で確定 — VM_EN を ON せずに `Serial1.begin()` だけで進めると `readPos` が timeout し続ける現象を確認済み。

### 11.5 初回キャリブ（首が斜めにならないために、NVS 方式 D 案）

#### 11.5.1 採用方式: ホスト側 NVS で zero raw を保持

`firmware/examples/cores3/home-calibration` の現行実装は **ホスト (CoreS3) の NVS (Preferences) に zero raw を保存し、起動時に SCServo クラスへ反映する D 案**。サーボ EEPROM の OFFSET / Calibration 機能は **SCSCL シリーズでは未対応**のため使えない (§11.5.3)。

**手順:**

1. K151 を電源 ON、home-calibration ファーム焼き (`pio run -e cores3-home-calibration -t upload`)
2. サーボ torque OFF のまま手で **真ん中の位置** (yaw 正面 / pitch 水平) に物理的に持っていく
3. LCD タップ → `runCalibration()` 発火、`readPos(SERVO_ID_YAW)` / `readPos(SERVO_ID_PITCH)` で生位置値を取得
4. NVS namespace `xstackchan` に `yaw_zero` / `pitch_zero` キーで `int16_t` 保存 (ESP32 Preferences API)
5. 以降のファーム (safe-startup / set-angle-demo / 本番運用) は起動時に同 namespace から読み出して `servo.setZeroYaw() / setZeroPitch()` で反映
6. `setAngle*()` は zero ベースの相対角度として動作 (`raw = zero + delta * STEP_PER_DEG`)

**NVS キー一覧** (`firmware/examples/cores3/safe-startup/main.cpp` および `home-calibration/main.cpp` で共有):

| key | 型 | 説明 |
| --- | --- | --- |
| namespace | — | `xstackchan` |
| `yaw_zero` | `int16_t` | yaw サーボの zero raw (0..1023)、未設定時は `DEFAULT_ZERO_RAW = 512` |
| `pitch_zero` | `int16_t` | pitch サーボの zero raw (0..1023)、未設定時は `DEFAULT_ZERO_RAW = 512` |

未設定 (NVS namespace 自体が無い) の場合は SCServo クラス側で `DEFAULT_ZERO_RAW = 512` (raw 中央) のまま安全側で動く。

**実機検証済 (home-calibration + set-angle-demo の組み合わせで確認):**
- home-calibration 焼き直後: `[homecal] saved to NVS: yaw_zero=441 pitch_zero=651` (個体差あり)
- 電源 OFF/ON: `[homecal] loaded zero from NVS: yaw=441 pitch=651` (永続化 OK)
- 別 env (safe-startup / set-angle-demo) の焼き直し後: 同じ NVS namespace から正しく読み出せる
- `setAngleYaw(0)` で raw≈zeroYaw (誤差 ±5 step ≈ ±1°、SCSCL の dead zone 内)、`setAngleYaw(±30)` で raw が zero±154 step に正しく収束

これをやらないと、サーボの組み付け公差で **「角度 0° = 中央」が斜めになっている**個体があり、ハード可動域の対称性も崩れて片側だけ早く物理限界に到達する。

#### 11.5.2 検討した他案 (採用見送り)

| 案 | 内容 | 結果 |
| --- | --- | --- |
| A 案 | NVS 方式 (D 案と同じ) | → D 案として採用 |
| B 案 | サーボ EEPROM の OFFSET 書き換え | **SCSCL 未対応で見送り** (§11.5.3) |
| C 案 | サーボ EEPROM の `setMiddle` (TORQUE_ENABLE=128 マジック値) | **SCSCL 未対応で見送り** (§11.5.3) |

#### 11.5.3 SCSCL に永続キャリブ機能が無い根拠 (3 ソース裏取り)

過去に B/C 案 (サーボ EEPROM 書き換え) を一度実装して焼いたが、`setMiddle` 焼いても `readPos` が変わらず破綻。徹底調査の結果、**SCSCL シリーズには「物理現在位置を中央 raw 512 として永続記録する公式機能は存在しない」**ことが 3 一次ソースで確定した:

1. **Feetech 公式 [FTServo_Arduino](https://github.com/ftservo/FTServo_Arduino) (MIT)**:
   - `examples/SCSCL/` 配下に `CalibrationOfs.ino` が**意図的に無い** (`examples/SMS_STS/` と `examples/HLSCL/` には存在する)
   - `src/SCSCL.h` に `ADDR_OFFSET` (31) の `#define` も**無い** (SMS_STS / HLSCL のヘッダにはある)
2. **SMS_STS / HLSCL の `CalibrationOfs()` の実装**:
   - 中身は `writeByte(ID, TORQUE_ENABLE, 128)` の 1 行だけ
   - これは SMS_STS / HLSCL ファーム固有のマジック値で、SCSCL ファームは解釈しない (試したが OFFSET が変わらない)
3. **元祖 [stack-chan/scservo.ts](https://github.com/stack-chan/stack-chan/blob/main/firmware/stackchan/drivers/scservo.ts) :252 の作者コメント**:
   ```ts
   /**
    * @note SCS series does not have zero position calibration function.
    *       The offset value should be handled by the application.
    */
   ```

→ アプリ側 (= ホストの NVS) で zero raw を保持する D 案が SCSCL における唯一の正解。`SCServo` クラスから `setMiddle` / `setOffset` / `ADDR_OFFSET` 定数は dead code として削除済。

> **教訓**: SCS01 / SMS_STS / HLSCL の datasheet 知識を SCSCL に勝手に拡張しないこと。Feetech 公式 SDK の `examples/<シリーズ名>/` 配下の不在は、その機能がそのシリーズで未対応であることの強いシグナル。一次ソース (datasheet / 公式 SDK ソース) で必ず裏取りする (memory 5/10 22:55 教訓)。

### 11.6 試験時の追加ガード（実機テスト初回）

safe-startup ファームの実機テストを始める時の運用ルール:

1. **電源は USB のみ**（または電流制限可能なベンチ電源）から開始。バッテリー駆動 / フル電源は安全マージンの動作確認後
2. **`setAngle*()` の最初のテストは中央±10°** から始める。少しずつ範囲を広げる
3. **`setAngleInTime` の goalTime は 1000ms 以上** から開始。速い動作は最後
4. 万一サーボが「ガガッ」「ジー」と異音を立てたら **即電源 OFF**。stall current で焼ける
5. テスト中は `Serial.println` で `setAngle*()` の入力値・clamp 後の値・実機の `readPos()` を全部ログ出力

これらは仕様じゃなく運用則だが、初回ビルドの主目的は「サーボを壊さずに動作を確認する」ことなので、実装側で `#define SCS_DEBUG_TRACE 1` フラグとして組み込んで OFF/ON 切替できるようにする。

### 11.7 PRESENT_POSITION / GOAL_POSITION のエンディアン (実機確定済)

§5 / §7 でも触れているが、safe-startup〜set-angle-demo の **実機検証で確定**したのでここで再掲する。SCSCL シリーズの 2 byte レジスタは **読み書きとも Big Endian** (低アドレスに High byte、高アドレスに Low byte):

| 操作 | 並び | 実装 (`firmware/lib/scservo/SCServo.cpp`) |
| --- | --- | --- |
| `writePos` (GOAL_POSITION 書込) | **BE** `{ hi, lo }` | `data[2] = { hi(position), lo(position) }` |
| `writePos` (GOAL_POSITION + GOAL_TIME 連続書込) | **BE** `{ hi, lo, hi, lo }` | `data[4] = { hi(pos), lo(pos), hi(time), lo(time) }` |
| `readPos` (PRESENT_POSITION 応答) | **BE** `resp[5]=high, resp[6]=low` | `pos = (resp[5] << 8) | resp[6]` |

**実機エビデンス**:
- LE で読み書きすると `0x01C3 → 0xC301 = 49921` のようなガベージ値が返る (safe-startup 焼き時に検出、2026-05-10)
- BE に統一後は set-angle-demo の `0° → -30° → 0° → +30° → 0°` スイープが raw 値で zero±154 step ぴったりに収束 (誤差 ±5 step ≈ ±1°、SCSCL dead zone 内、set-angle-demo 実機検証)
- 外部からの裏取り: necobut さん ( <https://x.com/necobut/status/2053427233096880569> ) と nnn さん ( <https://x.com/nnn112358/status/2053377465406644290> ) のツイートが BE 統一改修のトリガー

**根拠**: Feetech datasheet の `*_L` / `*_H` サフィックスは「Low/High **アドレス**」を意味し、低アドレス側に High byte が入る (= Big Endian)。Feetech 公式 [FTServo_Arduino](https://github.com/ftservo/FTServo_Arduino) の `SCS::Host2SCS` で SCSCL コンストラクタが `End=1` のとき `*DataL = (Data>>8)` (= Low アドレス側に High byte) としているのが決定打。

> **STS / SMS シリーズは LE** (`End=0`)。本実装は SCSCL 専用なので関係ないが、将来 STS 系を扱う場合は別 class / template で分離する。

## 12. 不明点 / 実機検証事項

このドキュメントは元祖 TS 実装と Feetech 公式仕様書の汎用知識から組み立てた。**以下は K151 実機で確認するまで断定しない**:

1. **シリアルパラメータ詳細**: 8N1 で動くと予想だが、Feetech datasheet で確認 or 実機で `Serial1.begin(1000000, SERIAL_8N1)` を試す
2. **応答の遅延**: 1Mbps × 8N1 で送信〜応答開始までの間隔。`Serial1.setTimeout(40)` で十分か、もっと短くて良いか
3. **エラーバイト**: 応答パケット中の error bit の位置と意味 (元祖実装は明示的に check していない)
4. ~~**`PRESENT_POSITION` 読み出し時のバイト並び**: 元祖 TS の `el(values[0], values[1])` がビッグエンディアン読みになってる箇所と、`le()` のリトルエンディアン書きの差。SCS と STS で違う可能性あり~~ → **§5 / §7 で BE 確定済**。SCSCL は読み書きとも BE。STS / SMS は LE。元祖の `le()` 関数は名前が誤解を招くが中身は BE 実装
5. ~~**可動域の安全マージン**: yaw ±128°、pitch 5°〜85° は MEMORY.md 記載値だが、ソフト側 clamp は **±100°、20°〜70° 程度の安全マージン**を入れたほうが無難~~ → **§11.2 で zero ベースに統一済**。yaw=±100°(SAFE)/±128°(PHYS)、pitch=±30°(SAFE)/±40°(PHYS) を SCServo.h で constexpr 定義。yaw 上限の確定根拠は引き続き未確認 (実機ストッパで再判定)
6. **複数サーボ同時駆動**: SYNC_WRITE 命令はサポートされていないっぽいので、yaw / pitch は順次書き込み

実機テスト時に `pio device monitor` で生バイト列をダンプして突き合わせる。

---

## 13. 参考リンク

- 元祖 stack-chan: [stack-chan/stack-chan](https://github.com/stack-chan/stack-chan) (Apache-2.0)
  - `firmware/stackchan/drivers/scservo.ts` (低レベル)
  - `firmware/stackchan/drivers/scservo-driver.ts` (高レベル、yaw/pitch 統合)
  - `firmware/stackchan/drivers/manifest_driver.json` (依存)
- Feetech 公式: <https://www.feetechrc.com/> (SCS0009 datasheet)
- M5Stack 公式 K151: <https://docs.m5stack.com/en/products/sku/K151>
- [m5stack/StackChan-BSP](https://github.com/m5stack/StackChan-BSP): `Motion API` キャリブ知識（角度単位 10=1°、可動域 ±128° / 5..85°）の参照元、および FTServo_Arduino (MIT) ベースの参考実装
