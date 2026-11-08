"""5v5 scrim signup, team balancing and persistent Discord controls."""

import asyncio
import itertools
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
        if "winner" not in columns:
            conn.execute("ALTER TABLE scrims ADD COLUMN winner INTEGER")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS level_overrides (
                guild_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                set_by INTEGER NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (guild_id, discord_id)
            )
        """)


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
            "UPDATE scrims SET status=?, players_json=?, teams_json=?, message_id=?, winner=? WHERE id=?",
            (item["status"], json.dumps(item["players"]), json.dumps(item.get("teams", [[], []])),
             item.get("message_id"), item.get("winner"), item["id"]),
        )


def _get_override(guild_id, discord_id):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT level FROM level_overrides WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id)).fetchone()
    return row["level"] if row else None


def _set_override(guild_id, discord_id, level, set_by):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO level_overrides (guild_id, discord_id, level, set_by, updated_at)
               VALUES (?, ?, ?, ?, strftime('%s','now'))
               ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                 level = excluded.level, set_by = excluded.set_by, updated_at = excluded.updated_at""",
            (guild_id, discord_id, level, set_by))


def _delete_override(guild_id, discord_id):
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM level_overrides WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
        return cur.rowcount > 0


def _list_overrides(guild_id):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT discord_id, level FROM level_overrides WHERE guild_id = ? ORDER BY level DESC, discord_id",
            (guild_id,)).fetchall()
    return [(r["discord_id"], r["level"]) for r in rows]


def _apply_level(guild_id, discord_id, level, manual):
    """Обновляет уровень игрока во всех активных скримах сервера. Возвращает изменённые скримы."""
    changed = []
    for scrim_id in _active():
        item = _load(scrim_id)
        if not item or item["guild_id"] != guild_id:
            continue
        touched = False
        for p in itertools.chain(item["players"], *item.get("teams", [[], []])):
            if p["id"] == discord_id:
                p["level"] = level
                p["level_manual"] = manual
                touched = True
        if not touched:
            continue
        if item["status"] == "open" and item["mode"] == "level" and len(item["players"]) >= MAX_PLAYERS:
            _auto_teams(item)  # уровень изменился, предварительные составы пересобираем
        _save(item)
        changed.append(item)
    return changed


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


