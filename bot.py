"""Raspberry Pi Minecraft server control bot for Discord.

Slash commands:
  /start   Start the Minecraft server (systemd user service)
  /stop    Stop the Minecraft server (saves the world first)
  /status  Show whether the server is running and who is online
  /pc      Show Raspberry Pi temperature, CPU, memory and disk usage
  /cmd     Run a server console command via RCON (admins only)
  /panel   Post a server control panel with buttons (admins only)

If PANEL_CHANNEL_ID is set, the panel is kept as the last message of that channel:
when someone posts there, the old panel is deleted and a new one is sent at the bottom.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
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

# Channel where the panel is kept at the bottom (0 = off: the panel stays where /panel put it).
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0") or 0)

GUILD = discord.Object(id=GUILD_ID)

# The bot's own systemd user service (used by the panel's "bot restart" button).
BOT_SERVICE = "mcbot"

# Where the panel's channel/message IDs are kept across restarts.
PANEL_FILE = Path(__file__).resolve().parent / "panel.json"
PANEL_INTERVAL = 30       # normal auto refresh (seconds)
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

    if info.state != ONLINE:
        players = "-"
    elif not info.players_ok:
        players = "取得できませんでした"
    else:
        players = "\n".join(info.players) or "なし"
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.panel_fast()
        code, out = await systemctl("start", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"起動に失敗しました。\n```\n{out[:1500]}\n```", ephemeral=True)
            return
        await interaction.followup.send(
            "🟢 起動を開始しました。入れるようになるまで1分ほど待ってください。", ephemeral=True
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.panel_fast()
        code, out = await systemctl("stop", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"停止に失敗しました。\n```\n{out[:1500]}\n```", ephemeral=True)
            return
        await interaction.followup.send("🔴 サーバーを停止しました。ワールドは保存済みです。", ephemeral=True)

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
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.panel_fast()
        code, out = await systemctl("restart", SERVICE_NAME)
        bot.panel_fast()
        if code != 0:
            await interaction.followup.send(f"再起動に失敗しました。\n```\n{out[:1500]}\n```", ephemeral=True)
            return
        await interaction.followup.send(
            "🔄 サーバーを再起動しました。入れるようになるまで1分ほど待ってください。", ephemeral=True
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
            "🤖 ボットを再起動します。10秒ほどで戻ってきます。", ephemeral=True
        )
        log.info("panel: bot restart requested by %s", interaction.user.id)
        # --no-block: systemd carries out the restart even though it kills this process.
        await systemctl("restart", "--no-block", BOT_SERVICE)

# ---------------------------------------------------------------- bot

class McBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.warned = False
        self.panel_fast_since = 0.0
        self.panel_fast_until = 0.0
        self.panel_task: asyncio.Task | None = None
        self.repost_task: asyncio.Task | None = None
        self.repost_first: float | None = None  # when the first not-yet-handled message came in
        self.repost_at = 0.0

    async def setup_hook(self) -> None:
        self.panel_wake = asyncio.Event()
        self.panel_lock = asyncio.Lock()
        self.add_view(PanelView())  # keep panel buttons working after a restart
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        if ALERT_CHANNEL_ID:
            self.temp_watch.start()
        self.panel_task = asyncio.create_task(self.panel_loop())

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

    async def on_message(self, message: discord.Message) -> None:
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


if __name__ == "__main__":
    bot.run(TOKEN)
