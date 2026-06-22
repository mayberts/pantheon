import logging
import time

import httpx

from app import config

log = logging.getLogger(__name__)

_token: str | None = None
_token_expires: float = 0.0

_AUTH_URL = "https://id.twitch.tv/oauth2/token"
_BASE = "https://api.igdb.com/v4"


async def _get_token() -> str | None:
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token
    if not config.IGDB_CLIENT_ID or not config.IGDB_CLIENT_SECRET:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_AUTH_URL, params={
            "client_id": config.IGDB_CLIENT_ID,
            "client_secret": config.IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        })
    if resp.status_code != 200:
        log.warning("IGDB auth failed: %s", resp.text)
        return None
    data = resp.json()
    _token = data["access_token"]
    _token_expires = time.time() + data.get("expires_in", 3600)
    return _token


async def search_cover(name: str) -> tuple[int, str] | None:
    """Return (igdb_id, cover_url) for the best match, or None."""
    token = await _get_token()
    if not token:
        return None
    headers = {
        "Client-ID": config.IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    body = f'search "{name}"; fields id,name,cover.url; limit 5;'
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{_BASE}/games", headers=headers, content=body)
    if resp.status_code != 200:
        log.warning("IGDB search failed for '%s': %s", name, resp.text)
        return None
    results = resp.json()
    if not results:
        return None
    # Pick first result that has a cover
    for r in results:
        if r.get("cover", {}).get("url"):
            cover = r["cover"]["url"].replace("//", "https://").replace("t_thumb", "t_cover_big")
            return r["id"], cover
    return None
