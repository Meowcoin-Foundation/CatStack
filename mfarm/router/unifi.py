"""UniFi Network backend — UDM, Cloud Key, USG.

Auth model: cookie-based session via /api/auth/login (UniFi OS). Port
forwards live at /proxy/network/api/s/{site}/rest/portforward.

Tested against UDM Pro Max running UniFi Network 9.x. Older Cloud Key
controllers without UniFi OS use /api/login instead — handled via the
`is_unifi_os` heuristic during login.

Required config keys:
  url       — controller URL, e.g. "https://192.168.1.1"
  username  — admin user with port-forward edit rights
  password  — that user's password
  site      — UniFi site name (default "default")
  verify_ssl — bool; default False (UDMs ship self-signed certs)
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests
import urllib3

from mfarm.router.base import (
    ApplyResult,
    ConfigError,
    ForwardRule,
    RouterBackend,
)

log = logging.getLogger(__name__)

# Suppress the InsecureRequestWarning when verify_ssl=False — UDMs ship
# self-signed certs and refusing to talk to them is unhelpful, but we don't
# want one warning per request flooding the log.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _rule_name(rig_name: str) -> str:
    """Stable, human-readable rule name. Matches `list_rules` lookup."""
    return f"CatStack: {rig_name}"


class UnifiBackend(RouterBackend):
    name = "unifi"
    requires_credentials = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._session: requests.Session | None = None
        self._is_unifi_os = True  # detected on first login
        self._lock = threading.Lock()  # serialize requests; UniFi can be flaky under concurrency

    # ── config / auth ────────────────────────────────────────────────

    def validate_config(self) -> None:
        for key in ("url", "username", "password"):
            if not self.config.get(key):
                raise ConfigError(f"unifi backend missing config key: {key}")
        if not str(self.config["url"]).startswith(("http://", "https://")):
            raise ConfigError("unifi 'url' must start with http:// or https://")

    def _site(self) -> str:
        return self.config.get("site") or "default"

    def _verify(self) -> bool:
        return bool(self.config.get("verify_ssl", False))

    def _api_base(self) -> str:
        """Return URL prefix for /api/s/<site> calls.

        UDM/UniFi OS: <url>/proxy/network/api/s/<site>
        Legacy controller: <url>/api/s/<site>
        """
        url = self.config["url"].rstrip("/")
        if self._is_unifi_os:
            return f"{url}/proxy/network/api/s/{self._site()}"
        return f"{url}/api/s/{self._site()}"

    def _login(self) -> requests.Session:
        """Establish a session. Detects UniFi OS vs legacy controller and
        sets self._is_unifi_os accordingly."""
        sess = requests.Session()
        sess.verify = self._verify()
        url = self.config["url"].rstrip("/")
        creds = {
            "username": self.config["username"],
            "password": self.config["password"],
            "remember": True,
        }
        # UniFi OS path first (UDM Pro Max); fall back to legacy /api/login
        # if the new endpoint 404s.
        try:
            r = sess.post(f"{url}/api/auth/login", json=creds, timeout=10)
            if r.status_code == 200:
                self._is_unifi_os = True
                # CSRF token comes back as a header; subsequent requests need
                # to echo it.
                csrf = r.headers.get("X-CSRF-Token") or r.headers.get("x-csrf-token")
                if csrf:
                    sess.headers["X-CSRF-Token"] = csrf
                return sess
            if r.status_code == 404:
                raise FileNotFoundError("not unifi-os")
            r.raise_for_status()
        except (FileNotFoundError, requests.exceptions.RequestException) as e:
            if isinstance(e, FileNotFoundError) or "404" in str(e):
                # Legacy controller path
                r = sess.post(f"{url}/api/login", json=creds, timeout=10)
                r.raise_for_status()
                self._is_unifi_os = False
                return sess
            raise
        return sess

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._login()
        return self._session

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        """Make an API request. On 401, re-login once and retry."""
        with self._lock:
            sess = self._ensure_session()
            url = f"{self._api_base()}{path}"
            kw.setdefault("timeout", 15)
            r = sess.request(method, url, **kw)
            if r.status_code == 401:
                # Session expired — log back in and retry once
                self._session = None
                sess = self._ensure_session()
                r = sess.request(method, url, **kw)
            return r

    # ── RouterBackend interface ──────────────────────────────────────

    def test_connection(self) -> ApplyResult:
        try:
            self.validate_config()
            r = self._request("GET", "/rest/portforward")
            r.raise_for_status()
            count = len(r.json().get("data", []))
            return ApplyResult(
                ok=True,
                messages=[f"connected to UniFi ({'UDM/UniFi OS' if self._is_unifi_os else 'legacy controller'}); {count} existing port-forward rule(s)"],
            )
        except ConfigError as e:
            return ApplyResult(ok=False, messages=[f"config error: {e}"])
        except requests.exceptions.SSLError as e:
            return ApplyResult(
                ok=False,
                messages=[f"TLS error: {e}. Set verify_ssl=False or install the UDM CA."],
            )
        except requests.exceptions.ConnectionError as e:
            return ApplyResult(ok=False, messages=[f"can't reach controller: {e}"])
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            return ApplyResult(ok=False, messages=[f"HTTP {code} from controller: {e}"])
        except Exception as e:
            return ApplyResult(ok=False, messages=[f"{type(e).__name__}: {e}"])

    def list_rules(self) -> list[ForwardRule]:
        try:
            r = self._request("GET", "/rest/portforward")
            r.raise_for_status()
            out: list[ForwardRule] = []
            for row in r.json().get("data", []):
                name = row.get("name", "")
                if not name.startswith("CatStack: "):
                    continue
                rig = name[len("CatStack: "):]
                lo, hi = _parse_port_range(row.get("dst_port", ""))
                out.append(ForwardRule(
                    rig_name=rig,
                    internal_ip=row.get("fwd", ""),
                    port_lo=lo,
                    port_hi=hi,
                    protocol=row.get("proto", "tcp_udp"),
                ))
            return out
        except Exception as e:
            log.warning("unifi list_rules failed: %s", e)
            return []

    def _find_rule_id(self, rig_name: str) -> str | None:
        target = _rule_name(rig_name)
        try:
            r = self._request("GET", "/rest/portforward")
            r.raise_for_status()
            for row in r.json().get("data", []):
                if row.get("name") == target:
                    return row.get("_id")
        except Exception:
            pass
        return None

    def apply_rule(self, rule: ForwardRule) -> ApplyResult:
        try:
            self.validate_config()
        except ConfigError as e:
            return ApplyResult(ok=False, messages=[f"config error: {e}"])

        body = {
            "name": _rule_name(rule.rig_name),
            "enabled": True,
            "src": "any",
            "dst_port": _format_port_range(rule.port_lo, rule.port_hi),
            "fwd": rule.internal_ip,
            "fwd_port": _format_port_range(rule.port_lo, rule.port_hi),
            "proto": rule.protocol,
            "log": False,
            "pfwd_interface": "wan",
        }
        try:
            existing = self._find_rule_id(rule.rig_name)
            if existing:
                r = self._request("PUT", f"/rest/portforward/{existing}", json=body)
                r.raise_for_status()
                return ApplyResult(
                    ok=True,
                    messages=[f"updated UniFi rule for {rule.rig_name} → {rule.internal_ip}:{rule.port_lo}-{rule.port_hi}"],
                )
            r = self._request("POST", "/rest/portforward", json=body)
            r.raise_for_status()
            return ApplyResult(
                ok=True,
                messages=[f"created UniFi rule for {rule.rig_name} → {rule.internal_ip}:{rule.port_lo}-{rule.port_hi}"],
            )
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text if e.response is not None else ""
            return ApplyResult(ok=False, messages=[f"HTTP {code} applying rule for {rule.rig_name}: {detail}"])
        except Exception as e:
            return ApplyResult(ok=False, messages=[f"{type(e).__name__}: {e}"])

    def remove_rule(self, rig_name: str) -> ApplyResult:
        try:
            existing = self._find_rule_id(rig_name)
            if not existing:
                return ApplyResult(ok=True, messages=[f"no UniFi rule for {rig_name} (already absent)"])
            r = self._request("DELETE", f"/rest/portforward/{existing}")
            r.raise_for_status()
            return ApplyResult(ok=True, messages=[f"removed UniFi rule for {rig_name}"])
        except Exception as e:
            return ApplyResult(ok=False, messages=[f"{type(e).__name__}: {e}"])


# ── helpers ──────────────────────────────────────────────────────────

def _format_port_range(lo: int, hi: int) -> str:
    """UniFi accepts a single port as "X" or a range as "X-Y"."""
    return str(lo) if lo == hi else f"{lo}-{hi}"


def _parse_port_range(s: str) -> tuple[int, int]:
    if not s:
        return (0, 0)
    if "-" in s:
        a, b = s.split("-", 1)
        return (int(a), int(b))
    n = int(s)
    return (n, n)
