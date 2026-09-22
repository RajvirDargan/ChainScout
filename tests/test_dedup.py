"""Tests for the parts that must not fail silently: URL canonicalization,
fingerprinting, aggregator rejection and the three-layer dedup."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import normalize as nz
import yaml  # noqa: E402
import render  # noqa: E402
import prescore  # noqa: E402
from store import Store  # noqa: E402


def job(company, title, location, url, geo="nl"):
    return {"company": company, "title": title, "location": location, "url": url,
            "canonical_url": nz.canonical_url(url), "source": "test", "geo": geo,
            "salary_text": "", "posted_date": "", "body": "x" * 500}


# ------------------------------------------------------------ canonicalization

def test_strips_tracking_params():
    a = nz.canonical_url("https://www.linkedin.com/jobs/view/123?utm_source=x&trackingId=q&ref=y")
    b = nz.canonical_url("https://linkedin.com/jobs/view/123/")
    assert a == b == "https://linkedin.com/jobs/view/123"


def test_keeps_meaningful_params():
    assert "jk=abc" in nz.canonical_url("https://nl.indeed.com/viewjob?jk=abc&utm_medium=x")


# ---------------------------------------------------------------- fingerprints

def test_fingerprint_collapses_board_noise():
    assert nz.fingerprint("Acme B.V.", "Junior Logistics Engineer (m/v) 40 uur", "Amsterdam, NL") \
        == nz.fingerprint("Acme", "Logistics Engineer", "Amsterdam")


def test_fingerprint_separates_real_differences():
    assert nz.fingerprint("Acme", "Logistics Engineer", "Amsterdam") \
        != nz.fingerprint("Picnic", "Logistics Engineer", "Amsterdam")
    assert nz.fingerprint("Acme", "Logistics Engineer", "Amsterdam") \
        != nz.fingerprint("Acme", "Supply Chain Analyst", "Amsterdam")


# ------------------------------------------------------- aggregator rejection

def test_rejects_indeed_search_pages():
    assert nz.looks_like_index("https://nl.indeed.com/q-logistics-engineer-vacatures.html", "")


def test_rejects_category_pages_and_count_titles():
    assert nz.looks_like_index("https://www.nationalevacaturebank.nl/vacatures/functie/proces-engineer", "")
    assert nz.looks_like_index("https://example.com/job/x", "300+ vacatures voor Supply Chain Engineer")


def test_accepts_real_postings():
    for url in [
        "https://www.linkedin.com/jobs/view/4458666142",
        "https://puma.wd502.myworkdayjobs.com/en-US/Work_at_stichd/job/Logistic-Engineer-1_R42699",
        "https://acme.recruitee.com/o/logistics-engineer",
    ]:
        assert not nz.looks_like_index(url, ""), url
        assert nz.looks_like_posting(url), url


# ------------------------------------------------------------- the three layers

def _store():
    tmp = tempfile.mkdtemp()
    return Store(Path(tmp) / "seen.sqlite")


def test_layer1_same_url_different_tracking():
    s = _store()
    s.insert_new(job("Acme", "Logistics Engineer", "Amsterdam",
                     "https://linkedin.com/jobs/view/999"))
    dupe = job("Totally Different Ltd", "Something Else", "Mars",
               "https://www.linkedin.com/jobs/view/999?utm_source=email")
    assert s.is_known(dupe) is not None


def test_layer2_same_job_different_boards():
    s = _store()
    s.insert_new(job("Coolblue", "Supply Chain Analyst", "Tilburg",
                     "https://careers.coolblue.nl/job/sca-1"))
    cross = job("Coolblue B.V.", "Supply Chain Analyst (m/v) 40 uur", "Tilburg, Netherlands",
                "https://nl.indeed.com/viewjob?jk=zzz")
    assert s.is_known(cross) is not None


def test_layer3_fuzzy_translation_drift():
    s = _store()
    s.insert_new(job("Vanderlande", "Logistics Process Engineer", "Veghel",
                     "https://vanderlande.com/job/lpe-1"))
    drifted = job("Vanderlande Industries", "Logistics Process Engineer",
                  "Veghel", "https://magnet.me/job/lpe-x")
    assert s.is_known(drifted) is not None


def test_genuinely_new_job_is_not_suppressed():
    s = _store()
    s.insert_new(job("Acme", "Logistics Engineer", "Amsterdam",
                     "https://acme.recruitee.com/o/le-1"))
    fresh = job("Picnic", "Operations Engineer", "Utrecht",
                "https://picnic.recruitee.com/o/oe-1")
    assert s.is_known(fresh) is None


def test_below_threshold_jobs_are_never_reoffered():
    s = _store()
    j = job("SmallCo", "Logistics Coordinator", "Venlo", "https://smallco.nl/job/lc-1")
    fp = s.insert_new(j)
    s.mark_below_threshold([fp])
    assert s.is_known(j) is not None
    assert s.stats().get("below_threshold") == 1


def test_sent_marking_is_explicit():
    s = _store()
    fp = s.insert_new(job("Acme", "Logistics Engineer", "Amsterdam",
                          "https://acme.recruitee.com/o/le-2"))
    assert s.stats().get("new") == 1
    s.mark_sent([fp], "2026-08-30")
    assert s.stats().get("sent") == 1


# -------------------------------------------------------------------- prescreen

def test_rejects_out_of_range_seniority():
    rules = {"reject_title_terms": ["head of", "director"], "min_body_chars": 100,
             "flag_body_terms": []}
    keep, reason, _ = prescore.screen(
        {"title": "Logistics Engineer", "body": "You bring 8 years of experience. " + "x" * 200,
         "geo": "nl"}, rules)
    assert not keep and "8_years" in reason


def test_flags_local_language_requirement_without_dropping():
    """A local-language requirement is advisory, never a hard reject — the scorer
    decides, because plenty of postings list the language as "a plus"."""
    rules = {"reject_title_terms": [], "min_body_chars": 100,
             "flag_body_terms": ["deutschkenntnisse"]}
    keep, _, flags = prescore.screen(
        {"title": "Logistics Engineer", "geo": "de",
         "body": "Gute deutschkenntnisse erwartet. " + "x" * 200}, rules)
    assert keep and "deutschkenntnisse" in flags


def _floor_rules(floor=None, label=None):
    r = {"reject_title_terms": [], "min_body_chars": 100, "flag_body_terms": []}
    if floor:
        r["salary_floor_month"], r["salary_floor_label"] = floor, label
    return r


def test_flags_salary_below_the_configured_permit_floor():
    """Belgium's Flanders under-30 single-permit rate: EUR 39,129.60/yr = 3,260.80/mo."""
    _, _, flags = prescore.screen(
        {"title": "Logistics Engineer", "geo": "be",
         "body": "Salary €2.600 - €2.900 per month. " + "x" * 200},
        _floor_rules(3260.80, "Flanders under-30"))
    assert any("BELOW" in f and "Flanders under-30" in f for f in flags)


