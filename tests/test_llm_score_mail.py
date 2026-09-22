"""Tests for the pieces that replaced Claude: the LLM backends, the batched scorer
and the SMTP sender."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import llm  # noqa: E402
import mailer  # noqa: E402
import score  # noqa: E402


class FakeBackend:
    """Stands in for a real backend. `replies` is consumed one call at a time."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.last_model = "fake-1"
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


def _jobs(n):
    return [{"fingerprint": f"fp{i}", "title": f"Logistics Engineer {i}",
             "company": "Acme", "location": "Eindhoven", "geo": "nl",
             "source": "adzuna", "posted_date": "", "salary_text": "",
             "body": "Warehouse process work. " * 20, "flags": ["IND recognised sponsor"]}
            for i in range(n)]


def _reply(fps, score_value=70):
    return {"jobs": [{"id": fp, "score": score_value, "rationale": "Fits. Check salary.",
                      "flags": ["English-working"], "years_required": 2} for fp in fps]}


# ------------------------------------------------------------------ the scorer

def test_a_clean_batch_scores_everything():
    jobs = _jobs(3)
    chain = FakeBackend([_reply([j["fingerprint"] for j in jobs])])
    scored, unscored = score.score_jobs(jobs, "PROFILE", chain, batch_size=10,
                                        log=lambda *_: None)
    assert len(scored) == 3 and unscored == []
    assert scored[0]["score"] == 70


def test_a_dropped_job_is_retried_not_assumed_scored():
    """The model silently omitting a job must not look like a score of zero."""
    jobs = _jobs(3)
    fps = [j["fingerprint"] for j in jobs]
    chain = FakeBackend([
        _reply(fps[:2]),        # first batch drops fp2
        _reply([fps[2]]),       # retry picks it up
    ])
    scored, unscored = score.score_jobs(jobs, "PROFILE", chain, batch_size=3,
                                        log=lambda *_: None)
    assert {j["fingerprint"] for j in scored} == set(fps)
    assert unscored == []


def test_a_job_the_model_never_returns_is_left_unscored():
    """Unscored means carry-forward, never 'written off'."""
    jobs = _jobs(2)
    chain = FakeBackend([_reply([jobs[0]["fingerprint"]])] * 6)
    scored, unscored = score.score_jobs(jobs, "PROFILE", chain, batch_size=2,
                                        log=lambda *_: None)
    assert [j["fingerprint"] for j in scored] == ["fp0"]
    assert [j["fingerprint"] for j in unscored] == ["fp1"]


def test_a_malformed_response_degrades_rather_than_crashing():
    jobs = _jobs(2)
    chain = FakeBackend(["not json at all", None, {"unexpected": "shape"}, None, None, None])
    scored, unscored = score.score_jobs(jobs, "PROFILE", chain, batch_size=2,
                                        log=lambda *_: None)
    assert scored == [] and len(unscored) == 2


def test_prescreen_flags_are_carried_through_not_replaced():
    jobs = _jobs(1)
    chain = FakeBackend([_reply(["fp0"])])
    scored, _ = score.score_jobs(jobs, "PROFILE", chain, log=lambda *_: None)
    assert "IND recognised sponsor" in scored[0]["flags"]   # from prescreen
    assert "English-working" in scored[0]["flags"]          # from the model


def test_the_profile_and_hard_rules_reach_the_prompt():
    jobs = _jobs(1)
    chain = FakeBackend([_reply(["fp0"])])
    score.score_jobs(jobs, "MY-PROFILE-MARKER", chain, log=lambda *_: None)
    prompt = chain.prompts[0]
    assert "MY-PROFILE-MARKER" in prompt
    assert "AT MOST 3 years" in prompt
    assert "Do NOT claim" in prompt


# ----------------------------------------------------------------- the chain

def test_the_chain_falls_back_when_the_primary_returns_nothing():
    primary, secondary = FakeBackend([None]), FakeBackend([{"ok": True}])
    secondary.name = "secondary"
    chain = llm.Chain(primary, secondary, log=lambda *_: None)
    assert chain.complete("p") == {"ok": True}
    assert "secondary:fake-1" in chain.summary()


def test_the_chain_reports_which_backend_answered():
    primary = FakeBackend([{"a": 1}, {"a": 2}])
    chain = llm.Chain(primary, None, log=lambda *_: None)
    chain.complete("p")
    chain.complete("p")
    assert chain.summary() == "fake:fake-1 x2"


def test_an_absent_agy_is_reported_unavailable_rather_than_raising():
    agy = llm.AntigravityCLI({"binary": "definitely-not-installed"}, log=lambda *_: None)
    assert agy.available() is False
    ok, why = agy.preflight()
    assert ok is False and "not installed" in why


# ---------------------------------------------------------------- the mailer

def test_missing_credentials_refuse_loudly():
    for env in ({}, {"GMAIL_ADDRESS": "a@b.com"},
                {"GMAIL_ADDRESS": "a@b.com", "GMAIL_APP_PASSWORD": "your-16-char"}):
        try:
            mailer.credentials(env)
            raise AssertionError(f"accepted bad credentials: {env}")
        except mailer.MailerError:
            pass


