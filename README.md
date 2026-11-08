# raspi-minecraft-discord-bot
<img width="464" height="299" alt="スクリーンショット 2026-11-09 003413" src="https://github.com/user-attachments/assets/2dc514d2-eec5-4624-9aec-a008df395d12" />

Raspberry Pi 5 で動かしているマイクラサーバー（Paper + Geyser）を、Discord から起動・停止・監視するボットです。

## できること

| コマンド | 内容 | 使える人 |
|---|---|---|
| `/start` | サーバーを起動 | 全員 |
| `/stop` | サーバーを停止（ワールドを保存してから止める） | `ADMIN_IDS` の人 |
| `/status` | 起動中かどうか、接続中のプレイヤー | 全員 |
| `/pc` | CPU温度・CPU使用率・負荷・メモリ・ストレージ・性能制限の有無 | 全員 |
| `/cmd` | サーバーのコンソールコマンドを実行（例: `whitelist add .名前`） | `ADMIN_IDS` の人 |
| `/panel` | 今のチャンネルにボタン付きの「サーバー管理パネル」を設置 | `ADMIN_IDS` の人 |

### サーバー管理パネル

`/panel` を実行すると、そのチャンネルに次のようなパネルが投稿されます。

- **表示**: ステータス（🟢オンライン／🔴オフライン／🟡起動中・停止中）、オンライン人数、描画距離、接続中のプレイヤー、ラズパイの CPU温度・CPU使用率・メモリ使用率、接続先、最終更新時刻。枠の色もステータスに合わせて緑・赤・黄色に変わります
- **ボタン**:

  | ボタン | 内容 | 押せる人 |
  |---|---|---|
  | ▶️ 起動 | サーバーを起動（オンライン中は押せません） | 全員 |
  | ⏹️ 停止 | ワールドを保存して停止（オフライン中は押せません） | `ADMIN_IDS` の人 |
  | 🔄 再起動 | サーバーを再起動（オフライン中は押せません） | `ADMIN_IDS` の人 |
  | 🔃 更新 | パネルをすぐに更新 | 全員 |
  | 🤖 ボット再起動 | `systemctl --user restart mcbot` でボットを再起動 | `ADMIN_IDS` の人 |

- **描画距離メニュー**: ボタンの下のセレクトメニューで `view-distance` を 6（軽い）/ 8（おすすめ）/ 10（4人くらいまで）/ 12（重い・少人数向け）から変更できます。**Discord サーバーの管理者権限**を持つ人だけが使えます
  - `server.properties` の `view-distance` 行の数字だけを書き換えます（ほかの行・コメント・並び順は変えません）。書き換える前に `server.properties.bak-YYYYMMDDHHMMSS` を作ります
  - サーバーは自動では再起動しません。🔄 再起動ボタンを押したときに反映されます。反映前は、パネルに「8（再起動で反映）」のように表示されます
  - `simulation-distance` は変更しません
- **自動更新**: 30秒ごとに更新されます。起動・停止・再起動ボタンを押した直後は、状態が落ち着くまで3秒おきに更新されます
- `PANEL_CHANNEL_ID` を設定すると、そのチャンネルにボット以外の誰かが投稿したとき、古いパネルを削除して新しいパネルを一番下に送り直します（連続投稿は5秒待ってまとめて1回だけ送り直します）。ボットの起動時にもパネルが一番下にあるか確認します。`/panel` をどのチャンネルで実行しても、パネルはこのチャンネルに設置されます
- パネルの場所は `panel.json` に保存されるので、ボットを再起動しても同じメッセージが更新され続け、ボタンもそのまま使えます
- もう一度 `/panel` を実行すると、新しいチャンネルにパネルを設置し直し、前のパネルは削除されます。パネルのメッセージを手で消した場合は、`/panel` で設置し直してください
- ボットには、そのチャンネルの「チャンネルを見る」「メッセージを送信」「埋め込みリンク」の権限が必要です
- `ADMIN_IDS` が空だと、`/panel` と管理者用のボタンは誰も使えません

`ALERT_CHANNEL_ID` を設定すると、CPU温度が `TEMP_WARN`（初期値75℃）を超えたときにそのチャンネルへ警告します。

## 構成

```
Discord ──▶ bot.py (mcbot.service)
              ├─ systemctl --user start/stop minecraft ──▶ minecraft.service (Paper)
              ├─ RCON (127.0.0.1:25575) ──▶ プレイヤー一覧・コマンド実行
              └─ /sys/class/thermal, vcgencmd, psutil ──▶ ラズパイの状態
```

マイクラサーバーとボットはどちらも systemd のユーザーサービスとして動くため、`sudo` は不要です。

## セットアップ

### 1. Discord でボットを作る

1. [Discord Developer Portal](https://discord.com/developers/applications) で「New Application」
2. 「Bot」→「Reset Token」でトークンを控える（**公開しない**）
3. 「OAuth2」→「URL Generator」で Scopes に `bot` と `applications.commands`、Bot Permissions に `Send Messages` と `Embed Links`（パネル用）を選び、出てきたURLからサーバーに追加

### 2. マイクラ側で RCON を有効にする

`~/minecraft/server.properties`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=長くてランダムなパスワード
broadcast-rcon-to-ops=false
```

RCON のポート（25575）は playit などで外に公開しないでください。

### 3. マイクラをサービスにする

```bash
mkdir -p ~/.config/systemd/user
cp systemd/minecraft.service ~/.config/systemd/user/
loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user start minecraft
```

ログ: `journalctl --user -u minecraft -f`

### 4. ボットを入れる

```bash
cd ~
git clone https://github.com/<you>/raspi-minecraft-discord-bot.git
cd raspi-minecraft-discord-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # トークンなどを入れる
chmod 600 .env
```

動作確認: `.venv/bin/python bot.py`（`Ctrl + C` で終了）

### 5. ボットを常駐させる

```bash
cp systemd/mcbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mcbot
```

これでラズパイを再起動してもボットが自動で立ち上がります。マイクラ本体も電源投入時に起動したい場合は `systemctl --user enable minecraft` を実行します。

## 設定 (.env)

| 変数 | 説明 |
|---|---|
| `DISCORD_TOKEN` | ボットのトークン |
| `GUILD_ID` | ボットを使う Discord サーバーのID（右クリック →「IDをコピー」。開発者モードをオンにすると表示されます） |
| `ADMIN_IDS` | `/stop` と `/cmd` を使えるユーザーID（カンマ区切り） |
| `SERVER_ADDRESS` / `SERVER_PORT` | `/start` と `/status` に表示する接続先 |
| `RCON_PASSWORD` など | `server.properties` と同じ値 |
| `ALERT_CHANNEL_ID` / `TEMP_WARN` | 温度警告の送り先と、警告する温度 |
| `SERVER_PROPERTIES` | `server.properties` の場所（空なら `~/minecraft/server.properties`） |
| `PANEL_CHANNEL_ID` | 管理パネルを置くチャンネルのID。設定すると、パネルが常に一番下になるように送り直されます |

## メモ

- `.env` は `.gitignore` 済みです。トークンや RCON パスワードはコミットしないでください
- `/pc` の「性能制限」は `vcgencmd get_throttled` の結果です。ユーザーが `video` グループに入っていないと取得できません

## License

MIT
