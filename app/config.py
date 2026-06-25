import os
from dotenv import load_dotenv

load_dotenv()

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ID = os.getenv("STEAM_ID", "")

RA_USERNAME = os.getenv("RA_USERNAME", "")
RA_API_KEY = os.getenv("RA_API_KEY", "")
RA_TARGET_USER = os.getenv("RA_TARGET_USER", "") or RA_USERNAME

XBOX_CLIENT_ID = os.getenv("XBOX_CLIENT_ID", "")
XBOX_REFRESH_TOKEN = os.getenv("XBOX_REFRESH_TOKEN", "")

SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "12"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "0.4"))

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pantheon:pantheon@db:5432/pantheon",
)

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID", "")
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET", "")

SGDB_API_KEY = os.getenv("SGDB_API_KEY", "")

WARGAMING_APP_ID = os.getenv("WARGAMING_APP_ID", "")
WARGAMING_NICKNAME = os.getenv("WARGAMING_NICKNAME", "")
WARGAMING_REGION = os.getenv("WARGAMING_REGION", "eu")

EXOPHASE_PLAYER_ID = os.getenv("EXOPHASE_PLAYER_ID", "")
EXOPHASE_REMEMBERME = os.getenv("EXOPHASE_REMEMBERME", "")
EXOPHASE_XF_USER = os.getenv("EXOPHASE_XF_USER", "")
EXOPHASE_ACCESS_TOKEN = os.getenv("EXOPHASE_ACCESS_TOKEN", "")


def enabled_accounts() -> list[dict]:
    from app.xbox_auth import load_refresh_token
    accounts = []
    if STEAM_API_KEY and STEAM_ID:
        accounts.append({"platform": "steam", "external_id": STEAM_ID})
    if RA_USERNAME and RA_API_KEY:
        accounts.append({"platform": "retroachievements", "external_id": RA_TARGET_USER})
    if XBOX_REFRESH_TOKEN or load_refresh_token():
        accounts.append({"platform": "xbox", "external_id": "xbox"})
    if WARGAMING_APP_ID and WARGAMING_NICKNAME:
        accounts.append({"platform": "wargaming", "external_id": WARGAMING_NICKNAME})
    return accounts