STATUS_COLORS = {
    "open": discord.Color.orange(),
    "locked": discord.Color.green(),
    "finished": discord.Color.blurple(),
    "cancelled": discord.Color.red(),
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

    random.shuffle(pool)  # равные по рейтингу игроки при перемешивании меняются местами
    pool.sort(key=value, reverse=True)
    teams = [[], []]
    totals = [0.0, 0.0]
    for player in pool:
        open_teams = [i for i in (0, 1) if len(teams[i]) < half]
        idx = min(open_teams, key=lambda i: totals[i])
        teams[idx].append(player)
        totals[idx] += value(player)
    return teams


def _auto_teams(item):
    """Как только набралось 10 игроков — сразу делим на команды; меньше 10 — команд нет."""
    if len(item["players"]) >= MAX_PLAYERS:
        item["teams"] = _balanced(item["players"][:MAX_PLAYERS], item["mode"])
    else:
        item["teams"] = [[], []]


def _partition(teams):
    return frozenset(frozenset(p["id"] for p in team) for team in teams)


def _swap_players(item, a_id, b_id):
    """Меняет игрока из команды A (a_id) и игрока из команды B (b_id) местами."""
    teams = item.get("teams") or [[], []]
    ia = next((i for i, p in enumerate(teams[0]) if p["id"] == a_id), None)
    ib = next((i for i, p in enumerate(teams[1]) if p["id"] == b_id), None)
    if ia is None or ib is None:
        return False
    teams[0][ia], teams[1][ib] = teams[1][ib], teams[0][ia]
    item["teams"] = teams
    return True


def _remove_player(item, player_id):
    """Убирает игрока; скрим снова открывается для записи, составы пересобираются."""
    before = len(item["players"])
    item["players"] = [p for p in item["players"] if p["id"] != player_id]
    if len(item["players"]) == before:
        return False
    item["status"] = "open"
    _auto_teams(item)
    return True


def _public_buttons(item):
    """Кнопки в общем сообщении: видят все участники."""
    if item["status"] == "open":
        if len(item["players"]) < MAX_PLAYERS:
            return {"join", "leave", "manage"}
        return {"manage"}
    if item["status"] == "locked":
        return {"manage"}
    return set()


def _staff_buttons(item):
    """Кнопки личного окна управления: видят только организатор и модераторы."""
    players = len(item["players"])
    if item["status"] == "open":
        if players < MAX_PLAYERS:
            return {"kick", "cancel"} if players else {"cancel"}
        return {"lock", "reshuffle", "swap", "kick", "ping", "cancel"}
    if item["status"] == "locked":
        return {"finish", "swap", "kick", "ping", "cancel"}
    return set()


def _player_label(p, mode):
    name = p.get("nickname") or p.get("name") or str(p["id"])
    return f"{name} · {_stat_text(p, mode)}"[:100]


def _lv(p):
    return f"Lv {int(p['level'])}" + (" ✎" if p.get("level_manual") else "")


def _stat_text(p, mode):
    """Статистика рядом с ником в зависимости от выбранного метода."""
    def has(key):
        return p.get(key) is not None

    if mode == "level":
        return _lv(p) if has("level") else "нет данных"
    if mode == "elo":
        return f"{int(p['elo'])} ELO" if has("elo") else "нет данных"
    if mode == "kd":
        return f"{p['kd']:.2f} K/D" if has("kd") else "нет данных"
    # random: показываем всё, что известно
    parts = []
    if has("level"):
        parts.append(_lv(p))
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
              "cancelled": "Отменено", "finished": "Скрим сыгран"}.get(item["status"], item["status"])
    desc = [f"**Статус:** {status} · **Метод:** {mode}",
            f"**Записались:** {min(len(players), MAX_PLAYERS)}/{MAX_PLAYERS}"]
    if item["status"] == "finished":
        winner = item.get("winner")
        desc.append("**Результат:** " + (f"победила команда {('A', 'B')[winner]}" if winner in (0, 1)
                                          else "не указан"))
    if assigned:
        for i, team in enumerate(teams):
            body = "\n".join(_player_line(p, mode_key) for p in team) or "—"
            desc.append(f"\n**Команда {('A', 'B')[i]}**{_team_average(team, mode_key)}\n{body}")
    elif players:
        main = players[:MAX_PLAYERS]
        desc.append("\n**Готовы к игре**\n" +
                    "\n".join(_player_line(p, mode_key, n) for n, p in enumerate(main, 1)))
    if assigned and item["status"] == "open":
        desc.append("\n*Составы предварительные. Организатор может поменять игроков, перемешать "
                    "команды или нажать «Зафиксировать составы».*")
    if waiting:
        desc.append("\n**Список ожидания**\n" + "\n".join(_player_line(p, mode_key) for p in waiting))
    if any(p.get("level_manual") for p in players):
        desc.append("\n✎ — уровень выставлен администратором вручную")
    if not players:
        desc.append("\nПока игроков нет. Нажми «Присоединиться», чтобы записаться.")
    embed = discord.Embed(title="Скрим 5v5", description="\n".join(desc), color=STATUS_COLORS.get(item["status"], discord.Color.orange()))
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
        await msg.edit(embed=_embed(item), view=_view_for(item))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Could not refresh scrim message %s", item["id"], exc_info=True)


bot_ref = None


def _is_staff(interaction, item):
    if interaction.user.id == item["organizer_id"]:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.manage_messages))


async def _faceit_profile(user_id):
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


async def _deny(interaction, text):
    await interaction.response.send_message(text, ephemeral=True)


async def _staff_gate(interaction, item):
    if not item or item["status"] not in ACTIVE:
        await _deny(interaction, "Скрим уже не активен.")
        return False
    if not _is_staff(interaction, item):
        await _deny(interaction, "Это может сделать только организатор или модератор.")
        return False
    return True


PUBLIC_BUTTONS = ("join", "leave", "manage")
STAFF_BUTTONS = ("lock", "reshuffle", "finish", "swap", "kick", "ping", "cancel")


async def _profile(user_id, guild_id=None):
    """Профиль игрока для скрима: данные FACEIT + закреплённый администратором уровень (если есть)."""
    profile = await _faceit_profile(user_id)
    if profile is None:
        return None
    if guild_id is not None:
        level = _get_override(guild_id, user_id)
        if level is not None:
            profile["level"] = level
            profile["level_manual"] = True
    return profile


