"""
Ubisoft Connect authentication via email/password + 2FA (TOTP or email code).

Setup (one-time):
  1. Hit POST /api/ubisoft-setup with {"email": "...", "password": "..."}
  2. If 2FA required, you get a two_factor_ticket back
  3. Hit POST /api/ubisoft-setup/verify with {"ticket": "...", "code": "..."}
  4. Done — rememberMeTicket is saved automatically for future syncs
"""

import logging
import os
from base64 import b64encode
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_BASE = "https://public-ubiservices.ubi.com"
_APP_ID = "e3d5ea9e-50bd-43b7-88bf-39794f4e3d40"  # Ubisoft PC client app ID
_TOKEN_FILE = Path("/data/ubisoft_remember_me.txt")
_SESSION_FILE = Path("/data/ubisoft_session.txt")


def _base_headers(app_id: str = _APP_ID) -> dict:
    return {
        "Ubi-AppId": app_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "UbiServices_SDK_2019.Release.27_PC64_ansi_static",
    }


async def start_auth(email: str, password: str) -> dict:
    """
    Initiate authentication with email + password.
    Returns either:
      {"status": "done", "profile_id": ..., "user_id": ..., "ticket": ...}
      {"status": "2fa_required", "two_factor_ticket": ..., "method": "TOTP"|"EMAIL"}
    """
    creds = b64encode(f"{email}:{password}".encode()).decode()
    headers = {**_base_headers(), "Authorization": f"Basic {creds}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_BASE}/v3/profiles/sessions",
            json={"rememberMe": True},
            headers=headers,
        )

    if resp.status_code == 200:
        data = resp.json()
        _save_session(data.get("ticket", ""), data.get("rememberMeTicket", ""))
        return {
            "status": "done",
            "profile_id": data.get("profileId"),
            "user_id": data.get("userId"),
        }

    if resp.status_code == 409:
        data = resp.json()
        two_fa_ticket = data.get("twoFactorAuthenticationTicket")
        if two_fa_ticket:
            # Determine 2FA method — prefer TOTP if available
            inline_code = data.get("inlineAuthenticationMethods") or []
            method = "TOTP" if any(m.get("type") == "Totp" for m in inline_code) else "EMAIL"
            return {
                "status": "2fa_required",
                "two_factor_ticket": two_fa_ticket,
                "method": method,
            }

    raise RuntimeError(f"Ubisoft auth failed: HTTP {resp.status_code} — {resp.text[:300]}")


async def complete_2fa(two_factor_ticket: str, code: str) -> dict:
    """
    Complete 2FA with the given code.
    Returns {"status": "done", "profile_id": ..., "user_id": ...}
    """
    headers = {
        **_base_headers(),
        "Authorization": f"ubi_2fa_v1 t={two_factor_ticket}",
        "Ubi-2FaCode": code,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_BASE}/v3/profiles/sessions",
            json={"rememberMe": True},
            headers=headers,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Ubisoft 2FA failed: HTTP {resp.status_code} — {resp.text[:300]}")

    data = resp.json()
    _save_session(data.get("ticket", ""), data.get("rememberMeTicket", ""))
    return {
        "status": "done",
        "profile_id": data.get("profileId"),
        "user_id": data.get("userId"),
    }


async def refresh_session() -> tuple[str, str]:
    """
    Return (session_ticket, profile_id) for API calls.

    The stored token may be either:
      - a rememberMeTicket (from the setup flow) → renew it with `rm` auth,
        which yields a fresh session ticket + profileId directly
      - a session ticket (JWE grabbed from the browser's localStorage,
        starts with "ewog") → use it directly as `Ubi_v1 t=`; the profileId
        is encrypted inside the JWE so we look it up via the /profiles/me API
    """
    stored = _load_remember_me()
    if not stored:
        raise RuntimeError("Ubisoft not configured — run the setup flow at /api/ubisoft-setup")

    is_session_ticket = stored.startswith("ewog")

    async with httpx.AsyncClient(timeout=20) as client:
        if not is_session_ticket:
            # rememberMeTicket → renew to get a session ticket + profileId
            headers = {**_base_headers(), "Authorization": f"rm {stored}"}
            resp = await client.post(
                f"{_BASE}/v3/profiles/sessions",
                json={"rememberMe": True},
                headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Ubisoft session refresh failed: HTTP {resp.status_code} — {resp.text[:200]}")
            data = resp.json()
            new_rm = data.get("rememberMeTicket", "")
            ticket = data.get("ticket", "")
            if new_rm:
                _save_session(ticket, new_rm)
            return ticket, data.get("profileId", "")

        # Session ticket → use directly; fetch profileId from a "me" endpoint
        headers = {**_base_headers(), "Authorization": f"Ubi_v1 t={stored}"}
        profile_id = await _lookup_profile_id(client, headers)
        return stored, profile_id


async def _lookup_profile_id(client: httpx.AsyncClient, headers: dict) -> str:
    """Resolve the current account's profileId using a valid session ticket."""
    for url in (
        f"{_BASE}/v3/profiles/me",
        f"{_BASE}/v3/users/me",
        f"{_BASE}/v3/profiles",
    ):
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # /v3/profiles returns {"profiles": [{...}]}; the others return a flat object
            profile = data
            if isinstance(data.get("profiles"), list) and data["profiles"]:
                profile = data["profiles"][0]
            pid = profile.get("profileId") or profile.get("userId")
            if pid:
                return pid
        if resp.status_code == 401:
            raise RuntimeError(
                "Ubisoft session ticket expired or invalid (401). Grab a fresh ticket from "
                "connect.ubisoft.com → DevTools → Local storage and re-save it."
            )
    raise RuntimeError("Could not resolve Ubisoft profileId from any /me endpoint")


def load_remember_me() -> str | None:
    return _load_remember_me()


def _load_remember_me() -> str | None:
    try:
        return _TOKEN_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _save_session(ticket: str, remember_me: str) -> None:
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if remember_me:
            _TOKEN_FILE.write_text(remember_me)
        if ticket:
            _SESSION_FILE.write_text(ticket)
    except Exception as e:
        log.warning("Could not persist Ubisoft tokens: %s", e)
