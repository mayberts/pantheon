import asyncio
from datetime import datetime, timezone

import httpx

from app import config, db
from app.platforms.base import Platform

_BASE = "https://api.steampowered.com"


class SteamPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        steam_id = account["external_id"]
        key = config.STEAM_API_KEY
        delay = config.REQUEST_DELAY_SECONDS

        linked_id = await db.upsert_linked_account(conn, "steam", steam_id)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}/IPlayerService/GetOwnedGames/v1/",
                params={
                    "key": key,
                    "steamid": steam_id,
                    "include_appinfo": 1,
                    "include_played_free_games": 1,
                },
            )
            resp.raise_for_status()
            games = resp.json().get("response", {}).get("games", [])

            for game in games:
                self._inc("games_seen")
                app_id = str(game["appid"])
                name = game.get("name", f"App {app_id}")
                icon_hash = game.get("img_icon_url", "")
                icon_url = (
                    f"https://media.steampowered.com/steamcommunity/public/images/apps/{app_id}/{icon_hash}.jpg"
                    if icon_hash else None
                )
                playtime = game.get("playtime_forever", 0)
                last_played_ts = game.get("rtime_last_played")
                last_played = (
                    datetime.fromtimestamp(last_played_ts, tz=timezone.utc)
                    if last_played_ts else None
                )

                await asyncio.sleep(delay)
                ach_resp = await client.get(
                    f"{_BASE}/ISteamUserStats/GetPlayerAchievements/v1/",
                    params={"key": key, "steamid": steam_id, "appid": app_id, "l": "en"},
                )

                if ach_resp.status_code != 200:
                    pg_id = await db.upsert_platform_game(
                        conn, "steam", app_id, name, icon_url, 0
                    )
                    await db.upsert_user_game(conn, linked_id, pg_id, playtime, 0, 0, last_played)
                    continue

                ach_data = ach_resp.json().get("playerstats", {})
                if not ach_data.get("success"):
                    pg_id = await db.upsert_platform_game(conn, "steam", app_id, name, icon_url, 0)
                    await db.upsert_user_game(conn, linked_id, pg_id, playtime, 0, 0, last_played)
                    continue

                achievements = ach_data.get("achievements", [])
                total = len(achievements)
                earned = sum(1 for a in achievements if a.get("achieved"))

                pg_id = await db.upsert_platform_game(
                    conn, "steam", app_id, name, icon_url, total
                )
                await db.upsert_user_game(
                    conn, linked_id, pg_id, playtime, earned, total, last_played
                )

                # Skip detail fetch if earned count hasn't changed
                cached = earned_cache.get(app_id)
                if cached and cached["earned"] == earned:
                    continue

                await asyncio.sleep(delay)
                schema_resp = await client.get(
                    f"{_BASE}/ISteamUserStats/GetSchemaForGame/v2/",
                    params={"key": key, "appid": app_id, "l": "en"},
                )
                schema_map: dict[str, dict] = {}
                if schema_resp.status_code == 200:
                    for sa in (
                        schema_resp.json()
                        .get("game", {})
                        .get("availableGameStats", {})
                        .get("achievements", [])
                    ):
                        schema_map[sa["name"]] = sa

                global_map: dict[str, float] = {}
                await asyncio.sleep(delay)
                global_resp = await client.get(
                    f"{_BASE}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
                    params={"gameid": app_id},
                )
                if global_resp.status_code == 200:
                    for ga in global_resp.json().get("achievementpercentages", {}).get("achievements", []):
                        global_map[ga["name"]] = ga["percent"]

                for ach in achievements:
                    self._inc("achievements_synced")
                    api_name = ach["apiname"]
                    schema = schema_map.get(api_name, {})
                    ach_id = await db.upsert_achievement(
                        conn,
                        pg_id,
                        api_name,
                        schema.get("displayName") or api_name,
                        schema.get("description"),
                        schema.get("icon"),
                        None,
                        global_map.get(api_name),
                    )
                    unlocked_at = None
                    if ach.get("unlocktime"):
                        unlocked_at = datetime.fromtimestamp(ach["unlocktime"], tz=timezone.utc)
                    await db.upsert_user_achievement(
                        conn, linked_id, ach_id, bool(ach.get("achieved")), unlocked_at
                    )
