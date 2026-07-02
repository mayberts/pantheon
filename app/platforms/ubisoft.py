import asyncio
import logging

import httpx

from app import config, db
from app.platforms.base import Platform
from app.ubisoft_auth import refresh_session, session_headers

log = logging.getLogger(__name__)

_BASE = "https://public-ubiservices.ubi.com"
_SPACE_BASE = "https://public-ubiservices.ubi.com"


class UbisoftPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        delay = config.REQUEST_DELAY_SECONDS

        ticket, profile_id = await refresh_session()
        if not ticket or not profile_id:
            raise RuntimeError("Ubisoft session refresh returned empty credentials")

        headers = session_headers(ticket)

        linked_id = await db.upsert_linked_account(conn, "ubisoft", profile_id)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            # Fetch played games
            await asyncio.sleep(delay)
            games_resp = await client.get(
                f"{_BASE}/v2/profiles/{profile_id}/playedgames",
                headers=headers,
            )
            if games_resp.status_code != 200:
                raise RuntimeError(f"Ubisoft played games fetch failed: {games_resp.status_code} — {games_resp.text[:200]}")

            games_data = games_resp.json()
            games = games_data.get("games") or []
            if not games:
                log.info("Ubisoft: no games found for profile %s", profile_id)
                return

            log.info("Ubisoft: found %d games", len(games))

            for game in games:
                space_id = game.get("spaceId") or game.get("space_id")
                game_name = game.get("name") or space_id or "Unknown"
                icon_url = game.get("thumbUrl") or game.get("thumb_url") or None

                if not space_id:
                    continue

                self._inc("games_seen")

                # Fetch achievements for this space
                await asyncio.sleep(delay)
                ach_resp = await client.get(
                    f"{_BASE}/v1/profiles/{profile_id}/achievements",
                    params={"spaceIds": space_id, "populationId": "uplay"},
                    headers=headers,
                )

                if ach_resp.status_code != 200:
                    log.warning("Ubisoft achievements fetch failed for %s: %s", game_name, ach_resp.status_code)
                    continue

                ach_json = ach_resp.json()
                # Response: {"achievements": [{"achievementId": ..., "name": ..., "description": ..., "imageUrl": ..., "isUnlocked": bool, "unlockTime": ...}]}
                achievements = ach_json.get("achievements") or []

                if not achievements:
                    log.info("Ubisoft: no achievements for %s", game_name)
                    continue

                total = len(achievements)
                earned_count = sum(1 for a in achievements if a.get("isUnlocked"))

                log.info("Ubisoft %s: earned=%d total=%d", game_name, earned_count, total)

                pg_id = await db.upsert_platform_game(
                    conn, "ubisoft", space_id, game_name, icon_url, total,
                )
                await db.upsert_user_game(conn, linked_id, pg_id, 0, earned_count, total, None)

                cached = earned_cache.get(space_id)
                if cached and cached["earned"] == earned_count and cached["stored"] >= total > 0:
                    continue

                for ach in achievements:
                    ach_id_key = ach.get("achievementId") or ach.get("statName") or ach.get("name")
                    if not ach_id_key:
                        continue
                    self._inc("achievements_synced")
                    ach_icon = (ach.get("imageUrl") or "").replace("http://", "https://") or None
                    db_ach_id = await db.upsert_achievement(
                        conn,
                        pg_id,
                        str(ach_id_key),
                        ach.get("name") or str(ach_id_key),
                        ach.get("description"),
                        ach_icon,
                        None,
                        None,
                    )
                    unlocked = bool(ach.get("isUnlocked"))
                    unlock_time = ach.get("unlockTime") or None
                    await db.upsert_user_achievement(conn, linked_id, db_ach_id, unlocked, unlock_time)
