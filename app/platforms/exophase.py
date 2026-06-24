import re
import logging

import httpx

log = logging.getLogger(__name__)

_API = "https://api.exophase.com"
_IMG_BASE = "https://m.exophase.com"
_BASE_HEADERS = {
    "Origin": "https://www.exophase.com",
    "Referer": "https://www.exophase.com/",
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}
_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": _BASE_HEADERS["User-Agent"],
}

_IMG_TAG = re.compile(r'<img[^>]+class="[^"]*award-image[^"]*"[^>]*>', re.DOTALL)
_TIPPY_NAME = re.compile(r'data-tippy-content=".*?&lt;strong&gt;(.*?)&lt;/strong&gt;', re.DOTALL)
_SRC = re.compile(r'\bsrc="(https://m\.exophase\.com/[^"?]+)')


def _to_slug(name: str) -> str:
    s = name.lower()
    s = s.replace("'", "").replace("’", "")  # strip apostrophes before hyphenating
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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
            title = meta.get("title", "")
            # Derive the game page slug: {title-slug}-{platform-slug}
            platform_tag = "xbox-360" if is_360 else "xbox-one"
            exo_slug = f"{_to_slug(title)}-{platform_tag}"
            all_games.append({
                "master_id": g["master_id"],
                "master_playerid": g["master_playerid"],
                "title": title,
                "is_360": is_360,
                "exo_slug": exo_slug,
            })
        if len(batch) < 25:
            break
        page += 1
    return all_games


async def fetch_game_page_icons(exo_slug: str) -> dict[str, str]:
    """Scrape the Exophase game achievements page for all icons (earned + locked)."""
    url = f"https://www.exophase.com/game/{exo_slug}/achievements/"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_PAGE_HEADERS)
    if resp.status_code != 200:
        log.warning("Exophase game page HTTP %d for %s", resp.status_code, exo_slug)
        return {}

    icons: dict[str, str] = {}
    for m in _IMG_TAG.finditer(resp.text):
        tag = m.group(0)
        name_m = _TIPPY_NAME.search(tag)
        src_m = _SRC.search(tag)
        if name_m and src_m:
            icons[_to_slug(name_m.group(1))] = src_m.group(1)

    log.info("Exophase page scrape %s: %d icons", exo_slug, len(icons))
    return icons


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
