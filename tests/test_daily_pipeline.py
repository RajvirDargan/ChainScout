"""The daily chain's guarantees: a sent digest is always committed, a broken run never
masquerades as a quiet day, and a retry never re-spends the day's discovery quota."""

import json
import smtplib
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mailer  # noqa: E402
import normalize as nz  # noqa: E402
import run  # noqa: E402
from store import Store  # noqa: E402

DAY = "2026-10-01"


@pytest.fixture
def sandbox(monkeypatch):
    """Point run.py at a throwaway state/outbox/logs and stub out the network edges."""
    base = Path(tempfile.mkdtemp())
    monkeypatch.setattr(run, "STATE", base / "state")
    monkeypatch.setattr(run, "OUTBOX", base / "outbox")
    monkeypatch.setattr(run, "LOGS", base / "logs")
    (base / "outbox").mkdir()
    monkeypatch.setattr(run, "wait_for_network", lambda *a, **k: True)
    monkeypatch.setattr(run.mailer_src, "credentials", lambda env: ("a@b.c", "pw"))
    calls = []
    return base, calls


def _write(base, name, data):
    (base / "outbox" / f"{DAY}_{name}.json").write_text(json.dumps(data))


def _job(i):
    url = f"https://example.com/job/{i}"
    return {"company": f"Co{i}", "title": "Logistics Analyst", "location": "Amsterdam",
            "url": url, "canonical_url": nz.canonical_url(url), "source": "adzuna",
            "geo": "nl", "body": "x" * 400}


def _stub(monkeypatch, calls, name, rc=0, effect=None):
    def fake(args):
        calls.append(name)
        if effect:
            effect(args)
        return rc
    monkeypatch.setattr(run, name, fake)


def _daily(force=False):
    return run.stage_daily(run._ns(date=DAY, force=force))


# ------------------------------------------------------- send without a commit

def test_a_sent_but_uncommitted_digest_is_committed_not_resent(sandbox, monkeypatch):
    base, calls = sandbox
    store = Store(base / "state" / "seen.sqlite")
    fp = store.insert_new(_job(1))
    store.close()
    _write(base, "email", {"to": ["a@b.c"], "subject": "s", "fingerprints": [fp],
                           "deferred_fingerprints": [], "all_fingerprints": [fp],
                           "sent_message_id": "<m1@x>"})
    for name in ("stage_collect", "stage_score", "stage_render", "stage_send"):
        _stub(monkeypatch, calls, name)

    assert _daily() == 0
    assert calls == [], "the digest already went out — nothing may be redone"
    store = Store(base / "state" / "seen.sqlite")
    assert store.stats().get("sent") == 1
    assert store.committed(DAY)
    store.close()

    # And the next tick is a clean no-op.
    assert _daily() == 0 and calls == []


def test_an_earlier_days_orphaned_send_is_committed_on_the_next_run(sandbox, monkeypatch):
    """The crash may land just before midnight; the next tick is a different day."""
    base, calls = sandbox
    store = Store(base / "state" / "seen.sqlite")
    fp = store.insert_new(_job(2))
    store.close()
    (base / "outbox" / "2026-09-30_email.json").write_text(json.dumps({
        "fingerprints": [fp], "deferred_fingerprints": [], "all_fingerprints": [fp],
        "sent_message_id": "<m0@x>"}))
    log = run.Logger(base / "logs" / "t.log")
    assert run.recover_uncommitted(log, "main") == 0
    store = Store(base / "state" / "seen.sqlite")
    assert store.committed("2026-09-30")
    assert store.unsent_carry_forward(set()) == []
    store.close()


# ------------------------------------------------ never send a misleading digest

def test_a_dead_scorer_does_not_send_a_no_new_roles_email(sandbox, monkeypatch):
    base, calls = sandbox
    _stub(monkeypatch, calls, "stage_collect", effect=lambda a: _write(
        base, "candidates", {"scanned": 40, "candidates": [_job(1)]}))
    _stub(monkeypatch, calls, "stage_score", effect=lambda a: _write(
        base, "scored", {"jobs": [], "unscored_count": 1}))
    _stub(monkeypatch, calls, "stage_render")
    _stub(monkeypatch, calls, "stage_send")

    assert _daily() == 4
    assert "stage_send" not in calls and "stage_render" not in calls


