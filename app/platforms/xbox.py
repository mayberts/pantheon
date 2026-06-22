import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app import config, db
from app.platforms.base import Platform

_BASE = "https://xbl.io/api/v2"
log = logging.getLogger(__name__)


class XboxPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        xuid = account["external_id"]
        headers = {
            "X-Authorization": config.XBOX_OPENXBL_KEY,
            "Accept": "application/json",
        }
        delay = config.REQUEST_DELAY_SECONDS

        linked_id = await db.upsert_linked_account(conn, "xbox", xuid)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        # OpenXBL rate limit (500/hour on Small plan). Reserve headroom for the titles
        # fetch and buffer; stop fetching detail once budget is spent so that
        # subsequent syncs (which skip cached games for free) can fill in the rest.
        _RATE_LIMIT = 490
        detail_fetches = 0

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            # Fetch all titles the player has played
            resp = await client.get(f"{_BASE}/achievements/player/{xuid}")
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content")
            if data.get("titles"):
                titles = data["titles"]
            elif isinstance(content, list):
                titles = content
            elif isinstance(content, dict):
                titles = content.get("titles", [])
            else:
                titles = []

            for title in titles:
                self._inc("games_seen")
                title_id = str(title.get("titleId", ""))
                name = title.get("name", f"Title {title_id}")
                ach_info = title.get("achievement", {})
                total = int(ach_info.get("totalAchievements", 0))
                earned = int(ach_info.get("currentAchievements", 0))
                total_gamerscore = int(ach_info.get("totalGamerscore", 0))

                # sourceVersion 2 games have totalAchievements=0 but totalGamerscore>0
                if total == 0 and total_gamerscore == 0:
                    continue

                icon_url = title.get("displayImage")
                pfn = title.get("pfn") or None
                devices = title.get("devices") or []
                is_360 = devices == ["Xbox360"] or ach_info.get("sourceVersion") == 1

                # Last played timestamp
                last_played_at = None
                last_played_str = title.get("titleHistory", {}).get("lastTimePlayed")
                if last_played_str:
                    try:
                        last_played_at = datetime.fromisoformat(
                            last_played_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

                pg_id = await db.upsert_platform_game(
                    conn, "xbox", title_id, name, icon_url, total, xbox_pfn=pfn
                )
                await db.upsert_user_game(
                    conn, linked_id, pg_id, 0, earned, total, last_played_at
                )

                if earned == 0 and total > 0:
                    continue

                # Skip detail fetch if earned count hasn't changed and we already have the total
                if earned_cache.get(title_id) == earned and total > 0:
                    continue

                # Stop fetching detail once the rate-limit budget is exhausted.
                # Cached games are skipped above for free, so subsequent syncs fill in the rest.
                if detail_fetches >= _RATE_LIMIT:
                    log.warning("Xbox sync: rate-limit budget reached, %d games deferred to next sync", 1)
                    continue

                # Fetch per-achievement detail for this title
                await asyncio.sleep(delay)
                detail_fetches += 1
                achievements = []
                ach_resp = await client.get(f"{_BASE}/achievements/player/{xuid}/{title_id}")
                if ach_resp.status_code == 429:
                    retry_after = ach_resp.json().get("retryAfter", "?")
                    log.warning("OpenXBL rate limit hit; retryAfter=%s", retry_after)
                    raise RuntimeError(f"OpenXBL rate limit exceeded — retry after {retry_after}s")
                if ach_resp.status_code == 200:
                    ach_body = ach_resp.json()
                    content = ach_body.get("content")
                    if ach_body.get("achievements"):
                        achievements = ach_body["achievements"]
                    elif isinstance(content, list):
                        achievements = content
                    elif isinstance(content, dict):
                        achievements = content.get("achievements", [])

                # For sourceVersion 2 games, derive total from actual achievement list
                if total == 0 and achievements:
                    total = len(achievements)
                    await db.upsert_platform_game(conn, "xbox", title_id, name, icon_url, total, xbox_pfn=pfn)
                    await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, last_played_at)
                for ach in achievements:
                    self._inc("achievements_synced")
                    ach_id = str(ach.get("id", ""))
                    ach_name = ach.get("name", "")
                    description = ach.get("description") or ach.get("lockedDescription")

                    icon = None
                    for media in ach.get("mediaAssets", []):
                        if media.get("type") == "Icon":
                            icon = media.get("url")
                            break

                    # Gamerscore: modern games use rewards list, 360 uses gamerScore field
                    points = None
                    for reward in ach.get("rewards", []):
                        if reward.get("type") == "Gamerscore":
                            try:
                                points = int(reward.get("value", 0))
                            except (TypeError, ValueError):
                                pass
                            break
                    if points is None and ach.get("gamerScore") is not None:
                        try:
                            points = int(ach["gamerScore"])
                        except (TypeError, ValueError):
                            pass

                    rarity_pct = None
                    rarity = ach.get("rarity", {})
                    if rarity.get("currentPercentage") is not None:
                        try:
                            rarity_pct = float(rarity["currentPercentage"])
                        except (TypeError, ValueError):
                            pass

                    # Unlock state: modern uses progressState, 360 uses isEarned
                    unlocked = (
                        ach.get("progressState") == "Achieved"
                        or bool(ach.get("isEarned"))
                        or bool(ach.get("isUnlocked"))
                    )
                    unlocked_at = None
                    if unlocked:
                        time_str = (
                            ach.get("progression", {}).get("timeUnlocked")
                            or ach.get("dateEarned")
                            or ach.get("timeEarned")
                        )
                        if time_str and time_str != "0001-01-01T00:00:00Z":
                            try:
                                unlocked_at = datetime.fromisoformat(
                                    time_str.replace("Z", "+00:00")
                                )
                            except ValueError:
                                pass

                    db_ach_id = await db.upsert_achievement(
                        conn, pg_id, ach_id, ach_name, description, icon, points, rarity_pct
                    )
                    await db.upsert_user_achievement(
                        conn, linked_id, db_ach_id, unlocked, unlocked_at
                    )