def test_app_password_spaces_are_stripped():
    """Google displays app passwords in four space-separated groups."""
    _, pw = mailer.credentials({"GMAIL_ADDRESS": "a@b.com",
                                "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    assert pw == "abcdefghijklmnop"


def test_the_message_is_multipart_with_a_real_message_id():
    msg = mailer.build_message("a@b.com", ["x@y.com"], "S", "text", "<b>html</b>")
    assert msg.is_multipart()
    assert [p.get_content_subtype() for p in msg.iter_parts()] == ["plain", "html"]
    assert msg["Message-ID"].startswith("<")


# ------------------------------------- the agy envelope (what made runs slow)

def test_structured_output_is_preferred_over_the_response_string():
    """agy hands back a parsed `structured_output`. Reading the `response` string
    instead is what made successful batches look like failures."""
    envelope = {
        "status": "SUCCESS",
        "structured_output": {"jobs": [{"id": "fp0", "score": 71,
                                        "rationale": "Fits.", "flags": []}]},
        "response": "```json\n[{\"id\": \"fp0\", \"score\": 9, \"rationale\": \"stale\", \"flags\": []}]\n```",
    }
    got = score.parse_batch(envelope["structured_output"], {"fp0": {}})
    assert got["fp0"]["score"] == 71


def test_a_fenced_bare_array_response_still_parses():
    """When structured_output is absent the response arrives fence-wrapped AND as a
    bare array rather than the {"jobs": [...]} the schema asked for."""
    raw = "```json\n[{\"id\": \"fp0\", \"score\": 64, \"rationale\": \"ok fine\", \"flags\": []}]\n```"
    got = score.parse_batch(llm._extract_json(raw), {"fp0": {}})
    assert got["fp0"]["score"] == 64


# --------------------------------------------------- the experience ceiling

def test_a_role_above_the_ceiling_is_forced_below_the_email_threshold():
    """The guard that actually protects the complaint: whatever the model scores, a
    stated requirement above his ceiling must not reach the digest."""
    job = score.apply_ceiling({"score": 70, "years_required": 5, "flags": []}, 3, 45)
    assert job["score"] == 44
    assert any("above his 3-year ceiling" in f for f in job["flags"])


def test_a_role_within_the_ceiling_is_untouched():
    job = score.apply_ceiling({"score": 88, "years_required": 3, "flags": []}, 3, 45)
    assert job["score"] == 88 and job["flags"] == []


def test_an_already_low_scorer_is_still_flagged_with_the_reason():
    job = score.apply_ceiling({"score": 42, "years_required": 5, "flags": []}, 3, 45)
    assert job["score"] == 42
    assert any("above his 3-year ceiling" in f for f in job["flags"])


def test_the_trimmed_profile_keeps_the_rules_that_matter():
    profile = Path(ROOT / "PROFILE.md").read_text()
    trimmed = score.trim_profile(profile)
    for must in ("€3,122", "Do NOT claim", "Role fit", "Seniority fit"):
        assert must in trimmed, must
    assert len(trimmed) < len(profile)


def test_an_envelope_containing_a_fenced_string_is_not_shredded():
    """The bug that made batches fail intermittently: _extract_json stripped markdown
    fences BEFORE attempting a parse, so the agy envelope - valid JSON whose `response`
    field contains a fenced string - had its inner content extracted and the outer
    object destroyed. Parse first, strip fences only as a fallback."""
    import json as _json
    envelope = _json.dumps({
        "status": "SUCCESS",
        "response": "```json\n[{\"id\": \"fp0\", \"score\": 9}]\n```",
        "structured_output": {"jobs": [{"id": "fp0", "score": 71,
                                        "rationale": "Fits.", "flags": []}]},
    })
    parsed = llm._extract_json(envelope)
    assert isinstance(parsed, dict), "outer envelope must survive"
    assert parsed["structured_output"]["jobs"][0]["score"] == 71


def test_a_genuinely_fenced_payload_still_parses():
    assert llm._extract_json("```json\n[{\"b\": 2}]\n```") == [{"b": 2}]


# ------------------------------------------------------- the daily run guards

def test_a_live_lock_blocks_a_second_run():
    """The hourly catch-up must not re-enter a run that is already going."""
    import os, tempfile, run
    lock = Path(tempfile.mkdtemp()) / "daily.lock"
    lock.write_text(str(os.getpid()))          # this process is definitely alive
    assert run.lock_holder(lock) == os.getpid()
    assert lock.exists()


def test_a_stale_lock_is_cleared_rather_than_blocking_forever():
    """A lock left by a killed run must not block every future digest."""
    import tempfile, run
    lock = Path(tempfile.mkdtemp()) / "daily.lock"
    lock.write_text("999999")                  # a PID that cannot be running
    assert run.lock_holder(lock) is None
    assert not lock.exists()


def test_a_corrupt_lock_is_cleared():
    import tempfile, run
    lock = Path(tempfile.mkdtemp()) / "daily.lock"
    lock.write_text("not-a-pid")
    assert run.lock_holder(lock) is None
    assert not lock.exists()


def test_no_lock_means_no_holder():
    import tempfile, run
    assert run.lock_holder(Path(tempfile.mkdtemp()) / "absent.lock") is None
