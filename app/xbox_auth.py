"""
Xbox Live authentication via Microsoft Live OAuth2 auth code flow.

Setup (one-time):
  0. Register a free Azure app:
       portal.azure.com → App registrations → New registration
       - Supported account types: Personal Microsoft accounts only
       - Redirect URI platform: Mobile and desktop applications
       - Redirect URI value: https://login.live.com/oauth20_desktop.srf
       Copy the Application (client) ID and set XBOX_CLIENT_ID=<that UUID> in your .env
  1. Hit GET /api/xbox-setup — get a sign-in URL
  2. Open the URL in a browser, sign in with your Microsoft account
  3. After sign-in you'll be redirected to a blank page — copy the full URL from the address bar
  4. Hit GET /api/xbox-setup-complete?redirect_url=<paste URL here>
  5. Done — refresh token is saved automatically
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode

import httpx

log = logging.getLogger(__name__)

_AUTH_URL = "https://login.live.com/oauth20_authorize.srf"
_MS_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
_XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

_CLIENT_ID = os.getenv("XBOX_CLIENT_ID", "")
_SCOPE = "XboxLive.signin XboxLive.offline_access"

_TOKEN_FILE = Path("/data/xbox_refresh_token.txt")


@dataclass
class XboxTokens:
    xsts_token: str
    user_hash: str
    xuid: str

    @property
    def auth_header(self) -> str:
        return f"XBL3.0 x={self.user_hash};{self.xsts_token}"


def get_auth_url() -> str:
    """Return the URL the user must open in a browser to sign in."""
    if not _CLIENT_ID:
        raise RuntimeError(
            "XBOX_CLIENT_ID is not set. Register a free Azure app at portal.azure.com "
            "(App registrations → New registration, Personal Microsoft accounts only, "
            "Mobile/desktop redirect URI: https://login.live.com/oauth20_desktop.srf) "
            "then set XBOX_CLIENT_ID=<your app's client UUID>."
        )
    params = urlencode({
        "client_id": _CLIENT_ID,
        "response_type": "code",
        "prompt": "select_account",
        "scope": _SCOPE,
        "redirect_uri": _REDIRECT_URI,
    })
    return f"{_AUTH_URL}?{params}"


async def exchange_code(redirect_url: str) -> str:
    """Extract auth code from the redirect URL and exchange it for a refresh token."""
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    code = (params.get("code") or [""])[0]
    if not code:
        raise RuntimeError(
            f"No 'code' parameter found in the URL. Make sure you copied the full redirect URL. Got: {redirect_url[:200]}"
        )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _MS_TOKEN_URL,
            data={
                "client_id": _CLIENT_ID,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _REDIRECT_URI,
                "scope": _SCOPE,
            },
        )
        if not resp.is_success:
            raise RuntimeError(f"Token exchange failed: {resp.text}")
        data = resp.json()
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
