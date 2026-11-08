"""Raspberry Pi Minecraft server control bot for Discord.

Slash commands:
  /start   Start the Minecraft server (systemd user service)
  /stop    Stop the Minecraft server (saves the world first)
  /status  Show whether the server is running and who is online
  /pc      Show Raspberry Pi temperature, CPU, memory and disk usage
  /cmd     Run a server console command via RCON (admins only)
  /panel   Post a server control panel with buttons (admins only)
  /登録解除  Remove a member's Minecraft account from the whitelist (Discord administrators only)

The panel also has a select menu to change view-distance in server.properties
(Discord server administrators only). If PANEL_CHANNEL_ID is set, the panel is
kept as the last message of that channel: when someone posts there, the old
panel is deleted and a new one is sent at the bottom.

Self-service whitelist: a second panel in REGISTER_CHANNEL_ID lets any member of the Discord
server register their own Bedrock account (see the "registration" section below).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
import psutil
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
SERVICE_NAME = os.getenv("MC_SERVICE", "minecraft")
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "")
SERVER_PORT = os.getenv("SERVER_PORT", "19132")

RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

# Comma separated Discord user IDs allowed to use /stop and /cmd.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# Temperature alert (Celsius). Set ALERT_CHANNEL_ID to enable.
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0") or 0)
TEMP_WARN = float(os.getenv("TEMP_WARN", "75"))

# server.properties of the Minecraft server (only the view-distance line is touched).
SERVER_PROPERTIES = Path(
    os.path.expanduser(os.getenv("SERVER_PROPERTIES", "") or "~/minecraft/server.properties")
)
VIEW_DISTANCE_CHOICES = [
    (6, "6（軽い）"),
    (8, "8（おすすめ）"),
    (10, "10（4人くらいまで）"),
    (12, "12（重い・少人数向け）"),
]

# Channel where the panel is kept at the bottom (0 = off: the panel stays where /panel put it).
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0") or 0)

# Self-service whitelist registration (0 = off).
REGISTER_CHANNEL_ID = int(os.getenv("REGISTER_CHANNEL_ID", "0") or 0)
MC_DIR = Path(os.path.expanduser(os.getenv("MC_DIR", "") or "~/minecraft"))
WHITELIST_FILE = Path(os.path.expanduser(os.getenv("WHITELIST_FILE", "") or str(MC_DIR / "whitelist.json")))
MC_LOG_FILE = Path(os.path.expanduser(os.getenv("MC_LOG_FILE", "") or str(MC_DIR / "logs" / "latest.log")))
REGISTER_PANEL_FILE = Path(__file__).resolve().parent / "register_panel.json"
REGISTRATIONS_FILE = Path(__file__).resolve().parent / "registrations.json"
BEDROCK_PREFIX = "."          # floodgate username-prefix
PENDING_TTL = 300             # seconds a "waiting for connection" request stays valid
NOTICE_DELETE_AFTER = 300     # seconds until a registration notice in the register channel is deleted

GUILD = discord.Object(id=GUILD_ID)

# The bot's own systemd user service (used by the panel's "bot restart" button).
BOT_SERVICE = "mcbot"

# Where the panel's channel/message IDs are kept across restarts.
PANEL_FILE = Path(__file__).resolve().parent / "panel.json"
PANEL_INTERVAL = 30       # normal auto refresh (seconds)
REGISTER_PANEL_INTERVAL = 60  # register panel auto refresh (seconds)
PANEL_FAST_INTERVAL = 3   # refresh interval right after start/stop/restart
PANEL_FAST_MIN = 10       # keep fast refresh at least this long after a button
PANEL_FAST_MAX = 180      # ...and at most this long
PANEL_STICKY_DELAY = 5    # re-send the panel this long after the last message in the channel
PANEL_STICKY_MAX = 30     # ...but no later than this after the first one (busy chat)

log = logging.getLogger("mcbot")


# ---------------------------------------------------------------- systemd

async def systemctl(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode().strip()


async def is_running() -> bool:
    code, _ = await systemctl("is-active", "--quiet", SERVICE_NAME)
    return code == 0


# ---------------------------------------------------------------- RCON

class RconError(Exception):
    pass


def _rcon_send(sock: socket.socket, req_id: int, ptype: int, body: str) -> None:
    data = body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<iii", len(data) + 8, req_id, ptype) + data)


def _rcon_recv(sock: socket.socket) -> tuple[int, str]:
    def read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    (length,) = struct.unpack("<i", read_exact(4))
    payload = read_exact(length)
    req_id, _ptype = struct.unpack("<ii", payload[:8])
    return req_id, payload[8:-2].decode("utf-8", errors="replace")


def rcon_command_sync(command: str) -> str:
    if not RCON_PASSWORD:
        raise RconError("RCON_PASSWORD が設定されていません")
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=5) as sock:
        _rcon_send(sock, 1, 3, RCON_PASSWORD)  # login
        req_id, _ = _rcon_recv(sock)
        if req_id == -1:
            raise RconError("RCONのパスワードが違います")
        _rcon_send(sock, 2, 2, command)
        _, body = _rcon_recv(sock)
        return body


async def rcon_command(command: str) -> str:
    return await asyncio.to_thread(rcon_command_sync, command)


# ---------------------------------------------------------------- Pi stats

def cpu_temp() -> float | None:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(path.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def throttled() -> str:
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        value = int(out.split("=")[1], 16)
    except Exception:
        return "取得できませんでした"
    if value == 0:
        return "問題なし"
    now = []
    if value & 0x1:
        now.append("電圧不足")
    if value & 0x4:
        now.append("性能制限中")
    if value & 0x8:
        now.append("温度制限中")
    past = []
    if value & 0x10000:
        past.append("電圧不足")
    if value & 0x40000:
        past.append("性能制限")
    if value & 0x80000:
        past.append("温度制限")
    parts = []
    if now:
        parts.append("現在: " + "・".join(now))
    if past:
        parts.append("起動後にあり: " + "・".join(past))
    return " / ".join(parts) or hex(value)


def temp_label(t: float | None) -> str:
    if t is None:
        return "不明"
    if t >= 80:
        return f"{t:.1f}℃ 🔥 熱すぎ（性能低下中の可能性）"
    if t >= TEMP_WARN:
        return f"{t:.1f}℃ ⚠️ 高め"
    return f"{t:.1f}℃ ✅"


# ---------------------------------------------------------------- server.properties

# Matches the whole view-distance line; group 2 is the number. Works on bytes so that
# every other byte of the file (comments, order, line endings, encoding) stays as is.
VIEW_DISTANCE_RE = re.compile(rb"^([ \t]*view-distance[ \t]*[=:][ \t]*)(\d+)([ \t]*\r?)$", re.M)


class PropertiesError(Exception):
    pass


def _find_view_distance(data: bytes) -> re.Match[bytes]:
    matches = list(VIEW_DISTANCE_RE.finditer(data))
    if not matches:
        raise PropertiesError("server.properties に view-distance の行が見つかりません")
    if len(matches) > 1:
        raise PropertiesError("server.properties に view-distance の行が複数あります")
    return matches[0]


def read_view_distance() -> int:
    return int(_find_view_distance(SERVER_PROPERTIES.read_bytes()).group(2))


def write_view_distance(new: int) -> tuple[int, Path | None]:
    """Rewrite only the number on the view-distance line. Returns (old value, backup path)."""
    path = SERVER_PROPERTIES
    data = path.read_bytes()
    m = _find_view_distance(data)
    old = int(m.group(2))
    if old == new:
        return old, None

    # Same naming as the existing backups: server.properties.bak-YYYYMMDDHHMMSS
    while True:
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
        if not backup.exists():
            break
        time.sleep(1)  # never overwrite an older backup made in the same second
    shutil.copy2(path, backup)  # keeps the 600 permission (the file holds rcon.password)

    new_data = data[:m.start(2)] + str(new).encode() + data[m.end(2):]
    mode = stat.S_IMODE(path.stat().st_mode)
    tmp = path.with_name(path.name + ".mcbot-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)  # atomic: the file is never half written
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return old, backup


# ---------------------------------------------------------------- panel state

ONLINE, OFFLINE, STARTING, STOPPING = "online", "offline", "starting", "stopping"

STATE_STYLE = {
    ONLINE: ("🟢 オンライン", discord.Color.green()),
    OFFLINE: ("🔴 オフライン", discord.Color.red()),
    STARTING: ("🟡 起動中", discord.Color.gold()),
    STOPPING: ("🟡 停止中", discord.Color.gold()),
}

LIST_RE = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players online:?(.*)", re.S)
COLOR_CODE_RE = re.compile(r"§.")


@dataclass
class ServerInfo:
    state: str
    count: int | None = None
    max_players: int | None = None
    players: list[str] = field(default_factory=list)
    players_ok: bool = False


async def unit_state() -> str:
    """systemd の状態（active / activating / deactivating / inactive / failed ...）"""
    _, out = await systemctl("is-active", SERVICE_NAME)
    return out.splitlines()[0] if out else "unknown"


async def server_info() -> ServerInfo:
    unit = await unit_state()
    if unit == "deactivating":
        return ServerInfo(STOPPING)
    if unit == "activating":
        return ServerInfo(STARTING)
    if unit != "active":
        return ServerInfo(OFFLINE)
    # The service is up, but the world isn't ready until RCON answers.
    try:
        out = await rcon_command("list")
    except OSError:  # connection refused / timeout: still booting
        return ServerInfo(STARTING)
    except Exception:  # wrong password etc.: running, but no player info
        return ServerInfo(ONLINE)
    m = LIST_RE.search(COLOR_CODE_RE.sub("", out))
    if not m:
        return ServerInfo(ONLINE)
    names = [n.strip() for n in m.group(3).split(",") if n.strip()]
    return ServerInfo(ONLINE, int(m.group(1)), int(m.group(2)), names, True)


async def build_panel() -> tuple[discord.Embed, PanelView, str]:
    info = await server_info()
    cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
    mem = psutil.virtual_memory()
    label, color = STATE_STYLE[info.state]

    embed = discord.Embed(title="🎮 Minecraft サーバー管理パネル", color=color)
    embed.add_field(name="ステータス", value=label, inline=True)

    if info.count is not None:
        count = f"{info.count} / {info.max_players} 人"
    else:
        count = "-"
    embed.add_field(name="オンライン人数", value=count, inline=True)

    try:
        vd = await asyncio.to_thread(read_view_distance)
        bot.vd_error_logged = False
    except Exception as e:
        vd = None
        if not bot.vd_error_logged:  # log once, and never the file's contents
            bot.vd_error_logged = True
            log.warning("panel: could not read view-distance: %s", type(e).__name__)
    if vd is None:
        vd_text = "取得できませんでした"
    else:
        # While the server is not running, whatever is in the file is what it will use.
        if info.state != ONLINE or bot.vd_applied is None:
            bot.vd_applied = vd
        vd_text = str(vd)
        if vd != bot.vd_applied:
            vd_text += "（再起動で反映）"
    embed.add_field(name="描画距離", value=vd_text, inline=True)

    if info.state != ONLINE:
        players = "-"
    elif not info.players_ok:
        players = "取得できませんでした"
    else:
        players = "\n".join(show_name(n) for n in info.players) or "なし"
    embed.add_field(name="接続中のプレイヤー", value=players[:1024], inline=False)

    embed.add_field(
        name="ラズパイ",
        value=f"🌡️ {temp_label(cpu_temp())} ・ CPU {cpu:.0f}% ・ メモリ {mem.percent:.0f}%",
        inline=False,
    )
    if SERVER_ADDRESS:
        addr = f"`{SERVER_ADDRESS}` / ポート `{SERVER_PORT}`"
    else:
        addr = "未設定"
    embed.add_field(name="接続先", value=addr, inline=False)

    embed.set_footer(text="最終更新")
    embed.timestamp = discord.utils.utcnow()
    return embed, PanelView(info.state), info.state


def load_panel() -> tuple[int, int] | None:
    try:
        data = json.loads(PANEL_FILE.read_text())
        return int(data["channel_id"]), int(data["message_id"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("panel.json を読み込めませんでした")
        return None


def save_panel(channel_id: int, message_id: int) -> None:
    tmp = PANEL_FILE.with_name(PANEL_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"channel_id": channel_id, "message_id": message_id}))
    tmp.replace(PANEL_FILE)


def clear_panel() -> None:
    PANEL_FILE.unlink(missing_ok=True)


def is_panel_admin(interaction: discord.Interaction) -> bool:
    """パネルの停止・再起動・ボット再起動と /panel は ADMIN_IDS の人だけ（空なら誰も使えない）"""
    return interaction.user.id in ADMIN_IDS


# ---------------------------------------------------------------- panel view

class PanelView(discord.ui.View):
    """Persistent view: timeout=None and fixed custom_ids keep buttons working after a restart."""

    def __init__(self, state: str | None = None) -> None:
        super().__init__(timeout=None)
        if state is not None:
            self.start_button.disabled = state != OFFLINE
            self.stop_button.disabled = state in (OFFLINE, STOPPING)
            self.restart_button.disabled = state in (OFFLINE, STOPPING)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Any button/menu use: make sure the panel ends up as the last message of its channel.
        if PANEL_CHANNEL_ID:
            bot.request_repost(2)
        return True

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("このボタンは管理者だけが使えます。", ephemeral=True)

    @discord.ui.button(label="起動", emoji="▶️", style=discord.ButtonStyle.success,
                       custom_id="mcpanel:start", row=0)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        unit = await unit_state()
        if unit in ("active", "activating", "deactivating"):
            msg = "停止処理中です。止まってから押してください。" if unit == "deactivating" else "サーバーはすでに起動しています。"
            await interaction.response.send_message(msg, ephemeral=True)
            bot.panel_fast()
            return
        await interaction.response.defer(thinking=True)
        bot.panel_fast()
        code, out = await systemctl("start", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"起動に失敗しました。\n```\n{out[:1500]}\n```")
            return
        await interaction.followup.send(
            f"🟢 {interaction.user.display_name} さんがサーバーを起動しました。入れるようになるまで1分ほど待ってください。"
        )

    @discord.ui.button(label="停止", emoji="⏹️", style=discord.ButtonStyle.danger,
                       custom_id="mcpanel:stop", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_panel_admin(interaction):
            await self._deny(interaction)
            return
        if await unit_state() not in ("active", "activating"):
            await interaction.response.send_message("サーバーは起動していません。", ephemeral=True)
            bot.panel_fast()
            return
        await interaction.response.defer(thinking=True)
        bot.panel_fast()
        code, out = await systemctl("stop", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"停止に失敗しました。\n```\n{out[:1500]}\n```")
            return
        await interaction.followup.send(f"🔴 {interaction.user.display_name} さんがサーバーを停止しました。ワールドは保存済みです。")

    @discord.ui.button(label="再起動", emoji="🔄", style=discord.ButtonStyle.primary,
                       custom_id="mcpanel:restart", row=0)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_panel_admin(interaction):
            await self._deny(interaction)
            return
        if await unit_state() not in ("active", "activating"):
            await interaction.response.send_message(
                "サーバーは起動していません。「起動」ボタンを使ってください。", ephemeral=True
            )
            bot.panel_fast()
            return
        await interaction.response.defer(thinking=True)
        bot.panel_fast()
        code, out = await systemctl("restart", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"再起動に失敗しました。\n```\n{out[:1500]}\n```")
            return
        await interaction.followup.send(
            f"🔄 {interaction.user.display_name} さんがサーバーを再起動しました。入れるようになるまで1分ほど待ってください。"
        )

    @discord.ui.button(label="更新", emoji="🔃", style=discord.ButtonStyle.secondary,
                       custom_id="mcpanel:refresh", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await bot.refresh_panel(extra=interaction.message)

    @discord.ui.button(label="ボット再起動", emoji="🤖", style=discord.ButtonStyle.secondary,
                       custom_id="mcpanel:botrestart", row=1)
    async def bot_restart_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_panel_admin(interaction):
            await self._deny(interaction)
            return
        await interaction.response.send_message(
            f"🤖 {interaction.user.display_name} さんがボットを再起動しました。10秒ほどで戻ってきます。"
        )
        log.info("panel: bot restart requested by %s", interaction.user.id)
        # --no-block: systemd carries out the restart even though it kills this process.
        await systemctl("restart", "--no-block", BOT_SERVICE)

    @discord.ui.select(
        custom_id="mcpanel:viewdistance",
        placeholder="🔭 描画距離を変更（管理者のみ・再起動で反映）",
        options=[discord.SelectOption(label=label, value=str(v)) for v, label in VIEW_DISTANCE_CHOICES],
        row=2,
    )
    async def view_distance_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                "描画距離の変更は Discord サーバーの管理者だけができます。", ephemeral=True
            )
            bot.panel_wake.set()  # re-render the panel so the menu goes back to its placeholder
            return
        new = int(select.values[0])
        await interaction.response.defer(thinking=True)
        try:
            async with bot.vd_lock:
                old, _backup = await asyncio.to_thread(write_view_distance, new)
        except PropertiesError as e:
            await interaction.followup.send(f"変更できませんでした: {e}", ephemeral=True)
            return
        except Exception as e:
            log.exception("view-distance: could not update %s", SERVER_PROPERTIES)
            await interaction.followup.send(
                f"変更できませんでした（{type(e).__name__}）。ボットのログを確認してください。", ephemeral=True
            )
            return
        finally:
            bot.panel_wake.set()
        if old == new:
            await interaction.followup.send(f"描画距離はすでに {new} です。")
            return
        log.info("view-distance: %s -> %s by %s", old, new, interaction.user.id)
        await interaction.followup.send(
            f"{interaction.user.display_name} さんが描画距離を {old} → {new} に変更しました。再起動で反映されます"
        )


# ---------------------------------------------------------------- registration

# Written by the plugin in plugin/ for every connection the whitelist rejects.
REJECT_RE = re.compile(
    r"\[McbotWhitelist\] REJECT (\S+) "
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
FLOODGATE_UUID_PREFIX = "00000000-0000-0000-"
USERNAME_RE = re.compile(r"[\w ]{1,16}")  # letters/digits/_/space only: also keeps RCON input safe


def normalize_name(raw: str) -> tuple[str, str] | None:
    """User input -> (original name for fwhitelist, in-server name with the prefix)."""
    s = raw.strip()
    if s.startswith(BEDROCK_PREFIX):
        s = s[len(BEDROCK_PREFIX):].strip()
    if not USERNAME_RE.fullmatch(s):
        return None
    orig = s.replace(" ", "_")  # floodgate: replace-spaces
    return orig, BEDROCK_PREFIX + orig


def show_name(name: str) -> str:
    """Name as shown on Discord: without the floodgate prefix ('.')."""
    return name[len(BEDROCK_PREFIX):] if name.startswith(BEDROCK_PREFIX) else name


def write_json_atomic(path: Path, data, mode: int = 0o600) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class RegStore:
    """Who registered which account (registrations.json). Survives bot restarts."""

    def __init__(self) -> None:
        self.registrations: dict[str, dict] = {}  # discord id -> {"name", "uuid", "at"}
        self.pending: dict[str, dict] = {}        # discord id -> {"name", "orig", "expires"}
        try:
            data = json.loads(REGISTRATIONS_FILE.read_text(encoding="utf-8"))
            self.registrations = dict(data.get("registrations", {}))
            self.pending = dict(data.get("pending", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            log.warning("registrations.json を読み込めませんでした（空として扱います）")

    def save(self) -> None:
        write_json_atomic(
            REGISTRATIONS_FILE, {"registrations": self.registrations, "pending": self.pending}
        )

    def owner_of_name(self, ingame: str) -> str | None:
        for uid, r in self.registrations.items():
            if r["name"].lower() == ingame.lower():
                return uid
        return None

    def pending_owner_of_name(self, ingame: str) -> str | None:
        now = time.time()
        for uid, p in self.pending.items():
            if p["name"].lower() == ingame.lower() and p["expires"] > now:
                return uid
        return None


def wl_read() -> list[dict]:
    try:
        data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError("whitelist.json is not a list")
    return data


def wl_write(entries: list[dict]) -> None:
    try:
        mode = stat.S_IMODE(WHITELIST_FILE.stat().st_mode)
    except FileNotFoundError:
        mode = 0o664
    write_json_atomic(WHITELIST_FILE, entries, mode)


def wl_find(entries: list[dict], name: str | None = None, uuid: str | None = None) -> dict | None:
    for e in entries:
        if (uuid and str(e.get("uuid", "")).lower() == uuid.lower()) or (
            name and str(e.get("name", "")).lower() == name.lower()
        ):
            return e
    return None


async def wl_reload() -> None:
    try:
        await rcon_command("whitelist reload")
    except Exception as e:
        log.warning("whitelist reload failed: %s", type(e).__name__)


async def wl_add(uuid: str, name: str) -> None:
    """Put (uuid, name) into whitelist.json, then tell the server to re-read it."""
    entries = [
        e for e in wl_read()
        if str(e.get("uuid", "")).lower() != uuid.lower()
        and str(e.get("name", "")).lower() != name.lower()
    ]
    entries.append({"uuid": uuid, "name": name})
    wl_write(entries)
    await wl_reload()


async def wl_remove(name: str | None, uuid: str | None) -> bool:
    entries = wl_read()
    keep = [
        e for e in entries
        if not ((uuid and str(e.get("uuid", "")).lower() == uuid.lower())
                or (name and str(e.get("name", "")).lower() == name.lower()))
    ]
    if len(keep) == len(entries):
        return False
    wl_write(keep)
    await wl_reload()
    return True


def addr_text() -> str:
    return f"`{SERVER_ADDRESS}` / ポート `{SERVER_PORT}`" if SERVER_ADDRESS else "未設定"


async def build_register_panel() -> tuple[discord.Embed, "RegisterView"]:
    info = await server_info()
    label, color = STATE_STYLE[info.state]
    embed = discord.Embed(title="🎫 Minecraft 参加登録", color=color)
    embed.add_field(name="ステータス", value=label, inline=False)
    embed.add_field(name="接続先", value=addr_text(), inline=False)
    embed.add_field(
        name="参加までの手順",
        value=(
            "① 下の「🟢 参加登録」ボタンを押して、マイクラのユーザー名を入力\n"
            "② マイクラの「サーバー」タブでサーバーを追加し、上のアドレスとポートで接続"
        ),
        inline=False,
    )
    embed.set_footer(text="最終更新")
    embed.timestamp = discord.utils.utcnow()
    return embed, RegisterView()


INVALID_NAME_MSG = (
    "ユーザー名に使えない文字があります。英数字・「_」・スペースだけ、16文字までで入力してください。"
    "（マイクラ上の名前をそのまま入力してください）"
)


class RegisterModal(discord.ui.Modal, title="参加登録"):
    username = discord.ui.TextInput(
        label="マイクラのユーザー名を入力してください",
        placeholder="マイクラ上の名前",
        min_length=1, max_length=20, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.username.value)
        names = normalize_name(raw)
        if names is None:
            await interaction.response.send_message(INVALID_NAME_MSG, ephemeral=True)
            return
        # Ask once more before anything is registered (typos are easy to make).
        await interaction.response.send_message(
            f"次の名前で登録します。合っていますか？\n\n## `{names[0]}`\n"
            "間違えていたら「やり直す」を押してください。",
            view=ConfirmRegisterView(raw), ephemeral=True,
        )


class ConfirmRegisterView(discord.ui.View):
    """One-off confirmation shown (only to the user) after the name was typed."""

    def __init__(self, raw: str) -> None:
        super().__init__(timeout=180)
        self.raw = raw

    @discord.ui.button(label="この名前で登録する", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="登録しています…", view=None)
        try:
            msg = await bot.register(interaction.user, self.raw)
        except Exception:
            log.exception("register: failed")
            msg = "登録中にエラーが起きました。少し待ってからもう一度試してください。"
        await interaction.edit_original_response(content=msg)

    @discord.ui.button(label="やり直す", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="登録しませんでした。もう一度「参加登録」を押して、名前を入力してください。", view=None
        )


class RegisterView(discord.ui.View):
    """Persistent view (timeout=None + fixed custom_ids): works after a bot restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Any button use: make sure the panel ends up as the last message of its channel
        # (same as the admin panel).
        if REGISTER_CHANNEL_ID and bot.reg_tasks:
            bot.register_panel.request_repost(2, force=True)
        return True

    @discord.ui.button(label="参加登録", emoji="🟢", style=discord.ButtonStyle.success,
                       custom_id="mcreg:join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RegisterModal())

    @discord.ui.button(label="参加解除", emoji="🚪", style=discord.ButtonStyle.danger,
                       custom_id="mcreg:leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = str(interaction.user.id)
        reg = bot.store.registrations.get(uid)
        pend = bot.store.pending.get(uid)
        if reg is None and pend is None:
            await interaction.response.send_message("あなたの登録は見つかりませんでした。", ephemeral=True)
            return
        name = show_name((reg or pend)["name"])
        what = "の登録を解除" if reg is not None else "の登録（接続待ち）を取り消し"
        await interaction.response.send_message(
            f"`{name}` {what}します。よろしいですか？\n"
            "解除するとホワイトリストから外れ、このアカウントではサーバーに入れなくなります。",
            view=ConfirmLeaveView(), ephemeral=True,
        )

    @discord.ui.button(label="登録一覧", emoji="📋", style=discord.ButtonStyle.secondary,
                       custom_id="mcreg:list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            entries = await asyncio.to_thread(wl_read)
        except Exception:
            await interaction.followup.send("ホワイトリストを読み込めませんでした。", ephemeral=True)
            return
        lines = []
        linked_uuids, linked_names = set(), set()
        for uid, r in bot.store.registrations.items():
            linked_uuids.add(str(r.get("uuid", "")).lower())
            linked_names.add(r["name"].lower())
            note = "" if wl_find(entries, r["name"], r.get("uuid")) else "（ホワイトリストから外れています）"
            lines.append(f"<@{uid}> → `{show_name(r['name'])}`{note}")
        for e in entries:
            if (str(e.get("uuid", "")).lower() in linked_uuids
                    or str(e.get("name", "")).lower() in linked_names):
                continue
            lines.append(f"Discord 未連携 → `{show_name(str(e.get('name', '?')))}`")
        if not lines:
            await interaction.followup.send("まだ誰も登録していません。", ephemeral=True)
            return
        text, shown = "", 0
        for line in lines:
            if len(text) + len(line) + 1 > 3800:
                break
            text += line + "\n"
            shown += 1
        if shown < len(lines):
            text += f"…ほか {len(lines) - shown} 件"
        embed = discord.Embed(
            title=f"📋 登録一覧（{len(lines)} 件）", description=text, color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ConfirmLeaveView(discord.ui.View):
    """One-off confirmation (only visible to the user) before removing their own registration."""

    def __init__(self) -> None:
        super().__init__(timeout=180)

    @discord.ui.button(label="解除する", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="解除しています…", view=None)
        try:
            name = await bot.unregister(interaction.user.id, "self (leave button)")
        except Exception:
            log.exception("leave: failed")
            await interaction.edit_original_response(content="解除中にエラーが起きました。少し待ってからもう一度試してください。")
            return
        if name is None:
            msg = "接続待ちの登録を取り消しました。"
        else:
            msg = f"`{show_name(name)}` の登録を解除しました。また遊ぶときは「参加登録」からもう一度登録してください。"
        await interaction.edit_original_response(content=msg)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="解除しませんでした。", view=None)


class StickyPanel:
    """A panel message that is edited in place and kept as the last message of its channel
    (same behaviour as the admin panel: saved message id, debounced re-send)."""

    def __init__(self, channel_id: int, file: Path, build) -> None:
        self.channel_id, self.file, self.build = channel_id, file, build
        self.lock = asyncio.Lock()
        self.repost_task: asyncio.Task | None = None
        self.repost_first: float | None = None
        self.repost_at = 0.0
        self.repost_force = False  # re-send even if the panel is already the last message

    def load(self) -> tuple[int, int] | None:
        try:
            d = json.loads(self.file.read_text())
            return int(d["channel_id"]), int(d["message_id"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("%s を読み込めませんでした", self.file.name)
            return None

    def save(self, channel_id: int, message_id: int) -> None:
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps({"channel_id": channel_id, "message_id": message_id}))
        tmp.replace(self.file)

    async def channel(self):
        return bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)

    async def refresh(self) -> None:
        async with self.lock:
            saved = self.load()
            if saved is None:
                return
            embed, view = await self.build()
            try:
                ch = await self.channel()
                await ch.get_partial_message(saved[1]).edit(embed=embed, view=view)
            except discord.NotFound:
                log.warning("register panel: message %s was deleted; re-sending", saved[1])
                self.file.unlink(missing_ok=True)
                self.request_repost(0)
            except Exception:
                log.exception("register panel: could not edit message %s", saved[1])

    async def place(self, force: bool = False) -> None:
        channel = await self.channel()
        async with self.lock:
            old = self.load()
            if (not force and old is not None and old[0] == channel.id
                    and channel.last_message_id == old[1]):
                return
            embed, view = await self.build()
            message = await channel.send(embed=embed, view=view)  # send first, then delete the old one
            self.save(channel.id, message.id)
        if old is not None and old[1] != message.id:
            try:
                oc = bot.get_channel(old[0]) or await bot.fetch_channel(old[0])
                await oc.get_partial_message(old[1]).delete()
            except discord.NotFound:
                pass
            except Exception:
                log.exception("register panel: could not delete old panel %s", old[1])

    def request_repost(self, delay: float = PANEL_STICKY_DELAY, force: bool = False) -> None:
        now = time.monotonic()
        if force:
            self.repost_force = True
        if self.repost_first is None:
            self.repost_first = now
        self.repost_at = min(now + delay, self.repost_first + PANEL_STICKY_MAX)
        if self.repost_task is None or self.repost_task.done():
            self.repost_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        while self.repost_first is not None:
            wait = self.repost_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            self.repost_first = None
            force, self.repost_force = self.repost_force, False
            try:
                await self.place(force=force)
            except Exception:
                log.exception("register panel: could not re-send the panel")


# ---------------------------------------------------------------- bot

class McBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Privileged intent (must be ON in the Developer Portal): needed to notice members leaving.
        # MEMBERS_INTENT=0 starts the bot without it (leaving members are then not removed).
        intents.members = os.getenv("MEMBERS_INTENT", "1") != "0"
        super().__init__(intents=intents, max_messages=None)  # no message cache: only new messages matter
        self.tree = app_commands.CommandTree(self)
        self.warned = False
        self.panel_fast_since = 0.0
        self.panel_fast_until = 0.0
        self.panel_task: asyncio.Task | None = None
        self.repost_task: asyncio.Task | None = None
        self.repost_first: float | None = None  # when the first not-yet-handled message came in
        self.repost_at = 0.0
        self.vd_applied: int | None = None  # view-distance the running server was started with
        self.vd_error_logged = False
        self.store = RegStore()
        self.register_panel = StickyPanel(REGISTER_CHANNEL_ID, REGISTER_PANEL_FILE, build_register_panel)

    async def setup_hook(self) -> None:
        self.panel_wake = asyncio.Event()
        self.panel_lock = asyncio.Lock()
        self.vd_lock = asyncio.Lock()
        self.add_view(PanelView())  # keep panel buttons working after a restart
        self.reg_lock = asyncio.Lock()
        self.add_view(RegisterView())
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        if ALERT_CHANNEL_ID:
            self.temp_watch.start()
        self.panel_task = asyncio.create_task(self.panel_loop())
        self.reg_tasks: list[asyncio.Task] = []
        if REGISTER_CHANNEL_ID and REGISTER_CHANNEL_ID == PANEL_CHANNEL_ID:
            log.error("REGISTER_CHANNEL_ID と PANEL_CHANNEL_ID が同じです。別のチャンネルにしてください（登録機能は無効）")
        elif REGISTER_CHANNEL_ID:
            self.reg_tasks = [
                asyncio.create_task(self.register_panel_loop()),
                asyncio.create_task(self.pending_loop()),
                asyncio.create_task(self.log_loop()),
            ]

    # ------------------------------------------------------------ panel

    def panel_fast(self) -> None:
        """Refresh the panel now and then every few seconds until the state settles."""
        now = time.monotonic()
        self.panel_fast_since = now
        self.panel_fast_until = now + PANEL_FAST_MAX
        self.panel_wake.set()

    async def panel_loop(self) -> None:
        await self.wait_until_ready()
        if PANEL_CHANNEL_ID:
            # Messages may have been posted while the bot was offline: put the panel back at the bottom.
            try:
                await self.place_panel(await self.panel_channel())
            except Exception:
                log.exception("panel: could not place the panel in channel %s", PANEL_CHANNEL_ID)
        while not self.is_closed():
            self.panel_wake.clear()
            try:
                state = await self.refresh_panel()
            except Exception:
                log.exception("panel: refresh failed")
                state = None
            now = time.monotonic()
            if (
                self.panel_fast_until > now
                and state in (ONLINE, OFFLINE)
                and now - self.panel_fast_since >= PANEL_FAST_MIN
            ):
                self.panel_fast_until = 0.0
            fast = self.panel_fast_until > now
            try:
                await asyncio.wait_for(
                    self.panel_wake.wait(), PANEL_FAST_INTERVAL if fast else PANEL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

    async def refresh_panel(self, extra: discord.Message | None = None) -> str | None:
        """Edit the saved panel message (and `extra`, if it is a different panel)."""
        async with self.panel_lock:
            saved = load_panel()
            if saved is None and extra is None:
                return None
            embed, view, state = await build_panel()
            if saved is not None:
                channel_id, message_id = saved
                try:
                    channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                    await channel.get_partial_message(message_id).edit(embed=embed, view=view)
                except discord.NotFound:
                    log.warning("panel: message %s was deleted; forgetting it", message_id)
                    clear_panel()
                    saved = None
                    if PANEL_CHANNEL_ID:
                        self.request_repost(0)
                except Exception:
                    log.exception("panel: could not edit message %s", message_id)
            if extra is not None and (saved is None or extra.id != saved[1]):
                try:
                    await extra.edit(embed=embed, view=PanelView(state))
                except Exception:
                    log.exception("panel: could not edit message %s", extra.id)
            return state

    # ------------------------------------------------------------ keep the panel at the bottom

    async def panel_channel(self) -> discord.abc.Messageable:
        return self.get_channel(PANEL_CHANNEL_ID) or await self.fetch_channel(PANEL_CHANNEL_ID)

    async def place_panel(self, channel, force: bool = False) -> tuple[discord.Message | None, str]:
        """Send a new panel to `channel` and delete the previous one.

        Unless `force`, nothing happens if the saved panel is already the channel's last message.
        Returns (new message or None, note about the old panel).
        """
        async with self.panel_lock:
            old = load_panel()
            if (
                not force and old is not None and old[0] == channel.id
                and channel.last_message_id == old[1]
            ):
                return None, ""
            embed, view, _ = await build_panel()
            # Send first, then delete: there is never a moment without a panel.
            message = await channel.send(embed=embed, view=view)
            save_panel(channel.id, message.id)
        note = ""
        if old is not None and old[1] != message.id:
            try:
                old_channel = self.get_channel(old[0]) or await self.fetch_channel(old[0])
                await old_channel.get_partial_message(old[1]).delete()
                note = "以前のパネルは削除しました。"
            except discord.NotFound:
                pass
            except Exception:
                log.exception("panel: could not delete old panel %s", old[1])
                note = "以前のパネルは削除できなかったので、手動で消してください（もう更新されません）。"
        return message, note

    def request_repost(self, delay: float = PANEL_STICKY_DELAY) -> None:
        """Debounced re-send: many messages in a row lead to a single re-send."""
        now = time.monotonic()
        if self.repost_first is None:
            self.repost_first = now
        self.repost_at = min(now + delay, self.repost_first + PANEL_STICKY_MAX)
        if self.repost_task is None or self.repost_task.done():
            self.repost_task = asyncio.create_task(self._repost_worker())

    async def _repost_worker(self) -> None:
        while self.repost_first is not None:
            wait = self.repost_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            # Messages arriving from here on set repost_first again and get another round.
            self.repost_first = None
            try:
                await self.place_panel(await self.panel_channel())
            except Exception:
                log.exception("panel: could not re-send the panel")

    # ------------------------------------------------------------ self-service registration

    async def notify_user(self, user_id: int, text: str) -> None:
        """Mention the user in the register channel; the notice is deleted after NOTICE_DELETE_AFTER."""
        try:
            channel = await self.register_panel.channel()
            await channel.send(
                f"<@{user_id}> {text}",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]),
                delete_after=NOTICE_DELETE_AFTER,
            )
        except Exception:
            log.exception("register: could not notify %s in the register channel", user_id)

    async def _finalize(self, uid: str, name: str, uuid: str) -> None:
        """Record uid -> (name, uuid). An older account of the same Discord user is removed first.
        Caller holds reg_lock."""
        old = self.store.registrations.get(uid)
        if old and str(old.get("uuid", "")).lower() != uuid.lower():
            await wl_remove(old["name"], old.get("uuid"))
        self.store.registrations[uid] = {"name": name, "uuid": uuid, "at": int(time.time())}
        self.store.pending.pop(uid, None)
        self.store.save()

    async def register(self, user: discord.abc.User, raw: str) -> str:
        names = normalize_name(raw)
        if names is None:
            return INVALID_NAME_MSG
        orig, ingame = names
        uid = str(user.id)
        async with self.reg_lock:
            cur = self.store.registrations.get(uid)
            same_as_cur = cur is not None and cur["name"].lower() == ingame.lower()
            other = self.store.owner_of_name(ingame)
            if other is not None and other != uid:
                return "その名前はほかの Discord ユーザーがすでに登録しています。"
            other_p = self.store.pending_owner_of_name(ingame)
            if other_p is not None and other_p != uid:
                return "その名前は、ほかの人が登録の手続き中です。少し待ってからやり直してください。"
            entries = wl_read()
            existing = wl_find(entries, ingame)
            if existing is not None and not same_as_cur:
                return ("その名前はすでにホワイトリストに入っています（管理者が登録した名前など）。"
                        "自分の名前なのに登録できない場合は、管理者に連絡してください。")
            if same_as_cur and existing is not None:
                return f"すでにこの名前（`{orig}`）で登録済みです。上のアドレスとポートで接続してください。"

            try:
                await rcon_command(f"fwhitelist add {orig}")
            except Exception:
                return "サーバーに接続できませんでした（停止中かもしれません）。サーバーが起動してから、もう一度試してください。"
            # fwhitelist reports problems only in the server console, so check the file instead.
            found = None
            for _ in range(14):
                await asyncio.sleep(0.5)
                found = wl_find(wl_read(), ingame)
                if found:
                    break
            if found:
                await self._finalize(uid, found["name"], found["uuid"])
                return "登録しました！上のアドレスとポートで接続してください。\n" + addr_text()

            self.store.pending[uid] = {
                "name": ingame, "orig": orig, "expires": time.time() + PENDING_TTL,
            }
            self.store.save()
            return (
                "5分以内に、マイクラから上のアドレスとポートでサーバーに一度接続してください。\n"
                "ホワイトリストで弾かれますが、それで本人確認が完了します。\n"
                f"（接続先: {addr_text()} ／ 登録する名前: `{orig}`）"
            )

    async def on_reject(self, name: str, uuid: str) -> None:
        """A connection was rejected by the whitelist: complete a matching pending request."""
        if not uuid.lower().startswith(FLOODGATE_UUID_PREFIX):
            return  # only Bedrock (floodgate) accounts can be registered
        async with self.reg_lock:
            now = time.time()
            uid = next(
                (u for u, p in self.store.pending.items()
                 if p["name"].lower() == name.lower() and p["expires"] > now),
                None,
            )
            if uid is None:
                return
            if self.store.owner_of_name(name) not in (None, uid):
                self.store.pending.pop(uid, None)
                self.store.save()
                return
            await wl_add(uuid, name)
            await self._finalize(uid, name, uuid)
        log.info("register: %s registered %s", uid, name)
        await self.notify_user(int(uid), "登録しました！もう一度接続してください。")

    async def unregister(self, discord_id: int, reason: str) -> str | None:
        """Drop a Discord user's account from the whitelist and the registry. Returns the name."""
        uid = str(discord_id)
        async with self.reg_lock:
            r = self.store.registrations.pop(uid, None)
            had_pending = self.store.pending.pop(uid, None) is not None
            if r is None and not had_pending:
                return None
            self.store.save()
            if r is not None:
                await wl_remove(r["name"], r.get("uuid"))
        if r is not None:
            log.info("register: removed %s of %s (%s)", r["name"], uid, reason)
        return r["name"] if r else None

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id == GUILD_ID:
            await self.unregister(member.id, "left the server")

    async def reconcile_members(self) -> None:
        """Members who left while the bot was offline lose their whitelist entry too."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        for uid in list(self.store.registrations):
            if guild.get_member(int(uid)) is not None:
                continue
            try:
                await guild.fetch_member(int(uid))
            except discord.NotFound:
                await self.unregister(int(uid), "left the server (found at startup)")
            except Exception:
                log.exception("register: could not check member %s", uid)

    async def pending_loop(self) -> None:
        await self.wait_until_ready()
        try:
            await self.reconcile_members()
        except Exception:
            log.exception("register: reconcile failed")
        while not self.is_closed():
            try:
                async with self.reg_lock:
                    now = time.time()
                    expired = [u for u, p in self.store.pending.items() if p["expires"] <= now]
                    for u in expired:
                        del self.store.pending[u]
                    if expired:
                        self.store.save()
                for u in expired:
                    await self.notify_user(int(u), "期限切れです。もう一度「参加登録」を押してください。")
            except Exception:
                log.exception("register: expiry check failed")
            await asyncio.sleep(20)

    async def log_loop(self) -> None:
        """Follow logs/latest.log and pick up the plugin's REJECT lines."""
        await self.wait_until_ready()
        ino: int | None = None
        pos = 0
        buf = b""
        try:
            st = MC_LOG_FILE.stat()
            ino, pos = st.st_ino, st.st_size  # old lines are not replayed
        except OSError:
            pass
        failed = False
        while not self.is_closed():
            try:
                st = MC_LOG_FILE.stat()
                if st.st_ino != ino or st.st_size < pos:  # rotated (server restart) or truncated
                    ino, pos, buf = st.st_ino, 0, b""
                if st.st_size > pos:
                    with open(MC_LOG_FILE, "rb") as f:
                        f.seek(pos)
                        data = f.read()
                    pos += len(data)
                    *lines, buf = (buf + data).split(b"\n")
                    for raw_line in lines:
                        m = REJECT_RE.search(raw_line.decode("utf-8", errors="replace"))
                        if m:
                            await self.on_reject(m.group(1), m.group(2))
                failed = False
            except FileNotFoundError:
                pass
            except Exception:
                if not failed:  # log once per failure streak
                    log.exception("register: log watcher error")
                failed = True
            await asyncio.sleep(1)

    async def register_panel_loop(self) -> None:
        await self.wait_until_ready()
        try:
            await self.register_panel.place()
        except Exception:
            log.exception("register panel: could not place the panel in channel %s", REGISTER_CHANNEL_ID)
        while not self.is_closed():
            await asyncio.sleep(REGISTER_PANEL_INTERVAL)
            try:
                await self.register_panel.refresh()
            except Exception:
                log.exception("register panel: refresh failed")

    async def on_message(self, message: discord.Message) -> None:
        if (REGISTER_CHANNEL_ID and message.channel.id == REGISTER_CHANNEL_ID
                and self.reg_tasks
                and not (self.user is not None and message.author.id == self.user.id)):
            self.register_panel.request_repost()
        if not PANEL_CHANNEL_ID or message.channel.id != PANEL_CHANNEL_ID:
            return
        if self.user is not None and message.author.id == self.user.id:
            return  # our own messages (including the panel itself) don't trigger a re-send
        self.request_repost()

    @tasks.loop(minutes=1)
    async def temp_watch(self) -> None:
        t = cpu_temp()
        if t is None:
            return
        channel = self.get_channel(ALERT_CHANNEL_ID)
        if channel is None:
            return
        if t >= TEMP_WARN and not self.warned:
            self.warned = True
            await channel.send(f"⚠️ ラズパイのCPU温度が {t:.1f}℃ になっています。")
        elif t < TEMP_WARN - 5 and self.warned:
            self.warned = False
            await channel.send(f"✅ CPU温度が {t:.1f}℃ まで下がりました。")

    @temp_watch.before_loop
    async def before_temp_watch(self) -> None:
        await self.wait_until_ready()


bot = McBot()


def is_admin(interaction: discord.Interaction) -> bool:
    return not ADMIN_IDS or interaction.user.id in ADMIN_IDS


@bot.tree.command(name="start", description="マイクラサーバーを起動します")
async def start(interaction: discord.Interaction) -> None:
    if await is_running():
        await interaction.response.send_message("サーバーはすでに起動しています。")
        return
    await interaction.response.defer(thinking=True)
    code, out = await systemctl("start", SERVICE_NAME)
    if code != 0:
        await interaction.followup.send(f"起動に失敗しました。\n```\n{out[:1500]}\n```")
        return
    msg = "🟢 サーバーを起動しました。入れるようになるまで1分ほど待ってください。"
    if SERVER_ADDRESS:
        msg += f"\nアドレス: `{SERVER_ADDRESS}` ポート: `{SERVER_PORT}`"
    await interaction.followup.send(msg)


@bot.tree.command(name="stop", description="マイクラサーバーを停止します（保存してから止めます）")
async def stop(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("このコマンドは管理者だけが使えます。", ephemeral=True)
        return
    if not await is_running():
        await interaction.response.send_message("サーバーはすでに停止しています。")
        return
    await interaction.response.defer(thinking=True)
    code, out = await systemctl("stop", SERVICE_NAME)
    if code != 0:
        await interaction.followup.send(f"停止に失敗しました。\n```\n{out[:1500]}\n```")
        return
    await interaction.followup.send("🔴 サーバーを停止しました。ワールドは保存済みです。")


@bot.tree.command(name="status", description="サーバーの状態と接続中のプレイヤーを表示します")
async def status(interaction: discord.Interaction) -> None:
    running = await is_running()
    embed = discord.Embed(
        title="マイクラサーバー",
        color=discord.Color.green() if running else discord.Color.red(),
    )
    embed.add_field(name="状態", value="🟢 起動中" if running else "🔴 停止中", inline=False)
    if running:
        try:
            players = await rcon_command("list")
            players = re.sub(r"(?<=: )\.|(?<=, )\.", "", players)  # hide the bedrock prefix
            embed.add_field(name="プレイヤー", value=players or "-", inline=False)
        except Exception:
            embed.add_field(name="プレイヤー", value="取得できませんでした（起動直後かもしれません）", inline=False)
    if SERVER_ADDRESS:
        embed.add_field(name="接続先", value=f"`{SERVER_ADDRESS}` / ポート `{SERVER_PORT}`", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="pc", description="ラズパイの温度・CPU・メモリを表示します")
async def pc(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    t = cpu_temp()
    color = discord.Color.green()
    if t is not None and t >= TEMP_WARN:
        color = discord.Color.orange() if t < 80 else discord.Color.red()
    embed = discord.Embed(title="ラズパイの状態", color=color)
    embed.add_field(name="CPU温度", value=temp_label(t), inline=False)
    embed.add_field(name="CPU使用率", value=f"{cpu:.0f}%", inline=True)
    embed.add_field(name="負荷 (1/5/15分)", value=f"{load1:.2f} / {load5:.2f} / {load15:.2f}", inline=True)
    embed.add_field(
        name="メモリ",
        value=f"{mem.used / 2**30:.1f} / {mem.total / 2**30:.1f} GB ({mem.percent:.0f}%)",
        inline=False,
    )
    embed.add_field(
        name="ストレージ",
        value=f"{disk.used / 2**30:.0f} / {disk.total / 2**30:.0f} GB ({disk.percent:.0f}%)",
        inline=True,
    )
    embed.add_field(name="性能制限", value=throttled(), inline=True)
    embed.set_footer(text="負荷は4を超えると重い状態の目安（4コア）")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="cmd", description="サーバーのコンソールコマンドを実行します（管理者のみ）")
@app_commands.describe(command="例: whitelist add .ゲーマータグ")
async def cmd(interaction: discord.Interaction, command: str) -> None:
    if not ADMIN_IDS or interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("このコマンドは管理者だけが使えます。", ephemeral=True)
        return
    if not await is_running():
        await interaction.response.send_message("サーバーが停止しています。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        result = await rcon_command(command.lstrip("/"))
    except Exception as e:
        await interaction.followup.send(f"実行できませんでした: {e}", ephemeral=True)
        return
    await interaction.followup.send(f"```\n{(result or '(出力なし)')[:1900]}\n```", ephemeral=True)


@bot.tree.command(name="panel", description="このチャンネルにサーバー管理パネルを設置します（管理者のみ）")
async def panel(interaction: discord.Interaction) -> None:
    if not is_panel_admin(interaction):
        await interaction.response.send_message("このコマンドは管理者だけが使えます。", ephemeral=True)
        return
    channel = interaction.channel
    if PANEL_CHANNEL_ID and (channel is None or channel.id != PANEL_CHANNEL_ID):
        # The panel lives in PANEL_CHANNEL_ID; /panel just (re)sends it there.
        try:
            channel = await bot.panel_channel()
        except discord.HTTPException:
            await interaction.response.send_message(
                f"PANEL_CHANNEL_ID のチャンネル（{PANEL_CHANNEL_ID}）を開けませんでした。", ephemeral=True
            )
            return
        perms = channel.permissions_for(channel.guild.me)
    elif channel is None or not hasattr(channel, "send"):
        await interaction.response.send_message("このチャンネルには設置できません。", ephemeral=True)
        return
    else:
        perms = interaction.app_permissions
    can_send = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
    missing = [
        name for ok, name in (
            (perms.view_channel, "チャンネルを見る"),
            (can_send, "メッセージを送信"),
            (perms.embed_links, "埋め込みリンク"),
        ) if not ok
    ]
    if missing:
        await interaction.response.send_message(
            f"ボットに {channel.mention} の権限が足りません: " + "、".join(missing), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # Only one panel is kept up to date; the previous one is removed.
        _, note = await bot.place_panel(channel, force=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"パネルを投稿できませんでした: {e}", ephemeral=True)
        return
    msg = f"✅ {channel.mention} にパネルを設置しました。30秒ごとに自動で更新されます。"
    if PANEL_CHANNEL_ID:
        msg += "\nこのチャンネルに投稿があると、パネルは自動で一番下に送り直されます。"
    if note:
        msg += "\n" + note
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="登録解除", description="ユーザーのマイクラアカウントをホワイトリストから外します（管理者のみ）")
@app_commands.describe(user="登録を解除する Discord ユーザー")
@app_commands.rename(user="ユーザー")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def unregister_cmd(interaction: discord.Interaction, user: discord.User) -> None:
    if not interaction.permissions.administrator:
        await interaction.response.send_message("このコマンドは Discord サーバーの管理者だけが使えます。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    name = await bot.unregister(user.id, f"/登録解除 by {interaction.user.id}")
    if name is None:
        await interaction.followup.send(f"{user.mention} の登録は見つかりませんでした。", ephemeral=True)
    else:
        await interaction.followup.send(
            f"{user.mention} の `{show_name(name)}` をホワイトリストから外しました。（遊んでいる最中の場合、退出するまでは入ったままです）",
            ephemeral=True,
        )


if __name__ == "__main__":
    bot.run(TOKEN)
