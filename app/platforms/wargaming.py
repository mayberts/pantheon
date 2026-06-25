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
                    raw_enc = enc_resp.json().get("data") or {}
                    if game_key == "wows":
                        # WoWS encyclopedia uses numeric IDs as keys; re-key by achievement name
                        if isinstance(raw_enc, dict):
                            encyclopedia = {
                                v["name"]: v for v in raw_enc.values()
                                if isinstance(v, dict) and v.get("name")
                            }
                        elif isinstance(raw_enc, list):
                            encyclopedia = {
                                v["name"]: v for v in raw_enc
                                if isinstance(v, dict) and v.get("name")
                            }
                    else:
                        encyclopedia = raw_enc
                    log.info("Wargaming %s encyclopedia: %d achievements", game_key, len(encyclopedia))
                else:
                    log.warning("Wargaming %s encyclopedia fetch failed: %s", game_key, enc_resp.status_code)

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
                if game_key == "wows":
                    # WoWS may return a flat dict {name: count} or nested {category: {name: count}}
                    # Detect by checking if the first value is a dict (nested) or int (flat)
                    first_val = next(iter(ach_data.values()), None)
                    if isinstance(first_val, dict):
                        raw_achievements: dict = {}
                        for category_val in ach_data.values():
                            if isinstance(category_val, dict):
                                raw_achievements.update(category_val)
                        log.info("Wargaming wows account achievements: nested, %d categories, %d total", len(ach_data), len(raw_achievements))
                    else:
                        raw_achievements = ach_data
                        log.info("Wargaming wows account achievements: flat, %d entries", len(raw_achievements))
                else:
                    raw_achievements = ach_data.get("achievements") or {}
                for k, v in raw_achievements.items():
                    if isinstance(v, dict):
                        earned_map[k] = v.get("count", 1) or 1
                    elif isinstance(v, int) and v > 0:
                        earned_map[k] = v

                earned = len(earned_map)
                log.info("Wargaming %s: earned=%d total=%d", game_key, earned, total)

                # If encyclopedia was empty, fall back to account achievements for total
                if not encyclopedia and raw_achievements:
                    log.warning("Wargaming %s encyclopedia empty; using account achievements as total", game_key)
                    total = len(raw_achievements)

                self._inc("games_seen")
                pg_id = await db.upsert_platform_game(
                    conn, "wargaming", game_key, game_cfg["name"], game_cfg["icon"], total,
                )
                await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, None)

                cached = earned_cache.get(game_key)
                if cached and cached["earned"] == earned and cached["stored"] >= total > 0:
                    continue

                # Upsert achievements — fall back to raw_achievements when encyclopedia is empty
                ach_source = encyclopedia if encyclopedia else {k: {} for k in raw_achievements}
                for ach_name, enc in ach_source.items():
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
