"""
Discord-бот для статистики FACEIT (CS2) с хранением привязок в SQLite.

Команды:
  /link               — авторизация через Discord, бот берёт подключённый Steam,
                        запоминает его и присылает статистику FACEIT
  /stats [участник]   — статистика FACEIT: твоя или другого привязавшего аккаунт участника
  /leaderboard        — топ участников сервера по ELO со статистикой за последние матчи
  /faceit <ник>       — статистика по нику FACEIT
  /unlink             — удалить свои данные из базы бота
"""

import os
import uuid
import asyncio
import threading
import logging
import html
import math
import time
from urllib.parse import quote

import requests
from flask import Flask, request, Response
from waitress import serve
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

import db
import faceit

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
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # секунд, кэш только в памяти
STATS_MATCHES = int(os.getenv("STATS_MATCHES", "30"))  # статистика за последние N матчей
LEADERBOARD_SIZE = max(1, min(25, int(os.getenv("LEADERBOARD_SIZE", "10"))))
LEADERBOARD_MAX_USERS = 100  # сколько привязанных пользователей проверять за один вызов
LEADERBOARD_CACHE_TTL = int(os.getenv("LEADERBOARD_CACHE_TTL", "300"))  # секунд
LEADERBOARD_SHOW_RECENT = os.getenv("LEADERBOARD_SHOW_RECENT", "1") != "0"  # K/D и WR
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

db.configure(DB_PATH, DB_ENCRYPTION_KEY)
db.init_db()
faceit.configure(FACEIT_API_KEY, CACHE_TTL, STATS_MATCHES)

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
        if "scrim" not in bot.extensions:
            await bot.load_extension("scrim")
        if "tournament" not in bot.extensions:
            await bot.load_extension("tournament")
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
            "Ссылка личная, действует 10 минут и работает только с твоего аккаунта Discord.\n\n"
            "После привязки твоя статистика FACEIT будет доступна участникам серверов, где ты состоишь "
            "(`/stats @ты` и `/leaderboard`). Убрать себя можно командой `/unlink`.\n\n"
            f"[Авторизоваться через Discord]({oauth_url})"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Статистика FACEIT: твоя или другого участника")
@app_commands.describe(member="Участник сервера (если не указан — ты)")
@cooldown
async def stats_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    own = target.id == interaction.user.id
    no_ping = discord.AllowedMentions.none()

    if target.bot:
        await interaction.response.send_message(
            "У ботов нет статистики FACEIT.",
            ephemeral=True,
        )
        return

    user = db.get_user(target.id)
    if not user:
        if own:
            await interaction.response.send_message(
                "Аккаунт не привязан. Сначала используй `/link`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} ещё не привязал(а) аккаунт. "
                "Чтобы появилась статистика, нужно сделать `/link`.",
                allowed_mentions=no_ping,
            )
        return

    # Своя статистика — приватно; статистика другого участника — в канале.
    await interaction.response.defer(thinking=True, ephemeral=own)

    try:
        player = await asyncio.to_thread(
            faceit.player_by_steam,
            user["steam_id64"],
        )

        if player is None:
            await interaction.followup.send(
                f"Для Steam-аккаунта **{user.get('steam_name') or 'пользователя'}** "
                "не найден профиль FACEIT с игрой CS2.",
                allowed_mentions=no_ping,
            )
            return

        cs2 = player.get("games", {}).get("cs2")
        if not cs2:
            await interaction.followup.send(
                f"У игрока **{player.get('nickname', '—')}** "
                "нет статистики по CS2 на FACEIT.",
                allowed_mentions=no_ping,
            )
            return

        try:
            recent = await asyncio.to_thread(
                faceit.recent_stats,
                player["player_id"],
            )
        except Exception:
            log.warning("Не удалось получить недавнюю статистику FACEIT", exc_info=True)
            recent = {"matches": 0}

        nickname = player.get("nickname", "—")
        display_name = target.display_name
        profile_url = player.get("faceit_url", "").replace("{lang}", "en")
        matches = int(recent.get("matches") or 0)
        wins = int(recent.get("wins") or 0)
        losses = max(0, matches - wins)

        # Карточка в стиле примера: компактные поля в три колонки и FACEIT OG image.
        embed = discord.Embed(
            title=f"╭  FACEIT CS2 — {nickname} ({display_name}) ╮",
            url=profile_url or None,
            color=0x1B1B1B,
        )

        embed.add_field(name="Уровень:", value=str(cs2.get("skill_level", "—")), inline=True)
        embed.add_field(name="ELO:", value=str(cs2.get("faceit_elo", "—")), inline=True)
        embed.add_field(name="Регион:", value=str(cs2.get("region", "—")), inline=True)
        embed.add_field(name="Страна:", value=str(player.get("country", "—")).upper(), inline=True)

        if matches:
            embed.add_field(
                name="Win Rate:",
                value=f"{recent.get('win_rate', 0):.0f}% ({wins}W / {losses}L)",
                inline=True,
            )
            if recent.get("kd") is not None:
                embed.add_field(name="Средний K/D:", value=f"{recent['kd']:.2f}", inline=True)
            if recent.get("hs") is not None:
                embed.add_field(name="Хедшоты:", value=f"{recent['hs']:.0f}%", inline=True)
            if recent.get("avg_kills") is not None:
                embed.add_field(name="Убийств за матч:", value=f"{recent['avg_kills']:.1f}", inline=True)
            embed.add_field(
                name="Текущая серия побед:",
                value=str(recent.get("streak", 0)),
                inline=True,
            )
        else:
            embed.add_field(
                name="Последние матчи:",
                value="Пока нет данных о недавних матчах CS2.",
                inline=False,
            )

        # FACEIT генерирует OG-картинку профиля по никнейму; thumbnail остаётся запасным вариантом.
        og_image = f"https://og-images.faceit-cdn.net/v1/players/{quote(nickname, safe='')}/profile"
        embed.set_image(url=og_image)
        if not player.get("faceit_url") and player.get("avatar"):
            embed.set_thumbnail(url=player["avatar"])

        if matches:
            embed.set_footer(text=f"Показатели за последние {matches} матчей FACEIT")

        await interaction.followup.send(
            embed=embed,
            allowed_mentions=no_ping,
        )

    except Exception:
        log.exception("Ошибка /stats")
        await interaction.followup.send(
            "Не удалось получить статистику FACEIT, попробуй позже.",
            allowed_mentions=no_ping,
        )


