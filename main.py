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

# Если запущено под systemd с LoadCredentialEncrypted=env:..., секреты лежат
# в расшифрованном виде во временной папке, доступной только этому сервису.
_cred_dir = os.getenv("CREDENTIALS_DIRECTORY")
if _cred_dir and os.path.isfile(os.path.join(_cred_dir, "env")):
    load_dotenv(os.path.join(_cred_dir, "env"))
else:
    load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "https://faceit.makki-solutions.bid/callback")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # секунд
DB_PATH = os.getenv("DB_PATH", "bot.db")
DB_ENCRYPTION_KEY = os.getenv("DB_ENCRYPTION_KEY")

if not all([DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET, FACEIT_API_KEY]):
    raise RuntimeError(
        "Не заполнены переменные окружения (DISCORD_TOKEN, DISCORD_CLIENT_ID, "
        "DISCORD_CLIENT_SECRET, FACEIT_API_KEY)."
    )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faceit-bot")

OAUTH_SCOPES = "identify connections"
FACEIT_API = "https://open.faceit.com/data/v4"
FACEIT_HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

db.configure(DB_PATH, DB_ENCRYPTION_KEY)
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

# не чаще одного запуска команды раз в 15 секунд на пользователя
cooldown = app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)


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


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Подожди {error.retry_after:.0f} с перед следующим запуском."
    else:
        log.error("Ошибка команды", exc_info=error)
        msg = "Произошла ошибка, попробуй позже."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="link", description="Привязать Steam через Discord и получить статистику FACEIT")
@cooldown
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
            "Ссылка личная, действует 10 минут и работает только с твоего аккаунта Discord. "
            "После привязки можно использовать `/stats`.\n\n"
            f"[Авторизоваться через Discord]({oauth_url})"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Твоя статистика FACEIT (нужна привязка через /link)")
@cooldown
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
    except Exception:
        log.exception("Ошибка /stats")
        text = "Не удалось получить статистику FACEIT, попробуй позже."
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name="faceit", description="Статистика FACEIT по нику")
@app_commands.describe(nickname="Ник на FACEIT")
@cooldown
async def faceit_cmd(interaction: discord.Interaction, nickname: app_commands.Range[str, 2, 32]):
    await interaction.response.defer(thinking=True)
    try:
        text = await asyncio.to_thread(
            cached, f"nick:{nickname.lower()}", lambda: stats_by_nickname(nickname)
        )
    except Exception:
        log.exception("Ошибка /faceit")
        text = "Не удалось получить статистику FACEIT, попробуй позже."
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


@flask_app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return resp


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
        return page("Авторизация отклонена", "Попробуй /link ещё раз.", 400)
    if not code or not state:
        return page("Некорректный запрос", "Попробуй /link ещё раз.", 400)

    discord_user_id = db.pop_state(state)
    if discord_user_id is None:
        return page("Ссылка устарела", "Попробуй /link ещё раз.", 400)

    access_token = None
    try:
        access_token = exchange_code_for_token(code)

        # Проверяем, что авторизовался тот же аккаунт Discord, который вызвал /link.
        # Иначе чужую ссылку можно подсунуть другому человеку и привязать его Steam к себе.
        me = get_discord_user(access_token)
        if int(me["id"]) != discord_user_id:
            log.warning("OAuth: аккаунт Discord не совпал с владельцем ссылки")
            return page(
                "Ссылка выдана другому аккаунту",
                "Открой ссылку с того же аккаунта Discord, который вызвал /link, "
                "и запусти /link заново.",
                403,
            )

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

    except Exception:
        log.exception("Ошибка при обработке OAuth callback")
        return page("Произошла ошибка", "Не удалось завершить привязку. Попробуй /link ещё раз позже.", 500)
    finally:
        # токен нужен был один раз — отзываем, чтобы он не мог быть использован повторно
        if access_token:
            revoke_token(access_token)


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


def get_discord_user(access_token: str) -> dict:
    resp = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_discord_connections(access_token: str) -> list[dict]:
    resp = requests.get(
        "https://discord.com/api/users/@me/connections",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def revoke_token(access_token: str):
    try:
        requests.post(
            "https://discord.com/api/oauth2/token/revoke",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "token": access_token,
                "token_type_hint": "access_token",
            },
            timeout=10,
        )
    except Exception:
        log.warning("Не удалось отозвать OAuth-токен (не критично)")


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
