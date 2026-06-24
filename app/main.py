import asyncio
import logging

import httpx
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.db import _fetch, _fetchrow
from app.platforms import PLATFORMS

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()
_scheduler = AsyncIOScheduler()

# In-memory sync progress tracker
_sync_progress: dict = {
    "running": False,
    "started_at": None,
    "platforms": {},  # platform -> {status, games_seen, achievements_synced, error}
}


async def _enrich_igdb() -> None:
    """Fetch IGDB cover art for games that don't have it yet (parallel with semaphore)."""
    if not config.IGDB_CLIENT_ID or not config.IGDB_CLIENT_SECRET:
        return
    from app.igdb import search_cover
    pool = await db.get_pool()
    async with pool.connection() as conn:
        rows = await _fetch(
            conn,
            "SELECT id, name FROM platform_games WHERE igdb_id IS NULL AND total_achievements > 0",
        )
    log.info("IGDB enrichment: %d games to look up", len(rows))
    sem = asyncio.Semaphore(5)

    async def _lookup(row):
        async with sem:
            try:
                result = await search_cover(row["name"])
                if result:
                    igdb_id, cover_url = result
                    async with pool.connection() as conn:
                        await db.upsert_igdb_game(conn, igdb_id, row["name"], cover_url)
                        await db.set_igdb_id(conn, row["id"], igdb_id)
                    log.info("IGDB cover found for '%s'", row["name"])
                else:
                    async with pool.connection() as conn:
                        await conn.execute(
                            "UPDATE platform_games SET igdb_id = -1 WHERE id = %s", (row["id"],)
                        )
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
            except Exception:
                log.exception("IGDB lookup failed for %s", row["name"])

    await asyncio.gather(*[_lookup(row) for row in rows])


async def _enrich_hltb() -> None:
    """Fetch How Long To Beat times for games that don't have them yet (parallel with semaphore)."""
    try:
        from howlongtobeatpy import HowLongToBeat
    except ImportError:
        log.warning("howlongtobeatpy not installed; skipping HLTB enrichment")
        return

    pool = await db.get_pool()
    async with pool.connection() as conn:
        rows = await _fetch(
            conn,
            "SELECT id, name FROM platform_games WHERE hltb_main IS NULL AND total_achievements > 0",
        )

    log.info("HLTB enrichment: %d games to look up", len(rows))
    hltb = HowLongToBeat(0.0)
    sem = asyncio.Semaphore(3)

    async def _lookup(row):
        async with sem:
            try:
                results = await hltb.async_search(row["name"])
                if not results:
                    log.info("HLTB no results for: %s", row["name"])
                    async with pool.connection() as conn:
                        await db.update_hltb(conn, row["id"], -1, None, None)
                    return
                best = max(results, key=lambda r: r.similarity)
                log.info("HLTB best match for '%s': '%s' (sim=%.2f)", row["name"], best.game_name, best.similarity)
                main = float(best.main_story) if best.main_story and best.main_story > 0 else None
                extra = float(best.main_extra) if best.main_extra and best.main_extra > 0 else None
                complete = float(best.completionist) if best.completionist and best.completionist > 0 else None
                async with pool.connection() as conn:
                    await db.update_hltb(conn, row["id"], main or -1, extra, complete)
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
            except Exception:
                log.exception("HLTB lookup failed for %s", row["name"])

    await asyncio.gather(*[_lookup(row) for row in rows])




# Manual slug aliases for games with abbreviated/different names in our DB vs Exophase
_EXOPHASE_TITLE_ALIASES: dict[str, str] = {
    "pgr-4": "project-gotham-racing-4",
    "pgr-3": "project-gotham-racing-3",
    "gta-iv": "grand-theft-auto-iv",
    "gta-iv-pc": "grand-theft-auto-iv",
    "modern-warfare": "call-of-duty-4-modern-warfare",
    "brothers-in-arms-hh": "brothers-in-arms-hells-highway",
    "nfs-undercover": "need-for-speed-undercover",
    "nfs-prostreet": "need-for-speed-prostreet",
    "guitar-hero-iii": "guitar-hero-iii-legends-of-rock",
    "medal-of-honor-airborne": "moh-airborne",
    "alone-in-the-dark": "alone-in-the-dark-2008",
    "kane-and-lynch-deadmen": "kane-lynch-dead-men",
}