# --- Лидерборд -------------------------------------------------------------

_member_cache: dict[tuple[int, int], tuple[float, bool]] = {}
MEMBER_CACHE_TTL = 600


async def is_member(guild: discord.Guild, user_id: int) -> bool:
    """Состоит ли пользователь в гильдии (REST, без привилегированного Members intent)."""
    key = (guild.id, user_id)
    hit = _member_cache.get(key)
    if hit and time.monotonic() - hit[0] < MEMBER_CACHE_TTL:
        return hit[1]

    ok = guild.get_member(user_id) is not None
    if not ok:
        try:
            await guild.fetch_member(user_id)
            ok = True
        except discord.NotFound:
            ok = False
        except discord.HTTPException:
            log.warning("Не удалось проверить участника гильдии", exc_info=True)
            return False  # не кэшируем сбой
    _member_cache[key] = (time.monotonic(), ok)
    return ok


async def collect_leaderboard(guild: discord.Guild) -> tuple[list[dict], int]:
    """Возвращает (все игроки по убыванию ELO, сколько привязавших не имеют CS2 на FACEIT)."""
    users = await asyncio.to_thread(db.list_users, LEADERBOARD_MAX_USERS)
    sem = asyncio.Semaphore(4)  # не заваливаем FACEIT API параллельными запросами

    async def load(u: dict):
        if not await is_member(guild, u["discord_id"]):
            return None
        async with sem:
            player = await asyncio.to_thread(faceit.player_by_steam, u["steam_id64"])
        return u["discord_id"], player

    results = await asyncio.gather(*(load(u) for u in users), return_exceptions=True)

    ranked, without_cs2 = [], 0
    for r in results:
        if isinstance(r, Exception):
            log.warning("Ошибка загрузки профиля для лидерборда", exc_info=r)
            continue
        if r is None:
            continue
        discord_id, player = r
        cs2 = (player or {}).get("games", {}).get("cs2")
        elo = faceit._num(cs2.get("faceit_elo")) if cs2 else None
        if elo is None:
            without_cs2 += 1
            continue
        ranked.append({
            "discord_id": discord_id,
            "elo": int(elo),
            "level": cs2.get("skill_level", "—"),
            "player_id": player["player_id"],
            "recent": {"matches": 0},
            "recent_loaded": False,
        })

    ranked.sort(key=lambda row: row["elo"], reverse=True)
    return ranked, without_cs2


_lb_cache: dict[int, tuple[float, list[dict], int]] = {}
_lb_locks: dict[int, asyncio.Lock] = {}
_recent_locks: dict[str, asyncio.Lock] = {}


async def get_leaderboard(guild: discord.Guild) -> tuple[list[dict], int]:
    """Лидерборд сервера из кэша или собранный заново (не чаще раза в LEADERBOARD_CACHE_TTL)."""
    lock = _lb_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        hit = _lb_cache.get(guild.id)
        if hit and time.monotonic() - hit[0] < LEADERBOARD_CACHE_TTL:
            return hit[1], hit[2]
        ranked, without_cs2 = await collect_leaderboard(guild)
        _lb_cache[guild.id] = (time.monotonic(), ranked, without_cs2)
        return ranked, without_cs2


