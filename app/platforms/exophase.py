import re
import logging

import httpx

log = logging.getLogger(__name__)

_API = "https://api.exophase.com"
_IMG_BASE = "https://www.exophase.com"
_HEADERS = {
    "Origin": "https://www.exophase.com",
    "Referer": "https://www.exophase.com/",
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
}


def _to_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _auth_headers(rememberme: str = "", xf_user: str = "") -> dict:
    headers = dict(_HEADERS)
    if rememberme or xf_user:
        parts = []
        if rememberme:
            parts.append(f"REMEMBERME={rememberme}")
        if xf_user:
            parts.append(f"xf_user={xf_user}")
        headers["Cookie"] = "; ".join(parts)
    return headers


async def fetch_games_list(
    client: httpx.AsyncClient,
    player_id: str,
    rememberme: str = "",
    xf_user: str = "",
) -> list[dict]:
    """Return all Xbox games for the player with exophase metadata."""
    all_games: list[dict] = []
    page = 1
    headers = _auth_headers(rememberme, xf_user)
    while True:
        resp = await client.get(
            f"{_API}/public/player/{player_id}/games",
            params={"page": page, "environment": "xbox", "sort": 1, "showHidden": 0, "query": ""},
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning("Exophase games list HTTP %d (page %d)", resp.status_code, page)
            break
        data = resp.json()
        batch = data.get("games") or []
        if not batch:
            break
        for g in batch:
            meta = g.get("meta") or {}
            platforms = meta.get("platforms") or []
            is_360 = any(p.get("slug") == "xbox-360" for p in platforms)
            all_games.append({
                "master_id": g["master_id"],
                "master_playerid": g["master_playerid"],
                "title": meta.get("title", ""),
                "is_360": is_360,
            })
        if len(batch) < 25:
            break
        page += 1
    return all_games


async def fetch_earned_icons(
    client: httpx.AsyncClient, master_playerid: int, game_id: int
) -> dict[str, str]:
    """Return {achievement_slug: icon_url} for all earned achievements in a game."""
    icons: dict[str, str] = {}
    last = 9999999999999
    seen: set[int] = set()

    while True:
        resp = await client.get(
            f"{_API}/public/player/{master_playerid}/game/{game_id}/earned",
            params={"last": last},
            headers=_HEADERS,
        )
        if resp.status_code != 200:
            log.warning("Exophase earned HTTP %d (game %s)", resp.status_code, game_id)
            break
        data = resp.json()
        items = data.get("list") or []
        if not items:
            break

        for item in items:
            slug = item.get("slug")
            icon_path = (item.get("icons") or {}).get("m") or (item.get("icons") or {}).get("s")
            if slug and icon_path:
                icons[slug] = f"{_IMG_BASE}{icon_path}"

        timestamps = [item.get("timestamp") for item in items if item.get("timestamp")]
        if not timestamps:
            break
        oldest = min(timestamps)
        if oldest in seen:
            break
        seen.add(oldest)
        if len(items) < 12:
            break
        last = oldest

    return icons
