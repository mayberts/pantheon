import asyncio
import logging

import httpx

from app import config, db
from app.platforms.base import Platform

log = logging.getLogger(__name__)

_BASE = "https://api.guildwars2.com/v2"
_GAME_ID = "gw2"
_GAME_NAME = "Guild Wars 2"
_GAME_ICON = "https://render.guildwars2.com/file/AB3E265F96FFAA25B9F0909B44CEAF7B28A41826/102532.png"


class GuildWars2Platform(Platform):
    async def sync(self, account: dict, conn) -> None:
        api_key = config.GW2_API_KEY
        delay = config.REQUEST_DELAY_SECONDS

        if not api_key:
            raise RuntimeError("Guild Wars 2 not configured — set GW2_API_KEY")

        headers = {"Authorization": f"Bearer {api_key}"}
        linked_id = await db.upsert_linked_account(conn, "guildwars2", account["external_id"])
        earned_cache = await db.get_earned_counts(conn, linked_id)

        async with httpx.AsyncClient(timeout=30) as client:
            # Fetch total achievement count
            resp = await client.get(f"{_BASE}/achievements", headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"GW2 achievements list failed: {resp.status_code}")
            all_ids: list[int] = resp.json()
            total = len(all_ids)

            # Fetch account achievements (only ones with progress)
            await asyncio.sleep(delay)
            acc_resp = await client.get(f"{_BASE}/account/achievements", headers=headers)
            if acc_resp.status_code != 200:
                raise RuntimeError(f"GW2 account achievements failed: {acc_resp.status_code}")
            account_achievements: list[dict] = acc_resp.json()

            earned = sum(1 for a in account_achievements if a.get("done"))
            progressed_ids = [a["id"] for a in account_achievements]

            pg_id = await db.upsert_platform_game(
                conn, "guildwars2", _GAME_ID, _GAME_NAME, _GAME_ICON, total,
            )
            await db.upsert_user_game(conn, linked_id, pg_id, 0, earned, total, None)
            self._inc("games_seen")

            cached = earned_cache.get(_GAME_ID)
            if cached and cached["earned"] == earned and cached["stored"] >= earned > 0:
                return

            # Fetch details for achievements the user has touched, in batches of 200
            detail_map: dict[int, dict] = {}
            for i in range(0, len(progressed_ids), 200):
                batch = progressed_ids[i:i + 200]
                ids_str = ",".join(str(x) for x in batch)
                await asyncio.sleep(delay)
                det_resp = await client.get(
                    f"{_BASE}/achievements",
                    headers=headers,
                    params={"ids": ids_str, "lang": "en"},
                )
                if det_resp.status_code == 200:
                    for item in det_resp.json():
                        detail_map[item["id"]] = item

            # Build a global percentage map — GW2 doesn't provide this natively,
            # so we skip it (None)
            acc_map = {a["id"]: a for a in account_achievements}

            for acc_ach in account_achievements:
                self._inc("achievements_synced")
                ach_id_gw2 = acc_ach["id"]
                detail = detail_map.get(ach_id_gw2, {})
                name = detail.get("name") or f"Achievement {ach_id_gw2}"
                description = detail.get("requirement") or detail.get("description") or None
                icon = detail.get("icon") or None
                done = bool(acc_ach.get("done"))

                ach_id = await db.upsert_achievement(
                    conn,
                    pg_id,
                    str(ach_id_gw2),
                    name,
                    description,
                    icon,
                    None,
                    None,
                )
                await db.upsert_user_achievement(conn, linked_id, ach_id, done, None)
