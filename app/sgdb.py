import logging
import re

import httpx

from app import config

log = logging.getLogger(__name__)

_BASE = "https://www.steamgriddb.com/api/v2"
_CLEAN_RE = re.compile(r'[®™©]')


def _headers() -> dict | None:
    if not config.SGDB_API_KEY:
        return None
    return {"Authorization": f"Bearer {config.SGDB_API_KEY}"}


async def search_grid(name: str) -> str | None:
    """Return a landscape grid URL for the best matching game, or None.
    Tries 460x215 first, then 920x430 as fallback."""
    headers = _headers()
    if not headers:
        return None
    clean_name = _CLEAN_RE.sub('', name).strip()
    async with httpx.AsyncClient(timeout=15) as client:
        # Search for game
        resp = await client.get(
            f"{_BASE}/search/autocomplete/{clean_name}",
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning("SGDB search failed for '%s': %s", name, resp.text)
            return None
        data = resp.json()
        if not data.get("success") or not data.get("data"):
            return None
        game_id = data["data"][0]["id"]

        # Try 460x215 first, fall back to 920x430
        for dimensions in ("460x215", "920x430"):
            resp = await client.get(
                f"{_BASE}/grids/game/{game_id}",
                headers=headers,
                params={"dimensions": dimensions, "limit": 5},
            )
            if resp.status_code != 200:
                continue
            grid_data = resp.json()
            if grid_data.get("success") and grid_data.get("data"):
                return grid_data["data"][0]["url"]
        return None
