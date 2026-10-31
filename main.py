"""
Discord-бот для статистики FACEIT (CS2) с хранением привязок в SQLite.

Команды:
  /link            — авторизация через Discord, бот берёт подключённый Steam,
                     запоминает его и присылает статистику FACEIT
  /stats           — твоя статистика FACEIT (после /link, без повторной авторизации)
  /faceit <ник>    — статистика по нику FACEIT
  /unlink          — удалить свои данные из базы бота
"""

import os
import uuid
import asyncio
import threading
import logging
import html
from urllib.parse import quote

import requests
from flask import Flask, request, Response
from waitress import serve
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

import db

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "https://makki-solutions.bid/callback")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # секунд

if not all([DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET, FACEIT_API_KEY]):
    raise RuntimeError(
        "Не заполнены переменные окружения. Скопируй .env.example в .env и заполни значения."
    )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faceit-bot")

OAUTH_SCOPES = "identify connections"
FACEIT_API = "https://open.faceit.com/data/v4"
FACEIT_HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

db.init_db()

# ---------------------------------------------------------------------------
# FACEIT API
# ---------------------------------------------------------------------------

def faceit_get(path: str, params: dict | None = None) -> requests.Response:
    return requests.get(f"{FACEIT_API}{path}", params=params, headers=FACEIT_HEADERS, timeout=15)


def player_by_steam(steam_id64: str) -> dict | None:
    r = faceit_get("/players", {"game": "cs2", "game_player_id": steam_id64})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def player_by_nickname(nickname: str) -> dict | None:
    r = faceit_get("/players", {"nickname": nickname})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


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

    stats = faceit_get(f"/players/{player['player_id']}/stats/cs2")
    if stats.ok:
        lt = stats.json().get("lifetime", {})
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


def stats_by_steam(steam_id64: str, steam_name: str = "") -> str:
    player = player_by_steam(steam_id64)
    if player is None:
        return (
            f"Для Steam-аккаунта {steam_name or steam_id64} не найден профиль FACEIT "
            "с игрой CS2 (возможно, FACEIT не привязан к этому Steam)."
        )
    return build_faceit_message(player)


def stats_by_nickname(nickname: str) -> str:
    player = player_by_nickname(nickname)
    if player is None:
        return f"Игрок '{nickname}' не найден на FACEIT."
    return build_faceit_message(player)


def cached(key: str, producer) -> str:
    """Кэширует текстовый ответ в SQLite на CACHE_TTL секунд."""
    hit = db.cache_get(key, CACHE_TTL)
    if hit is not None:
        return hit
    value = producer()
    db.cache_set(key, value)
    return value


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


@bot.tree.command(name="link", description="Привязать Steam через Discord и получить статистику FACEIT")
async def link(interaction: discord.Interaction):
    state = str(uuid.uuid4())
    db.create_state(state, interaction.user.id)

    oauth_url = (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        "&response_type=code"
        f"&scope={quote(OAUTH_SCOPES)}"
        f"&state={state}"
    )

    embed = discord.Embed(
        title="Привязка Steam → FACEIT",
        description=(
            "Нажми на ссылку ниже и разреши доступ к своим Discord Connections.\n"
            "В User Settings → Connections должен быть подключён **Steam** "
            "с включённой видимостью.\n"
            "Ссылка действует 10 минут. После привязки можно использовать `/stats`.\n\n"
            f"[Авторизоваться через Discord]({oauth_url})"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Твоя статистика FACEIT (нужна привязка через /link)")
async def stats_cmd(interaction: discord.Interaction):
    user = db.get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "Аккаунт не привязан. Сначала используй `/link`.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    steam_id, steam_name = user["steam_id64"], user["steam_name"] or ""
    try:
        text = await asyncio.to_thread(
            cached, f"steam:{steam_id}", lambda: stats_by_steam(steam_id, steam_name)
        )
    except Exception as e:
        log.exception("Ошибка /stats")
        text = f"Ошибка при запросе к FACEIT: {e}"
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name="faceit", description="Статистика FACEIT по нику")
@app_commands.describe(nickname="Ник на FACEIT")
async def faceit_cmd(interaction: discord.Interaction, nickname: str):
    await interaction.response.defer(thinking=True)
    try:
        text = await asyncio.to_thread(
            cached, f"nick:{nickname.lower()}", lambda: stats_by_nickname(nickname)
        )
    except Exception as e:
        log.exception("Ошибка /faceit")
        text = f"Ошибка при запросе к FACEIT: {e}"
    await interaction.followup.send(text)


@bot.tree.command(name="unlink", description="Удалить свои данные из базы бота")
async def unlink(interaction: discord.Interaction):
    deleted = db.delete_user(interaction.user.id)
    msg = "Готово, твои данные удалены." if deleted else "У бота нет сохранённых данных о тебе."
    await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Flask: OAuth2 callback
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


def page(title: str, body: str = "", status: int = 200) -> Response:
    content = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>FACEIT bot</title>"
        "<body style='font-family:sans-serif;max-width:560px;margin:40px auto;padding:0 16px'>"
        f"<h2>{html.escape(title)}</h2>"
        f"<pre style='white-space:pre-wrap;font-family:inherit'>{html.escape(body)}</pre>"
    )
    return Response(content, status=status, mimetype="text/html")


@flask_app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return page("Авторизация отклонена", error, 400)
    if not code or not state:
        return page("Некорректный запрос", "Попробуй /link ещё раз.", 400)

    discord_user_id = db.pop_state(state)
    if discord_user_id is None:
        return page("Ссылка устарела", "Попробуй /link ещё раз.", 400)

    try:
        access_token = exchange_code_for_token(code)
        connections = get_discord_connections(access_token)
        steam = next((c for c in connections if c.get("type") == "steam"), None)

        if not steam:
            msg = (
                "Не нашёл Steam среди твоих подключений.\n"
                "Добавь Steam в User Settings → Connections, включи видимость и повтори /link."
            )
            schedule_dm(discord_user_id, msg)
            return page("Steam не найден", msg)

        # у подключения Steam поле id — это SteamID64
        db.save_user(discord_user_id, steam["id"], steam.get("name"))
        text = stats_by_steam(steam["id"], steam.get("name", ""))
        schedule_dm(discord_user_id, text)

        # дублируем на страницу — на случай, если ЛС у пользователя закрыты
        return page(
            "Готово! Steam привязан",
            text.replace("**", "") + "\n\nТеперь можно использовать /stats в Discord.",
        )

    except Exception as e:
        log.exception("Ошибка при обработке OAuth callback")
        return page("Произошла ошибка", str(e), 500)


def exchange_code_for_token(code: str) -> str:
    resp = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
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
# Отправка ЛС из потока веб-сервера
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
        log.warning(f"Не удалось отправить ЛС пользователю {discord_user_id} (возможно, ЛС закрыты)")


def run_web():
    serve(flask_app, host=WEB_HOST, port=WEB_PORT, threads=4)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    log.info(f"Веб-сервер OAuth запущен на {WEB_HOST}:{WEB_PORT}")
    bot.run(DISCORD_TOKEN)
