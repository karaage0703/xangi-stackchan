# xangi-stack-chan

![xangi-stackchan](docs/images/cover.jpg)

[xangi](https://github.com/karaage0703/xangi)の応答に合わせて、スタックチャンの表情・首振り・音声を動かす拡張機能です。

## 主な機能

- AIの応答や長時間作業の完了を音声でお知らせ
- スタックチャン顔／画像キャラの表示、首振り、状態LED
- CoreS3のカメラ撮影とLCDマイクからの音声入力
- USB接続、CoreS3のWi-Fi・Tailnet接続（実験機能）、ブラウザシミュレータ

CoreS3（公式K151 / K151-Rを含む）、AtomS3R + Voice/Echo Base、M5Stack Basic + アールティ Ver.βに対応します。使える機能は搭載ハードウェアによって異なります。

## クイックスタート

xangiが起動済みで、対応ファームを書き込んだ実機をUSB接続している場合:

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

`--port`は実際の接続先に置き換えてください。設定画面は`http://127.0.0.1:7897/`です。

xangiのManaged Extensionとして登録する場合は[セットアップ手順](XANGI_SETUP.md)を参照してください。設定画面はxangi Web UIの「拡張」から開けます。

## ドキュメント

- [使い方](docs/usage.md): 対応機種、セットアップ、Wi-Fi接続、顔の切り替え、音声入力、複数台運用、トラブルシュート
- [設計](docs/design.md): 構成、イベント処理、接続方式、画像・音声の流れ
- [ファームウェア](firmware/README.md): 機種別ビルド・書き込み
- [ブリッジプロトコル](docs/xangi_bridge_protocol.md): コマンドとデータ転送
- [サーボプロトコル](docs/scservo_protocol.md): Feetech SCSの仕様
- [AIエージェント向け手順](skills/xs-xangi-stackchan/SKILL.md): 設定・診断・操作

## ライセンス

MIT License