def test_a_run_that_reached_no_source_does_not_send(sandbox, monkeypatch):
    base, calls = sandbox
    _stub(monkeypatch, calls, "stage_collect", effect=lambda a: _write(
        base, "candidates", {"scanned": 0, "candidates": []}))
    _stub(monkeypatch, calls, "stage_score", effect=lambda a: _write(
        base, "scored", {"jobs": [], "unscored_count": 0}))
    _stub(monkeypatch, calls, "stage_send")

    assert _daily() == 4
    assert "stage_send" not in calls


def test_a_genuinely_quiet_day_still_sends(sandbox, monkeypatch):
    base, calls = sandbox
    _stub(monkeypatch, calls, "stage_collect", effect=lambda a: _write(
        base, "candidates", {"scanned": 40, "candidates": []}))
    _stub(monkeypatch, calls, "stage_score", effect=lambda a: _write(
        base, "scored", {"jobs": [], "unscored_count": 0}))
    _stub(monkeypatch, calls, "stage_render")
    _stub(monkeypatch, calls, "stage_send", effect=lambda a: _write(
        base, "email", {"sent_message_id": "<q@x>", "fingerprints": [],
                        "all_fingerprints": [], "deferred_fingerprints": []}))
    assert _daily() == 0
    assert calls[-1] == "stage_send"


# ----------------------------------------------------- a retry reuses the collect

def test_a_retry_after_a_failed_send_does_not_collect_again(sandbox, monkeypatch):
    base, calls = sandbox
    _write(base, "candidates", {"scanned": 40, "candidates": [_job(1)]})
    _stub(monkeypatch, calls, "stage_collect")
    _stub(monkeypatch, calls, "stage_score", effect=lambda a: _write(
        base, "scored", {"jobs": [{"fingerprint": "f", "score": 60}],
                         "unscored_count": 0}))
    _stub(monkeypatch, calls, "stage_render")
    _stub(monkeypatch, calls, "stage_send", rc=2)

    assert _daily() == 2
    assert "stage_collect" not in calls
    calls.clear()
    assert _daily() == 2
    assert calls == ["stage_render", "stage_send"], "a fully scored file is reused too"


# ---------------------------------------------------------------------- mailer

class FakeSMTP:
    refused: dict = {}
    quit_raises = False

    def __init__(self, *a, **k):
        pass

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        return dict(self.refused)

    def quit(self):
        if self.quit_raises:
            raise smtplib.SMTPServerDisconnected("gone")


ENV = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}


def test_a_partial_recipient_refusal_still_counts_as_sent(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(FakeSMTP, "refused", {"bad@x.y": (550, b"no")})
    mid = mailer.send(ENV, ["ok@x.y", "bad@x.y"], "s", "t", "<p>h</p>", log=lambda *_: None)
    assert mid.startswith("<")


def test_a_dropped_connection_after_delivery_still_counts_as_sent(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(FakeSMTP, "quit_raises", True)
    assert mailer.send(ENV, ["ok@x.y"], "s", "t", "<p>h</p>", log=lambda *_: None)


# ----------------------------------------------------------------------- store

def test_a_later_collect_does_not_erase_the_commit_record():
    store = Store(Path(tempfile.mkdtemp()) / "seen.sqlite")
    store.record_run(DAY, 120, 30, None)
    store.record_run(DAY, None, 30, 12, notes="gmail:<m@x>")
    store.record_run(DAY, 140, 31, None)          # a forced re-collect the same day
    row = store.conn.execute("SELECT * FROM runs WHERE run_date = ?", (DAY,)).fetchone()
    assert store.committed(DAY)
    assert (row["collected"], row["sent"]) == (140, 12)
