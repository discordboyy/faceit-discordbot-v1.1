"""5v5 scrim signup, team balancing and persistent Discord controls."""

import asyncio
import json
import logging
import random
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import db
import faceit

log = logging.getLogger("faceit-bot.scrim")
ACTIVE = ("open", "locked")
MAX_PLAYERS = 10


def init_db():
    with db.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scrims (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                organizer_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                mode TEXT NOT NULL,
                players_json TEXT NOT NULL DEFAULT '[]',
                teams_json TEXT NOT NULL DEFAULT '[[],[]]',
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(scrims)")}
        if "teams_json" not in columns:
            conn.execute("ALTER TABLE scrims ADD COLUMN teams_json TEXT NOT NULL DEFAULT '[[],[]]'")


def _load(scrim_id):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM scrims WHERE id = ?", (scrim_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["players"] = json.loads(item.pop("players_json"))
    item["teams"] = json.loads(item.pop("teams_json"))
    return item


def _save(item):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE scrims SET status=?, players_json=?, teams_json=?, message_id=? WHERE id=?",
            (item["status"], json.dumps(item["players"]), json.dumps(item.get("teams", [[], []])),
             item.get("message_id"), item["id"]),
        )


def _active():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM scrims WHERE status IN ('open','locked')"
        ).fetchall()
    return [r["id"] for r in rows]


