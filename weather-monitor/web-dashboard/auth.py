"""Authentication gate using streamlit-authenticator.

Loads credentials and cookie settings from `st.secrets["auth"]`, builds a
single cached `Authenticate` instance per session, and exposes:

- `require_login()` — render the login form and `st.stop()` if the user is
  not authenticated. Call once at the top of `streamlit_app.py`.
- `logout_sidebar()` — render a "Signed in as …" + logout button in the
  sidebar. Safe to call from anywhere after `require_login()`.

If `st.secrets["auth"]` is missing or `auth.enabled = false`, the gate is a
no-op so the dashboard still runs (useful in early dev).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

import streamlit as st
import streamlit_authenticator as stauth

_AUTH_INSTANCE_KEY = "_weather_dashboard_authenticator"


def _to_plain(obj: Any) -> Any:
    """Deep-convert `st.secrets` AttrDicts to plain Python dict/list.

    streamlit-authenticator mutates the credentials dict in place (it writes
    hashed passwords back into the structure on first login) — `st.secrets`
    objects are read-only, so we must hand it a mutable copy.

    NOTE: we check for `Mapping`, not `dict`. Streamlit's `AttrDict` (the type
    nested values inside `st.secrets` come back as) does not inherit from
    `dict`, only from `collections.abc.Mapping`. Using `isinstance(obj, dict)`
    here silently skips the recursion and leaves AttrDicts in the result.
    """
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(x) for x in obj]
    return obj


def _build_authenticator() -> Optional[stauth.Authenticate]:
    """Construct the Authenticate instance from secrets. Returns None when
    auth is disabled or not configured (caller treats that as 'no gate')."""
    try:
        raw = st.secrets["auth"]
    except (KeyError, FileNotFoundError, AttributeError):
        return None

    cfg = _to_plain(raw)
    if not cfg.get("enabled", True):
        return None

    credentials = cfg.get("credentials")
    cookie_name = cfg.get("cookie_name", "weather_dashboard_auth")
    cookie_key = cfg.get("cookie_key")
    expiry_days = int(cfg.get("cookie_expiry_days", 1))
    auto_hash = bool(cfg.get("auto_hash", True))

    if not credentials or not isinstance(credentials, dict) \
            or not credentials.get("usernames"):
        st.error(
            "`auth.credentials.usernames` is missing or empty in "
            "`.streamlit/secrets.toml`. Add at least one user, e.g.:\n\n"
            "```toml\n"
            "[auth.credentials.usernames.admin]\n"
            'email      = "you@example.com"\n'
            'first_name = "Admin"\n'
            'last_name  = "User"\n'
            'password   = "change-me"\n'
            'roles      = ["admin"]\n'
            "```"
        )
        st.stop()
    if not cookie_key:
        st.error(
            "`auth.cookie_key` is missing from secrets.toml. Generate one with:\n"
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`"
        )
        st.stop()

    return stauth.Authenticate(
        credentials,
        cookie_name,
        cookie_key,
        expiry_days,
        auto_hash=auto_hash,
    )


def _get_authenticator() -> Optional[stauth.Authenticate]:
    """Cached accessor. The instance is built once per Streamlit session so
    the in-memory hashed-password state persists across reruns."""
    if _AUTH_INSTANCE_KEY not in st.session_state:
        st.session_state[_AUTH_INSTANCE_KEY] = _build_authenticator()
    return st.session_state[_AUTH_INSTANCE_KEY]


def require_login() -> None:
    """Halt the script with a login form unless the user is authenticated.

    Safe to call before `st.set_page_config` has finished — but in practice
    we call it right after, so the page title is already set.
    """
    authenticator = _get_authenticator()
    if authenticator is None:
        return  # auth disabled or not configured

    if st.session_state.get("authentication_status") is True:
        return  # already signed in this session

    # Show a friendly title above the form so the page isn't blank chrome.
    st.title("🔒 Sign in to Weather Monitor")
    st.caption("Enter your credentials to access the dashboard.")

    try:
        authenticator.login(location="main")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Login error: {exc}")
        st.stop()

    status = st.session_state.get("authentication_status")
    if status is True:
        # Successful login on this run — rerun so the page renders cleanly
        # without the login form artefacts left behind.
        st.rerun()
    if status is False:
        st.error("Username or password is incorrect.")
    else:
        st.info("Please enter your username and password.")
    st.stop()


def logout_sidebar() -> None:
    """Render a sign-in indicator + logout button in the sidebar.

    No-op when auth is disabled or the user isn't signed in."""
    authenticator = _get_authenticator()
    if authenticator is None:
        return
    if st.session_state.get("authentication_status") is not True:
        return

    with st.sidebar:
        name = (
            st.session_state.get("name")
            or st.session_state.get("username")
            or "user"
        )
        st.caption(f"Signed in as **{name}**")
        authenticator.logout(button_name="Log out", location="sidebar")