def test_salary_above_the_configured_floor_clears_it():
    _, _, flags = prescore.screen(
        {"title": "Logistics Engineer", "geo": "be",
         "body": "Salary €3.600 per month. " + "x" * 200},
        _floor_rules(3260.80, "Flanders under-30"))
    assert any("clears" in f for f in flags)


def test_geography_without_a_floor_is_not_judged_against_one():
    """Germany's sec. 18b has no statutory minimum under 45. A missing floor must skip
    the check entirely — defaulting to zero would mark every posting as clearing it."""
    _, _, flags = prescore.screen(
        {"title": "Logistics Engineer", "geo": "de",
         "body": "Salary €2.400 per month. " + "x" * 200}, _floor_rules())
    assert not any("BELOW" in f or "clears" in f for f in flags)


def test_containment_rule_does_not_overmatch_different_roles():
    """The company-containment shortcut must not collapse genuinely different jobs."""
    s = _store()
    s.insert_new(job("Vanderlande", "Logistics Process Engineer", "Veghel",
                     "https://vanderlande.com/job/lpe-1"))
    other_role = job("Vanderlande Industries", "Supply Chain Analyst", "Veghel",
                     "https://vanderlande.com/job/sca-1")
    assert s.is_known(other_role) is None


def test_unsent_jobs_carry_forward_to_the_next_run():
    """A collected-but-never-emailed job must reappear, not vanish."""
    s = _store()
    j = job("Flexport", "Logistics Analyst", "Amsterdam",
            "https://flexport.com/job/la-1")
    j["body"] = "full description here"
    fp = s.insert_new(j)
    carried = s.unsent_carry_forward(exclude=set())
    assert [c["fingerprint"] for c in carried] == [fp]
    assert carried[0]["body"] == "full description here"

    s.mark_sent([fp], "2026-08-30")
    assert s.unsent_carry_forward(exclude=set()) == []


