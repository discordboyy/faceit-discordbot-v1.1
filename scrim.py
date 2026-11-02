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


def _rating_value(player, mode):
    if mode == "elo":
        return player.get("elo")
    if mode == "level":
        return player.get("level")
    return None


def _balanced(players, mode):
    pool = list(players)
    if mode == "random":
        random.shuffle(pool)
    else:
        # Missing FACEIT data is treated as neutral and remains eligible.
        pool.sort(key=lambda p: (_rating_value(p, mode) is not None,
                                 _rating_value(p, mode) or 0), reverse=True)
    teams = [[], []]
    totals = [0.0, 0.0]
    for player in pool:
        rating = _rating_value(player, mode)
        value = float(rating or 0)
        if totals[0] <= totals[1]:
            idx = 0
        else:
            idx = 1
        teams[idx].append(player)
        totals[idx] += value
    # Shuffle equal-looking random mode already handled above.
    return teams


def _embed(item):
    players = item["players"]
    teams = item.get("teams") or [[], []]
    team_ids = {p["id"] for team in teams for p in team}
    waiting = [p for p in players if p["id"] not in team_ids]
    def line(p):
        rating = f" · {p['elo']} ELO / Lv {p['level']}" if p.get("elo") is not None else ""
        return f"<@{p['id']}>{rating}"
    mode = {"random": "Tilfeldig", "elo": "FACEIT ELO", "level": "FACEIT level"}.get(item["mode"], item["mode"])
    status = {"open": "Påmelding åpen", "locked": "Lag låst", "cancelled": "Avlyst"}.get(item["status"], item["status"])
    desc = [f"**Status:** {status} · **Metode:** {mode}",
            f"**Påmeldt:** {len(players)}/10"]
    if teams and any(teams):
        for i, team in enumerate(teams):
            desc.append(f"\n**{('Lag A','Lag B')[i]}**\n" + ("\n".join(line(p) for p in team) or "—"))
    if waiting:
        desc.append("\n**Venteliste**\n" + "\n".join(line(p) for p in waiting))
    if not players:
        desc.append("\nIngen spillere ennå. Trykk Bli med for å melde deg på.")
    embed = discord.Embed(title="5v5 Scrim", description="\n".join(desc), color=discord.Color.orange())
    embed.set_footer(text=f"Scrim-ID: {item['id']} · Arrangør: {item['organizer_id']}")
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
    try:
        player = await asyncio.to_thread(faceit.player_by_steam, linked["steam_id64"])
        cs2 = (player or {}).get("games", {}).get("cs2", {})
        return {
            "id": user_id,
            "name": linked.get("steam_name") or str(user_id),
            "elo": faceit._num(cs2.get("faceit_elo")) if cs2 else None,
            "level": faceit._num(cs2.get("skill_level")) if cs2 else None,
        }
    except Exception:
        log.warning("FACEIT lookup failed for scrim signup user %s", user_id, exc_info=True)
        return {"id": user_id, "name": linked.get("steam_name") or str(user_id),
                "elo": None, "level": None}


class ScrimView(discord.ui.View):
    def __init__(self, scrim_id):
        super().__init__(timeout=None)
        self.scrim_id = scrim_id
        self.join.custom_id = f"scrim:{scrim_id}:join"
        self.leave.custom_id = f"scrim:{scrim_id}:leave"

    @discord.ui.button(label="Bli med", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await interaction.response.send_message("Påmeldingen er stengt.", ephemeral=True)
            return
        if any(p["id"] == interaction.user.id for p in item["players"]):
            await interaction.response.send_message("Du er allerede påmeldt.", ephemeral=True)
            return
        profile = await _profile(interaction.user.id)
        if profile is None:
            await interaction.response.send_message("Koble til Steam først med /link.", ephemeral=True)
            return
        item["players"].append(profile)
        _save(item)
        await interaction.response.edit_message(embed=_embed(item), view=self)

    @discord.ui.button(label="Meld av", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = _load(self.scrim_id)
        if not item or item["status"] != "open":
            await interaction.response.send_message("Påmeldingen er stengt.", ephemeral=True)
            return
        before = len(item["players"])
        item["players"] = [p for p in item["players"] if p["id"] != interaction.user.id]
        if len(item["players"]) == before:
            await interaction.response.send_message("Du står ikke på denne listen.", ephemeral=True)
            return
        _save(item)
        await interaction.response.edit_message(embed=_embed(item), view=self)


class Scrim(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="scrim", description="Opprett en 5v5 scrim med påmelding")
    @app_commands.guild_only()
    @app_commands.describe(balancing="Hvordan lagene skal balanseres")
    @app_commands.choices(balancing=[
        app_commands.Choice(name="Tilfeldig", value="random"),
        app_commands.Choice(name="FACEIT ELO", value="elo"),
        app_commands.Choice(name="FACEIT level", value="level"),
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

    @app_commands.command(name="scrim_rebalance", description="Fordel påmeldte scrimspillere på nytt")
    @app_commands.guild_only()
    async def rebalance(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Fant ikke scrimmen.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Kun arrangør eller moderator kan gjøre dette.", ephemeral=True); return
        if len(item["players"]) < 10:
            await interaction.response.send_message("Du trenger 10 påmeldte før lagene kan fordeles.", ephemeral=True); return
        item["teams"] = _balanced(item["players"][:10], item["mode"])
        _save(item)
        await interaction.response.send_message("Lagene er fordelt på nytt.", ephemeral=True)
        await _refresh(item, interaction)

    @app_commands.command(name="scrim_lock", description="Lås scrimlag og steng påmeldingen")
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Fant ikke scrimmen.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Kun arrangør eller moderator kan gjøre dette.", ephemeral=True); return
        if len(item["players"]) < 10:
            await interaction.response.send_message("Det må være 10 påmeldte før scrimmen kan låses.", ephemeral=True); return
        item["teams"] = _balanced(item["players"][:10], item["mode"])
        item["status"] = "locked"
        _save(item)
        await interaction.response.send_message("Scrimmen er låst.", ephemeral=True)
        await _refresh(item, interaction)

    @app_commands.command(name="scrim_cancel", description="Avlys en scrim")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction, scrim_id: str):
        item = _load(scrim_id)
        if not item or item["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Fant ikke scrimmen.", ephemeral=True); return
        if not _is_staff(interaction, item):
            await interaction.response.send_message("Kun arrangør eller moderator kan gjøre dette.", ephemeral=True); return
        item["status"] = "cancelled"
        _save(item)
        await interaction.response.send_message("Scrimmen er avlyst.", ephemeral=True)
        await _refresh(item, interaction)


async def setup(bot):
    global bot_ref
    bot_ref = bot
    init_db()
    await bot.add_cog(Scrim(bot))
    for scrim_id in _active():
        bot.add_view(ScrimView(scrim_id))
