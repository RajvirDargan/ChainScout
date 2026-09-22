"""Spend ledger for metered external APIs.

Exists because a single unattended run spent 324 Firecrawl credits against a
1,000/month allowance. Callers must reserve before they spend; a reservation that
would breach a cap returns False and the caller SKIPS the call rather than raising,
so a exhausted quota degrades the run instead of failing it.
"""

from __future__ import annotations

import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota (
    source TEXT NOT NULL,
    period TEXT NOT NULL,      -- 'YYYY-MM' for monthly caps, 'YYYY-MM-DD' for daily
    used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, period)
);
"""


def month_key(on: date | None = None) -> str:
    return (on or date.today()).strftime("%Y-%m")


def day_key(on: date | None = None) -> str:
    return (on or date.today()).isoformat()


class Quota:
    """Wraps an existing sqlite connection so the ledger lives beside the job store."""

    def __init__(self, conn: sqlite3.Connection, config: dict, log=print):
        self.conn = conn
        self.config = config or {}
        self.log = log
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._blocked: set[str] = set()

    # ------------------------------------------------------------------ reading

    def used(self, source: str, period: str) -> int:
        row = self.conn.execute(
            "SELECT used FROM quota WHERE source = ? AND period = ?", (source, period)
        ).fetchone()
        return int(row[0]) if row else 0

    def enabled(self, source: str) -> bool:
        return bool(self.config.get(source, {}).get("enabled", False))

    def limits(self, source: str) -> dict:
        return self.config.get(source, {}) or {}

    def remaining(self, source: str) -> dict:
        """What's left against each configured cap. Missing cap = unlimited (None)."""
        cfg = self.limits(source)
        out: dict[str, int | None] = {}
        for key, period in (("daily", day_key()), ("monthly", month_key())):
            cap = _cap_for(cfg, key)
            out[key] = None if cap is None else max(0, cap - self.used(source, period))
        return out

    # ------------------------------------------------------------------ spending

    def check_and_reserve(self, source: str, n: int = 1) -> bool:
        """Reserve n units. False means: don't make the call.

        Reserving before the call (rather than recording after) means a crash
        mid-request can over-count slightly, never under-count. Over-counting is
        the safe direction when the whole point is not to overspend.
        """
        if not self.enabled(source):
            self._warn_once(source, f"{source}: disabled in config — skipping")
            return False

        cfg = self.limits(source)
        for key, period in (("daily", day_key()), ("monthly", month_key())):
            cap = _cap_for(cfg, key)
            if cap is None:
                continue
            if self.used(source, period) + n > cap:
                self._warn_once(
                    f"{source}:{key}",
                    f"{source}: {key} cap reached "
                    f"({self.used(source, period)}/{cap}) — skipping further calls",
                )
                return False

        for period in (day_key(), month_key()):
            self.conn.execute(
                """INSERT INTO quota (source, period, used) VALUES (?,?,?)
                   ON CONFLICT(source, period) DO UPDATE SET used = used + excluded.used""",
                (source, period, n),
            )
        self.conn.commit()
        return True

    def refund(self, source: str, n: int = 1):
        """Give back an unspent reservation (e.g. the request never left the process)."""
        for period in (day_key(), month_key()):
            self.conn.execute(
                "UPDATE quota SET used = MAX(0, used - ?) WHERE source = ? AND period = ?",
                (n, source, period),
            )
        self.conn.commit()

    def reconcile(self, source: str, reserved: int, actual: int):
        """Correct a reservation once the true cost is known.

        Firecrawl reports creditsUsed per response, which rarely matches the estimate.
        """
        delta = actual - reserved
        if delta == 0:
            return
        if delta < 0:
            self.refund(source, -delta)
            return
        for period in (day_key(), month_key()):
            self.conn.execute(
                """INSERT INTO quota (source, period, used) VALUES (?,?,?)
                   ON CONFLICT(source, period) DO UPDATE SET used = used + excluded.used""",
                (source, period, delta),
            )
        self.conn.commit()

    # ------------------------------------------------------------------ reporting

    def summary(self) -> list[str]:
        lines = []
        for source in sorted(self.config):
            if not self.enabled(source):
                lines.append(f"  {source:10} disabled")
                continue
            cfg = self.limits(source)
            bits = []
            for key, period in (("daily", day_key()), ("monthly", month_key())):
                cap = _cap_for(cfg, key)
                if cap is not None:
                    bits.append(f"{key} {self.used(source, period)}/{cap}")
            lines.append(f"  {source:10} " + (", ".join(bits) or "no cap"))
        return lines

    def _warn_once(self, key: str, msg: str):
        if key not in self._blocked:
            self._blocked.add(key)
            self.log(f"  ! {msg}")


def _cap_for(cfg: dict, key: str) -> int | None:
    """Accept daily_calls / daily_requests / daily_credits interchangeably."""
    for suffix in ("calls", "requests", "credits"):
        if f"{key}_{suffix}" in cfg:
            return int(cfg[f"{key}_{suffix}"])
    return None
