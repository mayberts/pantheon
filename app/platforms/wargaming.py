import asyncio
import logging

import httpx

from app import config, db
from app.platforms.base import Platform

log = logging.getLogger(__name__)

_REGION_HOSTS = {
    "na":   "https://api.worldoftanks.com",
    "eu":   "https://api.worldoftanks.eu",
    "asia": "https://api.worldoftanks.asia",
}

_WOWS_HOSTS = {
    "na":   "https://api.worldofwarships.com",
    "eu":   "https://api.worldofwarships.eu",
    "asia": "https://api.worldofwarships.asia",
}

# Wargaming game configs: (url_base, account_search_path, achievements_path, encyclopedia_path, name)
_GAMES = {
    "wot": {
        "name": "World of Tanks",
        "icon": "https://eu-wotp.wgcdn.co/dcont/fb/image/wot_intro_logo_eng.png",
        "account_search": "/wot/account/list/",
        "account_achievements": "/wot/account/achievements/",
        "encyclopedia_achievements": "/wot/encyclopedia/achievements/",
        "hosts": _REGION_HOSTS,
    },
    "wows": {
        "name": "World of Warships",
        "icon": "https://eu.wargaming.net/img/wows_logo.png",
        "account_search": "/wows/account/search/",
        "account_achievements": "/wows/account/achievements/",
        "encyclopedia_achievements": "/wows/encyclopedia/achievements/",
        "hosts": _WOWS_HOSTS,
    },
}


class WargamingPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        app_id = config.WARGAMING_APP_ID
        nickname = config.WARGAMING_NICKNAME
        region = (config.WARGAMING_REGION or "eu").lower()
        delay = config.REQUEST_DELAY_SECONDS

        if not app_id or not nickname:
            raise RuntimeError("Wargaming not configured — set WARGAMING_APP_ID and WARGAMING_NICKNAME")

        linked_id = await db.upsert_linked_account(conn, "wargaming", nickname)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            for game_key, game_cfg in _GAMES.items():
                hosts = game_cfg["hosts"]
                base = hosts.get(region, hosts["eu"])

                # Find account ID — if nickname is already numeric, use it directly
                if nickname.isdigit():
                    account_id = int(nickname)
                else:
                    resp = await client.get(
                        f"{base}{game_cfg['account_search']}",
                        params={"application_id": app_id, "search": nickname, "type": "exact"},
                    )
                    if resp.status_code != 200:
                        log.warning("Wargaming account search failed for %s/%s", game_key, nickname)
                        continue
                    results = resp.json().get("data") or []
                    if not results:
                        log.info("No Wargaming account found for %s in %s", nickname, game_key)
                        continue
                    account_id = results[0]["account_id"]

                # Fetch achievement encyclopedia (all possible achievements)
                await asyncio.sleep(delay)
                enc_resp = await client.get(
                    f"{base}{game_cfg['encyclopedia_achievements']}",
                    params={"application_id": app_id, "language": "en"},
                )
                encyclopedia: dict[str, dict] = {}
                if enc_resp.status_code == 200:
                    encyclopedia = enc_resp.json().get("data") or {}

                total = len(encyclopedia)

                # Fetch earned achievements
                await asyncio.sleep(delay)
                ach_resp = await client.get(
                    f"{base}{game_cfg['account_achievements']}",
                    params={"application_id": app_id, "account_id": account_id, "language": "en"},
                )
                if ach_resp.status_code != 200:
                    log.warning("Wargaming achievements fetch failed for %s", game_key)
                    continue

                ach_data = (ach_resp.json().get("data") or {}).get(str(account_id)) or {}
                earned_map: dict[str, int] = {}
                # achievements dict: name -> count (ribbons/series) or name -> {count, ...}
                raw_achievements = ach_data.get("achievements") or {}
                for k, v in raw_achievements.items():
                    if isinstance(v, dict):
                        earned_map[k] = v.get("count", 1) or 1
                    elif isinstance(v, int) and v > 0:
                        earned_map[k] = v

                earned = len(earned_map)

                self._inc("games_seen")
                pg_id = await db.upsert_platform_game(
                    conn, "wargaming", game_key, game_cfg["name"], game_cfg["icon"], total,
                )
                await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, None)

                cached = earned_cache.get(game_key)
                if cached and cached["earned"] == earned and cached["stored"] >= total > 0:
                    continue

                # Upsert achievements
                for ach_name, enc in encyclopedia.items():
                    self._inc("achievements_synced")
                    icon_url = (enc.get("image") or enc.get("image_big") or "").replace("http://", "https://") or None
                    ach_id = await db.upsert_achievement(
                        conn,
                        pg_id,
                        ach_name,
                        enc.get("name") or ach_name,
                        enc.get("description"),
                        icon_url,
                        None,
                        None,
                    )
                    unlocked = ach_name in earned_map
                    await db.upsert_user_achievement(conn, linked_id, ach_id, unlocked, None)
