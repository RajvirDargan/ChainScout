"""collect must rule a posting out (already known, or a disqualifying title) BEFORE it
spends a page fetch on it, and must remember durable rejects."""

import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import extract  # noqa: E402
import normalize as nz  # noqa: E402
import run  # noqa: E402
from store import Store  # noqa: E402

DAY = "2026-10-01"


def _rec(n, title, body="short"):
    return {"title": title, "url": f"https://boards.greenhouse.io/acme/jobs/{n}",
            "location": "Amsterdam, Netherlands", "posted": "", "body": body}


@pytest.fixture
def sandbox(monkeypatch):
    base = Path(tempfile.mkdtemp())
    monkeypatch.setattr(run, "STATE", base / "state")
    monkeypatch.setattr(run, "OUTBOX", base / "outbox")
    monkeypatch.setattr(run, "LOGS", base / "logs")
    monkeypatch.setattr(run, "ROOT", base)          # no .env -> no Adzuna, no Gemini

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["sources"]["linkedin"]["enabled"] = False
    for g in cfg["geographies"]:
        g["visa_mode"] = "none"                     # no IND register download
    cfg["ats"] = {"greenhouse": ["acme"], "title_keywords": cfg["ats"]["title_keywords"],
                  "display_names": {"acme": "Acme"}}
    monkeypatch.setattr(run, "load_config", lambda: cfg)

    records = [
        _rec(1, "Logistics Analyst"),                  # already in the store
        _rec(2, "Senior Supply Chain Planner"),        # title reject
        _rec(3, "Junior Supply Chain Analyst"),        # genuinely new
    ]
    monkeypatch.setattr(run.fetch, "ATS_FETCHERS", {"greenhouse": lambda slug: records})

    fetched = []

    def fake_enrich(job, gemini=None, min_chars=300, log=print, fetcher=None):
        fetched.append(job["title"])
        job["body"] = "A real description of the role. " * 20
        return job, "jsonld"

    monkeypatch.setattr(run.extract, "enrich", fake_enrich)

    store = Store(base / "state" / "seen.sqlite")
    known = nz.from_ats_record(records[0], "Acme", "greenhouse", "nl")
    store.insert_new(known)
    store.close()
    return base, fetched


def _collect():
    return run.stage_collect(run._ns(date=DAY, dry_run=False, weekly=False,
                                     limit_queries=0))


def test_only_new_in_scope_postings_get_their_page_fetched(sandbox):
    base, fetched = sandbox
    assert _collect() == 0
    assert fetched == ["Junior Supply Chain Analyst"]


def test_a_title_reject_is_remembered_and_never_refetched(sandbox):
    base, fetched = sandbox
    _collect()
    store = Store(base / "state" / "seen.sqlite")
    assert store.stats().get("rejected") == 1
    store.close()

    fetched.clear()
    (base / "outbox" / f"{DAY}_candidates.json").unlink()
    _collect()
    assert fetched == [], "everything is known on the second morning"


def test_a_rejected_senior_role_does_not_hide_the_junior_one():
    store = Store(Path(tempfile.mkdtemp()) / "seen.sqlite")
    senior = nz.from_ats_record(_rec(9, "Senior Logistics Analyst"), "Acme", "greenhouse", "nl")
    store.insert_rejected(senior, "seniority:senior")
    junior = nz.from_ats_record(_rec(10, "Logistics Analyst"), "Acme", "greenhouse", "nl")
    assert store.is_known(junior) is None
    assert store.is_known(senior) is not None


def test_adzuna_session_params_do_not_make_a_new_url_every_day():
    a = nz.canonical_url("https://www.adzuna.nl/land/ad/4321?se=abc&utm_medium=api&v=1")
    b = nz.canonical_url("https://www.adzuna.nl/land/ad/4321?se=xyz&v=2")
    assert a == b == "https://adzuna.nl/land/ad/4321"


# ---------------------------------------------------------------- page fetcher

class FakeResp:
    def __init__(self, status=200, text="<html></html>", url="https://x"):
        self.status_code, self.text, self.url = status, text, url
        self.ok = 200 <= status < 300


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0

    def get(self, url, timeout=25):
        self.calls += 1
        return self.responses.pop(0)


def test_a_linkedin_block_stops_further_linkedin_fetches():
    session = FakeSession([FakeResp(429), FakeResp(200, "ok")])
    f = extract.PageFetcher(log=lambda *_: None, delay=0, session=session)
    assert f.get("https://www.linkedin.com/jobs/view/1") == ""
    assert f.blocked
    assert f.get("https://www.linkedin.com/jobs/view/2") == ""
    assert session.calls == 1, "no second request into a block"


def test_a_signin_redirect_counts_as_a_block():
    session = FakeSession([FakeResp(200, "Sign in", "https://www.linkedin.com/authwall?x")])
    f = extract.PageFetcher(log=lambda *_: None, delay=0, session=session)
    assert f.get("https://www.linkedin.com/jobs/view/3") == ""
    assert f.blocked


def test_other_hosts_are_unaffected_by_a_linkedin_block():
    session = FakeSession([FakeResp(429), FakeResp(200, "page")])
    f = extract.PageFetcher(log=lambda *_: None, delay=0, session=session)
    f.get("https://www.linkedin.com/jobs/view/1")
    assert f.get("https://boards.greenhouse.io/acme/jobs/1") == "page"
