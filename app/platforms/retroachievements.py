import asyncio
from datetime import datetime, timezone

import httpx

from app import config, db
from app.platforms.base import Platform

_BASE = "https://retroachievements.org/API"


class RetroAchievementsPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        username = account["external_id"]
        key = config.RA_API_KEY
        delay = config.REQUEST_DELAY_SECONDS
        auth = {"z": config.RA_USERNAME, "y": key}

        linked_id = await db.upsert_linked_account(conn, "retroachievements", username)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}/API_GetUserCompletionProgress.php",
                params={**auth, "u": username, "c": 500, "o": 0},
            )
            resp.raise_for_status()
            data = resp.json()
            games = data.get("Results", [])

            for game in games:
                self._inc("games_seen")
                ra_id = str(game["GameID"])
                name = game.get("Title", f"Game {ra_id}")
                icon_path = game.get("ImageIcon", "")
                icon_url = f"https://retroachievements.org{icon_path}" if icon_path else None
                earned = int(game.get("NumAwardedToUser", 0))
                total = int(game.get("MaxPossible", 0))

                pg_id = await db.upsert_platform_game(
                    conn, "retroachievements", ra_id, name, icon_url, total
                )
                await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, None)

                # Skip detail fetch if earned count hasn't changed
                cached = earned_cache.get(ra_id)
                if cached and cached["earned"] == earned:
                    continue

                # Per-achievement detail
                await asyncio.sleep(delay)
                detail_resp = await client.get(
                    f"{_BASE}/API_GetGameInfoAndUserProgress.php",
                    params={**auth, "g": ra_id, "u": username},
                )
                if detail_resp.status_code != 200:
                    continue

                detail = detail_resp.json()
                for ach_id_str, ach in detail.get("Achievements", {}).items():
                    self._inc("achievements_synced")
                    unlocked_at = None
                    date_earned = ach.get("DateEarned")
                    if date_earned:
                        try:
                            unlocked_at = datetime.strptime(date_earned, "%Y-%m-%d %H:%M:%S").replace(
                                tzinfo=timezone.utc
                            )
                        except ValueError:
                            pass

                    db_ach_id = await db.upsert_achievement(
                        conn,
                        pg_id,
                        ach_id_str,
                        ach.get("Title", ""),
                        ach.get("Description"),
                        f"https://retroachievements.org{ach.get('BadgeName', '')}" if ach.get("BadgeName") else None,
                        int(ach.get("Points", 0)),
                        float(ach.get("TrueRatio", 0)) or None,
                    )
                    await db.upsert_user_achievement(
                        conn,
                        linked_id,
                        db_ach_id,
                        bool(date_earned),
                        unlocked_at,
                    )
