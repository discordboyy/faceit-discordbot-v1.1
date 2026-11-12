"""Турниры 5 на 5: запись, баланс команд по рейтингу, сетка плей-офф и результаты кнопками.

Работает так же, как скримы: запись кнопкой, автоматический баланс, панель организатора.
Формат: плей-офф на выбывание. Команды получают номера (сиды) по силе: сильнейшая играет
со слабейшей, лучшие при нечётном числе команд проходят без игры. Матчи одного раунда
идут параллельно в разных лобби. Проигравшие полуфиналов играют за 3 место.
"""

import json
import logging
import random
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import db
from scrim import (MODE_LABELS, STATUS_COLORS, _deny, _is_staff, _player_line, _profile,
                   _team_average)

log = logging.getLogger("faceit-bot.tournament")

TEAM_SIZE = 5
MIN_TEAMS = 2
MAX_TEAMS = 8  # 8 × 5 = 40 игроков: больше не влезает в один embed
ACTIVE = ("open", "locked")

bot_ref = None


# ───────────────────────────── база данных ─────────────────────────────

def init_db():
    with db.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                organizer_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                mode TEXT NOT NULL,
                name TEXT,
                team_count INTEGER NOT NULL,
                players_json TEXT NOT NULL DEFAULT '[]',
                teams_json TEXT NOT NULL DEFAULT '[]',
                bracket_json TEXT,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)


def _load(tid):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["players"] = json.loads(item.pop("players_json"))
    item["teams"] = json.loads(item.pop("teams_json"))
    raw = item.pop("bracket_json")
    item["bracket"] = json.loads(raw) if raw else None
    return item


def _save(item):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE tournaments SET status=?, players_json=?, teams_json=?, bracket_json=?, "
            "message_id=? WHERE id=?",
            (item["status"], json.dumps(item["players"]), json.dumps(item.get("teams", [])),
             json.dumps(item["bracket"]) if item.get("bracket") else None,
             item.get("message_id"), item["id"]))


def _insert(item):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO tournaments
               (id,guild_id,channel_id,organizer_id,status,mode,name,team_count,players_json,teams_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item["id"], item["guild_id"], item["channel_id"], item["organizer_id"], item["status"],
             item["mode"], item.get("name"), item["team_count"], json.dumps(item["players"]),
             json.dumps(item.get("teams", []))))


def _active():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id FROM tournaments WHERE status IN ('open','locked')").fetchall()
    return [r["id"] for r in rows]


def _needed(item):
    return item["team_count"] * TEAM_SIZE


# ───────────────────────────── баланс команд ─────────────────────────────

def _seed_key(mode):
    return mode if mode in ("elo", "level", "kd") else "elo"


def _value_fn(players, mode):
    """Рейтинг игрока по выбранному методу. Нет данных, берём средний по остальным."""
    if mode not in ("elo", "level", "kd"):
        return lambda p: 0.0
    known = [p[mode] for p in players if p.get(mode) is not None]
    fallback = sum(known) / len(known) if known else 0.0
    return lambda p: float(p[mode]) if p.get(mode) is not None else fallback


def _local_search(teams, value):
    """Обмены игроков между командами, пока разрыв по сумме рейтинга уменьшается."""
    n = len(teams)
    totals = [sum(value(p) for p in t) for t in teams]
    improved = True
    while improved:
        improved = False
        for i in range(n):
            for j in range(i + 1, n):
                diff = totals[i] - totals[j]
                best = None
                for a in teams[i]:
                    for b in teams[j]:
                        d = value(a) - value(b)
                        new = abs(diff - 2 * d)
                        if new < abs(diff) - 1e-9 and (best is None or new < best[0]):
                            best = (new, a, b, d)
                if best:
                    _, a, b, d = best
                    teams[i][teams[i].index(a)] = b
                    teams[j][teams[j].index(b)] = a
                    totals[i] -= d
                    totals[j] += d
                    improved = True
    return totals


