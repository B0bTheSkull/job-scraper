"""
Tests for job_scraper.py

Covers:
- Job dataclass construction and normalisation
- detect_fake() heuristics (true positives and true negatives)
- LinkedIn HTML page parsing via _parse_linkedin_page()
- Deduplication and filtering in JobScraper.search_raw() / search()
- Network errors are handled gracefully
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from job_scraper import Job, JobScraper, _MIN_SNIPPET_LEN, detect_fake


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_job(**kwargs) -> Job:
    """Return a Job with sensible defaults, overridden by kwargs."""
    defaults = dict(
        title="Security Analyst",
        company="Acme Corp",
        location="Salt Lake City, UT",
        snippet="Analyze security events, respond to incidents, and maintain SIEM."
        " Requires 2+ years of SOC experience and strong Python skills.",
        url="https://www.linkedin.com/jobs/view/abc123",
        salary="$80,000–$100,000 a year",
        posted="2026-03-13",
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _linkedin_card(
    title="Security Analyst",
    company="Acme Corp",
    location="Salt Lake City, UT",
    jk="abc123",
    snippet=None,
) -> str:
    """Return a minimal LinkedIn-style job card HTML snippet."""
    snippet_el = (
        f'<span class="job-posting-benefits__text">{snippet}</span>'
        if snippet else ""
    )
    return (
        f'<div class="base-search-card">'
        f'<h3 class="base-search-card__title">{title}</h3>'
        f'<h4 class="base-search-card__subtitle">{company}</h4>'
        f'<span class="job-search-card__location">{location}</span>'
        f'<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{jk}">Link</a>'
        f'<time class="job-search-card__listdate" datetime="2026-03-13">2 weeks ago</time>'
        f'{snippet_el}'
        f'</div>'
    )


def _linkedin_page(*cards: str) -> str:
    """Wrap card HTML snippets in a minimal page."""
    return f"<html><body>{''.join(cards)}</body></html>"


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

class TestJobDataclass:
    def test_strips_whitespace_on_init(self):
        job = Job(
            title="  Analyst  ",
            company=" Acme ",
            location=" SLC, UT ",
            snippet=" Do stuff. ",
            url="https://example.com",
        )
        assert job.title == "Analyst"
        assert job.company == "Acme"
        assert job.location == "SLC, UT"
        assert job.snippet == "Do stuff."

    def test_defaults(self):
        job = make_job()
        assert job.is_fake is False
        assert job.fake_reasons == []
        assert job.salary == "$80,000–$100,000 a year"

    def test_optional_fields_default_none(self):
        job = Job(title="T", company="C", location="L", snippet="S", url="U")
        assert job.salary is None
        assert job.posted is None


# ---------------------------------------------------------------------------
# detect_fake — true positives (should flag)
# ---------------------------------------------------------------------------

class TestDetectFakeTruePositives:
    @pytest.mark.parametrize("phrase", [
        "unlimited earning potential",
        "Unlimited Income opportunity",
        "be your own boss today",
        "Work from anywhere, anytime",
        "financial freedom awaits",
        "passive income stream",
        "Multi-Level Marketing team",
        "join our MLM network",
        "network marketing leader",
        "commission-only role",
        "commission only compensation",
        "No experience necessary",
        "no experience required",
        "make $500+ per day",
        "earn up to $300+ a week",
        "set your own hours",
        "pyramid structure",
        "Amway distributor",
        "Primerica agent",
        "Cutco sales",
        "Vector Marketing rep",
        "Herbalife wellness",
        "1099 contractor position",
    ])
    def test_flags_known_bad_phrase(self, phrase: str):
        job = make_job(snippet=f"Great opportunity! {phrase}. Join our team.")
        is_fake, reasons = detect_fake(job)
        assert is_fake, f"Expected to flag: '{phrase}'"
        assert reasons

    def test_flags_suspicious_company_name(self):
        job = make_job(company="Independent Business Owner")
        is_fake, reasons = detect_fake(job)
        assert is_fake
        assert any("company" in r for r in reasons)

    def test_flags_self_employed_company(self):
        job = make_job(company="Self-Employed")
        is_fake, _ = detect_fake(job)
        assert is_fake

    def test_flags_short_snippet(self):
        short = "A" * (_MIN_SNIPPET_LEN - 1)
        job = make_job(snippet=short)
        is_fake, reasons = detect_fake(job)
        assert is_fake
        assert any("short" in r for r in reasons)

    def test_flags_empty_company(self):
        job = make_job(company="")
        is_fake, reasons = detect_fake(job)
        assert is_fake
        assert any("company" in r for r in reasons)

    def test_flags_confidential_company(self):
        job = make_job(company="Confidential")
        is_fake, reasons = detect_fake(job)
        assert is_fake

    def test_flags_insurance_sales_title(self):
        job = make_job(title="Life Insurance Sales Agent")
        is_fake, reasons = detect_fake(job)
        assert is_fake
        assert any("title" in r for r in reasons)

    def test_accumulates_multiple_reasons(self):
        job = make_job(
            title="Financial Advisor",
            company="",
            snippet="A" * (_MIN_SNIPPET_LEN - 1),
        )
        is_fake, reasons = detect_fake(job)
        assert is_fake
        assert len(reasons) >= 2


# ---------------------------------------------------------------------------
# detect_fake — true negatives (should NOT flag)
# ---------------------------------------------------------------------------

class TestDetectFakeTrueNegatives:
    def test_legitimate_security_analyst(self):
        job = make_job()
        is_fake, reasons = detect_fake(job)
        assert not is_fake, f"False positive — reasons: {reasons}"

    def test_legitimate_sysadmin(self):
        job = make_job(
            title="Systems Administrator",
            company="Utah State University",
            snippet=(
                "Manage Linux and Windows servers, automate deployments with Ansible, "
                "monitor infrastructure using Prometheus and Grafana. On-call rotation."
            ),
        )
        is_fake, _ = detect_fake(job)
        assert not is_fake

    def test_legitimate_pentest_role(self):
        job = make_job(
            title="Penetration Tester",
            company="Coalfire Systems",
            snippet=(
                "Conduct network and web application penetration tests for clients. "
                "Produce detailed reports. OSCP or equivalent certification preferred."
            ),
        )
        is_fake, _ = detect_fake(job)
        assert not is_fake

    def test_snippet_at_minimum_length_is_ok(self):
        job = make_job(snippet="A" * _MIN_SNIPPET_LEN)
        is_fake, _ = detect_fake(job)
        assert not is_fake

    def test_salary_range_does_not_trigger(self):
        job = make_job(salary="$120,000 – $150,000 a year")
        is_fake, _ = detect_fake(job)
        assert not is_fake


# ---------------------------------------------------------------------------
# LinkedIn HTML parsing
# ---------------------------------------------------------------------------

class TestParseLinkedInPage:
    def setup_method(self):
        self.scraper = JobScraper()

    def test_parses_single_card(self):
        html = _linkedin_page(_linkedin_card())
        jobs = self.scraper._parse_linkedin_page(html)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Security Analyst"
        assert job.company == "Acme Corp"
        assert job.location == "Salt Lake City, UT"
        assert job.url == "https://www.linkedin.com/jobs/view/abc123"
        assert job.posted == "2026-03-13"

    def test_parses_multiple_cards(self):
        html = _linkedin_page(
            _linkedin_card(title="SOC Analyst", jk="j1"),
            _linkedin_card(title="Network Engineer", jk="j2"),
            _linkedin_card(title="Cloud Security", jk="j3"),
        )
        jobs = self.scraper._parse_linkedin_page(html)
        assert len(jobs) == 3
        titles = [j.title for j in jobs]
        assert "SOC Analyst" in titles
        assert "Network Engineer" in titles
        assert "Cloud Security" in titles

    def test_parses_snippet_when_present(self):
        html = _linkedin_page(_linkedin_card(snippet="Hybrid · Full-time"))
        jobs = self.scraper._parse_linkedin_page(html)
        assert jobs[0].snippet == "Hybrid · Full-time"

    def test_snippet_empty_when_absent(self):
        html = _linkedin_page(_linkedin_card(snippet=None))
        jobs = self.scraper._parse_linkedin_page(html)
        assert jobs[0].snippet == ""

    def test_empty_page_returns_empty_list(self):
        jobs = self.scraper._parse_linkedin_page("<html><body></body></html>")
        assert jobs == []

    def test_malformed_card_skipped(self):
        broken = "<div class='base-search-card'><p>no title here</p></div>"
        good = _linkedin_card(title="IT Support Specialist", jk="good1")
        html = _linkedin_page(broken, good)
        jobs = self.scraper._parse_linkedin_page(html)
        assert len(jobs) == 1
        assert jobs[0].title == "IT Support Specialist"

    def test_url_query_string_stripped(self):
        card = (
            '<div class="base-search-card">'
            '<h3 class="base-search-card__title">Analyst</h3>'
            '<h4 class="base-search-card__subtitle">Corp</h4>'
            '<span class="job-search-card__location">SLC, UT</span>'
            '<a class="base-card__full-link" '
            'href="https://www.linkedin.com/jobs/view/xyz?trackingId=abc">Link</a>'
            '</div>'
        )
        jobs = self.scraper._parse_linkedin_page(f"<html><body>{card}</body></html>")
        assert "?" not in jobs[0].url


# ---------------------------------------------------------------------------
# JobScraper — network layer (mocked)
# ---------------------------------------------------------------------------

class TestJobScraperNetwork:
    def _mock_response(self, html: str, status: int = 200) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status
        resp.text = html
        resp.raise_for_status = MagicMock()
        return resp

    @patch("job_scraper.time.sleep", return_value=None)
    def test_search_raw_returns_all_jobs(self, _sleep):
        scraper = JobScraper(delay=0)
        html = _linkedin_page(
            _linkedin_card(title="SOC Analyst", jk="j1"),
            _linkedin_card(title="Life Insurance Sales Agent", company="", jk="j2"),
        )
        scraper.session.get = MagicMock(return_value=self._mock_response(html))
        jobs = scraper.search_raw(terms=["cybersecurity"])
        assert len(jobs) == 2
        titles = [j.title for j in jobs]
        assert "SOC Analyst" in titles
        assert "Life Insurance Sales Agent" in titles

    @patch("job_scraper.time.sleep", return_value=None)
    def test_search_filters_fake_jobs(self, _sleep):
        scraper = JobScraper(delay=0)
        html = _linkedin_page(
            _linkedin_card(title="SOC Analyst", jk="j1"),
            _linkedin_card(title="Life Insurance Sales Agent", company="", jk="j2"),
        )
        scraper.session.get = MagicMock(return_value=self._mock_response(html))
        jobs = scraper.search(terms=["cybersecurity"])
        titles = [j.title for j in jobs]
        assert "SOC Analyst" in titles
        assert "Life Insurance Sales Agent" not in titles

    @patch("job_scraper.time.sleep", return_value=None)
    def test_deduplicates_across_terms(self, _sleep):
        scraper = JobScraper(delay=0)
        html = _linkedin_page(_linkedin_card(title="Network Engineer", jk="same"))
        scraper.session.get = MagicMock(return_value=self._mock_response(html))
        jobs = scraper.search_raw(terms=["network security", "network engineer"])
        urls = [j.url for j in jobs]
        assert len(urls) == len(set(urls)), "Duplicates found"

    @patch("job_scraper.time.sleep", return_value=None)
    def test_handles_request_exception_gracefully(self, _sleep):
        scraper = JobScraper(delay=0)
        scraper.session.get = MagicMock(side_effect=requests.ConnectionError("down"))
        jobs = scraper.search(terms=["cybersecurity"])
        assert jobs == []

    @patch("job_scraper.time.sleep", return_value=None)
    def test_stops_paging_when_no_results(self, _sleep):
        scraper = JobScraper(delay=0)
        call_count = 0

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._mock_response(_linkedin_page(_linkedin_card(jk="j1")))
            return self._mock_response("<html><body></body></html>")

        scraper.session.get = MagicMock(side_effect=mock_get)
        jobs = scraper.search_raw(terms=["cybersecurity"])
        assert len(jobs) == 1

    @patch("job_scraper.time.sleep", return_value=None)
    def test_respects_max_per_term(self, _sleep):
        scraper = JobScraper(delay=0)
        html = _linkedin_page(
            _linkedin_card(title="A", jk="j1"),
            _linkedin_card(title="B", jk="j2"),
            _linkedin_card(title="C", jk="j3"),
        )
        scraper.session.get = MagicMock(return_value=self._mock_response(html))
        jobs = scraper.search_raw(terms=["cybersecurity"], max_per_term=2)
        assert len(jobs) <= 2

    @patch("job_scraper.time.sleep", return_value=None)
    def test_is_fake_flag_set_correctly(self, _sleep):
        scraper = JobScraper(delay=0)
        html = _linkedin_page(
            _linkedin_card(title="SOC Analyst", jk="good"),
            _linkedin_card(title="Insurance Agent", company="Self-Employed", jk="bad"),
        )
        scraper.session.get = MagicMock(return_value=self._mock_response(html))
        jobs = scraper.search_raw(terms=["test"])
        fake_jobs = [j for j in jobs if j.is_fake]
        good_jobs = [j for j in jobs if not j.is_fake]
        assert any("Insurance" in j.title for j in fake_jobs)
        assert any("SOC" in j.title for j in good_jobs)
