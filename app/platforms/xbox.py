import asyncio
from datetime import datetime, timezone

import httpx

from app import config, db
from app.platforms.base import Platform

_BASE = "https://xbl.io/api/v2"


class XboxPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        xuid = account["external_id"]
        headers = {
            "X-Authorization": config.XBOX_OPENXBL_KEY,
            "Accept": "application/json",
        }
        delay = config.REQUEST_DELAY_SECONDS

        linked_id = await db.upsert_linked_account(conn, "xbox", xuid)

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            # Fetch all titles the player has played
            resp = await client.get(f"{_BASE}/achievements/player/{xuid}")
            resp.raise_for_status()
            data = resp.json()
            titles = data.get("titles") or data.get("content", {}).get("titles", [])
            if not titles and "content" in data:
                titles = data["content"] if isinstance(data["content"], list) else []

            for title in titles:
                self._inc("games_seen")
                title_id = str(title.get("titleId", ""))
                name = title.get("name", f"Title {title_id}")
                total = int(title.get("achievement", {}).get("totalAchievements", 0))
                earned = int(title.get("achievement", {}).get("currentAchievements", 0))
                gamerscore_earned = int(title.get("achievement", {}).get("currentGamerscore", 0))

                if total == 0:
                    continue

                # Use the display image as icon
                display_image = title.get("displayImage") or title.get("titleHistory", {}).get("lastTimePlayed")
                icon_url = title.get("displayImage")

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
                    conn, "xbox", title_id, name, icon_url, total
                )
                await db.upsert_user_game(
                    conn, linked_id, pg_id, 0, earned, total, last_played_at
                )

                if earned == 0:
                    continue

                # Fetch per-achievement detail for this title
                await asyncio.sleep(delay)
                ach_resp = await client.get(
                    f"{_BASE}/achievements/player/{xuid}/{title_id}"
                )
                if ach_resp.status_code != 200:
                    continue

                achievements = ach_resp.json().get("achievements", [])
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

                    # Gamerscore is in rewards list
                    points = None
                    for reward in ach.get("rewards", []):
                        if reward.get("type") == "Gamerscore":
                            try:
                                points = int(reward.get("value", 0))
                            except (TypeError, ValueError):
                                pass
                            break

                    rarity_pct = None
                    rarity = ach.get("rarity", {})
                    if rarity.get("currentPercentage") is not None:
                        try:
                            rarity_pct = float(rarity["currentPercentage"])
                        except (TypeError, ValueError):
                            pass

                    unlocked = ach.get("progressState") == "Achieved"
                    unlocked_at = None
                    if unlocked:
                        time_str = ach.get("progression", {}).get("timeUnlocked")
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
