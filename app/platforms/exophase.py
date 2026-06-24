import re
import logging
from urllib.parse import unquote

import httpx

log = logging.getLogger(__name__)

_API = "https://api.exophase.com"
_IMG_BASE = "https://www.exophase.com"
_BASE_HEADERS = {
    "Origin": "https://www.exophase.com",
    "Referer": "https://www.exophase.com/",
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}


def _to_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


async def get_access_token(rememberme: str, xf_user: str = "") -> str | None:
    """Exchange REMEMBERME cookie for a fresh ACCESS_TOKEN by hitting /account/me."""
    cookie_parts = []
    if rememberme:
        cookie_parts.append(f"REMEMBERME={unquote(rememberme)}")
    if xf_user:
        cookie_parts.append(f"xf_user={unquote(xf_user)}")
    if not cookie_parts:
        return None

    headers = dict(_BASE_HEADERS)
    headers["Cookie"] = "; ".join(cookie_parts)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(f"{_API}/account/me", headers=headers)
            log.info("Exophase /account/me status: %d", resp.status_code)
            # ACCESS_TOKEN is set as a response cookie
            token = resp.cookies.get("ACCESS_TOKEN")
            if token:
                log.info("Exophase: got fresh ACCESS_TOKEN")
                return token
            # Also check if it was in the request (already valid)
            log.warning("Exophase: no ACCESS_TOKEN in response cookies; resp=%s", resp.text[:200])
        except Exception:
            log.exception("Exophase /account/me failed")
    return None


async def fetch_games_list(
    client: httpx.AsyncClient, player_id: str, access_token: str
) -> list[dict]:
    """Return all Xbox games for the player with exophase metadata."""
    all_games: list[dict] = []
    page = 1
    headers = dict(_BASE_HEADERS)
    headers["Cookie"] = f"ACCESS_TOKEN={access_token}"

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
    master_playerid: int, game_id: int
) -> dict[str, str]:
    """Return {achievement_slug: icon_url} for all earned achievements in a game."""
    icons: dict[str, str] = {}
    last = 9999999999999
    seen: set[int] = set()

    async with httpx.AsyncClient(timeout=30, headers=_BASE_HEADERS) as client:
        while True:
            resp = await client.get(
                f"{_API}/public/player/{master_playerid}/game/{game_id}/earned",
                params={"last": last},
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
