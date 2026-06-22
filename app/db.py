import asyncio
from pathlib import Path

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from app import config

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open()
    return _pool


async def apply_schema(pool: AsyncConnectionPool) -> None:
    schema = Path("schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute(schema)


async def _fetchrow(conn, query: str, *args):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, args)
        return await cur.fetchone()


async def _fetch(conn, query: str, *args):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, args)
        return await cur.fetchall()


async def upsert_linked_account(conn, platform: str, external_id: str) -> int:
    row = await _fetchrow(
        conn,
        """
        INSERT INTO linked_accounts (platform, external_id, display_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (platform, external_id) DO UPDATE SET enabled = TRUE
        RETURNING id
        """,
        platform, external_id, external_id,
    )
    return row["id"]


async def upsert_platform_game(conn, platform: str, platform_app_id: str, name: str,
                                icon_url: str | None, total_achievements: int,
                                store_id: str | None = None) -> int:
    row = await _fetchrow(
        conn,
        """
        INSERT INTO platform_games (platform, platform_app_id, name, icon_url, total_achievements, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (platform, platform_app_id) DO UPDATE
            SET name = EXCLUDED.name,
                icon_url = EXCLUDED.icon_url,
                total_achievements = EXCLUDED.total_achievements,
                store_id = COALESCE(EXCLUDED.store_id, platform_games.store_id)
        RETURNING id
        """,
        platform, platform_app_id, name, icon_url, total_achievements, store_id,
    )
    return row["id"]


async def upsert_user_game(conn, linked_account_id: int, platform_game_id: int,
                            playtime_minutes: int, earned: int, total: int,
                            last_played_at=None) -> None:
    pct = round(earned / total * 100, 1) if total else 0
    await conn.execute(
        """
        INSERT INTO user_games
            (linked_account_id, platform_game_id, playtime_minutes,
             earned_achievements, total_achievements, completion_pct, last_played_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (linked_account_id, platform_game_id) DO UPDATE
            SET playtime_minutes      = EXCLUDED.playtime_minutes,
                earned_achievements   = EXCLUDED.earned_achievements,
                total_achievements    = EXCLUDED.total_achievements,
                completion_pct        = EXCLUDED.completion_pct,
                last_played_at        = EXCLUDED.last_played_at
        """,
        (linked_account_id, platform_game_id, playtime_minutes, earned, total, pct, last_played_at),
    )


async def upsert_achievement(conn, platform_game_id: int, platform_ach_id: str,
                              name: str, description: str | None,
                              icon_url: str | None, points: int | None,
                              rarity_pct: float | None) -> int:
    row = await _fetchrow(
        conn,
        """
        INSERT INTO achievements
            (platform_game_id, platform_ach_id, name, description, icon_url, points, rarity_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (platform_game_id, platform_ach_id) DO UPDATE
            SET name        = EXCLUDED.name,
                description = EXCLUDED.description,
                icon_url    = EXCLUDED.icon_url,
                points      = EXCLUDED.points,
                rarity_pct  = EXCLUDED.rarity_pct
        RETURNING id
        """,
        platform_game_id, platform_ach_id, name, description, icon_url, points, rarity_pct,
    )
    return row["id"]


async def get_earned_counts(conn, linked_account_id: int) -> dict[str, int]:
    """Return {platform_app_id: earned_achievements} for a linked account."""
    rows = await _fetch(
        conn,
        """
        SELECT pg.platform_app_id, ug.earned_achievements
        FROM user_games ug
        JOIN platform_games pg ON pg.id = ug.platform_game_id
        WHERE ug.linked_account_id = %s
        """,
        linked_account_id,
    )
    return {r["platform_app_id"]: r["earned_achievements"] for r in rows}


async def upsert_igdb_game(conn, igdb_id: int, name: str, cover_url: str) -> None:
    await conn.execute(
        """
        INSERT INTO igdb_games (id, name, cover_url)
        VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, cover_url = EXCLUDED.cover_url
        """,
        (igdb_id, name, cover_url),
    )


async def set_igdb_id(conn, platform_game_id: int, igdb_id: int) -> None:
    await conn.execute(
        "UPDATE platform_games SET igdb_id = %s WHERE id = %s",
        (igdb_id, platform_game_id),
    )


async def update_hltb(conn, platform_game_id: int, main: float | None, extra: float | None, complete: float | None) -> None:
    await conn.execute(
        "UPDATE platform_games SET hltb_main=%s, hltb_extra=%s, hltb_complete=%s WHERE id=%s",
        (main, extra, complete, platform_game_id),
    )


async def upsert_user_achievement(conn, linked_account_id: int, achievement_id: int,
                                   unlocked: bool, unlocked_at=None) -> None:
    await conn.execute(
        """
        INSERT INTO user_achievements (linked_account_id, achievement_id, unlocked, unlocked_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (linked_account_id, achievement_id) DO UPDATE
            SET unlocked    = EXCLUDED.unlocked,
                unlocked_at = EXCLUDED.unlocked_at
        """,
        (linked_account_id, achievement_id, unlocked, unlocked_at),
    )
