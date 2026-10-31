"""
FACEIT Data API: профиль игрока и статистика за последние N матчей.

Ничего не пишется на диск: ответы кэшируются только в памяти на CACHE_TTL секунд.
"""

import threading
import time

import requests

API = "https://open.faceit.com/data/v4"

_headers: dict[str, str] = {}
CACHE_TTL = 300
MATCHES = 30


class TTLCache:
    """Потокобезопасный кэш в памяти. get() возвращает (найдено, значение)."""

    def __init__(self, maxsize: int = 2000):
        self._data: dict = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return False, None
            if item[0] <= time.monotonic():
                del self._data[key]
                return False, None
            return True, item[1]

    def set(self, key, value, ttl: float):
        with self._lock:
            if len(self._data) >= self._maxsize:
                now = time.monotonic()
                self._data = {k: v for k, v in self._data.items() if v[0] > now}
                if len(self._data) >= self._maxsize:
                    self._data.clear()
            self._data[key] = (time.monotonic() + ttl, value)


_profiles = TTLCache()
_recent = TTLCache()


def configure(api_key: str, cache_ttl: int = 300, matches: int = 30):
    global CACHE_TTL, MATCHES
    _headers["Authorization"] = f"Bearer {api_key}"
    CACHE_TTL = cache_ttl
    MATCHES = max(1, min(100, matches))


def _get(path: str, params: dict | None = None) -> requests.Response:
    return requests.get(f"{API}{path}", params=params, headers=_headers, timeout=15)


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

def _fetch_player(cache_key: str, params: dict) -> dict | None:
    hit, value = _profiles.get(cache_key)
    if hit:
        return value
    r = _get("/players", params)
    if r.status_code == 404:
        value = None
    else:
        r.raise_for_status()
        value = r.json()
    _profiles.set(cache_key, value, CACHE_TTL)
    return value


def player_by_steam(steam_id64: str) -> dict | None:
    return _fetch_player(f"steam:{steam_id64}", {"game": "cs2", "game_player_id": steam_id64})


def player_by_nickname(nickname: str) -> dict | None:
    return _fetch_player(f"nick:{nickname.lower()}", {"nickname": nickname})


def _num(value) -> float | None:
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_matches(items: list[dict]) -> dict:
    """Сводка по последним матчам из ответа /players/{id}/games/cs2/stats."""
    stats = [i.get("stats", {}) for i in items if isinstance(i, dict)]
    # от новых к старым (если у матчей есть время окончания)
    stats.sort(key=lambda s: _num(s.get("Match Finished At")) or 0, reverse=True)
    stats = stats[:MATCHES]
    n = len(stats)
    if n == 0:
        return {"matches": 0}

    results = [str(s.get("Result")) == "1" for s in stats]
    wins = sum(results)

    streak = 0
    for won in results:
        if not won:
            break
        streak += 1

    def col(name):
        return [x for x in (_num(s.get(name)) for s in stats) if x is not None]

    return {
        "matches": n,
        "wins": wins,
        "win_rate": wins / n * 100,
        "kd": _mean(col("K/D Ratio")),
        "hs": _mean(col("Headshots %")),
        "avg_kills": _mean(col("Kills")),
        "streak": streak,
    }


def recent_stats(player_id: str) -> dict:
    """Статистика за последние MATCHES матчей (кэш в памяти)."""
    hit, value = _recent.get(player_id)
    if hit:
        return value
    r = _get(f"/players/{player_id}/games/cs2/stats", {"limit": MATCHES})
    if r.status_code == 404:
        value = {"matches": 0}
    else:
        r.raise_for_status()
        value = summarize_matches(r.json().get("items", []))
    _recent.set(player_id, value, CACHE_TTL)
    return value


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

def format_stats(player: dict, recent: dict, label: str | None = None) -> str:
    cs2 = player.get("games", {}).get("cs2", {})
    nickname = player.get("nickname", "—")
    url = player.get("faceit_url", "").replace("{lang}", "en")

    if not cs2:
        return f"У игрока {nickname} нет статистики по CS2 на FACEIT."

    title = f"**FACEIT CS2 — {nickname}**" + (f" ({label})" if label else "")
    lines = [
        title,
        f"Уровень: {cs2.get('skill_level', '—')}",
        f"ELO: {cs2.get('faceit_elo', '—')}",
        f"Регион: {cs2.get('region', '—')}",
        f"Страна: {str(player.get('country', '—')).upper()}",
    ]

    n = recent.get("matches", 0)
    if n:
        lines.append(f"\n**За последние {n} матчей**")
        lines.append(f"Win Rate: {recent['win_rate']:.0f}% ({recent['wins']}W / {n - recent['wins']}L)")
        if recent.get("kd") is not None:
            lines.append(f"Средний K/D: {recent['kd']:.2f}")
        if recent.get("hs") is not None:
            lines.append(f"Хедшоты: {recent['hs']:.0f}%")
        if recent.get("avg_kills") is not None:
            lines.append(f"Убийств за матч: {recent['avg_kills']:.1f}")
        lines.append(f"Текущая серия побед: {recent['streak']}")
    else:
        lines.append("\nНет данных о недавних матчах CS2.")

    if url:
        lines.append(f"\nПрофиль: {url}")
    return "\n".join(lines)


def _text(player: dict, label: str | None) -> str:
    try:
        recent = recent_stats(player["player_id"])
    except requests.RequestException:
        recent = {"matches": 0}  # профиль всё равно покажем
    return format_stats(player, recent, label)


def text_by_steam(steam_id64: str, steam_name: str = "", label: str | None = None) -> str:
    player = player_by_steam(steam_id64)
    if player is None:
        return (
            f"Для Steam-аккаунта {steam_name or steam_id64} не найден профиль FACEIT "
            "с игрой CS2 (возможно, FACEIT не привязан к этому Steam)."
        )
    return _text(player, label)


def text_by_nickname(nickname: str) -> str:
    player = player_by_nickname(nickname)
    if player is None:
        return f"Игрок '{nickname}' не найден на FACEIT."
    return _text(player, None)