def _insert(item):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO scrims
            (id,guild_id,channel_id,organizer_id,status,mode,players_json,teams_json)
            VALUES (?,?,?,?,?,?,?,?)""",
            (item["id"], item["guild_id"], item["channel_id"], item["organizer_id"],
             item["status"], item["mode"], json.dumps(item["players"]),
             json.dumps(item.get("teams", [[], []]))),
        )


MODE_LABELS = {
    "random": "Случайно",
    "elo": "FACEIT ELO",
    "level": "Уровень FACEIT",
    "kd": "K/D",
}


def _rating_value(player, mode):
    if mode in ("elo", "level", "kd"):
        return player.get(mode)
    return None


def _balanced(players, mode):
    """Делит игроков на две команды с равным числом людей и близкой суммой рейтинга."""
    pool = list(players)
    half = (len(pool) + 1) // 2
    if mode == "random":
        random.shuffle(pool)
        return [pool[:half], pool[half:]]

    # Игрокам без данных ставим средний рейтинг остальных, чтобы они не рушили баланс.
    known = [v for v in (_rating_value(p, mode) for p in pool) if v is not None]
    fallback = sum(known) / len(known) if known else 0.0

    def value(p):
        v = _rating_value(p, mode)
        return float(v) if v is not None else fallback

    pool.sort(key=value, reverse=True)
    teams = [[], []]
    totals = [0.0, 0.0]
    for player in pool:
        open_teams = [i for i in (0, 1) if len(teams[i]) < half]
        idx = min(open_teams, key=lambda i: totals[i])
        teams[idx].append(player)
        totals[idx] += value(player)
    return teams


def _stat_text(p, mode):
    """Статистика рядом с ником в зависимости от выбранного метода."""
    def has(key):
        return p.get(key) is not None

    if mode == "level":
        return f"Lv {int(p['level'])}" if has("level") else "нет данных"
    if mode == "elo":
        return f"{int(p['elo'])} ELO" if has("elo") else "нет данных"
    if mode == "kd":
        return f"{p['kd']:.2f} K/D" if has("kd") else "нет данных"
    # random: показываем всё, что известно
    parts = []
    if has("level"):
        parts.append(f"Lv {int(p['level'])}")
    if has("elo"):
        parts.append(f"{int(p['elo'])} ELO")
    if has("kd"):
        parts.append(f"{p['kd']:.2f} K/D")
    return " · ".join(parts) or "нет данных"


def _player_line(p, mode, number=None):
    prefix = f"{number}. " if number is not None else ""
    faceit_nick = f" (`{p['nickname']}`)" if p.get("nickname") else ""
    return f"{prefix}<@{p['id']}>{faceit_nick} · {_stat_text(p, mode)}"


def _team_average(team, mode):
    values = [_rating_value(p, mode) for p in team]
    values = [v for v in values if v is not None]
    if not values:
        return ""
    avg = sum(values) / len(values)
    if mode == "kd":
        return f" · ср. {avg:.2f} K/D"
    if mode == "elo":
        return f" · ср. {avg:.0f} ELO"
    if mode == "level":
        return f" · ср. Lv {avg:.1f}"
    return ""


def _embed(item):
    players = item["players"]
    mode_key = item["mode"]
    teams = item.get("teams") or [[], []]
    assigned = bool(any(teams))
    team_ids = {p["id"] for team in teams for p in team}
    waiting = [p for p in players if p["id"] not in team_ids] if assigned else players[MAX_PLAYERS:]
    mode = MODE_LABELS.get(mode_key, mode_key)
    status = {"open": "Регистрация открыта", "locked": "Составы зафиксированы",
              "cancelled": "Отменено"}.get(item["status"], item["status"])
    desc = [f"**Статус:** {status} · **Метод:** {mode}",
            f"**Записались:** {min(len(players), MAX_PLAYERS)}/{MAX_PLAYERS}"]
    if assigned:
        for i, team in enumerate(teams):
            body = "\n".join(_player_line(p, mode_key) for p in team) or "—"
            desc.append(f"\n**Команда {('A', 'B')[i]}**{_team_average(team, mode_key)}\n{body}")
    elif players:
        main = players[:MAX_PLAYERS]
        desc.append("\n**Готовы к игре**\n" +
                    "\n".join(_player_line(p, mode_key, n) for n, p in enumerate(main, 1)))
    if waiting:
        desc.append("\n**Список ожидания**\n" + "\n".join(_player_line(p, mode_key) for p in waiting))
    if not players:
        desc.append("\nПока игроков нет. Нажми «Присоединиться», чтобы записаться.")
    embed = discord.Embed(title="Скрим 5v5", description="\n".join(desc), color=discord.Color.orange())
    embed.set_footer(text=f"ID скрима: {item['id']} · Организатор: {item['organizer_id']}")
    return embed


async def _refresh(item, interaction=None):
    channel = None
    if interaction:
        channel = interaction.channel
    if channel is None:
        channel = bot_ref.get_channel(item["channel_id"]) if bot_ref else None
    if channel is None or not item.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(item["message_id"])
        await msg.edit(embed=_embed(item), view=ScrimView(item["id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Could not refresh scrim message %s", item["id"], exc_info=True)


bot_ref = None


def _is_staff(interaction, item):
    if interaction.user.id == item["organizer_id"]:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.manage_messages))


async def _profile(user_id):
    linked = await asyncio.to_thread(db.get_user, user_id)
    if not linked:
        return None
    profile = {"id": user_id, "name": linked.get("steam_name") or str(user_id),
               "nickname": None, "elo": None, "level": None, "kd": None}
    try:
        player = await asyncio.to_thread(faceit.player_by_steam, linked["steam_id64"])
    except Exception:
        log.warning("FACEIT lookup failed for scrim signup user %s", user_id, exc_info=True)
        return profile
    if not player:
        return profile
    cs2 = player.get("games", {}).get("cs2", {})
    profile["nickname"] = player.get("nickname")
    if cs2:
        profile["elo"] = faceit._num(cs2.get("faceit_elo"))
        profile["level"] = faceit._num(cs2.get("skill_level"))
    try:
        recent = await asyncio.to_thread(faceit.recent_stats, player["player_id"])
        profile["kd"] = recent.get("kd")
    except Exception:
        log.warning("FACEIT K/D lookup failed for scrim signup user %s", user_id, exc_info=True)
    return profile


class ScrimView(discord.ui.View):
    def __init__(self, scrim_id):
        super().__init__(timeout=None)
        self.scrim_id = scrim_id
        self.join.custom_id = f"scrim:{scrim_id}:join"
        self.leave.custom_id = f"scrim:{scrim_id}:leave"

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        if any(p["id"] == interaction.user.id for p in item["players"]):
            await interaction.response.send_message("Ты уже записан.", ephemeral=True)
            return
        # Запросы к FACEIT могут занять >3 секунд, поэтому сначала подтверждаем нажатие.
        await interaction.response.defer()
        profile = await _profile(interaction.user.id)
        if profile is None:
            await interaction.followup.send("Сначала привяжи Steam с помощью /link.", ephemeral=True)
            return
        item = _load(self.scrim_id)  # состояние могло измениться за время запроса
        if not item or item["status"] != "open":
            await interaction.followup.send("Регистрация закрыта.", ephemeral=True)
            return
        if any(p["id"] == interaction.user.id for p in item["players"]):
            return
        item["players"].append(profile)
        _save(item)
        await interaction.edit_original_response(embed=_embed(item), view=self)

    @discord.ui.button(label="Отменить запись", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        before = len(item["players"])
        item["players"] = [p for p in item["players"] if p["id"] != interaction.user.id]
        if len(item["players"]) == before:
            await interaction.response.send_message("Тебя нет в списке участников.", ephemeral=True)
            return
        _save(item)
        await interaction.response.edit_message(embed=_embed(item), view=self)


class ConfirmMoveView(discord.ui.View):
    def __init__(self, scrim_id, actor_id, player_id, team_index):
        super().__init__(timeout=60)
        self.scrim_id = scrim_id
        self.actor_id = actor_id
        self.player_id = player_id
        self.team_index = team_index

    async def _apply(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Подтвердить перенос может только тот, кто его начал.", ephemeral=True)
            return
        item = _load(self.scrim_id)
        if not item or item["status"] != "locked":
            await interaction.response.edit_message(content="Скрим больше не заблокирован или уже не активен.", view=None)
            return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("У тебя нет доступа.", ephemeral=True)
            return
        player = next((p for p in item["players"] if p["id"] == self.player_id), None)
        if not player:
            await interaction.response.edit_message(content="Игрок не найден среди записавшихся.", view=None)
            return
        teams = item.get("teams", [[], []])
        for team in teams:
            team[:] = [p for p in team if p["id"] != self.player_id]
        teams[self.team_index].append(player)
        item["teams"] = teams
        _save(item)
        await interaction.response.edit_message(content="Игрок перенесён.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Подтвердить перенос", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self._apply(interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction, button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Отменить перенос может только тот, кто его начал.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Перенос отменён.", view=None)


class Scrim(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="scrim", description="Создать скрим 5v5 с записью участников")
    @app_commands.guild_only()
    @app_commands.describe(balancing="Способ распределения игроков по командам")
    @app_commands.choices(balancing=[
        app_commands.Choice(name="Случайно", value="random"),
        app_commands.Choice(name="FACEIT ELO", value="elo"),
        app_commands.Choice(name="Уровень FACEIT", value="level"),
        app_commands.Choice(name="K/D", value="kd"),
    ])
    async def create(self, interaction: discord.Interaction, balancing: app_commands.Choice[str]):
        init_db()
        item = {
            "id": uuid.uuid4().hex[:8],
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "organizer_id": interaction.user.id,
            "status": "open",
            "mode": balancing.value,
            "players": [],
            "teams": [[], []],
        }
        _insert(item)
        await interaction.response.send_message(embed=_embed(item), view=ScrimView(item["id"]))
        msg = await interaction.original_response()
        item["message_id"] = msg.id
        _save(item)

    @app_commands.command(name="scrim_rebalance", description="Перераспределить записавшихся игроков по командам")
    @app_commands.guild_only()
    async def rebalance(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Скрим не найден.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Это может сделать только организатор или модератор.", ephemeral=True); return
        if len(item["players"]) < 10:
            await interaction.response.send_message("Для распределения команд нужно 10 участников.", ephemeral=True); return
        item["teams"] = _balanced(item["players"][:10], item["mode"])
        _save(item)
        await interaction.response.send_message("Команды распределены заново.", ephemeral=True)
        await _refresh(item, interaction)

    @app_commands.command(name="scrim_lock", description="Зафиксировать составы команд и закрыть запись")
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Скрим не найден.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Это может сделать только организатор или модератор.", ephemeral=True); return
        if len(item["players"]) < 10:
            await interaction.response.send_message("Для блокировки скрима нужно 10 участников.", ephemeral=True); return
        item["teams"] = _balanced(item["players"][:10], item["mode"])
        item["status"] = "locked"
        _save(item)
        await interaction.response.send_message("Скрим заблокирован.", ephemeral=True)
        await _refresh(item, interaction)

    @app_commands.command(name="scrim_move", description="Перенести игрока в другую команду")
    @app_commands.guild_only()
    @app_commands.choices(team=[
        app_commands.Choice(name="Команда A", value=0),
        app_commands.Choice(name="Команда B", value=1),
    ])
    async def move(self, interaction: discord.Interaction, scrim_id: str,
                   member: discord.Member, team: app_commands.Choice[int]):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Скрим не найден.", ephemeral=True)
            return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Это может сделать только организатор или модератор.", ephemeral=True)
            return
        if item["status"] != "locked":
            await interaction.response.send_message("Сначала зафиксируй составы, чтобы менять команды вручную.", ephemeral=True)
            return
        if not any(p["id"] == member.id for p in item["players"]):
            await interaction.response.send_message("Игрок не записан на скрим.", ephemeral=True)
            return
        current = next((i for i, group in enumerate(item.get("teams", [[], []]))
                        if any(p["id"] == member.id for p in group)), None)
        if current is None:
            await interaction.response.send_message("Команды ещё не распределены.", ephemeral=True)
            return
        if current == team.value:
            await interaction.response.send_message("Игрок уже в этой команде.", ephemeral=True)
            return
        view = ConfirmMoveView(scrim_id, interaction.user.id, member.id, team.value)
        await interaction.response.send_message(
            f"Подтверди перенос {member.mention} в {'команду A' if team.value == 0 else 'команду B'}. "
            "Это изменит состав команд вручную.",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="scrim_cancel", description="Отменить скрим")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Скрим не найден.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Это может сделать только организатор или модератор.", ephemeral=True); return
        item["status"] = "cancelled"
        _save(item)
        await interaction.response.send_message("Скрим отменён.", ephemeral=True)
        await _refresh(item, interaction)


async def setup(bot):
    global bot_ref
    bot_ref = bot
    init_db()
    await bot.add_cog(Scrim(bot))
    for scrim_id in _active():
        bot.add_view(ScrimView(scrim_id))