async def _enrich_exophase_360_icons() -> None:
    """Fetch Xbox 360 achievement icons from Exophase (earned + locked via page scrape)."""
    if not config.EXOPHASE_PLAYER_ID:
        return

    from app.platforms.exophase import fetch_games_list, fetch_earned_icons, _to_slug

    access_token = config.EXOPHASE_ACCESS_TOKEN
    if not access_token:
        log.warning("Exophase: EXOPHASE_ACCESS_TOKEN not set; skipping enrichment")
        return

    pool = await db.get_pool()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            exo_games = await fetch_games_list(client, config.EXOPHASE_PLAYER_ID, access_token)
        except Exception:
            log.exception("Exophase games list fetch failed")
            return

        # Build title→exo_game map for ALL games
        exo_by_title: dict[str, dict] = {}
        for g in exo_games:
            exo_by_title[_to_slug(g["title"])] = g

        if not exo_by_title:
            return

        # Find ALL Xbox achievements without icons (earned and locked)
        async with pool.connection() as conn:
            rows = await _fetch(
                conn,
                """
                SELECT a.id, a.name, pg.name AS game_name
                FROM achievements a
                JOIN platform_games pg ON pg.id = a.platform_game_id
                WHERE pg.platform = 'xbox' AND a.icon_url IS NULL
                """,
            )

        if not rows:
            log.info("Exophase enrichment: no Xbox achievements missing icons")
            return

        log.info("Exophase enrichment: %d Xbox achievements missing icons", len(rows))

        # Group achievements by game name
        by_game: dict[str, list[dict]] = {}
        for row in rows:
            by_game.setdefault(row["game_name"], []).append(row)

        updated = 0
        for game_name, achs in by_game.items():
            db_slug = _to_slug(game_name)
            exo_slug = _EXOPHASE_TITLE_ALIASES.get(db_slug, db_slug)
            exo_game = exo_by_title.get(exo_slug)
            if not exo_game:
                continue

            try:
                icons = await fetch_earned_icons(
                    exo_game["master_playerid"], exo_game["master_id"]
                )
            except Exception:
                log.exception("Exophase earned fetch failed for %s", game_name)
                continue

            if not icons:
                continue

            await asyncio.sleep(config.REQUEST_DELAY_SECONDS)

            async with pool.connection() as conn:
                for ach in achs:
                    slug = _to_slug(ach["name"])
                    icon_url = icons.get(slug)
                    if icon_url:
                        await conn.execute(
                            "UPDATE achievements SET icon_url = %s WHERE id = %s",
                            (icon_url, ach["id"]),
                        )
                        updated += 1

        log.info("Exophase enrichment: updated %d achievement icons", updated)