def test_linkedin_slug_urls_are_recognised_as_postings():
    """LinkedIn uses /jobs/view/<slug>-<id>; a bare \\d+ pattern missed every one."""
    url = "https://uk.linkedin.com/jobs/view/logistics-process-engineer-at-isq-ltd-4458513258"
    assert nz.looks_like_posting(url)
    assert not nz.looks_like_index(url, "")


def test_search_hit_geography_is_taken_from_the_posting_not_the_query():
    """A 'Netherlands' query returns US and UK roles; the posting's own location wins."""
    hit = {
        "url": "https://www.linkedin.com/jobs/view/sr-process-engineer-at-novelis-4460542166",
        "title": "Sr Process Engineer",
        "markdown": "We are hiring a process engineer. " + "detail " * 100,
        "html": '<script type="application/ld+json">'
                '{"@type":"JobPosting","title":"Sr Process Engineer",'
                '"hiringOrganization":{"name":"Novelis"},'
                '"jobLocation":{"address":{"addressLocality":"Atlanta",'
                '"addressCountry":"United States"}}}</script>',
    }
    job, reason = nz.from_search_hit(hit, "de", 300)
    assert job is None and reason.startswith("geo_mismatch")


def test_matching_geography_is_kept():
    hit = {
        "url": "https://www.linkedin.com/jobs/view/logistics-engineer-at-acme-4460542167",
        "title": "Logistics Engineer",
        "markdown": "We are hiring a logistics engineer. " + "detail " * 100,
        "html": '<script type="application/ld+json">'
                '{"@type":"JobPosting","title":"Logistics Engineer",'
                '"hiringOrganization":{"name":"Acme"},'
                '"jobLocation":{"address":{"addressLocality":"Hamburg",'
                '"addressCountry":"Germany"}}}</script>',
    }
    job, reason = nz.from_search_hit(hit, "de", 300)
    assert job is not None, reason
    assert job["company"] == "Acme"


def test_careers_landing_pages_without_jsonld_are_rejected():
    """A /job/-shaped URL with no JSON-LD and no location is a landing page or article,
    not a posting — this is how "ISRO Exam Date 2026" slipped through."""
    hit = {
        "url": "https://www.adda247.com/job/isro-exam-date-2026",
        "title": "ISRO Exam Date 2026 Out, Check Complete Exam Schedule",
        "markdown": "The exam schedule has been released. " + "detail " * 100,
    }
    job, reason = nz.from_search_hit(hit, "india", 300)
    assert job is None and reason == "no_jsonld_and_no_location"


# ---------------------------------------------------------------- candidate cap

def _round_robin(candidates, cap, prio):
    """Mirror of the cap logic in run.py collect(). Kept in the test rather than
    imported because collect() is one long function with live network calls."""
    by_geo = {}
    for j in candidates:
        by_geo.setdefault(j.get("geo") or "", []).append(j)
    queues = [by_geo[g] for g in sorted(by_geo, key=lambda g: (prio.get(g, 9), g))]
    picked = []
    while len(picked) < cap and any(queues):
        for q in queues:
            if not q:
                continue
            picked.append(q.pop(0))
            if len(picked) >= cap:
                break
    return picked


