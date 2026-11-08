# raspi-minecraft-discord-bot

Raspberry Pi 5 で動かしているマイクラサーバー（Paper + Geyser）を、Discord から起動・停止・監視するボットです。

## できること

| コマンド | 内容 | 使える人 |
|---|---|---|
| `/start` | サーバーを起動 | 全員 |
| `/stop` | サーバーを停止（ワールドを保存してから止める） | `ADMIN_IDS` の人 |
| `/status` | 起動中かどうか、接続中のプレイヤー | 全員 |
| `/pc` | CPU温度・CPU使用率・負荷・メモリ・ストレージ・性能制限の有無 | 全員 |
| `/cmd` | サーバーのコンソールコマンドを実行（例: `whitelist add .名前`） | `ADMIN_IDS` の人 |

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
3. 「OAuth2」→「URL Generator」で Scopes に `bot` と `applications.commands`、Bot Permissions に `Send Messages` を選び、出てきたURLからサーバーに追加

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

## メモ

- `.env` は `.gitignore` 済みです。トークンや RCON パスワードはコミットしないでください
- `/pc` の「性能制限」は `vcgencmd get_throttled` の結果です。ユーザーが `video` グループに入っていないと取得できません

## License

MIT
