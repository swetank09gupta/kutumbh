"""
Central configuration — reads from .env and config/accounts.yml.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# IMAP host map
# ---------------------------------------------------------------------------
IMAP_HOSTS: dict[str, tuple[str, int]] = {
    "gmail":   ("imap.gmail.com", 993),
    "yahoo":   ("imap.mail.yahoo.com", 993),
    "outlook": ("imap-mail.outlook.com", 993),
    "hotmail": ("imap-mail.outlook.com", 993),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = "postgresql://financeuser:financepass@postgres:5432/financedb"

    # ── Email credentials (4 accounts: user + wife × gmail + yahoo) ───────
    user_gmail: str = ""
    user_gmail_app_password: str = ""
    user_yahoo: str = ""
    user_yahoo_app_password: str = ""
    wife_gmail: str = ""
    wife_gmail_app_password: str = ""
    wife_yahoo: str = ""
    wife_yahoo_app_password: str = ""

    # ── Scheduler ─────────────────────────────────────────────────────────
    email_check_interval_hours: int = 6

    # ── Grafana ───────────────────────────────────────────────────────────
    grafana_user: str = "admin"
    grafana_password: str = "financeadmin"

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def get_email_accounts(self) -> list[dict[str, Any]]:
        """
        Returns a list of email account dicts, preferring config/accounts.yml
        if it exists, otherwise falling back to env-var pairs.
        Skips any account whose address or password looks like a placeholder.
        """
        import os, re

        def resolve(v: str) -> str:
            """Expand ${VAR} placeholders and strip surrounding quotes."""
            v = re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v))
            return v.strip().strip("'\"")

        def is_placeholder(address: str, password: str) -> bool:
            """Return True if this looks like an unfilled placeholder."""
            placeholder_patterns = [
                r"^xxxx", r"your_email", r"wife_email", r"@example\.",
            ]
            for pat in placeholder_patterns:
                if re.search(pat, address, re.I) or re.search(pat, password, re.I):
                    return True
            # Empty after resolving
            if not address or not password:
                return True
            return False

        cfg = Path("config/accounts.yml")
        if cfg.exists():
            data = yaml.safe_load(cfg.read_text())
            raw = data.get("email_accounts", [])
            accounts = []
            for acc in raw:
                resolved = {k: resolve(v) for k, v in acc.items()}
                addr = resolved.get("address", "")
                pwd  = resolved.get("app_password", "")
                if is_placeholder(addr, pwd):
                    import logging
                    logging.getLogger(__name__).info(
                        "Skipping unconfigured email account: %s", addr or "(empty)")
                    continue
                accounts.append(resolved)
            return accounts

        accounts: list[dict[str, Any]] = []
        for owner_id, owner_name, pairs in [
            ("user", "User",
             [("gmail", self.user_gmail, self.user_gmail_app_password),
              ("yahoo", self.user_yahoo, self.user_yahoo_app_password)]),
            ("wife", "Wife",
             [("gmail", self.wife_gmail, self.wife_gmail_app_password),
              ("yahoo", self.wife_yahoo, self.wife_yahoo_app_password)]),
        ]:
            for provider, address, password in pairs:
                address  = resolve(address)
                password = resolve(password)
                if not is_placeholder(address, password):
                    host, port = IMAP_HOSTS[provider]
                    accounts.append({
                        "owner_id":     owner_id,
                        "owner_name":   owner_name,
                        "address":      address,
                        "app_password": password,
                        "imap_host":    host,
                        "imap_port":    port,
                    })
        return accounts

    def get_owner_profile(self, owner_id: str) -> dict[str, str]:
        """
        Return personal-info fields for an owner from accounts.yml.
        Keys: name, dob (YYYY-MM-DD), pan, mobile.
        Used to derive PDF open passwords at runtime — never stored.
        """
        cfg = Path("config/accounts.yml")
        if not cfg.exists():
            return {}
        data = yaml.safe_load(cfg.read_text()) or {}
        for owner in data.get("owners", []):
            if owner.get("id") == owner_id:
                return {
                    "name":             str(owner.get("name",             "") or ""),
                    "dob":              str(owner.get("dob",              "") or ""),
                    "pan":              str(owner.get("pan",              "") or "").upper().strip(),
                    "mobile":           str(owner.get("mobile",           "") or ""),
                    "hdfc_pdf_password": str(owner.get("hdfc_pdf_password", "") or ""),
                }
        return {}

    def get_salary_patterns(self) -> list[str]:
        """
        Collect employer-specific salary-narration patterns from accounts.yml.

        Supports two styles per owner — users only need to know the simpler one:

          salary_keywords:          # plain words/phrases (recommended for most users)
            - "acmecorp"
            - "acme sal"

          salary_patterns:          # full regex for advanced users
            - "neft.*acme|acme.*neft"

        Keywords are escaped and wrapped in a word-boundary regex automatically,
        so "acmecorp" matches "NEFT FROM ACMECORP" without the user knowing regex.
        Both lists may coexist. Falls back to an empty list (the generic
        salary|payroll rule in the categoriser then applies).
        """
        cfg = Path("config/accounts.yml")
        if not cfg.exists():
            return []
        data = yaml.safe_load(cfg.read_text()) or {}
        out: list[str] = []
        for owner in data.get("owners", []):
            # Plain keywords → auto-wrapped in \b...\b so they match as substrings
            for kw in (owner.get("salary_keywords") or []):
                kw = str(kw).strip()
                if kw:
                    out.append(re.escape(kw))
            # Advanced regex patterns — used as-is
            for pat in (owner.get("salary_patterns") or []):
                pat = str(pat).strip()
                if pat:
                    out.append(pat)
        return out

    def get_accounts_config(self) -> dict[str, Any]:
        cfg = Path("config/accounts.yml")
        if cfg.exists():
            return yaml.safe_load(cfg.read_text()) or {}
        return {}


settings = Settings()
