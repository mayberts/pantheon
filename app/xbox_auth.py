"""
Xbox Live authentication via Microsoft Live device code flow.

Setup (one-time):
  1. Hit GET /api/xbox-setup and follow the instructions to sign in
  2. The app saves the refresh token automatically

No Azure app registration needed.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Microsoft Live auth endpoints (used by Xbox apps, not Azure AD)
_MS_DEVICE_URL = "https://login.live.com/oauth20_connect.srf"
_MS_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
_XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

# Well-known public client ID used by Xbox community tools
_CLIENT_ID = "000000004C12AE6F"
_SCOPE = "XboxLive.signin offline_access"

# Path where refresh token is persisted between container restarts
_TOKEN_FILE = Path("/data/xbox_refresh_token.txt")


@dataclass
class XboxTokens:
    xsts_token: str
    user_hash: str
    xuid: str

    @property
    def auth_header(self) -> str:
        return f"XBL3.0 x={self.user_hash};{self.xsts_token}"


async def start_device_flow() -> dict:
    """Start device code flow. Returns {user_code, verification_uri, device_code, interval, expires_in}."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _MS_DEVICE_URL,
            data={"client_id": _CLIENT_ID, "scope": _SCOPE},
        )
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()


async def poll_device_flow(device_code: str) -> str | None:
    """Poll for token. Returns refresh_token on success, None if still pending."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _MS_TOKEN_URL,
            data={
                "client_id": _CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )
    data = resp.json()
    if "refresh_token" in data:
        return data["refresh_token"]
    error = data.get("error", "")
    if error == "authorization_pending":
        return None
    raise RuntimeError(f"Device flow error: {error} — {data.get('error_description', '')}")


async def _get_ms_access_token(refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _MS_TOKEN_URL,
            data={
                "client_id": _CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": _SCOPE,
            },
        )
        if not resp.is_success:
            raise RuntimeError(f"Token refresh failed: {resp.text}")
        data = resp.json()
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        _save_refresh_token(new_refresh)
    return data["access_token"]


async def _get_xbl_token(ms_access_token: str) -> tuple[str, str]:
    """Returns (xbl_token, user_hash)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _XBL_URL,
            json={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"d={ms_access_token}",
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    token = data["Token"]
    user_hash = data["DisplayClaims"]["xui"][0]["uhs"]
    return token, user_hash


async def _get_xsts_token(xbl_token: str) -> tuple[str, str, str]:
    """Returns (xsts_token, user_hash, xuid)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _XSTS_URL,
            json={
                "Properties": {
                    "SandboxId": "RETAIL",
                    "UserTokens": [xbl_token],
                },
                "RelyingParty": "http://xboxlive.com",
                "TokenType": "JWT",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code == 401:
            err = resp.json().get("XErr", 0)
            if err == 2148916233:
                raise RuntimeError("This Microsoft account has no Xbox profile. Create one at xbox.com first.")
            if err == 2148916238:
                raise RuntimeError("Child account — parental consent required.")
            raise RuntimeError(f"XSTS auth failed: XErr={err}")
        resp.raise_for_status()
        data = resp.json()
    xsts_token = data["Token"]
    claims = data["DisplayClaims"]["xui"][0]
    user_hash = claims["uhs"]
    xuid = claims.get("xid", "")
    return xsts_token, user_hash, xuid


async def get_tokens(refresh_token: str) -> XboxTokens:
    """Exchange a refresh token for live XSTS tokens ready for API calls."""
    ms_token = await _get_ms_access_token(refresh_token)
    xbl_token, _ = await _get_xbl_token(ms_token)
    xsts_token, user_hash, xuid = await _get_xsts_token(xbl_token)
    return XboxTokens(xsts_token=xsts_token, user_hash=user_hash, xuid=xuid)


def load_refresh_token() -> str | None:
    """Load persisted refresh token from file."""
    try:
        return _TOKEN_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _save_refresh_token(token: str) -> None:
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token)
    except Exception as e:
        log.warning("Could not persist refresh token to %s: %s", _TOKEN_FILE, e)