async def load_recent_for(rows: list[dict]) -> None:
    """Подгружает K/D и WR только для переданных строк (одной страницы)."""
    if not LEADERBOARD_SHOW_RECENT:
        return
    sem = asyncio.Semaphore(4)

    async def load_recent(row: dict):
        if row["recent_loaded"]:
            return
        lock = _recent_locks.setdefault(str(row["player_id"]), asyncio.Lock())
        async with lock:
            # Повторно проверяем под блокировкой: другой вызов мог уже загрузить статистику.
            if row["recent_loaded"]:
                return
            async with sem:
                try:
                    row["recent"] = await asyncio.to_thread(faceit.recent_stats, row["player_id"])
                    row["recent_loaded"] = True
                except Exception:
                    log.warning("Ошибка загрузки последних матчей", exc_info=True)

    await asyncio.gather(*(load_recent(row) for row in rows))


def build_leaderboard_embed(
    guild: discord.Guild, ranked: list[dict], without_cs2: int, page: int, pages: int
) -> discord.Embed:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    start = page * LEADERBOARD_SIZE
    lines = []
    for i, row in enumerate(ranked[start:start + LEADERBOARD_SIZE], start + 1):
        parts = [f"Lv **{row['level']}**", f"**{row['elo']}** ELO"]
        r = row["recent"]
        if LEADERBOARD_SHOW_RECENT and r.get("matches"):
            if r.get("kd") is not None:
                parts.append(f"K/D {r['kd']:.2f}")
            parts.append(f"WR {r['win_rate']:.0f}%")
        lines.append(f"{medals.get(i, f'**{i}.**')} <@{row['discord_id']}> · " + " · ".join(parts))

    embed = discord.Embed(
        title=f"Топ FACEIT CS2 — {guild.name}",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    footer = f"Страница {page + 1}/{pages} · игроков: {len(ranked)}"
    if LEADERBOARD_SHOW_RECENT:
        footer += f" · K/D и WR — за последние {STATS_MATCHES} матчей"
    if without_cs2:
        footer += f" · без CS2 на FACEIT: {without_cs2}"
    embed.set_footer(text=footer)
    return embed


class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, ranked: list[dict], without_cs2: int, owner_id: int):
        super().__init__(timeout=180)
        self.guild = guild
        self.ranked = ranked
        self.without_cs2 = without_cs2
        self.owner_id = owner_id
        self.page = 0
        self.pages = max(1, math.ceil(len(ranked) / LEADERBOARD_SIZE))
        self.message: discord.Message | None = None
        self._sync_buttons()

    def page_rows(self) -> list[dict]:
        start = self.page * LEADERBOARD_SIZE
        return self.ranked[start:start + LEADERBOARD_SIZE]

    def embed(self) -> discord.Embed:
        return build_leaderboard_embed(self.guild, self.ranked, self.without_cs2, self.page, self.pages)

    def _sync_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Листать может только тот, кто вызвал команду. Запусти `/leaderboard` сам.",
                ephemeral=True,
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await load_recent_for(self.page_rows())
        self._sync_buttons()
        await interaction.edit_original_response(embed=self.embed(), view=self)

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Вперёд ▶", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="leaderboard", description="Топ участников сервера по ELO FACEIT")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 30.0, key=lambda i: i.guild_id)
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    ranked, without_cs2 = await get_leaderboard(interaction.guild)

    if not ranked:
        await interaction.followup.send(
            "На этом сервере пока никто не привязал аккаунт с CS2 на FACEIT. Используй `/link`."
        )
        return

    view = LeaderboardView(interaction.guild, ranked, without_cs2, interaction.user.id)
    await load_recent_for(view.page_rows())  # статистика только для первой страницы

    msg = await interaction.followup.send(
        embed=view.embed(),
        view=view if view.pages > 1 else discord.utils.MISSING,
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True,
    )
    view.message = msg


@bot.tree.command(name="faceit", description="Статистика FACEIT по нику")
@app_commands.describe(nickname="Ник на FACEIT")
@cooldown
async def faceit_cmd(interaction: discord.Interaction, nickname: app_commands.Range[str, 2, 32]):
    await interaction.response.defer(thinking=True)
    try:
        text = await asyncio.to_thread(faceit.text_by_nickname, nickname)
    except Exception:
        log.exception("Ошибка /faceit")
        text = "Не удалось получить статистику FACEIT, попробуй позже."
    await interaction.followup.send(text)


@bot.tree.command(name="unlink", description="Удалить свои данные из базы бота")
async def unlink(interaction: discord.Interaction):
    deleted = db.delete_user(interaction.user.id)
    _lb_cache.clear()
    msg = (
        "Готово, твои данные удалены, в лидерборде тебя больше нет."
        if deleted else "У бота нет сохранённых данных о тебе."
    )
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
        _lb_cache.clear()
        text = faceit.text_by_steam(steam["id"], steam.get("name", ""))
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