async def run_sync() -> None:
    if _sync_lock.locked():
        log.info("Sync already running, skipping")
        return
    async with _sync_lock:
        from datetime import datetime, timezone
        _sync_progress["running"] = True
        _sync_progress["started_at"] = datetime.now(timezone.utc).isoformat()
        _sync_progress["platforms"] = {}

        pool = await db.get_pool()
        for account in config.enabled_accounts():
            platform_cls = PLATFORMS.get(account["platform"])
            if not platform_cls:
                continue
            plat = account["platform"]
            log.info("Syncing %s / %s", plat, account["external_id"])
            _sync_progress["platforms"][plat] = {
                "status": "running",
                "games_seen": 0,
                "achievements_synced": 0,
                "error": None,
            }
            async with pool.connection() as conn:
                run_row = await _fetchrow(
                    conn,
                    "INSERT INTO sync_runs (platform, started_at, status) VALUES (%s, now(), 'running') RETURNING id",
                    plat,
                )
                run_id = run_row["id"] if run_row else None
                try:
                    worker = platform_cls()
                    worker._progress = _sync_progress["platforms"][plat]
                    await worker.sync(account, conn)
                    if run_id:
                        await conn.execute(
                            "UPDATE sync_runs SET finished_at = now(), status = 'ok' WHERE id = %s",
                            (run_id,),
                        )
                    _sync_progress["platforms"][plat]["status"] = "done"
                    log.info("Sync done: %s", plat)
                except Exception as exc:
                    log.exception("Sync failed: %s", plat)
                    _sync_progress["platforms"][plat]["status"] = "error"
                    _sync_progress["platforms"][plat]["error"] = str(exc)
                    if run_id:
                        await conn.execute(
                            "UPDATE sync_runs SET finished_at = now(), status = 'error', detail = %s WHERE id = %s",
                            (str(exc), run_id),
                        )

        _sync_progress["running"] = False
        asyncio.create_task(_enrich_hltb())
        asyncio.create_task(_enrich_igdb())
        asyncio.create_task(_enrich_exophase_360_icons())


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await db.get_pool()
    await db.apply_schema(pool)

    for account in config.enabled_accounts():
        async with pool.connection() as conn:
            await db.upsert_linked_account(conn, account["platform"], account["external_id"])

    asyncio.create_task(run_sync())
    asyncio.create_task(_enrich_hltb())
    asyncio.create_task(_enrich_igdb())

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
                     ELSE 0 END                          AS overall_pct,
                COUNT(*) FILTER (WHERE ug.completion_pct = 100) AS perfect_games
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
        last_synced = await _fetch(
            conn,
            """
            SELECT platform, MAX(finished_at) AS last_sync
            FROM sync_runs
            WHERE status = 'ok'
            GROUP BY platform
            """,
        )
    last_synced_map = {r["platform"]: r["last_sync"] for r in last_synced}
    return {
        "total_games": row["total_games"] or 0,
        "total_earned": int(row["total_earned"] or 0),
        "total_possible": int(row["total_possible"] or 0),
        "overall_pct": float(row["overall_pct"] or 0),
        "perfect_games": int(row["perfect_games"] or 0),
        "by_platform": [
            {**dict(r), "last_sync": last_synced_map.get(r["platform"])}
            for r in by_platform
        ],
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
                pg.store_id,
                ig.cover_url        AS igdb_cover_url,
                ug.playtime_minutes,
                ug.earned_achievements,
                ug.total_achievements,
                ug.completion_pct,
                ug.last_played_at
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            LEFT JOIN igdb_games ig ON ig.id = pg.igdb_id AND pg.igdb_id > 0
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


@app.get("/api/games/{platform_game_id}")
async def game_detail(platform_game_id: int):
    pool = await db.get_pool()
    async with pool.connection() as conn:
        row = await _fetchrow(
            conn,
            """
            SELECT
                pg.id               AS platform_game_id,
                pg.platform,
                pg.platform_app_id,
                pg.name,
                pg.icon_url,
                pg.store_id,
                pg.hltb_main,
                pg.hltb_extra,
                pg.hltb_complete,
                ig.cover_url        AS igdb_cover_url,
                ug.playtime_minutes,
                ug.earned_achievements,
                ug.total_achievements,
                ug.completion_pct,
                ug.last_played_at
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            LEFT JOIN igdb_games ig ON ig.id = pg.igdb_id AND pg.igdb_id > 0
            WHERE pg.id = %s
            """,
            platform_game_id,
        )
        rarity_summary = await _fetch(
            conn,
            """
            SELECT tier, COUNT(*) AS cnt FROM (
                SELECT CASE
                    WHEN a.rarity_pct <= 1  THEN 'Legendary'
                    WHEN a.rarity_pct <= 5  THEN 'Epic'
                    WHEN a.rarity_pct <= 20 THEN 'Rare'
                    WHEN a.rarity_pct <= 50 THEN 'Uncommon'
                    ELSE 'Common'
                END AS tier
                FROM user_achievements ua
                JOIN achievements a ON a.id = ua.achievement_id
                WHERE a.platform_game_id = %s AND ua.unlocked = true AND a.rarity_pct IS NOT NULL
            ) sub GROUP BY tier ORDER BY MIN(
                CASE tier
                    WHEN 'Legendary' THEN 1 WHEN 'Epic' THEN 2
                    WHEN 'Rare' THEN 3 WHEN 'Uncommon' THEN 4 ELSE 5
                END
            )
            """,
            platform_game_id,
        )
        points_row = await _fetchrow(
            conn,
            """
            SELECT SUM(a.points) AS total_points
            FROM user_achievements ua
            JOIN achievements a ON a.id = ua.achievement_id
            WHERE a.platform_game_id = %s AND ua.unlocked = true AND a.points IS NOT NULL
            """,
            platform_game_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    result = dict(row)
    result["rarity_summary"] = [dict(r) for r in rarity_summary]
    result["total_points"] = int(points_row["total_points"] or 0) if points_row else 0
    return result


@app.get("/api/statistics")
async def statistics():
    pool = await db.get_pool()
    try:
        async with pool.connection() as conn:
            general = await _fetchrow(
                conn,
                """
                SELECT
                    SUM(ug.earned_achievements)                                          AS unlocked,
                    SUM(ug.total_achievements - ug.earned_achievements)                  AS locked,
                    COUNT(*)                                                             AS games_total,
                    COUNT(*) FILTER (WHERE ug.completion_pct = 100)                     AS mastered,
                    COUNT(*) FILTER (WHERE ug.completion_pct >= 80 AND ug.completion_pct < 100) AS finished,
                    ROUND(AVG(ug.completion_pct), 1)                                    AS avg_completion,
                    ROUND(SUM(ug.earned_achievements)::numeric
                          / NULLIF(SUM(ug.total_achievements), 0) * 100, 2)             AS absolute_completion
                FROM user_games ug
                WHERE ug.total_achievements > 0
                """,
            )

            daily_max = await _fetchrow(
                conn,
                """
                SELECT COUNT(*) AS cnt
                FROM user_achievements
                WHERE unlocked = true AND unlocked_at IS NOT NULL
                GROUP BY unlocked_at::date
                ORDER BY cnt DESC LIMIT 1
                """,
            )

            monthly_max = await _fetchrow(
                conn,
                """
                SELECT COUNT(*) AS cnt
                FROM user_achievements
                WHERE unlocked = true AND unlocked_at IS NOT NULL
                GROUP BY DATE_TRUNC('month', unlocked_at)
                ORDER BY cnt DESC LIMIT 1
                """,
            )

            rarity_rows = await _fetch(
                conn,
                """
                SELECT tier, COUNT(*) AS cnt
                FROM (
                    SELECT
                        CASE
                            WHEN a.rarity_pct <= 1  THEN 'Legendary'
                            WHEN a.rarity_pct <= 5  THEN 'Epic'
                            WHEN a.rarity_pct <= 20 THEN 'Rare'
                            WHEN a.rarity_pct <= 50 THEN 'Uncommon'
                            ELSE 'Common'
                        END AS tier,
                        a.rarity_pct
                    FROM user_achievements ua
                    JOIN achievements a ON a.id = ua.achievement_id
                    WHERE ua.unlocked = true AND a.rarity_pct IS NOT NULL
                ) sub
                GROUP BY tier
                ORDER BY MIN(rarity_pct)
                """,
            )

            completion_dist = await _fetch(
                conn,
                """
                SELECT bracket, COUNT(*) AS cnt
                FROM (
                    SELECT
                        CASE
                            WHEN completion_pct = 0         THEN '0%%'
                            WHEN completion_pct <= 25       THEN '1-25%%'
                            WHEN completion_pct <= 50       THEN '25-50%%'
                            WHEN completion_pct <= 75       THEN '50-75%%'
                            WHEN completion_pct < 100       THEN '75-99%%'
                            ELSE '100%%'
                        END AS bracket
                    FROM user_games WHERE total_achievements > 0
                ) sub
                GROUP BY bracket
                """,
            )

            platform_rows = await _fetch(
                conn,
                """
                SELECT pg.platform, SUM(ug.earned_achievements) AS earned
                FROM user_games ug
                JOIN platform_games pg ON pg.id = ug.platform_game_id
                GROUP BY pg.platform
                """,
            )

            progression = await _fetch(
                conn,
                """
                SELECT DATE_TRUNC('month', unlocked_at)::date AS month, COUNT(*) AS cnt
                FROM user_achievements
                WHERE unlocked = true AND unlocked_at IS NOT NULL
                GROUP BY DATE_TRUNC('month', unlocked_at)::date
                ORDER BY DATE_TRUNC('month', unlocked_at)::date
                """,
            )

            best_day_row = await _fetchrow(
                conn,
                """
                SELECT unlocked_at::date AS day, COUNT(*) AS cnt
                FROM user_achievements
                WHERE unlocked = true AND unlocked_at IS NOT NULL
                GROUP BY unlocked_at::date
                ORDER BY cnt DESC LIMIT 1
                """,
            )

            best_month_row = await _fetchrow(
                conn,
                """
                SELECT DATE_TRUNC('month', unlocked_at)::date AS month, COUNT(*) AS cnt
                FROM user_achievements
                WHERE unlocked = true AND unlocked_at IS NOT NULL
                GROUP BY DATE_TRUNC('month', unlocked_at)::date
                ORDER BY cnt DESC LIMIT 1
                """,
            )

            streak_row = await _fetchrow(
                conn,
                """
                WITH daily AS (
                    SELECT DISTINCT unlocked_at::date AS day
                    FROM user_achievements
                    WHERE unlocked = true AND unlocked_at IS NOT NULL
                ),
                grouped AS (
                    SELECT day, day - (ROW_NUMBER() OVER (ORDER BY day))::int AS grp
                    FROM daily
                ),
                streaks AS (
                    SELECT MIN(day) AS start, MAX(day) AS finish,
                           COUNT(*) AS days
                    FROM grouped GROUP BY grp
                )
                SELECT start, finish, days FROM streaks ORDER BY days DESC LIMIT 1
                """,
            )

        cum, total = [], 0
        for r in progression:
            total += r["cnt"]
            cum.append({"month": r["month"].isoformat(), "total": total})

        bracket_order = ["0%", "1-25%", "25-50%", "50-75%", "75-99%", "100%"]
        dist_map = {r["bracket"]: r["cnt"] for r in completion_dist}

        return {
            "general": {
                "unlocked":           int(general["unlocked"] or 0),
                "locked":             int(general["locked"] or 0),
                "games_total":        int(general["games_total"] or 0),
                "mastered":           int(general["mastered"] or 0),
                "finished":           int(general["finished"] or 0),
                "avg_completion":     float(general["avg_completion"] or 0),
                "absolute_completion": float(general["absolute_completion"] or 0),
                "daily_max":          int(daily_max["cnt"]) if daily_max else 0,
                "monthly_max":        int(monthly_max["cnt"]) if monthly_max else 0,
                "best_day":           best_day_row["day"].isoformat() if best_day_row else None,
                "best_month":         best_month_row["month"].isoformat() if best_month_row else None,
                "best_month_cnt":     int(best_month_row["cnt"]) if best_month_row else 0,
                "best_streak_days":   int(streak_row["days"]) if streak_row else 0,
                "best_streak_start":  streak_row["start"].isoformat() if streak_row else None,
                "best_streak_end":    streak_row["finish"].isoformat() if streak_row else None,
            },
            "rarity": [{"tier": r["tier"], "cnt": r["cnt"]} for r in rarity_rows],
            "completion_dist": [{"bracket": b, "cnt": dist_map.get(b, 0)} for b in bracket_order],
            "platforms": [{"platform": r["platform"], "earned": int(r["earned"] or 0)} for r in platform_rows],
            "progression": cum,
        }
    except Exception:
        log.exception("statistics endpoint failed")
        raise


@app.get("/api/statistics/platform/{platform}")
async def statistics_platform(platform: str):
    """Top games by completion for a platform, for the drilldown modal."""
    pool = await db.get_pool()
    async with pool.connection() as conn:
        rows = await _fetch(
            conn,
            """
            SELECT pg.id AS platform_game_id, pg.name, pg.icon_url,
                   ig.cover_url AS igdb_cover_url, pg.platform_app_id,
                   ug.earned_achievements, ug.total_achievements, ug.completion_pct
            FROM user_games ug
            JOIN platform_games pg ON pg.id = ug.platform_game_id
            LEFT JOIN igdb_games ig ON ig.id = pg.igdb_id AND pg.igdb_id > 0
            WHERE pg.platform = %s AND ug.total_achievements > 0
            ORDER BY ug.earned_achievements DESC
            LIMIT 50
            """,
            platform,
        )
    return [dict(r) for r in rows]


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


@app.get("/api/hltb-test")
async def hltb_test(name: str = Query(...)):
    """Test HLTB search for a game name. Use to verify the library works."""
    try:
        from howlongtobeatpy import HowLongToBeat
    except ImportError:
        return {"error": "howlongtobeatpy not installed"}
    try:
        results = await HowLongToBeat(0.0).async_search(name)
        if not results:
            return {"error": "no results", "name": name}
        best = max(results, key=lambda r: r.similarity)
        return {
            "query": name,
            "matched": best.game_name,
            "similarity": best.similarity,
            "main_story": best.main_story,
            "main_extra": best.main_extra,
            "completionist": best.completionist,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/exophase-refresh", status_code=202)
async def exophase_refresh():
    """Clear all Exophase-sourced icons and re-enrich from scratch."""
    pool = await db.get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE achievements SET icon_url = NULL "
            "WHERE icon_url LIKE '%exophase%'"
        )
    asyncio.create_task(_enrich_exophase_360_icons())
    return {"status": "started"}


@app.post("/api/exophase-import-icons")
async def exophase_import_icons(payload: dict):
    """
    Accept a JSON body {game_name: str, icons: {achievement_name: icon_url}}
    and update matching achievements in the DB.
    """
    from app.platforms.exophase import _to_slug

    game_name = payload.get("game_name", "")
    icons: dict = payload.get("icons") or {}
    if not game_name or not icons:
        return {"error": "game_name and icons required"}

    pool = await db.get_pool()
    matched = None
    async with pool.connection() as conn:
        rows = await _fetch(
            conn,
            """
            SELECT a.id, a.name FROM achievements a
            JOIN platform_games pg ON pg.id = a.platform_game_id
            WHERE pg.platform = 'xbox' AND pg.name = %s
            """,
            game_name,
        )

    if not rows:
        # Try slug match, then prefix match (e.g. DB "Guitar Hero III" vs Exophase "Guitar Hero III: Legends of Rock")
        async with pool.connection() as conn:
            all_games = await _fetch(conn, "SELECT id, name FROM platform_games WHERE platform = 'xbox'")
        db_slug = _to_slug(game_name)
        matched = next((g for g in all_games if _to_slug(g["name"]) == db_slug), None)
        if not matched:
            matched = next((g for g in all_games if db_slug.startswith(_to_slug(g["name"]) + "-") or _to_slug(g["name"]).startswith(db_slug + "-")), None)
        if not matched:
            return {"error": f"No xbox game found matching '{game_name}'"}
        async with pool.connection() as conn:
            rows = await _fetch(
                conn,
                "SELECT a.id, a.name FROM achievements a WHERE a.platform_game_id = %s",
                matched["id"],
            )

    updated = 0
    created = 0
    slug_icons = {_to_slug(k): v for k, v in icons.items()}

    # Determine platform_game_id for potential inserts
    async with pool.connection() as conn:
        pg_row = await _fetchrow(
            conn,
            "SELECT id FROM platform_games WHERE platform = 'xbox' AND name = %s",
            game_name,
        )
        if not pg_row and matched:
            pg_id = matched["id"]
        elif pg_row:
            pg_id = pg_row["id"]
        else:
            pg_id = None

        # Get linked_account_id for xbox (to create user_achievement rows)
        la_row = await _fetchrow(
            conn,
            "SELECT id FROM linked_accounts WHERE platform = 'xbox' LIMIT 1",
        )
        linked_id = la_row["id"] if la_row else None

    async with pool.connection() as conn:
        existing_slugs = {_to_slug(ach["name"]) for ach in rows}
        for ach in rows:
            icon_url = slug_icons.get(_to_slug(ach["name"]))
            if icon_url:
                await conn.execute(
                    "UPDATE achievements SET icon_url = %s WHERE id = %s",
                    (icon_url, ach["id"]),
                )
                updated += 1

        # Create achievements that don't exist in DB yet
        if pg_id and linked_id:
            for name, icon_url in icons.items():
                slug = _to_slug(name)
                if slug not in existing_slugs:
                    synth_id = f"exo-{slug}"
                    ach_id = await db.upsert_achievement(
                        conn, pg_id, synth_id, name, None, icon_url, None, None
                    )
                    await db.upsert_user_achievement(conn, linked_id, ach_id, False, None)
                    created += 1

    return {"game_name": game_name, "achievements_found": len(rows), "icons_updated": updated, "achievements_created": created}


@app.post("/api/hltb-refresh", status_code=202)
async def hltb_refresh():
    """Reset all HLTB data and re-enrich from scratch."""
    pool = await db.get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE platform_games SET hltb_main=NULL, hltb_extra=NULL, hltb_complete=NULL")
    asyncio.create_task(_enrich_hltb())
    return {"status": "started"}


@app.get("/api/sync/progress")
async def sync_progress():
    return _sync_progress


@app.post("/api/sync", status_code=202)
async def trigger_sync():
    if _sync_lock.locked():
        raise HTTPException(status_code=409, detail="Sync already in progress")
    asyncio.create_task(run_sync())
    return {"status": "started"}


@app.get("/api/xbox-setup")
async def xbox_setup():
    """Start device code flow. Returns a user_code to enter at microsoft.com/devicelogin."""
    from app.xbox_auth import start_device_flow
    try:
        data = await start_device_flow()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "device_code": data.get("device_code"),
        "expires_in_seconds": data.get("expires_in"),
        "interval": data.get("interval", 5),
        "instructions": (
            f"Go to {data.get('verification_uri')} and enter code {data.get('user_code')}. "
            f"Then poll GET /api/xbox-setup-poll?device_code=<device_code> every {data.get('interval', 5)}s until status=done."
        ),
    }


@app.get("/api/xbox-setup-poll")
async def xbox_setup_poll(device_code: str):
    """Poll for device code flow completion. Call repeatedly until status=done."""
    from app.xbox_auth import poll_device_flow, get_tokens
    try:
        refresh_token = await poll_device_flow(device_code)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if refresh_token is None:
        return {"status": "pending"}
    try:
        tokens = await get_tokens(refresh_token)
        xuid = tokens.xuid
    except Exception as e:
        xuid = "unknown"
        log.warning("Could not fetch XUID after auth: %s", e)
    return {
        "status": "done",
        "xuid": xuid,
        "message": "Xbox authenticated successfully. The refresh token has been saved. Run a sync to import your games.",
        "env_hint": f"You can also set XBOX_REFRESH_TOKEN={refresh_token} in your .env for persistence across container recreations.",
    }


@app.get("/api/xbox-360-debug")
async def xbox_360_debug(game_id: int):
    """Return raw contract v1 achievement API responses for a 360 game (use Pantheon game_id from /game/<id> URL)."""
    from app.xbox_auth import get_tokens, load_refresh_token
    from app.platforms.xbox import _xbl_headers, _ACH
    # Look up the Xbox title_id from the DB
    pool = await db.get_pool()
    async with pool.connection() as conn:
        row = await _fetchrow(conn, "SELECT platform_app_id FROM platform_games WHERE id = %s", game_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    title_id = row["platform_app_id"]
    refresh_token = config.XBOX_REFRESH_TOKEN or load_refresh_token()
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Xbox not configured")
    tokens = await get_tokens(refresh_token)
    xuid = tokens.xuid
    async with httpx.AsyncClient(timeout=30) as client:
        title_v1_resp = await client.get(
            f"{_ACH}/titles/{title_id}/achievements",
            params={"maxItems": 5},
            headers=_xbl_headers(tokens, contract="1"),
        )
        user_v1_resp = await client.get(
            f"{_ACH}/users/xuid({xuid})/achievements",
            params={"titleId": title_id, "maxItems": 5},
            headers=_xbl_headers(tokens, contract="1"),
        )
        user_v2_resp = await client.get(
            f"{_ACH}/users/xuid({xuid})/achievements",
            params={"titleId": title_id, "maxItems": 5},
            headers=_xbl_headers(tokens, contract="2"),
        )
        title_v2_resp = await client.get(
            f"{_ACH}/titles/{title_id}/achievements",
            params={"maxItems": 5},
            headers=_xbl_headers(tokens, contract="2"),
        )
    return {
        "game_id": game_id,
        "xbox_title_id": title_id,
        "title_v1_status": title_v1_resp.status_code,
        "title_v1_sample": title_v1_resp.json() if title_v1_resp.status_code == 200 else title_v1_resp.text,
        "user_v1_status": user_v1_resp.status_code,
        "user_v1_sample": user_v1_resp.json() if user_v1_resp.status_code == 200 else user_v1_resp.text,
        "user_v2_status": user_v2_resp.status_code,
        "user_v2_sample": user_v2_resp.json() if user_v2_resp.status_code == 200 else user_v2_resp.text,
        "title_v2_status": title_v2_resp.status_code,
        "title_v2_sample": title_v2_resp.json() if title_v2_resp.status_code == 200 else title_v2_resp.text,
    }


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



@app.get("/api/exophase-debug")
async def exophase_debug():
    """Debug Exophase integration: show games list fetch result and sample icon lookup."""
    from app.platforms.exophase import fetch_games_list, fetch_earned_icons, _to_slug

    if not config.EXOPHASE_PLAYER_ID:
        return {"error": "EXOPHASE_PLAYER_ID not configured"}

    access_token = config.EXOPHASE_ACCESS_TOKEN
    if not access_token:
        return {"error": "EXOPHASE_ACCESS_TOKEN not set — copy the ACCESS_TOKEN cookie from your browser on exophase.com"}

    pool = await db.get_pool()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            exo_games = await fetch_games_list(client, config.EXOPHASE_PLAYER_ID, access_token)
        except Exception as e:
            return {"error": f"Games list fetch failed: {e}"}

    xbox_360_games = [g for g in exo_games if g["is_360"]]
    all_games_slugs = {_to_slug(g["title"]): g["master_id"] for g in exo_games}
    xbox_360_slugs = {_to_slug(g["title"]): g["master_id"] for g in xbox_360_games}

    # Get Xbox games in our DB that have achievements with no icons
    async with pool.connection() as conn:
        db_rows = await _fetch(
            conn,
            """
            SELECT DISTINCT pg.name, COUNT(a.id) FILTER (WHERE a.icon_url IS NULL) AS missing_icons
            FROM platform_games pg
            JOIN achievements a ON a.platform_game_id = pg.id
            WHERE pg.platform = 'xbox'
            GROUP BY pg.name
            ORDER BY missing_icons DESC
            """,
        )

    match_results = []
    for row in db_rows:
        db_slug = _to_slug(row["name"])
        exo_slug = _EXOPHASE_TITLE_ALIASES.get(db_slug, db_slug)
        aliased = exo_slug != db_slug
        match_results.append({
            "db_game": row["name"],
            "slug": db_slug,
            "exo_slug": exo_slug if aliased else None,
            "missing_icons": row["missing_icons"],
            "exophase_match": all_games_slugs.get(exo_slug),
            "is_360_match": xbox_360_slugs.get(exo_slug),
        })

    return {
        "exophase_total_games": len(exo_games),
        "exophase_360_games": len(xbox_360_games),
        "exophase_360_titles": [g["title"] for g in xbox_360_games],
        "db_xbox_games_with_missing_icons": match_results,
    }


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    return FileResponse("app/static/index.html")


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