def test_candidate_cap_does_not_starve_low_priority_geographies():
    """Germany alone yields ~165 candidates against a cap of 60. A strict priority sort
    gave Germany all 60 slots and every other country zero — permanently, because
    dropped candidates are deleted and re-collected identically the next run."""
    prio = {"de": 1, "be": 2, "ie": 3, "pl": 4, "uae": 5, "india": 6}
    counts = {"de": 165, "be": 25, "ie": 84, "pl": 14, "uae": 81, "india": 58}
    candidates = [{"geo": g} for g, n in counts.items() for _ in range(n)]

    picked = _round_robin(candidates, 60, prio)

    assert len(picked) == 60
    got = {g: sum(1 for j in picked if j["geo"] == g) for g in counts}
    assert all(got[g] > 0 for g in counts), f"a geography was starved: {got}"
    # Priority still wins ties within a round, so Germany is never worse off.
    assert got["de"] >= got["india"]


def test_candidate_cap_redistributes_when_a_geography_runs_dry():
    """A thin geography must not hold an empty slot — Poland returned 14 candidates
    against a cap that would otherwise give it more."""
    prio = {"de": 1, "pl": 4}
    candidates = [{"geo": "de"} for _ in range(50)] + [{"geo": "pl"} for _ in range(3)]
    picked = _round_robin(candidates, 20, prio)
    assert len(picked) == 20
    assert sum(1 for j in picked if j["geo"] == "pl") == 3
    assert sum(1 for j in picked if j["geo"] == "de") == 17


# ---------------------------------------------------------------- geography hints

def test_accented_locations_classify_to_the_right_country():
    """Adzuna returns "Krakow" accented and "Wroclaw" with a stroked l. Matching
    unaccented hints against them silently dropped real postings as out-of-scope."""
    for loc, expected in [
        ("Krak\u00f3w, ma\u0142opolskie", "pl"), ("Wroc\u0142aw", "pl"),
        ("Gda\u0144sk, pomorskie", "pl"), ("Bydgoszcz, kujawsko-pomorskie", "pl"),
        ("K\u00f6ln", "de"), ("M\u00fcnchen", "de"), ("D\u00fcsseldorf", "de"),
        ("Sint-Niklaas", "be"), ("Steenokkerzeel, Halle-Vilvoorde", "be"),
        ("Dublin", "ie"), ("Dubai", "uae"), ("Pune", "india"),
    ]:
        assert nz.classify_geo(loc) == expected, f"{loc} -> {nz.classify_geo(loc)}"


def test_netherlands_is_in_scope_and_takes_priority():
    """NL is priority 1 and gets its own section in the digest."""
    for loc in ("Amsterdam", "Eindhoven, Netherlands", "Veghel", "Schiphol"):
        assert nz.classify_geo(loc) == "nl", f"{loc} -> {nz.classify_geo(loc)}"
    assert render.GEO_ORDER["nl"] == 0
    assert render.PRIORITY_GEO == "nl"


def test_unsearched_geographies_are_out_of_scope():
    for loc in ("Austin, TX", "Singapore", "Sao Paulo"):
        assert nz.classify_geo(loc) is None, f"{loc} unexpectedly in scope"


def test_digest_splits_netherlands_from_everywhere_else():
    """NL gets its own top-level section, and a guaranteed floor of email slots so it
    does not have to out-score the rest of the world for every place."""
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    cfg["thresholds"].update(max_jobs_email=6, min_priority_geo_slots=3)
    scored = ([{"fingerprint": f"nl{i}", "url": f"https://x/nl{i}", "title": "Supply Chain Analyst",
                "company": "NLCo", "location": "Amsterdam", "geo": "nl", "source": "adzuna",
                "posted_date": "", "score": 60, "flags": []} for i in range(5)]
              + [{"fingerprint": f"de{i}", "url": f"https://x/de{i}", "title": "Supply Chain Analyst",
                  "company": "DECo", "location": "Hamburg", "geo": "de", "source": "adzuna",
                  "posted_date": "", "score": 90, "flags": []} for i in range(5)])

    subject, html, txt, fps = render.build(scored, cfg, {"run_date": "2026-09-02"})

    assert len(fps) == 6
    # Without the reserved floor, all six slots would go to the 90-point German roles.
    assert sum(1 for f in fps if f.startswith("nl")) == 3
    assert "NETHERLANDS (3)" in txt and "BEYOND THE NETHERLANDS (3)" in txt
    assert "NL 3 / elsewhere 3" in subject


