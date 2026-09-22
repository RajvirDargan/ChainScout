"""The Indeed India channel: a separate email that must never leak into, or repeat,
the 07:30 digest."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402
import indeed  # noqa: E402
import normalize as nz  # noqa: E402
import render  # noqa: E402
import run  # noqa: E402
import score  # noqa: E402
from store import Store  # noqa: E402

REC = {"job_id": "JOBSEARCH_1", "title": "Associate Analyst - Supply Chain",
       "company": "United Airlines", "location": "Gurugram, Haryana",
       "posted": "September 10, 2026", "url": "https://to.indeed.com/aapqn7jccnsg",
       "salary": "N/A", "description": "x" * 500}


def _store():
    return Store(Path(tempfile.mkdtemp()) / "seen.sqlite")


def _stored(store, rec, source):
    job = indeed.to_job(rec)
    job["source"] = source
    store.insert_new(job)
    return job


def test_to_job_maps_a_connector_record():
    job = indeed.to_job(REC)
    assert job["source"] == "indeed" and job["geo"] == "india"
    assert job["salary_text"] == ""            # "N/A" is not a salary
    assert job["indeed_job_id"] == "JOBSEARCH_1"
    assert indeed.to_job({**REC, "url": ""}) is None


def test_city_rank_prefers_ncr_then_mumbai():
    ranks = [indeed.city_rank(x) for x in
             ("Gurugram, Haryana", "Navi Mumbai, Maharashtra", "Remote", "Chennai")]
    assert ranks == sorted(ranks) == [0, 1, 2, 3]


def test_main_carry_forward_never_takes_indeed_rows():
    s = _store()
    _stored(s, REC, "indeed")
    _stored(s, {**REC, "title": "Logistics Analyst", "url": "https://x.test/2"}, "linkedin")
    main = s.unsent_carry_forward(set(), skip_source="indeed")
    ind = s.unsent_carry_forward(set(), only_source="indeed")
    assert [j["source"] for j in main] == ["linkedin"]
    assert [j["source"] for j in ind] == ["indeed"]


def test_a_role_already_sent_by_the_main_digest_is_not_fetched_again(monkeypatch, capsys):
    tmp = Path(tempfile.mkdtemp())
    s = Store(tmp / "seen.sqlite")
    job = _stored(s, REC, "linkedin")
    s.mark_sent([nz.fingerprint(job["company"], job["title"], job["location"])], "2026-09-18")
    s.close()
    raw = tmp / "raw.json"
    fresh = {**REC, "job_id": "JOBSEARCH_2", "title": "Logistics Executive",
             "url": "https://to.indeed.com/other"}
    raw.write_text(json.dumps([REC, fresh]))
    monkeypatch.setattr(run, "STATE", tmp)
    monkeypatch.setattr(run, "LOGS", tmp)

    class A:
        pass
    a = A()
    a.raw, a.max, a.date = str(raw), 12, "2026-09-19"
    assert run.stage_indeed_plan(a) == 0
    ids = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ids == ["JOBSEARCH_2"]


def test_indeed_channel_has_its_own_outbox():
    class A:
        channel = "indeed"
    assert run.outbox_for(A()) == run.OUTBOX / "indeed"
    assert run.outbox_for(object()) == run.OUTBOX
    assert run.run_key("2026-09-19", A()) == "2026-09-19#indeed"


def test_subject_is_labelled_indeed_india():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    job = {**indeed.to_job(REC), "score": 78, "rationale": "fit", "flags": [],
           "fingerprint": "fp1"}
    subject, html, text, fps = render.build([job], cfg, {"run_date": "2026-09-19",
                                                         "channel": "indeed"})
    assert subject.startswith("[Indeed India]") and "NL " not in subject
    assert "India — via Indeed" in html and fps == ["fp1"]


def test_the_india_addendum_survives_profile_trimming():
    profile = (ROOT / "PROFILE.md").read_text() + "\n\n" + \
        (ROOT / "PROFILE_INDIA_ADDENDUM.md").read_text()
    trimmed = score.trim_profile(profile)
    assert "India-only override" in trimmed and "Delhi NCR" in trimmed
    assert "{" not in (ROOT / "PROFILE_INDIA_ADDENDUM.md").read_text()  # PROMPT_HEAD.format safe