class ScrimView(discord.ui.View):
    """Кнопки под сообщением скрима: запись для всех и одна кнопка входа в меню организатора."""

    def __init__(self, item):
        super().__init__(timeout=None)
        self.scrim_id = item["id"]
        visible = _public_buttons(item)
        for name in PUBLIC_BUTTONS:
            button = getattr(self, name)
            button.custom_id = f"scrim:{self.scrim_id}:{name}"
            if name not in visible:
                self.remove_item(button)

    # ---------- участники ----------
    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.success, row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await _deny(interaction, "Регистрация закрыта.")
            return
        if any(p["id"] == interaction.user.id for p in item["players"]):
            await _deny(interaction, "Ты уже записан.")
            return
        if len(item["players"]) >= MAX_PLAYERS:
            await _deny(interaction, "Составы уже набраны (10/10).")
            return
        # Запросы к FACEIT могут занять >3 секунд, поэтому сначала подтверждаем нажатие.
        await interaction.response.defer()
        profile = await _profile(interaction.user.id, interaction.guild_id)
        if profile is None:
            await interaction.followup.send("Сначала привяжи Steam с помощью /link.", ephemeral=True)
            return
        item = _load(self.scrim_id)  # состояние могло измениться за время запроса
        if not item or item["status"] != "open":
            await interaction.followup.send("Регистрация закрыта.", ephemeral=True)
            return
        if any(p["id"] == interaction.user.id for p in item["players"]):
            return
        if len(item["players"]) >= MAX_PLAYERS:
            await interaction.followup.send("Пока ты записывался, места закончились.", ephemeral=True)
            return
        item["players"].append(profile)
        _auto_teams(item)
        _save(item)
        await interaction.edit_original_response(embed=_embed(item), view=_view_for(item))

    @discord.ui.button(label="Отменить запись", style=discord.ButtonStyle.secondary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await _deny(interaction, "Регистрация закрыта.")
            return
        if not _remove_player(item, interaction.user.id):
            await _deny(interaction, "Тебя нет в списке участников.")
            return
        _save(item)
        await interaction.response.edit_message(embed=_embed(item), view=_view_for(item))

    @discord.ui.button(label="⚙ Управление", style=discord.ButtonStyle.secondary, row=0)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] not in ACTIVE:
            await _deny(interaction, "Скрим уже не активен.")
            return
        if not _is_staff(interaction, item):
            await _deny(interaction, "Меню управления доступно только организатору и модераторам.")
            return
        await interaction.response.send_message(
            _panel_text(item), view=_manage_view(item, interaction.user.id), ephemeral=True)


def _view_for(item):
    view = ScrimView(item)
    return view if view.children else None


def _panel_text(item, note=None):
    teams = item.get("teams") or [[], []]
    lines = [f"**Управление скримом** `{item['id']}`",
             f"Записались: {min(len(item['players']), MAX_PLAYERS)}/{MAX_PLAYERS}"
             + (" · составы собраны" if any(teams) else "")]
    if note:
        lines.append(note)
    return "\n".join(lines)


def _manage_view(item, actor_id):
    view = ManageView(item, actor_id)
    return view if view.children else None


class _OwnedView(discord.ui.View):
    """Ephemeral-окно, которым управляет только тот, кто его открыл."""

    def __init__(self, scrim_id, actor_id, timeout=120):
        super().__init__(timeout=timeout)
        self.scrim_id = scrim_id
        self.actor_id = actor_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Это окно открыл другой пользователь.", ephemeral=True)
            return False
        return True

    async def _load_active(self, interaction):
        item = _load(self.scrim_id)
        if not await _staff_gate(interaction, item):
            return None
        return item