def _balanced_teams(players, mode, team_count):
    """Делит игроков на team_count команд по 5 с максимально близкой суммой рейтинга."""
    pool = list(players)[: team_count * TEAM_SIZE]
    random.shuffle(pool)  # равные по рейтингу игроки при перемешивании меняются местами
    if mode == "random":
        return [pool[i * TEAM_SIZE:(i + 1) * TEAM_SIZE] for i in range(team_count)]

    value = _value_fn(pool, mode)
    ordered = sorted(pool, key=value, reverse=True)
    greedy = [[] for _ in range(team_count)]
    totals = [0.0] * team_count
    for p in ordered:  # жадно: каждого в самую слабую команду, где ещё есть место
        i = min((i for i in range(team_count) if len(greedy[i]) < TEAM_SIZE), key=lambda i: totals[i])
        greedy[i].append(p)
        totals[i] += value(p)

    starts = [greedy]  # плюс случайные разбиения: выбираем лучший из локальных минимумов
    for _ in range(40):
        shuffled = random.sample(pool, len(pool))
        starts.append([shuffled[i * TEAM_SIZE:(i + 1) * TEAM_SIZE] for i in range(team_count)])
    best, best_spread = None, None
    for teams in starts:
        sums = _local_search(teams, value)
        spread = max(sums) - min(sums)
        if best is None or spread < best_spread - 1e-9:
            best, best_spread = teams, spread
        if best_spread < 1e-9:
            break
    return best


