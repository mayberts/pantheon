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
    Use the stored rememberMeTicket to get a fresh session ticket + profileId.
    Returns (ticket, profile_id).
    """
    remember_me = _load_remember_me()
    if not remember_me:
        raise RuntimeError("Ubisoft not configured — run the setup flow at /api/ubisoft-setup")

    headers = {**_base_headers(), "Authorization": f"rm {remember_me}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_BASE}/v3/profiles/sessions",
            json={"rememberMe": True},
            headers=headers,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Ubisoft session refresh failed: HTTP {resp.status_code} — {resp.text[:300]}")

    data = resp.json()
    new_rm = data.get("rememberMeTicket", "")
    ticket = data.get("ticket", "")
    profile_id = data.get("profileId", "")
    if new_rm:
        _save_session(ticket, new_rm)
    return ticket, profile_id


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
