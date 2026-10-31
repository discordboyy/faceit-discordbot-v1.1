"""
Discord-бот для статистики FACEIT (CS2).

Как это работает:
1. Пользователь пишет /link
2. Бот присылает ссылку OAuth2-авторизации Discord (scope: identify + connections)
3. Discord редиректит на Flask-сервер (/callback), тот меняет code на access_token
4. Бот берёт из подключений Discord подключение Steam (в поле id лежит SteamID64)
5. По SteamID64 через официальный FACEIT Data API получает статистику
6. Результат отправляется пользователю в личные сообщения

Дополнительно: /faceit <ник> — статистика по нику FACEIT без авторизации.
"""

import os
import uuid
import asyncio
import threading
import logging
from urllib.parse import quote

import requests
from flask import Flask, request
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

if not all([DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET, FACEIT_API_KEY]):
    raise RuntimeError(
        "Не заполнены переменные окружения. Скопируй .env.example в .env и заполни значения."
    )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faceit-bot")

OAUTH_SCOPES = "identify connections"
FACEIT_API = "https://open.faceit.com/data/v4"
FACEIT_HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

# state -> discord_user_id (чтобы после OAuth знать, кому слать результат)
pending_states: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
bot_loop: asyncio.AbstractEventLoop | None = None


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    try:
        synced = await bot.tree.sync()
        log.info(f"Синхронизировано {len(synced)} слэш-команд")
    except Exception as e:
        log.error(f"Не удалось синхронизировать команды: {e}")
    log.info(f"Бот запущен как {bot.user}")


@bot.tree.command(name="link", description="Получить статистику FACEIT через подключённый Steam")
async def link(interaction: discord.Interaction):
    state = str(uuid.uuid4())
    pending_states[state] = interaction.user.id

    oauth_url = (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        "&response_type=code"
        f"&scope={quote(OAUTH_SCOPES)}"
        f"&state={state}"
    )

    embed = discord.Embed(
        title="Статистика FACEIT",
        description=(
            "Нажми на ссылку ниже и разреши доступ к своим Discord Connections.\n"
            "Важно: в User Settings → Connections у тебя должен быть подключён "
            "**Steam** с включённой видимостью.\n\n"
            f"[Авторизоваться через Discord]({oauth_url})"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="faceit", description="Статистика FACEIT по нику")
@app_commands.describe(nickname="Ник на FACEIT")
async def faceit_cmd(interaction: discord.Interaction, nickname: str):
    await interaction.response.defer(thinking=True)
    try:
        text = await asyncio.to_thread(fetch_faceit_by_nickname, nickname)
    except Exception as e:
        log.exception("Ошибка /faceit")
        text = f"Ошибка при запросе к FACEIT: {e}"
    await interaction.followup.send(text)


# ---------------------------------------------------------------------------
# FACEIT API
# ---------------------------------------------------------------------------

def fetch_faceit_by_steam(steam_id64: str, steam_name: str = "") -> str:
    resp = requests.get(
        f"{FACEIT_API}/players",
        params={"game": "cs2", "game_player_id": steam_id64},
        headers=FACEIT_HEADERS,
        timeout=15,
    )
    if resp.status_code == 404:
        return (
            f"Для Steam-аккаунта {steam_name or steam_id64} не найден профиль FACEIT "
            "с игрой CS2 (возможно, FACEIT не привязан к этому Steam)."
        )
    resp.raise_for_status()
    return build_faceit_message(resp.json())


def fetch_faceit_by_nickname(nickname: str) -> str:
    resp = requests.get(
        f"{FACEIT_API}/players",
        params={"nickname": nickname},
        headers=FACEIT_HEADERS,
        timeout=15,
    )
    if resp.status_code == 404:
        return f"Игрок '{nickname}' не найден на FACEIT."
    resp.raise_for_status()
    return build_faceit_message(resp.json())


def build_faceit_message(player: dict) -> str:
    cs2 = player.get("games", {}).get("cs2", {})
    nickname = player.get("nickname", "—")
    faceit_url = player.get("faceit_url", "").replace("{lang}", "en")

    if not cs2:
        return f"У игрока {nickname} нет статистики по CS2 на FACEIT."

    lines = [
        f"**FACEIT CS2 — {nickname}**",
        f"Уровень: {cs2.get('skill_level', '—')}",
        f"ELO: {cs2.get('faceit_elo', '—')}",
        f"Регион: {cs2.get('region', '—')}",
        f"Страна: {str(player.get('country', '—')).upper()}",
    ]

    stats_resp = requests.get(
        f"{FACEIT_API}/players/{player['player_id']}/stats/cs2",
        headers=FACEIT_HEADERS,
        timeout=15,
    )
    if stats_resp.ok:
        lt = stats_resp.json().get("lifetime", {})
        lines += [
            f"Матчи: {lt.get('Matches', '—')}",
            f"Win Rate: {lt.get('Win Rate %', '—')}%",
            f"Средний K/D: {lt.get('Average K/D Ratio', '—')}",
            f"Хедшоты: {lt.get('Average Headshots %', '—')}%",
            f"Серия побед: {lt.get('Current Win Streak', '—')}",
        ]

    if faceit_url:
        lines.append(f"Профиль: {faceit_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flask: OAuth2 callback
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return f"Авторизация отклонена: {error}", 400
    if not code or not state or state not in pending_states:
        return "Некорректный или устаревший запрос. Попробуй /link ещё раз.", 400

    discord_user_id = pending_states.pop(state)

    try:
        access_token = exchange_code_for_token(code)
        connections = get_discord_connections(access_token)
        steam = next((c for c in connections if c.get("type") == "steam"), None)

        if not steam:
            schedule_dm(
                discord_user_id,
                "Не нашёл Steam среди твоих подключений.\n"
                "Добавь Steam в User Settings → Connections и включи видимость, затем повтори /link.",
            )
            return "Steam не найден. Подробности отправлены в ЛС Discord."

        # у подключения Steam поле id — это SteamID64
        text = fetch_faceit_by_steam(steam.get("id"), steam.get("name", ""))
        schedule_dm(discord_user_id, text)
        return f"Готово! Steam: {steam.get('name')}. Статистика отправлена в ЛС Discord. Можно закрыть вкладку."

    except Exception as e:
        log.exception("Ошибка при обработке OAuth callback")
        schedule_dm(discord_user_id, f"Ошибка при получении статистики: {e}")
        return f"Произошла ошибка: {e}", 500


def exchange_code_for_token(code: str) -> str:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_discord_connections(access_token: str) -> list[dict]:
    resp = requests.get(
        "https://discord.com/api/users/@me/connections",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Отправка ЛС из потока Flask
# ---------------------------------------------------------------------------

def schedule_dm(discord_user_id: int, message: str):
    if bot_loop is None:
        log.error("Bot loop ещё не готов, не могу отправить сообщение")
        return
    asyncio.run_coroutine_threadsafe(send_dm(discord_user_id, message), bot_loop)


async def send_dm(discord_user_id: int, message: str):
    try:
        user = await bot.fetch_user(discord_user_id)
        for i in range(0, len(message), 1900):
            await user.send(message[i:i + 1900])
    except Exception:
        log.exception(f"Не удалось отправить ЛС пользователю {discord_user_id}")


def run_flask():
    flask_app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"Flask OAuth-сервер запущен на порту {WEB_PORT}")
    bot.run(DISCORD_TOKEN)