# ------------------------------------------------- seniority ceiling (max 3 years)

SENIORITY_RULES = {
    "reject_title_terms": [], "min_body_chars": 50, "flag_body_terms": [],
    "max_years_experience": 3,
    "reject_seniority_always": ["senior", "sr.", "lead", "principal", "head of",
                                "director", "vice president", "vp ", "chief"],
    "reject_seniority_unless_junior": ["manager", "supervisor"],
    "junior_markers": ["trainee", "graduate", "junior", "entry level", "associate"],
    "seniority_false_positives": ["disturbance management", "lead time",
                                  "inventory management", "warehouse management"],
}


def _screen(title, body="Logistics work. " * 20):
    return prescore.screen({"title": title, "body": body, "geo": "nl"}, SENIORITY_RULES)


def test_bare_plus_years_is_now_caught():
    """'5+ years' as its own bullet used to return None and reach the digest."""
    keep, reason, _ = _screen("Logistics Engineer", "We need 5+ years. " + "x" * 200)
    assert not keep and reason == "requires_5_years"


def test_experience_keyword_before_the_number_is_caught():
    """'Experience: Minimum 8 years' — the keyword sits before, not after."""
    keep, reason, _ = _screen("Warehouse Associate",
                              "Experience: Minimum 8 years. " + "x" * 200)
    assert not keep and reason == "requires_8_years"


def test_three_years_is_still_allowed():
    keep, _, flags = _screen("Logistics Engineer",
                             "You bring 3 years of experience. " + "x" * 200)
    assert keep
    assert any("ceiling" in f for f in flags)


def test_a_range_reads_its_lower_bound():
    """'3-5 years' asks a minimum of 3, which is within his ceiling."""
    keep, _, _ = _screen("Supply Chain Analyst",
                         "We ask 3-5 years of experience. " + "x" * 200)
    assert keep


def test_four_years_is_rejected():
    keep, reason, _ = _screen("Supply Chain Analyst",
                              "At least 4 years relevant experience. " + "x" * 200)
    assert not keep and reason == "requires_4_years"


def test_age_text_is_not_an_experience_bar():
    keep, _, _ = _screen("Logistics Coordinator",
                         "Must be at least 18 years of age. " + "x" * 200)
    assert keep


def test_senior_titles_are_rejected():
    for title in ["Senior Logistics Engineer", "Lead Supply Chain Analyst",
                  "Head of Operations", "Principal Engineer", "Sr. Planner"]:
        keep, reason, _ = _screen(title)
        assert not keep and reason.startswith("seniority:"), title


def test_manager_titles_are_rejected():
    """AME Customs Manager reached the last digest and should not have."""
    for title in ["AME Customs Manager", "Logistics Manager", "Warehouse Supervisor"]:
        keep, reason, _ = _screen(title)
        assert not keep and reason.startswith("seniority:"), title


def test_domain_words_are_not_mistaken_for_rank():
    """'Disturbance Management' is a domain phrase, and 'Lead Time' is a metric. An
    over-rejection is invisible to him, so it is worse than a low score."""
    for title in ["Logistics Engineer Disturbance Management", "Lead Time Analyst",
                  "Inventory Management Analyst", "Warehouse Management Systems Analyst"]:
        keep, reason, _ = _screen(title)
        assert keep, f"{title} wrongly rejected as {reason}"


def test_executive_is_not_treated_as_senior():
    """In Gulf and Indian markets 'Executive' commonly means a junior IC, so the years
    gate and the scorer judge it rather than a blanket title ban."""
    keep, _, _ = _screen("Supply Chain Executive")
    assert keep


def test_junior_marker_rescues_the_manager_family():
    """'Management Trainee' is a graduate scheme, not a management role."""
    for title in ["Management Trainee", "Graduate Supply Chain Programme",
                  "Junior Operations Manager", "Associate Process Engineer"]:
        keep, reason, _ = _screen(title)
        assert keep, f"{title} rejected as {reason}"


def test_junior_marker_does_not_rescue_an_explicit_senior_title():
    keep, reason, _ = _screen("Senior Associate, Logistics")
    assert not keep and reason == "seniority:senior"