def _strength(team, mode):
    key = _seed_key(mode)
    vals = [p[key] for p in team if p.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _sort_teams(teams, mode):
    """Сильнейшая команда первая (сид №1). В режиме «Случайно» порядок случайный."""
    teams = [list(t) for t in teams]
    if mode == "random":
        random.shuffle(teams)
    else:
        teams.sort(key=lambda t: _strength(t, mode), reverse=True)
    return teams


def _make_names(teams, mode):
    """Название команды по её сильнейшему игроку: «Team nick»."""
    key = _seed_key(mode)
    names, used = [], {}
    for team in teams:
        top = max(team, key=lambda p: (p.get(key) is not None, p.get(key) or 0))
        nick = str(top.get("nickname") or top.get("name") or top["id"])[:20]
        used[nick] = used.get(nick, 0) + 1
        names.append(f"Team {nick}" + (f" {used[nick]}" if used[nick] > 1 else ""))
    return names


def _auto_teams(item):
    """Как только набрался полный комплект, сразу делим на команды (предварительно)."""
    if len(item["players"]) >= _needed(item):
        teams = _balanced_teams(item["players"], item["mode"], item["team_count"])
        item["teams"] = _sort_teams(teams, item["mode"])
    else:
        item["teams"] = []


def _names(item):
    if item.get("bracket"):
        return item["bracket"]["names"]
    return _make_names(item["teams"], item["mode"]) if item["teams"] else []


# ───────────────────────────── сетка плей-офф ─────────────────────────────

def _seed_order(size):
    order = [1]
    while len(order) < size:
        n = len(order) * 2 + 1
        order = [x for s in order for x in (s, n - s)]
    return order


def _build_bracket(n):
    """Сетка на n команд. Команды в сетке обозначаются индексом (0 = сид №1)."""
    size = 1
    while size < n:
        size *= 2
    order = _seed_order(size)
    first = []
    for i in range(0, size, 2):
        a, b = order[i] - 1, order[i + 1] - 1
        first.append({"a": a if a < n else None, "b": b if b < n else None, "winner": None})
    rounds = [first]
    m = size // 2
    while m > 1:
        m //= 2
        rounds.append([{"a": None, "b": None, "winner": None} for _ in range(m)])
    bracket = {"rounds": rounds, "third": None}
    for i, match in enumerate(first):  # проход без игры: у лучших сидов нет соперника
        if match["a"] is None or match["b"] is None:
            w = match["a"] if match["a"] is not None else match["b"]
            match["winner"], match["bye"] = w, True
            _advance(bracket, 0, i, w)
    return bracket


def _advance(bracket, r, i, winner):
    rounds = bracket["rounds"]
    if r + 1 < len(rounds):
        rounds[r + 1][i // 2]["a" if i % 2 == 0 else "b"] = winner


def _update_third(bracket):
    rounds = bracket["rounds"]
    if bracket["third"] is not None or len(rounds) < 2:
        return
    semis = rounds[-2]
    if all(m["winner"] is not None and m["a"] is not None and m["b"] is not None for m in semis):
        losers = [m["b"] if m["winner"] == m["a"] else m["a"] for m in semis]
        bracket["third"] = {"a": losers[0], "b": losers[1], "winner": None}


def _round_name(total, r):
    return {1: "Финал", 2: "Полуфинал", 3: "Четвертьфинал"}.get(total - r, f"Раунд {r + 1}")


def _match_list(bracket):
    """[(ключ, название, матч)] без матчей «проход без игры»."""
    rounds, out = bracket["rounds"], []
    for r, matches in enumerate(rounds):
        name = _round_name(len(rounds), r)
        real = [(i, m) for i, m in enumerate(matches) if not m.get("bye")]
        for n, (i, m) in enumerate(real, 1):
            out.append((f"{r}:{i}", f"{name} · Лобби {n}" if len(real) > 1 else name, m))
    if bracket.get("third"):
        out.append(("third", "Матч за 3 место", bracket["third"]))
    return out


def _find(bracket, key):
    if key == "third":
        return bracket["third"]
    r, i = map(int, key.split(":"))
    return bracket["rounds"][r][i]


def _ready(bracket):
    return [(k, label, m) for k, label, m in _match_list(bracket)
            if m["a"] is not None and m["b"] is not None and m["winner"] is None]


def _set_winner(bracket, key, team):
    m = _find(bracket, key)
    m["winner"] = team
    if key != "third":
        r, i = map(int, key.split(":"))
        _advance(bracket, r, i, team)
        _update_third(bracket)


def _can_undo(bracket, key):
    """Результат можно отменить, только если дальше по сетке с ним ещё не сыграли."""
    m = _find(bracket, key)
    if m is None or m["winner"] is None or m.get("bye"):
        return False
    if key == "third":
        return True
    r, i = map(int, key.split(":"))
    rounds = bracket["rounds"]
    if r + 1 < len(rounds) and rounds[r + 1][i // 2]["winner"] is not None:
        return False
    if r == len(rounds) - 2 and bracket.get("third") and bracket["third"]["winner"] is not None:
        return False
    return True


def _undoable(bracket):
    return [(k, label, m) for k, label, m in _match_list(bracket) if _can_undo(bracket, k)]


def _undo(bracket, key):
    """Снимает результат матча и убирает победителя из следующего матча."""
    if not _can_undo(bracket, key):
        return False
    m = _find(bracket, key)
    if key != "third":
        r, i = map(int, key.split(":"))
        rounds = bracket["rounds"]
        if r + 1 < len(rounds):
            rounds[r + 1][i // 2]["a" if i % 2 == 0 else "b"] = None
        if r == len(rounds) - 2:
            bracket["third"] = None
    m["winner"] = None
    return True


def _finished(bracket):
    final = bracket["rounds"][-1][0]
    third = bracket.get("third")
    return final["winner"] is not None and (third is None or third["winner"] is not None)


def _podium(bracket):
    """(золото, серебро, бронза). Бронза None, если играть за неё было некому."""
    final = bracket["rounds"][-1][0]
    gold = final["winner"]
    silver = final["b"] if gold == final["a"] else final["a"]
    bronze = None
    third = bracket.get("third")
    if third and third["winner"] is not None:
        bronze = third["winner"]
    elif len(bracket["rounds"]) >= 2:
        real = [m for m in bracket["rounds"][-2] if m["a"] is not None and m["b"] is not None]
        if len(real) == 1 and real[0]["winner"] is not None:
            m = real[0]
            bronze = m["b"] if m["winner"] == m["a"] else m["a"]
    return gold, silver, bronze


def _start(item):
    """Фиксирует составы, расставляет сиды и строит сетку."""
    teams = _sort_teams(item["teams"], item["mode"])
    item["teams"] = teams
    item["bracket"] = _build_bracket(len(teams))
    item["bracket"]["names"] = _make_names(teams, item["mode"])
    item["status"] = "locked"


# ───────────────────────────── оформление ─────────────────────────────

def _bar(done, total, width=10):
    filled = round(width * min(done, total) / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def _bracket_text(item):
    bracket, names = item["bracket"], item["bracket"]["names"]
    rounds, lines = bracket["rounds"], []
    for r, matches in enumerate(rounds):
        lines.append(f"\n**{_round_name(len(rounds), r)}**")
        real = [m for m in matches if not m.get("bye")]
        n = 0
        for m in matches:
            a = names[m["a"]] if m["a"] is not None else "…"
            b = names[m["b"]] if m["b"] is not None else "…"
            if m.get("bye"):
                lines.append(f"▫️ {names[m['winner']]}: проходит без игры")
                continue
            n += 1
            lobby = f"Лобби {n}: " if len(real) > 1 else ""
            lines.append(_match_line(lobby, a, b, m))
    third = bracket.get("third")
    semis = rounds[-2] if len(rounds) >= 2 else []
    if third or (semis and not any(m.get("bye") for m in semis)):
        lines.append("\n**Матч за 3 место**")
        if third:
            lines.append(_match_line("", names[third["a"]], names[third["b"]], third))
        else:
            lines.append("⏳ Проигравшие полуфиналов")
    return "\n".join(lines)


def _match_line(prefix, a, b, m):
    if m["winner"] is not None:
        a_won = m["winner"] == m["a"]
        win, lose = (a, b) if a_won else (b, a)
        return f"✅ {prefix}**{win}** · ~~{lose}~~"
    if m["a"] is not None and m["b"] is not None:
        return f"🟢 {prefix}**{a}** vs **{b}** · идёт игра"
    return f"⏳ {prefix}{a} vs {b}"


def _embed(item):
    mode_key, players, need = item["mode"], item["players"], _needed(item)
    status = {"open": "Запись открыта", "locked": "Турнир идёт", "finished": "Турнир завершён",
              "cancelled": "Отменён"}.get(item["status"], item["status"])
    title = "🏆 Турнир 5v5" + (f" · {item['name']}" if item.get("name") else "")
    desc = [f"**Статус:** {status} · **Баланс:** {MODE_LABELS.get(mode_key, mode_key)}",
            f"**Формат:** {item['team_count']} команд × {TEAM_SIZE} = {need} игроков · плей-офф"]

    if item["status"] == "open":
        desc.append(f"**Записались:** {min(len(players), need)}/{need}  {_bar(len(players), need)}")

    if item.get("bracket") and item["status"] == "finished":
        gold, silver, bronze = _podium(item["bracket"])
        names = item["bracket"]["names"]
        podium = [f"\n🥇 **{names[gold]}**", f"🥈 {names[silver]}"]
        if bronze is not None:
            podium.append(f"🥉 {names[bronze]}")
        desc.append("\n".join(podium))

    if item.get("bracket"):
        desc.append(_bracket_text(item))
    elif item["teams"]:
        desc.append("\n*Составы предварительные. Организатор может перемешать команды или нажать "
                    "«Начать турнир»: тогда команды получат номера по силе и появится сетка.*")
    elif players:
        desc.append("\n**Записались**\n" + "\n".join(
            _player_line(p, mode_key, n) for n, p in enumerate(players[:need], 1)))
    else:
        desc.append("\nПока никого нет. Нажми «Присоединиться», чтобы записаться.")

    embed = discord.Embed(title=title[:256], description="\n".join(desc)[:4000],
                          color=STATUS_COLORS.get(item["status"], discord.Color.orange()))
    if item["teams"]:
        names = _names(item)
        for i, team in enumerate(item["teams"]):
            body = "\n".join(_player_line(p, mode_key) for p in team) or "—"
            embed.add_field(name=f"#{i + 1} {names[i]}{_team_average(team, mode_key)}"[:256],
                            value=body[:1024], inline=True)
    embed.set_footer(text=f"ID турнира: {item['id']} · Организатор: {item['organizer_id']}")
    return embed


async def _refresh(item, interaction=None):
    channel = interaction.channel if interaction else None
    if channel is None and bot_ref:
        channel = bot_ref.get_channel(item["channel_id"])
    if channel is None or not item.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(item["message_id"])
        await msg.edit(embed=_embed(item), view=_view_for(item))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Could not refresh tournament message %s", item["id"], exc_info=True)


async def _say(item, interaction, text):
    channel = interaction.channel if interaction else None
    if channel is None and bot_ref:
        channel = bot_ref.get_channel(item["channel_id"])
    if channel is None:
        return
    try:
        await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Could not announce in tournament %s", item["id"], exc_info=True)


def _ready_text(item, matches, header=None):
    """Объявление матчей: названия команд и упоминания всех игроков."""
    names, parts = item["bracket"]["names"], ([header] if header else [])
    for _, label, m in matches:
        ids = [p["id"] for t in (m["a"], m["b"]) for p in item["teams"][t]]
        parts.append(f"⚔️ **{label}**: **{names[m['a']]}** vs **{names[m['b']]}**\n"
                     + " ".join(f"<@{i}>" for i in ids))
    return "\n".join(parts)


def _finish_text(item):
    names = item["bracket"]["names"]
    gold, silver, bronze = _podium(item["bracket"])
    lines = ["🏆 **Турнир завершён!**", f"🥇 **{names[gold]}**", f"🥈 {names[silver]}"]
    if bronze is not None:
        lines.append(f"🥉 {names[bronze]}")
    lines.append("Поздравляем чемпионов: " + " ".join(f"<@{p['id']}>" for p in item["teams"][gold]))
    return "\n".join(lines)


# ───────────────────────────── кнопки ─────────────────────────────

def _public_buttons(item):
    if item["status"] == "open":
        return {"join", "leave", "manage"} if len(item["players"]) < _needed(item) else {"manage"}
    return {"manage"} if item["status"] == "locked" else set()


def _staff_buttons(item):
    full = len(item["players"]) >= _needed(item)
    if item["status"] == "open":
        return {"lock", "reshuffle", "ping", "cancel"} if full else {"cancel"}
    if item["status"] == "locked":
        buttons = {"result", "ping", "cancel"}
        if item.get("bracket") and _undoable(item["bracket"]):
            buttons.add("undo")
        return buttons
    return set()


def _view_for(item):
    view = TournamentView(item)
    return view if view.children else None


class TournamentView(discord.ui.View):
    """Кнопки под сообщением турнира: запись для всех и вход в меню организатора."""

    def __init__(self, item):
        super().__init__(timeout=None)
        self.tid = item["id"]
        visible = _public_buttons(item)
        for name in ("join", "leave", "manage"):
            button = getattr(self, name)
            button.custom_id = f"tour:{self.tid}:{name}"
            if name not in visible:
                self.remove_item(button)

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.success, row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.tid)
        if not item or item["status"] != "open":
            return await _deny(interaction, "Запись закрыта.")
        if any(p["id"] == interaction.user.id for p in item["players"]):
            return await _deny(interaction, "Ты уже записан.")
        if len(item["players"]) >= _needed(item):
            return await _deny(interaction, "Все места заняты.")
        await interaction.response.defer()  # запрос к FACEIT может занять >3 секунд
        profile = await _profile(interaction.user.id, interaction.guild_id)
        if profile is None:
            return await interaction.followup.send("Сначала привяжи Steam с помощью /link.", ephemeral=True)
        item = _load(self.tid)  # состояние могло измениться за время запроса
        if not item or item["status"] != "open":
            return await interaction.followup.send("Запись закрыта.", ephemeral=True)
        if any(p["id"] == interaction.user.id for p in item["players"]):
            return
        if len(item["players"]) >= _needed(item):
            return await interaction.followup.send("Пока ты записывался, места закончились.", ephemeral=True)
        item["players"].append(profile)
        _auto_teams(item)
        _save(item)
        await interaction.edit_original_response(embed=_embed(item), view=_view_for(item))

    @discord.ui.button(label="Отменить запись", style=discord.ButtonStyle.secondary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.tid)
        if not item or item["status"] != "open":
            return await _deny(interaction, "Запись закрыта.")
        before = len(item["players"])
        item["players"] = [p for p in item["players"] if p["id"] != interaction.user.id]
        if len(item["players"]) == before:
            return await _deny(interaction, "Тебя нет в списке.")
        _auto_teams(item)
        _save(item)
        await interaction.response.edit_message(embed=_embed(item), view=_view_for(item))

    @discord.ui.button(label="⚙ Управление", style=discord.ButtonStyle.secondary, row=0)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.tid)
        if not item or item["status"] not in ACTIVE:
            return await _deny(interaction, "Турнир уже не активен.")
        if not _is_staff(interaction, item):
            return await _deny(interaction, "Меню управления доступно только организатору и модераторам.")
        await interaction.response.send_message(
            _panel_text(item), view=ManageView(item, interaction.user.id), ephemeral=True)


def _panel_text(item, note=None):
    lines = [f"**Управление турниром** `{item['id']}`",
             f"Записались: {min(len(item['players']), _needed(item))}/{_needed(item)}"]
    if item.get("bracket"):
        lines.append(f"Матчей ждут результата: {len(_ready(item['bracket']))}")
    if note:
        lines.append(note)
    return "\n".join(lines)


class _OwnedView(discord.ui.View):
    """Ephemeral-окно, которым управляет только тот, кто его открыл."""

    def __init__(self, tid, actor_id, timeout=300):
        super().__init__(timeout=timeout)
        self.tid, self.actor_id = tid, actor_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Это окно открыл другой пользователь.", ephemeral=True)
            return False
        return True

    async def _item(self, interaction):
        item = _load(self.tid)
        if not item or item["status"] not in ACTIVE:
            await _deny(interaction, "Турнир уже не активен.")
            return None
        if not _is_staff(interaction, item):
            await _deny(interaction, "Это может сделать только организатор или модератор.")
            return None
        return item


class ManageView(_OwnedView):
    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=600)
        visible = _staff_buttons(item)
        for name in ("lock", "reshuffle", "result", "undo", "ping", "cancel"):
            if name not in visible:
                self.remove_item(getattr(self, name))

    async def _done(self, interaction, item, note):
        view = ManageView(item, self.actor_id)
        view = view if view.children else None
        await interaction.response.edit_message(
            content=_panel_text(item, note) if view else note, view=view)
        await _refresh(item, interaction)

    @discord.ui.button(label="Начать турнир", style=discord.ButtonStyle.primary, row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if item["status"] != "open" or len(item["players"]) < _needed(item):
            return await _deny(interaction, f"Для старта нужно {_needed(item)} записавшихся.")
        if not item["teams"]:
            _auto_teams(item)
        _start(item)
        _save(item)
        await self._done(interaction, item, "Турнир начался, сетка построена.")
        await _say(item, interaction, _ready_text(
            item, _ready(item["bracket"]), "🏁 **Турнир начался!** Первые матчи:"))

    @discord.ui.button(label="Перемешать", style=discord.ButtonStyle.secondary, row=0)
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if item["status"] != "open" or len(item["players"]) < _needed(item):
            return await _deny(interaction, "Перемешать можно только до старта турнира.")
        _auto_teams(item)
        _save(item)
        await self._done(interaction, item, "Составы пересобраны.")

    @discord.ui.button(label="Внести результат", style=discord.ButtonStyle.success, row=0)
    async def result(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if item["status"] != "locked" or not _ready(item["bracket"]):
            return await _deny(interaction, "Сейчас нет матчей, ожидающих результата.")
        await interaction.response.send_message(
            "Выбери матч, затем победителя.", view=ResultView(item, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Исправить результат", style=discord.ButtonStyle.secondary, row=1)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if not item.get("bracket") or not _undoable(item["bracket"]):
            return await _deny(interaction, "Нет результатов, которые можно отменить.")
        await interaction.response.send_message(
            "Выбери матч, результат которого нужно отменить.",
            view=UndoView(item, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Позвать игроков", style=discord.ButtonStyle.secondary, row=1)
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if item.get("bracket"):
            ready = _ready(item["bracket"])
            text = _ready_text(item, ready, "📣 Матчи, которые ждут игроков:") if ready else "Все матчи сыграны."
        else:
            text = " ".join(f"<@{p['id']}>" for p in item["players"][:_needed(item)]) + \
                "\nТурнир скоро начнётся, будьте на связи!"
        await interaction.response.send_message(text[:2000], allowed_mentions=discord.AllowedMentions(users=True))

    @discord.ui.button(label="Отменить турнир", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        await interaction.response.send_message(
            "Точно отменить турнир? Сетка и кнопки исчезнут.",
            view=ConfirmCancelView(self.tid, interaction.user.id), ephemeral=True)


class ConfirmCancelView(_OwnedView):
    @discord.ui.button(label="Да, отменить турнир", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        item = await self._item(interaction)
        if not item:
            return
        item["status"] = "cancelled"
        _save(item)
        await interaction.response.edit_message(content="Турнир отменён.", view=None)
        await _refresh(item, interaction)

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(content="Турнир остался активным.", view=None)


class ResultView(_OwnedView):
    """Выбор матча из списка и нажатие на победителя."""

    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=180)
        self.key = None
        names = item["bracket"]["names"]
        options = [discord.SelectOption(label=label[:100], value=key,
                                        description=f"{names[m['a']]} vs {names[m['b']]}"[:100])
                   for key, label, m in _ready(item["bracket"])][:25]
        select = discord.ui.Select(placeholder="Какой матч сыгран?", options=options, row=0)
        select.callback = self._picked
        self.add_item(select)
        self.win_a.disabled = self.win_b.disabled = True

    async def _picked(self, interaction: discord.Interaction):
        item = await self._item(interaction)
        if not item:
            return
        self.key = interaction.data["values"][0]
        m, names = _find(item["bracket"], self.key), item["bracket"]["names"]
        self.win_a.label = f"Победа: {names[m['a']]}"[:80]
        self.win_b.label = f"Победа: {names[m['b']]}"[:80]
        self.win_a.disabled = self.win_b.disabled = False
        await interaction.response.edit_message(view=self)

    async def _report(self, interaction, side):
        item = await self._item(interaction)
        if not item:
            return
        if item["status"] != "locked" or not self.key:
            return await _deny(interaction, "Сначала выбери матч.")
        m = _find(item["bracket"], self.key)
        if m is None or m["a"] is None or m["b"] is None or m["winner"] is not None:
            return await interaction.response.edit_message(content="Результат этого матча уже внесён.", view=None)
        before = {k for k, _, _ in _ready(item["bracket"])}
        winner = m["a"] if side == 0 else m["b"]
        _set_winner(item["bracket"], self.key, winner)
        names = item["bracket"]["names"]
        finished = _finished(item["bracket"])
        if finished:
            item["status"] = "finished"
        _save(item)
        await interaction.response.edit_message(
            content=f"Записано: победила **{names[winner]}**.", view=None)
        await _refresh(item, interaction)
        if finished:
            await _say(item, interaction, _finish_text(item))
        else:
            new = [x for x in _ready(item["bracket"]) if x[0] not in before]
            if new:
                await _say(item, interaction, _ready_text(
                    item, new, f"✅ **{names[winner]}** проходит дальше! Следующие матчи:"))

    @discord.ui.button(label="Победа A", style=discord.ButtonStyle.success, row=1)
    async def win_a(self, interaction, button):
        await self._report(interaction, 0)

    @discord.ui.button(label="Победа B", style=discord.ButtonStyle.success, row=1)
    async def win_b(self, interaction, button):
        await self._report(interaction, 1)


class UndoView(_OwnedView):
    """Отмена ошибочно внесённого результата. Работает и для завершённого турнира."""

    def __init__(self, item, actor_id):
        super().__init__(item["id"], actor_id, timeout=180)
        self.key = None
        names = item["bracket"]["names"]
        options = [discord.SelectOption(label=label[:100], value=key,
                                        description=f"Победила: {names[m['winner']]}"[:100])
                   for key, label, m in _undoable(item["bracket"])][:25]
        select = discord.ui.Select(placeholder="Какой результат отменить?", options=options, row=0)
        select.callback = self._picked
        self.add_item(select)
        self.confirm.disabled = True

    async def _item(self, interaction):
        item = _load(self.tid)
        if not item or item["status"] not in ("locked", "finished") or not item.get("bracket"):
            await _deny(interaction, "Турнир не найден или ещё не начался.")
            return None
        if not _is_staff(interaction, item):
            await _deny(interaction, "Это может сделать только организатор или модератор.")
            return None
        return item

    async def _picked(self, interaction: discord.Interaction):
        self.key = interaction.data["values"][0]
        self.confirm.disabled = False
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Отменить результат", style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self._item(interaction)
        if not item:
            return
        if not self.key or not _undo(item["bracket"], self.key):
            return await interaction.response.edit_message(
                content="Этот результат уже нельзя отменить: дальше по сетке уже сыграли.", view=None)
        label = next((l for k, l, _ in _match_list(item["bracket"]) if k == self.key), "Матч")
        item["status"] = "locked"
        _save(item)
        await interaction.response.edit_message(content="Результат отменён, матч снова ждёт игры.", view=None)
        await _refresh(item, interaction)
        await _say(item, interaction, f"↩️ Результат матча **{label}** отменён, его нужно сыграть заново.")

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Закрыто.", view=None)


# ───────────────────────────── команды ─────────────────────────────

class Tournament(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="tournament", description="Создать турнир 5 на 5 с сеткой плей-офф")
    @app_commands.guild_only()
    @app_commands.describe(teams=f"Сколько команд ({MIN_TEAMS}–{MAX_TEAMS}), в каждой по {TEAM_SIZE} игроков",
                           balancing="Способ распределения игроков по командам",
                           name="Название турнира (необязательно)")
    @app_commands.choices(balancing=[
        app_commands.Choice(name="Случайно", value="random"),
        app_commands.Choice(name="FACEIT ELO", value="elo"),
        app_commands.Choice(name="Уровень FACEIT", value="level"),
        app_commands.Choice(name="K/D", value="kd"),
    ])
    async def create(self, interaction: discord.Interaction,
                     teams: app_commands.Range[int, MIN_TEAMS, MAX_TEAMS],
                     balancing: app_commands.Choice[str], name: str | None = None):
        init_db()
        item = {"id": uuid.uuid4().hex[:8], "guild_id": interaction.guild_id,
                "channel_id": interaction.channel_id, "organizer_id": interaction.user.id,
                "status": "open", "mode": balancing.value, "team_count": teams,
                "name": name[:40] if name else None, "players": [], "teams": [], "bracket": None}
        _insert(item)
        await interaction.response.send_message(embed=_embed(item), view=_view_for(item))
        msg = await interaction.original_response()
        item["message_id"] = msg.id
        _save(item)

    async def _staff_item(self, interaction, tournament_id):
        item = _load(tournament_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await _deny(interaction, "Турнир не найден.")
            return None
        if not _is_staff(interaction, item):
            await _deny(interaction, "Это может сделать только организатор или модератор.")
            return None
        if item["status"] not in ACTIVE:
            await _deny(interaction, "Турнир уже не активен.")
            return None
        return item

    @app_commands.command(name="tournament_swap", description="Поменять двух игроков командами")
    @app_commands.guild_only()
    async def swap(self, interaction: discord.Interaction, tournament_id: str,
                   member_a: discord.Member, member_b: discord.Member):
        item = await self._staff_item(interaction, tournament_id)
        if not item:
            return
        pos = {}
        for ti, team in enumerate(item["teams"]):
            for pi, p in enumerate(team):
                pos[p["id"]] = (ti, pi)
        if member_a.id not in pos or member_b.id not in pos:
            return await _deny(interaction, "Оба игрока должны быть в составах команд.")
        (ta, pa), (tb, pb) = pos[member_a.id], pos[member_b.id]
        if ta == tb:
            return await _deny(interaction, "Игроки уже в одной команде.")
        teams = item["teams"]
        teams[ta][pa], teams[tb][pb] = teams[tb][pb], teams[ta][pa]
        _save(item)
        await interaction.response.send_message(
            f"{member_a.mention} и {member_b.mention} поменялись командами.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await _refresh(item, interaction)

    @app_commands.command(name="tournament_undo",
                          description="Исправить ошибочно внесённый результат матча (даже после финала)")
    @app_commands.guild_only()
    async def undo(self, interaction: discord.Interaction, tournament_id: str):
        item = _load(tournament_id)
        if not item or item["guild_id"] != interaction.guild_id or not item.get("bracket"):
            return await _deny(interaction, "Турнир не найден или ещё не начался.")
        if not _is_staff(interaction, item):
            return await _deny(interaction, "Это может сделать только организатор или модератор.")
        if item["status"] not in ("locked", "finished") or not _undoable(item["bracket"]):
            return await _deny(interaction, "Нет результатов, которые можно отменить.")
        await interaction.response.send_message(
            "Выбери матч, результат которого нужно отменить.",
            view=UndoView(item, interaction.user.id), ephemeral=True)

    @app_commands.command(name="tournament_kick", description="Убрать игрока из записи (до старта турнира)")
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, tournament_id: str, member: discord.Member):
        item = await self._staff_item(interaction, tournament_id)
        if not item:
            return
        if item["status"] != "open":
            return await _deny(interaction, "Убрать игрока можно только до старта турнира.")
        before = len(item["players"])
        item["players"] = [p for p in item["players"] if p["id"] != member.id]
        if len(item["players"]) == before:
            return await _deny(interaction, "Игрок не записан.")
        _auto_teams(item)
        _save(item)
        await interaction.response.send_message(
            f"{member.mention} убран, запись снова открыта.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await _refresh(item, interaction)


async def setup(bot):
    global bot_ref
    bot_ref = bot
    init_db()
    await bot.add_cog(Tournament(bot))
    for tid in _active():  # возвращаем рабочие кнопки после перезапуска бота
        item = _load(tid)
        view = _view_for(item) if item else None
        if view:
            bot.add_view(view, message_id=item.get("message_id"))