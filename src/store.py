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
        self._fuzzy = None          # in-memory index for the fuzzy layer, built lazily

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

    @staticmethod
    def _cutoff(lookback_days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    def _fuzzy_entry(self, row) -> tuple:
        return (row, nz.normalize_company(row["company"]), nz.normalize_title(row["title"]),
                nz.city_key(row["location"]), row["geo"] or "")

    def _fuzzy_index(self, lookback_days: int) -> dict:
        """Rows within the lookback, normalised once and bucketed by company and title.

        The old layer re-read and re-normalised the whole 90-day table and ran a
        SequenceMatcher against every row, once per raw hit — O(hits x store) regex and
        diff work that grows every day. Every rule below needs an exact company or an
        exact title, so two dict lookups find all the rows worth comparing.
        """
        if self._fuzzy is None or self._fuzzy["lookback"] != lookback_days:
            by_co: dict[str, list] = {}
            by_title: dict[str, list] = {}
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE last_seen >= ? AND status != ?",
                (self._cutoff(lookback_days), STATUS_REJECTED)).fetchall()
            self._fuzzy = {"lookback": lookback_days, "co": by_co, "title": by_title}
            for row in rows:
                self._index_row(row)
        return self._fuzzy

    def _index_row(self, row):
        if self._fuzzy is None:
            return
        entry = self._fuzzy_entry(row)
        if entry[1]:
            self._fuzzy["co"].setdefault(entry[1], []).append(entry)
        if entry[2]:
            self._fuzzy["title"].setdefault(entry[2], []).append(entry)

    @staticmethod
    def _same_place(geo_a, city_a, geo_b, city_b) -> bool:
        """Geography must agree, and so must the city when both name one. A multinational
        posts the same title in Germany and Poland; those are two jobs, not one."""
        if geo_a and geo_b and geo_a != geo_b:
            return False
        return not (city_a and city_b and city_a != city_b)

    def _fuzzy_match(self, job: dict, ratio: float, lookback_days: int):
        my_co = nz.normalize_company(job.get("company"))
        my_title = nz.normalize_title(job.get("title"))
        if not my_co or not my_title:
            return None
        my_geo, my_city = job.get("geo") or "", nz.city_key(job.get("location"))
        idx = self._fuzzy_index(lookback_days)
        my_co_tokens = set(my_co.split())

        # Identical title, and one company name contained in the other ("Vanderlande" /
        # "Vanderlande Industries"), or the names a near-miss spelling of each other
        # ("Kuehne Nagel" / "Kuhne Nagel"). Containment is by whole words, and the
        # spelling rule needs names long enough that one letter is not the whole
        # difference — "ABB" and "ABC" are two companies.
        for row, co, _title, city, geo in idx["title"].get(my_title, []):
            if not self._same_place(my_geo, my_city, geo, city):
                continue
            tokens = set(co.split())
            if my_co_tokens <= tokens or tokens <= my_co_tokens:
                return row
            if min(len(co), len(my_co)) >= 6 and \
                    SequenceMatcher(None, my_co, co).ratio() >= ratio:
                return row

        # Same company, title drifting by punctuation or a letter
        # ("Logistic Process Engineer" / "Logistics Process Engineer").
        for row, _co, title, city, geo in idx["co"].get(my_co, []):
            if not self._same_place(my_geo, my_city, geo, city):
                continue
            if SequenceMatcher(None, my_title, title).ratio() >= ratio:
                return row
        return None

    def _live_fingerprint_row(self, fp: str, job: dict, lookback_days: int, legacy: bool):
        """A fingerprint hit, unless it is a role that has been gone for longer than the
        lookback and has now been reposted. A posting that is still listed is touched
        every morning, so only a genuine absence lets one through."""
        row = self._by_fingerprint(fp)
        if row is None:
            return None
        if row["status"] in (STATUS_SENT, STATUS_BELOW) and \
                (row["last_seen"] or "") < self._cutoff(lookback_days):
            return None
        # A v1 key also merged internships into the full-time role; only accept a v1 hit
        # whose title still matches under the current rules.
        if legacy and nz.normalize_title(row["title"]) != nz.normalize_title(job["title"]):
            return None
        return row

    def is_known(self, job: dict, ratio: float = 0.92, lookback_days: int = 90):
        """Return the existing row if we've seen this job before, else None.

        A prescreen reject is only ever matched as itself (same URL, or same company,
        exact title and city) — never through the fingerprint or the fuzzy layer, which
        normalise seniority away and would let a rejected senior role hide a junior one.
        """
        args = (job["company"], job["title"], job["location"])
        return (
            self._by_url(job["canonical_url"])
            or self._by_fingerprint(nz.reject_key(*args))
            or self._live_fingerprint_row(nz.fingerprint(*args), job, lookback_days, False)
            or self._live_fingerprint_row(nz.fingerprint_v1(*args), job, lookback_days, True)
            or self._fuzzy_match(job, ratio, lookback_days)
        )

    # ------------------------------------------------------------------ writes

    def touch(self, row_fp: str):
        """Mark an already-known job as seen again today."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute("UPDATE jobs SET last_seen = ? WHERE fingerprint = ?", (now, row_fp))
        self.conn.commit()

    def insert_new(self, job: dict) -> str:
        """Insert a freshly discovered job as status='new'. Returns its fingerprint.

        If the fingerprint belongs to a role sent (or written off) long ago that is_known
        let through as a repost, that row is revived as new rather than ignored.
        """
        fp = nz.fingerprint(job["company"], job["title"], job["location"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        old = self._by_fingerprint(fp)
        if old is not None and old["status"] in (STATUS_SENT, STATUS_BELOW):
            self.conn.execute(
                """UPDATE jobs SET url = ?, source = ?, salary_text = ?, posted_date = ?,
                       body = ?, flags = ?, first_seen = ?, last_seen = ?, status = ?,
                       score = NULL, rationale = NULL, sent_on = NULL
                   WHERE fingerprint = ?""",
                (job["url"], job["source"], job.get("salary_text", ""),
                 job.get("posted_date", ""), job.get("body", "")[:12000],
                 "; ".join(job.get("flags") or []), now, now, STATUS_NEW, fp))
            self.conn.commit()
            self._index_row(self._by_fingerprint(fp))
            return fp
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
        row = self._by_fingerprint(fp)
        if row is not None:
            self._index_row(row)
        return fp

    def insert_rejected(self, job: dict, reason: str) -> str:
        """Remember a prescreen reject, so tomorrow it is 'already known' before its page
        is fetched again. Returns its key.

        Resurface rejects after loosening a rule with
        `UPDATE jobs SET status='new' WHERE status='rejected'` — the next collect
        re-screens every carried job against the current rules.
        """
        key = nz.reject_key(job["company"], job["title"], job["location"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (fingerprint, canonical_url, url, company, title, location, source, geo,
                salary_text, posted_date, body, rationale, first_seen, last_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, job["canonical_url"], job["url"], job["company"], job["title"],
             job["location"], job["source"], job.get("geo"), job.get("salary_text", ""),
             job.get("posted_date", ""), (job.get("body") or "")[:12000],
             f"prescreen: {reason}", now, now, STATUS_REJECTED),
        )
        self.conn.commit()
        return key

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

    def record_run(self, run_date: str, collected: int | None, new_jobs: int | None,
                   sent: int | None, notes: str = ""):
        """Upsert the run row. A None (or empty notes) leaves the stored value alone.

        collect and commit both write this row. Overwriting blindly let a later stage
        zero the other's figures, and let a re-run collect wipe the `gmail:<id>` note
        that proves a day was committed.
        """
        self.conn.execute(
            """INSERT INTO runs (run_date, started_at, collected, new_jobs, sent, notes)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(run_date) DO UPDATE SET
                 collected=COALESCE(excluded.collected, runs.collected),
                 new_jobs=COALESCE(excluded.new_jobs, runs.new_jobs),
                 sent=COALESCE(excluded.sent, runs.sent),
                 notes=COALESCE(NULLIF(excluded.notes, ''), runs.notes)""",
            (run_date, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             collected, new_jobs, sent, notes),
        )
        self.conn.commit()

    def committed(self, run_key: str) -> bool:
        """True once `commit` has recorded the Gmail message id for this run."""
        row = self.conn.execute(
            "SELECT notes FROM runs WHERE run_date = ?", (run_key,)).fetchone()
        return bool(row and (row["notes"] or "").startswith("gmail:"))

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
