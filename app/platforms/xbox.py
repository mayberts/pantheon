import asyncio
import logging
from datetime import datetime

import httpx

from app import config, db
from app.xbox_auth import XboxTokens
from app.platforms.base import Platform

log = logging.getLogger(__name__)

_TITLEHUB = "https://titlehub.xboxlive.com"
_ACH = "https://achievements.xboxlive.com"


def _xbl_headers(tokens: XboxTokens, contract: str = "2") -> dict:
    return {
        "Authorization": tokens.auth_header,
        "x-xbl-contract-version": contract,
        "Accept": "application/json",
        "Accept-Language": "en-US",
    }


class XboxPlatform(Platform):
    async def sync(self, account: dict, conn) -> None:
        from app.xbox_auth import get_tokens, load_refresh_token

        refresh_token = config.XBOX_REFRESH_TOKEN or load_refresh_token()
        if not refresh_token:
            raise RuntimeError("Xbox not configured — hit /api/xbox-setup to authenticate")

        tokens = await get_tokens(refresh_token)
        xuid = tokens.xuid
        delay = config.REQUEST_DELAY_SECONDS

        linked_id = await db.upsert_linked_account(conn, "xbox", xuid)
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            # Fetch all titles the player has played with achievement decoration
            resp = await client.get(
                f"{_TITLEHUB}/users/xuid({xuid})/titles/titleHistory/decoration/Achievement,Image",
                headers=_xbl_headers(tokens),
            )
            resp.raise_for_status()
            data = resp.json()
            titles = data.get("titles") or []

            for title in titles:
                self._inc("games_seen")
                title_id = str(title.get("titleId", ""))
                name = title.get("name", f"Title {title_id}")

                ach_info = title.get("achievement") or {}
                total = int(ach_info.get("totalAchievements") or 0)
                earned = int(ach_info.get("currentAchievements") or 0)
                total_gamerscore = int(ach_info.get("totalGamerscore") or 0)
                is_360 = ach_info.get("sourceVersion") == 1

                if total == 0 and total_gamerscore == 0:
                    continue

                # Best icon: prefer tile image from Image decoration
                icon_url = None
                for img in title.get("images") or []:
                    if img.get("type") in ("Icon", "Tile", "BrandedKeyArt"):
                        icon_url = img.get("url")
                        break
                if not icon_url:
                    icon_url = title.get("displayImage")

                # pfn and store_id directly from the API
                pfn = title.get("pfn") or None
                store_id = title.get("storeId") or None

                last_played_at = None
                last_played_str = (title.get("titleHistory") or {}).get("lastTimePlayed")
                if last_played_str:
                    try:
                        last_played_at = datetime.fromisoformat(
                            last_played_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

                pg_id = await db.upsert_platform_game(
                    conn, "xbox", title_id, name, icon_url, total,
                    store_id=store_id, xbox_pfn=pfn,
                )
                await db.upsert_user_game(
                    conn, linked_id, pg_id, 0, earned, total, last_played_at
                )

                if earned == 0 and total > 0:
                    continue

                # Xbox 360 games: OpenXBL returned nothing; direct API also doesn't
                # expose per-achievement detail for 360 titles.
                if is_360:
                    continue

                # Skip if earned count unchanged and achievements already stored
                if earned_cache.get(title_id) == earned and total > 0:
                    continue

                # Fetch per-achievement detail
                await asyncio.sleep(delay)
                ach_resp = await client.get(
                    f"{_ACH}/users/xuid({xuid})/achievements",
                    params={"titleId": title_id, "maxItems": 1000},
                    headers=_xbl_headers(tokens, contract="2"),
                )
                if ach_resp.status_code == 429:
                    log.warning("Xbox Live rate limit hit")
                    raise RuntimeError("Xbox Live rate limit — try again later")
                if ach_resp.status_code != 200:
                    log.warning("Achievements fetch failed for %s: HTTP %d", name, ach_resp.status_code)
                    continue

                ach_data = ach_resp.json()
                achievements = ach_data.get("achievements") or []

                if total == 0 and achievements:
                    total = len(achievements)
                    await db.upsert_platform_game(
                        conn, "xbox", title_id, name, icon_url, total,
                        store_id=store_id, xbox_pfn=pfn,
                    )
                    await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, last_played_at)

                for ach in achievements:
                    self._inc("achievements_synced")
                    ach_id = str(ach.get("id", ""))
                    ach_name = ach.get("name", "")
                    description = ach.get("description") or ach.get("lockedDescription")

                    icon = None
                    for media in ach.get("mediaAssets") or []:
                        if media.get("type") == "Icon":
                            icon = media.get("url")
                            break

                    points = None
                    for reward in ach.get("rewards") or []:
                        if reward.get("type") == "Gamerscore":
                            try:
                                points = int(reward.get("value", 0))
                            except (TypeError, ValueError):
                                pass
                            break

                    rarity_pct = None
                    rarity = ach.get("rarity") or {}
                    if rarity.get("currentPercentage") is not None:
                        try:
                            rarity_pct = float(rarity["currentPercentage"])
                        except (TypeError, ValueError):
                            pass

                    unlocked = ach.get("progressState") == "Achieved"
                    unlocked_at = None
                    if unlocked:
                        time_str = (ach.get("progression") or {}).get("timeUnlocked")
                        if time_str and time_str != "0001-01-01T00:00:00.0000000Z":
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
