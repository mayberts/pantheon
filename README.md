# Pantheon

A self-hosted, single-user cross-platform achievement aggregator. Track achievements across Steam, Xbox, and RetroAchievements in one dashboard — your own Exophase, running on your own box.

## Stack

FastAPI + APScheduler in one container, Postgres for storage, a vanilla-JS dashboard. No login, no leaderboards, no multi-tenancy: it is yours alone.

## Quick start

```bash
cp .env.example .env
# Fill in credentials for the platforms you want (see below)
docker compose up -d --build
```

Open `http://<host>:8744`. On first run it seeds your accounts from `.env`, applies the schema, and kicks off an initial sync automatically. After that it refreshes every `SYNC_INTERVAL_HOURS`, and the dashboard has a manual **Sync now** button.

## Supported platforms

| Platform | Achievements | Playtime | Notes |
|---|---|---|---|
| **Steam** | ✅ | ✅ | Full unlock dates, rarity |
| **Xbox** (One/Series) | ✅ | ✅ (if available) | Requires device-code auth via `/api/xbox-setup` |
| **Xbox 360** | ✅ earned | ❌ | Locked achievements via Exophase import |
| **RetroAchievements** | ✅ | ❌ | |

## Getting credentials

### Steam
- **API key**: https://steamcommunity.com/dev/apikey
- **SteamID (64-bit)**: https://steamid.io — paste your profile URL, copy `steamID64`
- Your Steam profile and game details must be **public**

### Xbox
1. Set `XBOX_CLIENT_ID` in `.env` (register an app at https://portal.azure.com)
2. Start the app, then open `http://<host>:8744/api/xbox-setup` in your browser
3. Follow the device-code flow — `XBOX_REFRESH_TOKEN` is saved automatically

### RetroAchievements
- Settings → Keys on retroachievements.org
- `RA_TARGET_USER` defaults to `RA_USERNAME` if left blank

### IGDB (optional — portrait cover art fallback)
- Create a Twitch app at https://dev.twitch.tv/console/apps (category: Game Integration)
- Add `IGDB_CLIENT_ID` and `IGDB_CLIENT_SECRET` to `.env`

### SteamGridDB (optional — landscape cover art, recommended)
- Get your API key at https://www.steamgriddb.com/profile/preferences/api
- Add `SGDB_API_KEY` to `.env`
- After deploying, trigger a backfill: `POST /api/sgdb-refresh`
- Use the **Change Cover** button on any game detail page to manually pick or fix a cover

### Exophase (optional — Xbox 360 locked achievements)
- Used to import locked achievement lists for Xbox 360 games that the Microsoft API won't return
- Extract credentials from your browser session on exophase.com (see browser console snippet)

## Cover art priority

For non-Steam games, covers are resolved in this order:
1. **SteamGridDB** landscape grid (460×215 or 920×430)
2. **IGDB** portrait cover (cropped to landscape)
3. **Platform icon** (Xbox tile image)

The **Change Cover** button on each game's detail page lets you search SGDB by name or game ID and save a specific image.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/summary` | Overall + per-platform stats including playtime |
| GET | `/api/games` | Library (`?sort=completion\|recent\|playtime\|name&platform=xbox\|steam\|retroachievements&completion=completed\|in_progress\|not_started`) |
| GET | `/api/games/{id}` | Game detail with achievements and rarity |
| POST | `/api/sync` | Trigger a full sync (202, or 409 if busy) |
| GET | `/api/status` | Linked accounts + last 10 sync runs |
| GET | `/api/xbox-setup` | Start Xbox device-code auth flow |
| POST | `/api/igdb-refresh` | Re-run IGDB enrichment (`?platform=xbox` to reset failed Xbox lookups) |
| POST | `/api/sgdb-refresh` | Re-run SteamGridDB cover enrichment |
| GET | `/api/sgdb-search` | Search SGDB by name or game ID (`?q=`) |
| POST | `/api/sgdb-set` | Save a specific SGDB cover URL to a game |
| POST | `/api/exophase-import-icons` | Import locked achievements from Exophase JSON |

## Version control and updates

### One-time: publish to GitHub

```bash
git init
git add .
git commit -m "Initial Pantheon"
git branch -M main
git remote add origin git@github.com:<you>/pantheon.git
git push -u origin main
```

`.env` and `pgdata/` are gitignored — secrets and the database are never committed.

### Tier 1: pull and rebuild on the box

```bash
chmod +x update.sh   # once
./update.sh          # git pull --ff-only + docker compose up -d --build + prune
```

### Tier 2: build in CI, pull the image

`.github/workflows/build.yml` builds on every push to `main` and pushes to GHCR. On the host:

1. Push to `main` — GitHub Actions publishes `ghcr.io/<you>/pantheon:latest`
2. Deploy with `docker-compose.ghcr.yml` (set `<youruser>` first, `docker login ghcr.io` once if private)
3. Update: `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`

## Local development

1. Clone and open in VS Code
2. `cp .env.example .env` and fill in your keys
3. `docker compose up -d --build` → open `http://localhost:8744`
4. Commit and push; deploy with `./update.sh` on the server

`.gitattributes` pins shell and code files to LF so Windows checkouts don't break Linux scripts.
