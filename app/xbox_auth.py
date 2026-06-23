"""
Xbox Live authentication via Microsoft identity platform device code flow.

Setup (one-time):
  0. Register a free Azure app:
       portal.azure.com → App registrations → New registration
       - Supported account types: Personal Microsoft accounts only
         (or "Accounts in any org directory and personal accounts")
       - No redirect URI needed
       Then under Authentication → Advanced settings:
         Enable "Allow public client flows" → Yes → Save
       Copy the Application (client) ID and set XBOX_CLIENT_ID=<UUID> in your .env
  1. Hit GET /api/xbox-setup — get a user code and verification URL
  2. Open the verification URL in a browser, enter the user code, sign in
  3. Hit GET /api/xbox-setup-poll?device_code=<device_code> until status=done
  4. Done — refresh token is saved automatically
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
_MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

_CLIENT_ID = os.getenv("XBOX_CLIENT_ID", "")
_SCOPE = "XboxLive.signin offline_access"

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
    """Start device code flow. Returns dict with user_code, verification_uri, device_code, interval."""
    if not _CLIENT_ID:
        raise RuntimeError(
            "XBOX_CLIENT_ID is not set. Register a free Azure app at portal.azure.com "
            "(App registrations → New registration, Personal Microsoft accounts only, "
            "no redirect URI needed, then Authentication → Allow public client flows → Yes) "
            "then set XBOX_CLIENT_ID=<your app's client UUID>."
        )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _DEVICE_CODE_URL,
            data={"client_id": _CLIENT_ID, "scope": _SCOPE},
        )
        if not resp.is_success:
            raise RuntimeError(f"Device code request failed: {resp.text}")
    return resp.json()


async def poll_device_flow(device_code: str) -> str | None:
    """
    Poll once for token. Returns refresh token if done, None if still pending.
    Raises RuntimeError on terminal errors (expired, denied).
    """
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
    error = data.get("error")
    if error == "authorization_pending":
        return None
    if error == "slow_down":
        return None
    if error == "authorization_declined":
        raise RuntimeError("Sign-in was declined by the user.")
    if error == "expired_token":
        raise RuntimeError("Device code expired. Start the flow again.")
    if error:
        raise RuntimeError(f"Token error: {error} — {data.get('error_description', '')}")
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"No refresh_token in response: {data}")
    _save_refresh_token(refresh_token)
    return refresh_token


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
