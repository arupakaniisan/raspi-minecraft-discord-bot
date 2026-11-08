"""Raspberry Pi Minecraft server control bot for Discord.

Slash commands:
  /start   Start the Minecraft server (systemd user service)
  /stop    Stop the Minecraft server (saves the world first)
  /status  Show whether the server is running and who is online
  /pc      Show Raspberry Pi temperature, CPU, memory and disk usage
  /cmd     Run a server console command via RCON (admins only)
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import subprocess
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

GUILD = discord.Object(id=GUILD_ID)


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


# ---------------------------------------------------------------- bot

class McBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.warned = False

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        if ALERT_CHANNEL_ID:
            self.temp_watch.start()

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


if __name__ == "__main__":
    bot.run(TOKEN)
