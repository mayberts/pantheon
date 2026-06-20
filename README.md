# Pantheon

A self-hosted, single-user cross-platform achievement aggregator. Your own
Trophies Hunter / Exophase, running on your own box. Phase 1 ships Steam and
RetroAchievements; the worker layer is built so PSN, Xbox and GOG drop in later.

## Stack

FastAPI + APScheduler in one container, Postgres for storage, a vanilla-JS
"service record" dashboard. No login, no leaderboards, no multi-tenancy: it is
yours alone.

## Quick start

```bash
cp .env.example .env
# fill in STEAM_API_KEY + STEAM_ID and/or RA_USERNAME + RA_API_KEY
docker compose up -d --build
```

Open `http://<host>:8744`. On first run it seeds your accounts from `.env`,
applies the schema, and kicks off an initial sync automatically. The first
Steam sync walks your whole library (two API calls per game with achievements),
so it can take a few minutes. After that it refreshes every `SYNC_INTERVAL_HOURS`,
and the dashboard has a manual **Sync now** button.

## Getting keys

- **Steam API key**: https://steamcommunity.com/dev/apikey
- **SteamID (64-bit)**: https://steamid.io — paste your profile URL, copy `steamID64`
- **RetroAchievements**: Settings -> Keys. `RA_USERNAME` owns the key,
  `RA_TARGET_USER` is the record to pull (same as username for yourself).

Your Steam profile and game details must be **public** for the Web API to
return achievement data.

## API

| Method | Path           | Purpose                                   |
|--------|----------------|-------------------------------------------|
| GET    | `/api/summary` | Overall + per-platform completion         |
| GET    | `/api/games`   | Library (`?sort=completion\|recent\|playtime\|name`) |
| POST   | `/api/sync`    | Trigger a full sync (202, or 409 if busy) |
| GET    | `/api/status`  | Linked accounts + last 10 sync runs       |

## Adding a platform

The data model is platform-agnostic, so a new platform is one worker file:

1. Create `app/platforms/<name>.py` with a class subclassing `Platform`
   (see `base.py`), implementing `sync(self, account, conn)` and mapping the
   platform's API onto the `upsert_*` helpers.
2. Register it in `app/platforms/__init__.py`.
3. Add its credentials to `config.py` / `.env` and to `config.enabled_accounts()`.

Recommended next workers and the libraries to wrap:

- **PSN** — `psn-api` (Node/TS). Auth is an NPSSO token exchanged for
  access/refresh tokens. The NPSSO expires roughly every two months, so store
  the refresh token and add a re-auth path. Easiest as a tiny Node sidecar
  container that exposes JSON to the Python app, or port the token flow.
- **Xbox** — either the OpenXBL gateway (`xbl.io`, free tier 150 req/hr) for a
  simple REST key, or `xbox-webapi-python` to do the XSTS auth flow fully
  in-house with no third-party dependency.
- **GOG** — unofficial `embed.gog.com` achievement endpoints, auth-token based.

Once two or more platforms feed in, wire up `app/igdb.py` (stub) so the same
game across platforms maps to one IGDB id and you get true deduplication.

## Notes

- Phase-1 RetroAchievements sync records game-level completion. Per-achievement
  rows are a per-game call; the hook is `RetroAchievements._sync_game_detail`.
- Steam per-achievement icons are skipped in phase 1 to limit API calls
  (`GetSchemaForGame` would add them).
- Keys live in `.env` for simplicity. When you add refreshable tokens (PSN/Xbox),
  store those encrypted in `linked_accounts.credentials_enc` rather than env.

## Version control and updates

Pantheon is meant to live in a Git repo so changes flow as commits, not file copies.

### One-time: publish to GitHub

Easiest from VS Code on Windows: open the project folder, then Source Control ->
Publish to Branch / "Publish to GitHub" and pick a private repo. VS Code creates the
repo and pushes, honouring `.gitignore`.

Equivalent from a terminal:

```bash
git init
git add .
git commit -m "Initial Pantheon"
git branch -M main
git remote add origin git@github.com:<you>/pantheon.git
git push -u origin main
```

`.env` and `pgdata/` are gitignored, so secrets and the database never get committed.

### Tier 1: pull and rebuild on the box

Simplest workflow. Edit and push from anywhere, then on the Unraid host:

```bash
chmod +x update.sh   # once
./update.sh          # git pull --ff-only + docker compose up -d --build + prune
```

### Tier 2: build in CI, pull the image (no build on the box)

`.github/workflows/build.yml` builds the image on every push to `main` and pushes it to
GHCR. The server then only pulls the finished image:

1. Push to `main`; GitHub Actions publishes `ghcr.io/<you>/pantheon:latest`.
2. On the host, deploy with `docker-compose.ghcr.yml` (set `<youruser>` first, and
   `docker login ghcr.io` once if the package is private).
3. Update with `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`,
   or just hit **Update Stack** in Compose Manager Plus, which pulls automatically.

Gitea/Forgejo equivalents (Actions + built-in container registry) work the same way.

## Local development (Windows + VS Code)

Develop on Windows, deploy by pushing. The two checkouts (your PC and the Unraid
host) share one remote.

1. Clone the repo on Windows and open the folder in VS Code. It will offer the
   recommended extensions (Python, Docker, YAML, GitLens).
2. `copy .env.example .env` and add your keys (this `.env` stays local, gitignored).
3. Run it locally on Docker Desktop:
   `docker compose up -d --build`, then open `http://localhost:8744`. This builds
   from source into a named volume, separate from any server deployment.
4. Commit and push. VS Code's Source Control panel handles commit/push; sign into
   GitHub via VS Code or use an SSH key.
5. Deploy: on the Unraid host run `./update.sh` (Tier 1) or click Update Stack
   (Tier 2).

`.gitattributes` pins shell and code files to LF so the Windows checkout never
breaks the Linux-side scripts.