class ManageView(_OwnedView):
    """Личное (ephemeral) окно организатора: кнопки управления видны только ему."""

    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=600)
        visible = _staff_buttons(item)
        for name in STAFF_BUTTONS:
            button = getattr(self, name)
            if name not in visible:
                self.remove_item(button)

    async def _done(self, interaction, item, note):
        """Обновляет и это окно, и общее сообщение скрима."""
        view = _manage_view(item, self.actor_id)
        text = _panel_text(item, note) if view else (note or "Готово.")
        await interaction.response.edit_message(content=text, view=view)
        await _refresh(item, interaction)

    @discord.ui.button(label="Зафиксировать составы", style=discord.ButtonStyle.primary, row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        if item["status"] != "open" or len(item["players"]) < MAX_PLAYERS:
            await _deny(interaction, "Для фиксации нужно 10 записавшихся.")
            return
        if not any(item.get("teams") or [[], []]):
            _auto_teams(item)
        item["status"] = "locked"
        _save(item)
        await self._done(interaction, item, "Составы зафиксированы.")

    @discord.ui.button(label="Перемешать", style=discord.ButtonStyle.secondary, row=0)
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        if item["status"] != "open" or len(item["players"]) < MAX_PLAYERS:
            await _deny(interaction, "Перемешать можно только до фиксации составов.")
            return
        before = _partition(item["teams"])
        item["teams"] = _balanced(item["players"][:MAX_PLAYERS], item["mode"])
        _save(item)
        note = ("Составы пересобраны." if _partition(item["teams"]) != before
                else "Составы получились такими же: другого равного по рейтингу разбиения нет.")
        await self._done(interaction, item, note)

    @discord.ui.button(label="Скрим сыгран", style=discord.ButtonStyle.success, row=0)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        if item["status"] != "locked":
            await _deny(interaction, "Сначала зафиксируй составы.")
            return
        await interaction.response.send_message(
            "Кто победил?", view=FinishView(self.scrim_id, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Поменять игрока", style=discord.ButtonStyle.primary, row=1)
    async def swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        teams = item.get("teams") or [[], []]
        if not (teams[0] and teams[1]):
            await _deny(interaction, "Команды ещё не распределены.")
            return
        await interaction.response.send_message(
            "Выбери игрока из команды A, затем игрока из команды B и нажми «Подтвердить выбор».",
            view=SwapView(item, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Убрать игрока", style=discord.ButtonStyle.secondary, row=1)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        if not item["players"]:
            await _deny(interaction, "Записавшихся нет.")
            return
        await interaction.response.send_message(
            "Выбери игрока, которого нужно убрать из скрима. Запись снова откроется.",
            view=KickView(item, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Позвать игроков", style=discord.ButtonStyle.secondary, row=1)
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        mentions = " ".join(f"<@{p['id']}>" for p in item["players"][:MAX_PLAYERS])
        await interaction.response.send_message(
            f"{mentions}\nСкрим 5v5 собран, заходите в игру!",
            allowed_mentions=discord.AllowedMentions(users=True))

    @discord.ui.button(label="Отменить скрим", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._load_active(interaction)
        if not item:
            return
        await interaction.response.send_message(
            "Точно отменить скрим? Кнопки исчезнут, записаться будет нельзя.",
            view=ConfirmCancelView(self.scrim_id, interaction.user.id), ephemeral=True)




class ConfirmCancelView(_OwnedView):
    @discord.ui.button(label="Да, отменить скрим", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        item = await self._load_active(interaction)
        if not item:
            return
        item["status"] = "cancelled"
        _save(item)
        await interaction.response.edit_message(content="Скрим отменён.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(content="Скрим остался активным.", view=None)


class FinishView(_OwnedView):
    async def _finish(self, interaction, winner):
        item = await self._load_active(interaction)
        if not item:
            return
        if item["status"] != "locked":
            await interaction.response.edit_message(content="Составы не зафиксированы.", view=None)
            return
        item["status"] = "finished"
        item["winner"] = winner
        _save(item)
        await interaction.response.edit_message(content="Скрим отмечен как сыгранный.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Победила команда A", style=discord.ButtonStyle.success)
    async def win_a(self, interaction, button):
        await self._finish(interaction, 0)

    @discord.ui.button(label="Победила команда B", style=discord.ButtonStyle.success)
    async def win_b(self, interaction, button):
        await self._finish(interaction, 1)

    @discord.ui.button(label="Без результата", style=discord.ButtonStyle.secondary)
    async def no_result(self, interaction, button):
        await self._finish(interaction, None)


class _PickSelect(discord.ui.Select):
    def __init__(self, key, players, mode, placeholder, row):
        options = [discord.SelectOption(label=_player_label(p, mode), value=str(p["id"]))
                   for p in players[:25]]
        super().__init__(placeholder=placeholder, options=options, row=row)
        self.key = key

    async def callback(self, interaction):
        view = self.view
        view.picked[self.key] = int(self.values[0])
        for option in self.options:
            option.default = option.value == self.values[0]
        view.refresh_confirm()
        await interaction.response.edit_message(view=view)


class SwapView(_OwnedView):
    """Выбор игрока из команды A, игрока из команды B и подтверждение обмена."""

    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=180)
        self.picked = {}
        mode = item["mode"]
        self.add_item(_PickSelect("a", item["teams"][0], mode, "Игрок из команды A", 0))
        self.add_item(_PickSelect("b", item["teams"][1], mode, "Игрок из команды B", 1))

    def refresh_confirm(self):
        self.confirm.disabled = not ("a" in self.picked and "b" in self.picked)

    @discord.ui.button(label="Подтвердить выбор", style=discord.ButtonStyle.success, row=2, disabled=True)
    async def confirm(self, interaction, button):
        item = await self._load_active(interaction)
        if not item:
            return
        if not _swap_players(item, self.picked["a"], self.picked["b"]):
            await interaction.response.edit_message(
                content="Состав уже изменился. Открой «Поменять игрока» заново.", view=None)
            return
        _save(item)
        await interaction.response.edit_message(content="Игроки поменялись командами.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary, row=2)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Замена отменена.", view=None)


class KickView(_OwnedView):
    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=120)
        self.picked = {}
        self.add_item(_PickSelect("p", item["players"], item["mode"], "Кого убрать", 0))

    def refresh_confirm(self):
        self.confirm.disabled = "p" not in self.picked

    @discord.ui.button(label="Убрать игрока", style=discord.ButtonStyle.danger, row=1, disabled=True)
    async def confirm(self, interaction, button):
        item = await self._load_active(interaction)
        if not item:
            return
        if not _remove_player(item, self.picked["p"]):
            await interaction.response.edit_message(content="Игрока уже нет в списке.", view=None)
            return
        _save(item)
        await interaction.response.edit_message(content="Игрок убран, запись снова открыта.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary, row=1)
    async def abort(self, interaction, button):
        await interaction.response.edit_message(content="Отменено.", view=None)


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
        if not item or item["status"] not in ACTIVE:
            await interaction.response.edit_message(content="Скрим уже не активен.", view=None)
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
        await interaction.response.send_message(embed=_embed(item), view=_view_for(item))
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
        if not any(item.get("teams") or [[], []]):
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
        if item["status"] not in ACTIVE:
            await interaction.response.send_message("Скрим уже не активен.", ephemeral=True)
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

    @staticmethod
    async def _require_admin(interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and perms.manage_guild:
            return True
        await interaction.response.send_message(
            "Эта команда доступна только администраторам сервера (право Manage Server).", ephemeral=True)
        return False

    @app_commands.command(name="scrim_setlevel",
                          description="Закрепить уровень игрока для скримов навсегда (только админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Игрок", level="Уровень от 1 до 10")
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member,
                       level: app_commands.Range[int, 1, 10]):
        if not await self._require_admin(interaction):
            return
        init_db()
        _set_override(interaction.guild_id, member.id, level, interaction.user.id)
        changed = _apply_level(interaction.guild_id, member.id, level, True)
        note = f"Уровень {member.mention} закреплён: Lv {level}. Он будет использоваться во всех скримах."
        if changed:
            note += f" Обновлено активных скримов: {len(changed)}."
        await interaction.response.send_message(
            note, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for item in changed:
            await _refresh(item)

    @app_commands.command(name="scrim_resetlevel",
                          description="Снять закреплённый уровень игрока и вернуть уровень FACEIT (только админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Игрок")
    async def resetlevel(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._require_admin(interaction):
            return
        init_db()
        if _get_override(interaction.guild_id, member.id) is None:
            await interaction.response.send_message(
                "У этого игрока нет закреплённого уровня.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _delete_override(interaction.guild_id, member.id)
        profile = await _faceit_profile(member.id)
        level = profile["level"] if profile else None
        changed = _apply_level(interaction.guild_id, member.id, level, False)
        note = f"Закреплённый уровень {member.mention} снят."
        note += (f" Теперь используется уровень FACEIT: Lv {int(level)}." if level is not None
                 else " Уровень FACEIT получить не удалось.")
        await interaction.followup.send(note, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for item in changed:
            await _refresh(item)

    @app_commands.command(name="scrim_levels", description="Список игроков с закреплённым уровнем")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def levels(self, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return
        init_db()
        rows = _list_overrides(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Закреплённых уровней пока нет.", ephemeral=True)
            return
        lines = [f"<@{uid}> — Lv {level}" for uid, level in rows[:50]]
        extra = f"\n…и ещё {len(rows) - 50}" if len(rows) > 50 else ""
        await interaction.response.send_message(
            "**Закреплённые уровни**\n" + "\n".join(lines) + extra,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    global bot_ref
    bot_ref = bot
    init_db()
    await bot.add_cog(Scrim(bot))
    for scrim_id in _active():
        item = _load(scrim_id)
        view = _view_for(item) if item else None
        if view:
            bot.add_view(view, message_id=item.get("message_id"))
