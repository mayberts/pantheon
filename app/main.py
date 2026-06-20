import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.db import _fetch, _fetchrow
from app.platforms import PLATFORMS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()
_scheduler = AsyncIOScheduler()


async def run_sync() -> None:
    if _sync_lock.locked():
        log.info("Sync already running, skipping")
        return
    async with _sync_lock:
        pool = await db.get_pool()
        for account in config.enabled_accounts():
            platform_cls = PLATFORMS.get(account["platform"])
            if not platform_cls:
                continue
            log.info("Syncing %s / %s", account["platform"], account["external_id"])
            async with pool.connection() as conn:
                run_row = await _fetchrow(
                    conn,
                    "INSERT INTO sync_runs (platform, started_at, status) VALUES (%s, now(), 'running') RETURNING id",
                    account["platform"],
                )
                run_id = run_row["id"] if run_row else None
                try:
                    worker = platform_cls()
                    await worker.sync(account, conn)
                    if run_id:
                        await conn.execute(
                            "UPDATE sync_runs SET finished_at = now(), status = 'ok' WHERE id = %s",
                            (run_id,),
                        )
                    log.info("Sync done: %s", account["platform"])
                except Exception as exc:
                    log.exception("Sync failed: %s", account["platform"])
                    if run_id:
                        await conn.execute(
                            "UPDATE sync_runs SET finished_at = now(), status = 'error', detail = %s WHERE id = %s",
                            (str(exc), run_id),
                        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await db.get_pool()
    await db.apply_schema(pool)

    for account in config.enabled_accounts():
        async with pool.connection() as conn:
            await db.upsert_linked_account(conn, account["platform"], account["external_id"])

    asyncio.create_task(run_sync())

    _scheduler.add_job(run_sync, "interval", hours=config.SYNC_INTERVAL_HOURS)
    _scheduler.start()

    yield

    _scheduler.shutdown(wait=False)
    if db._pool:
        await db._pool.close()


app = FastAPI(title="Pantheon", lifespan=lifespan)


@app.get("/api/summary")
async def summary():
    pool = await db.get_pool()
    async with pool.connection() as conn:
        row = await _fetchrow(
            conn,
            """
            SELECT
                COUNT(DISTINCT ug.platform_game_id)     AS total_games,
                SUM(ug.earned_achievements)              AS total_earned,
                SUM(ug.total_achievements)               AS total_possible,
                CASE WHEN SUM(ug.total_achievements) > 0
                     THEN ROUND(SUM(ug.earned_achievements)::numeric
                          / SUM(ug.total_achievements) * 100, 1)
                     ELSE 0 END                          AS overall_pct
            FROM user_games ug
            """,
        )
        by_platform = await _fetch(
            conn,
            """
            SELECT
                pg.platform,
                COUNT(*)                                 AS games,
                SUM(ug.earned_achievements)              AS earned,
                SUM(ug.total_achievements)               AS possible,
                CASE WHEN SUM(ug.total_achievements) > 0
                     THEN ROUND(SUM(ug.earned_achievements)::numeric
                          / SUM(ug.total_achievements) * 100, 1)
                     ELSE 0 END                          AS pct
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            GROUP BY pg.platform
            """,
        )
    return {
        "total_games": row["total_games"] or 0,
        "total_earned": int(row["total_earned"] or 0),
        "total_possible": int(row["total_possible"] or 0),
        "overall_pct": float(row["overall_pct"] or 0),
        "by_platform": [dict(r) for r in by_platform],
    }


@app.get("/api/games")
async def games(
    sort: str = Query("recent", pattern="^(completion|recent|playtime|name)$"),
    platform: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    order = {
        "completion": "ug.completion_pct DESC, ug.earned_achievements DESC",
        "recent": "ug.last_played_at DESC NULLS LAST",
        "playtime": "ug.playtime_minutes DESC",
        "name": "pg.name ASC",
    }[sort]

    filters = ["ug.total_achievements > 0"]
    params: list = []

    if platform:
        filters.append(f"pg.platform = %s")
        params.append(platform)
    if search:
        filters.append(f"pg.name ILIKE %s")
        params.append(f"%{search}%")

    where = "WHERE " + " AND ".join(filters)
    offset = (page - 1) * page_size

    pool = await db.get_pool()
    async with pool.connection() as conn:
        total_row = await _fetchrow(
            conn,
            f"""
            SELECT COUNT(*) AS cnt
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            {where}
            """,
            *params,
        )
        rows = await _fetch(
            conn,
            f"""
            SELECT
                pg.id               AS platform_game_id,
                pg.platform,
                pg.platform_app_id,
                pg.name,
                pg.icon_url,
                ug.playtime_minutes,
                ug.earned_achievements,
                ug.total_achievements,
                ug.completion_pct,
                ug.last_played_at
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            {where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            *params, page_size, offset,
        )

    return {
        "total": total_row["cnt"],
        "page": page,
        "page_size": page_size,
        "games": [dict(r) for r in rows],
    }


@app.get("/api/games/{platform_game_id}/achievements")
async def game_achievements(platform_game_id: int):
    pool = await db.get_pool()
    async with pool.connection() as conn:
        rows = await _fetch(
            conn,
            """
            SELECT
                a.platform_ach_id,
                a.name,
                a.description,
                a.icon_url,
                a.points,
                a.rarity_pct,
                ua.unlocked,
                ua.unlocked_at
            FROM achievements a
            LEFT JOIN user_achievements ua ON ua.achievement_id = a.id
            WHERE a.platform_game_id = %s
            ORDER BY ua.unlocked DESC NULLS LAST, a.name
            """,
            platform_game_id,
        )
    return [dict(r) for r in rows]


@app.post("/api/sync", status_code=202)
async def trigger_sync():
    if _sync_lock.locked():
        raise HTTPException(status_code=409, detail="Sync already in progress")
    asyncio.create_task(run_sync())
    return {"status": "started"}


@app.get("/api/status")
async def status():
    pool = await db.get_pool()
    async with pool.connection() as conn:
        accounts = await _fetch(
            conn,
            "SELECT id, platform, external_id, display_name, enabled, last_synced_at FROM linked_accounts",
        )
        runs = await _fetch(
            conn,
            "SELECT id, platform, started_at, finished_at, status, detail FROM sync_runs ORDER BY started_at DESC LIMIT 10",
        )
    return {
        "syncing": _sync_lock.locked(),
        "accounts": [dict(r) for r in accounts],
        "recent_runs": [dict(r) for r in runs],
    }


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
