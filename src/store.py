"""SQLite seen-store. This is the 'never send the same job twice' guarantee.

Three dedup layers, because one posting genuinely appears on LinkedIn, Indeed and the
company's own ATS on the same morning:
  1. canonical URL          - exact, cheapest
  2. fingerprint            - company|title|city, catches the cross-board case
  3. fuzzy near-match       - punctuation and translation drift within a lookback window
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import normalize as nz

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint   TEXT PRIMARY KEY,
    canonical_url TEXT UNIQUE,
    url           TEXT,
    company       TEXT,
    title         TEXT,
    location      TEXT,
    source        TEXT,
    geo           TEXT,
    salary_text   TEXT,
    posted_date   TEXT,
    body          TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    score         INTEGER,
    rationale     TEXT,
    flags         TEXT,
    sent_on       TEXT,
    status        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_lastseen ON jobs(last_seen);

CREATE TABLE IF NOT EXISTS runs (
    run_date   TEXT PRIMARY KEY,
    started_at TEXT,
    collected  INTEGER,
    new_jobs   INTEGER,
    sent       INTEGER,
    notes      TEXT
);
"""

STATUS_NEW = "new"
STATUS_SENT = "sent"
STATUS_BELOW = "below_threshold"
STATUS_REJECTED = "rejected"


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Migration for stores created before `body` existed.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "body" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN body TEXT")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ lookups

    def _by_url(self, canonical: str):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE canonical_url = ?", (canonical,)
        ).fetchone()

    def _by_fingerprint(self, fp: str):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE fingerprint = ?", (fp,)
        ).fetchone()

    def _fuzzy_match(self, company: str, title: str, ratio: float, lookback_days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        needle = f"{nz.normalize_company(company)} {nz.normalize_title(title)}"
        if not needle.strip():
            return None
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE last_seen >= ?", (cutoff,)
        ).fetchall()
        my_co, my_title = nz.normalize_company(company), nz.normalize_title(title)
        for row in rows:
            row_co = nz.normalize_company(row["company"])
            row_title = nz.normalize_title(row["title"])
            # Identical title plus one company name containing the other:
            # "Vanderlande" vs "Vanderlande Industries" is the same employer.
            # Checked separately from the blended ratio, which would need a
            # threshold low enough to also collapse genuinely different roles.
            if my_title and my_title == row_title and my_co and row_co:
                if my_co in row_co or row_co in my_co:
                    return row
            hay = f"{row_co} {row_title}"
            if SequenceMatcher(None, needle, hay).ratio() >= ratio:
                return row
        return None

    def is_known(self, job: dict, ratio: float = 0.92, lookback_days: int = 90):
        """Return the existing row if we've seen this job before, else None."""
        return (
            self._by_url(job["canonical_url"])
            or self._by_fingerprint(nz.fingerprint(job["company"], job["title"], job["location"]))
            or self._fuzzy_match(job["company"], job["title"], ratio, lookback_days)
        )

    # ------------------------------------------------------------------ writes

    def touch(self, row_fp: str):
        """Mark an already-known job as seen again today."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute("UPDATE jobs SET last_seen = ? WHERE fingerprint = ?", (now, row_fp))
        self.conn.commit()

    def insert_new(self, job: dict) -> str:
        """Insert a freshly discovered job as status='new'. Returns its fingerprint."""
        fp = nz.fingerprint(job["company"], job["title"], job["location"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (fingerprint, canonical_url, url, company, title, location, source, geo,
                salary_text, posted_date, body, flags, first_seen, last_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, job["canonical_url"], job["url"], job["company"], job["title"],
             job["location"], job["source"], job["geo"], job.get("salary_text", ""),
             job.get("posted_date", ""), job.get("body", "")[:12000],
             "; ".join(job.get("flags") or []), now, now, STATUS_NEW),
        )
        self.conn.commit()
        return fp

    def record_score(self, fp: str, score: int, rationale: str, flags: str):
        self.conn.execute(
            "UPDATE jobs SET score = ?, rationale = ?, flags = ? WHERE fingerprint = ?",
            (score, rationale, flags, fp),
        )
        self.conn.commit()

    def mark_sent(self, fingerprints: list[str], run_date: str):
        """Called ONLY after the email actually goes out."""
        self.conn.executemany(
            "UPDATE jobs SET sent_on = ?, status = ? WHERE fingerprint = ?",
            [(run_date, STATUS_SENT, fp) for fp in fingerprints],
        )
        self.conn.commit()
        return self.conn.total_changes

    def mark_below_threshold(self, fingerprints: list[str]):
        """Stored so they are never re-scored, but never emailed either."""
        self.conn.executemany(
            "UPDATE jobs SET status = ? WHERE fingerprint = ? AND status = ?",
            [(STATUS_BELOW, fp, STATUS_NEW) for fp in fingerprints],
        )
        self.conn.commit()

    def record_run(self, run_date: str, collected: int, new_jobs: int, sent: int, notes: str = ""):
        self.conn.execute(
            """INSERT INTO runs (run_date, started_at, collected, new_jobs, sent, notes)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(run_date) DO UPDATE SET
                 collected=excluded.collected, new_jobs=excluded.new_jobs,
                 sent=excluded.sent, notes=excluded.notes""",
            (run_date, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             collected, new_jobs, sent, notes),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ reports

    def pending_unsent(self):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC", (STATUS_NEW,)
        ).fetchall()

    def stats(self) -> dict:
        cur = self.conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
        out = {r["status"]: r["c"] for r in cur}
        out["total"] = self.conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        return out

    def unsent_carry_forward(self, exclude: set[str], only_source: str | None = None,
                             skip_source: str | None = None) -> list[dict]:
        """Jobs stored on an earlier run that were never emailed.

        Without this, a run that collects successfully but fails to send would bury
        those postings forever: the next collect would skip them as 'already known'.

        The source filters keep the two digests apart: the Indeed India channel sends its
        own email, so its leftovers must carry into that email, never the 07:30 one.
        """
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND sent_on IS NULL", (STATUS_NEW,)
        ).fetchall()
        out = []
        for r in rows:
            if r["fingerprint"] in exclude:
                continue
            if only_source and r["source"] != only_source:
                continue
            if skip_source and r["source"] == skip_source:
                continue
            out.append({
                "fingerprint": r["fingerprint"],
                "url": r["url"], "canonical_url": r["canonical_url"],
                "title": r["title"], "company": r["company"], "location": r["location"],
                "salary_text": r["salary_text"] or "", "posted_date": r["posted_date"] or "",
                "body": r["body"] or "", "source": r["source"], "geo": r["geo"],
                "flags": [f for f in (r["flags"] or "").split("; ") if f],
                "carried_forward": True,
            })
        return out
