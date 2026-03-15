#!/usr/bin/env python3
"""
job_scraper.py — Cybersecurity/IT job scraper for Salt Lake City, UT.

Scrapes LinkedIn and filters out likely fake or low-quality listings using
heuristic analysis of job titles, descriptions, and company signals.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_GOLDEN = "Golden"
STATUS_ACTIVE = "Active"
STATUS_LIKELY_CLOSED = "Likely Closed"
STATUS_UNKNOWN = "Unknown"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Job:
    title: str
    company: str
    location: str
    snippet: str          # Short description shown in search results
    url: str
    salary: Optional[str] = None
    posted: Optional[str] = None
    is_fake: bool = False
    fake_reasons: list[str] = field(default_factory=list)
    status: str = STATUS_UNKNOWN
    company_url: Optional[str] = None

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.company = self.company.strip()
        self.location = self.location.strip()
        self.snippet = self.snippet.strip()


# ---------------------------------------------------------------------------
# Fake-job detection
# ---------------------------------------------------------------------------

# Phrases that strongly suggest MLM, scam, or low-quality postings
_FAKE_PHRASES: list[str] = [
    r"unlimited earning",
    r"unlimited income",
    r"be your own boss",
    r"work from anywhere",
    r"financial freedom",
    r"passive income",
    r"multi.?level marketing",
    r"\bmlm\b",
    r"network marketing",
    r"commission.?only",
    r"no experience (necessary|required|needed)",
    r"make \$\d{3,}\+? (per|a) (day|week)",
    r"earn up to \$\d{3,}\+? (per|a) (day|week)",
    r"set your own (hours|schedule)",
    r"pyramid",
    r"\bamway\b",
    r"\bprimerica\b",
    r"\bcutco\b",
    r"\bvector marketing\b",
    r"\bherbalife\b",
    r"\baviator\b",
    r"insurance (sales )?agent.*independent",
    r"entry.?level.*unlimited",
    r"1099 (contractor|position)",
]

# Company name patterns that appear in many scam listings
_FAKE_COMPANY_PATTERNS: list[str] = [
    r"independent business owner",
    r"self.?employed",
    r"your own business",
]

_FAKE_TITLE_PATTERNS: list[str] = [
    r"life insurance (sales|agent)",
    r"insurance (sales )?agent",
    r"financial (advisor|representative)",
    r"sales representative.*commission",
    r"recruitment consultant.*commission",
]

# Minimum snippet length — very short descriptions are a red flag
_MIN_SNIPPET_LEN = 60


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_FAKE_RE = _compile(_FAKE_PHRASES)
_FAKE_COMPANY_RE = _compile(_FAKE_COMPANY_PATTERNS)
_FAKE_TITLE_RE = _compile(_FAKE_TITLE_PATTERNS)


def detect_fake(job: Job) -> tuple[bool, list[str]]:
    """Return (is_fake, reasons) for the given job listing."""
    reasons: list[str] = []
    text = f"{job.title} {job.company} {job.snippet}".lower()

    for pattern in _FAKE_RE:
        if pattern.search(text):
            reasons.append(f"suspicious phrase: '{pattern.pattern}'")

    for pattern in _FAKE_COMPANY_RE:
        if pattern.search(job.company.lower()):
            reasons.append(f"suspicious company name: '{pattern.pattern}'")

    for pattern in _FAKE_TITLE_RE:
        if pattern.search(job.title.lower()):
            reasons.append(f"suspicious title: '{pattern.pattern}'")

    # Only flag short snippets when the source provides some content.
    # LinkedIn search cards often omit descriptions entirely.
    if job.snippet and len(job.snippet) < _MIN_SNIPPET_LEN:
        reasons.append(f"description too short ({len(job.snippet)} chars)")

    if not job.company or job.company.lower() in {"confidential", "undisclosed", ""}:
        reasons.append("company name hidden or missing")

    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

_SEARCH_TERMS = [
    "cybersecurity",
    "information security analyst",
    "penetration tester",
    "SOC analyst",
    "network security engineer",
    "IT support",
    "systems administrator",
    "cloud security engineer",
    "DevSecOps",
]

_LINKEDIN_BASE = "https://www.linkedin.com/jobs/search/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class JobScraper:
    """Scrape cybersecurity/IT job listings from LinkedIn for a given location."""

    def __init__(
        self,
        location: str = "Salt Lake City, UT",
        delay: float = 2.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.location = location
        self.delay = delay
        self.session = session or requests.Session()
        self.session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        terms: Optional[list[str]] = None,
        max_per_term: int = 25,
    ) -> list[Job]:
        """
        Run searches for each term and return deduplicated, filtered jobs.

        Args:
            terms: List of search queries. Defaults to _SEARCH_TERMS.
            max_per_term: Maximum results to collect per query.

        Returns:
            List of Job objects that passed the fake-job filter.
        """
        return [j for j in self.search_raw(terms, max_per_term) if not j.is_fake]

    def search_raw(
        self,
        terms: Optional[list[str]] = None,
        max_per_term: int = 25,
    ) -> list[Job]:
        """Same as search() but returns all jobs including flagged ones."""
        terms = terms or _SEARCH_TERMS
        seen_urls: set[str] = set()
        all_jobs: list[Job] = []

        for term in terms:
            jobs = self._search_linkedin(term, max_results=max_per_term)
            for job in jobs:
                if job.url not in seen_urls:
                    seen_urls.add(job.url)
                    is_fake, reasons = detect_fake(job)
                    job.is_fake = is_fake
                    job.fake_reasons = reasons
                    all_jobs.append(job)
            time.sleep(self.delay)

        return all_jobs

    # ------------------------------------------------------------------
    # LinkedIn scraping
    # ------------------------------------------------------------------

    def _search_linkedin(self, query: str, max_results: int = 25) -> list[Job]:
        jobs: list[Job] = []
        start = 0
        per_page = 25  # LinkedIn shows 25 results per page

        while len(jobs) < max_results:
            params = {
                "keywords": query,
                "location": self.location,
                "f_TPR": "r2592000",  # posted in last 30 days
                "sortBy": "DD",       # most recent first
                "start": start,
            }
            url = f"{_LINKEDIN_BASE}?{urlencode(params)}"

            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
            except requests.RequestException as exc:
                print(f"[warn] LinkedIn request failed for '{query}' (start={start}): {exc}")
                break

            page_jobs = self._parse_linkedin_page(response.text)
            if not page_jobs:
                break

            jobs.extend(page_jobs)
            start += per_page

            if start > 0:
                time.sleep(self.delay)

        return jobs[:max_results]

    def _parse_linkedin_page(self, html: str) -> list[Job]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[Job] = []

        for card in soup.select("div.base-search-card"):
            try:
                job = self._parse_linkedin_card(card)
                if job:
                    jobs.append(job)
            except Exception:
                continue

        return jobs

    def _parse_linkedin_card(self, card: BeautifulSoup) -> Optional[Job]:
        title_el = card.select_one("h3.base-search-card__title")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        company_el = card.select_one("h4.base-search-card__subtitle")
        company = company_el.get_text(strip=True) if company_el else ""

        loc_el = card.select_one("span.job-search-card__location")
        location = loc_el.get_text(strip=True) if loc_el else self.location

        link_el = card.select_one("a.base-card__full-link")
        job_url = link_el.get("href", "").split("?")[0] if link_el else ""

        date_el = card.select_one("time.job-search-card__listdate")
        posted = date_el.get("datetime") or date_el.get_text(strip=True) if date_el else None

        # LinkedIn doesn't show salary or snippet in search cards;
        # use the benefits/tag line as a lightweight snippet proxy.
        benefit_el = card.select_one("span.job-posting-benefits__text")
        snippet = benefit_el.get_text(strip=True) if benefit_el else ""

        return Job(
            title=title,
            company=company,
            location=location,
            snippet=snippet,
            url=job_url,
            salary=None,
            posted=posted,
        )


# ---------------------------------------------------------------------------
# Cross-verification helpers
# ---------------------------------------------------------------------------

_CLOSED_PHRASES = [
    "no longer accepting applications",
    "this job is no longer available",
    "job has expired",
]

# Sites that aggregate jobs from many employers — NOT including ATS platforms
# (Greenhouse, Lever, Workday, iCIMS, Paylocity, etc.) because those host the
# company's own listing and count as a genuine company careers URL.
_AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "dice.com", "simplyhired.com", "careerbuilder.com",
    "snagajob.com", "lensa.com", "talent.com", "builtin.com",
    "wellfound.com", "joblist.com", "jooble.org", "jobzmall.com", "jobright.ai",
    "adzuna.com", "jobs2careers.com", "neuvoo.com", "jobrapido.com",
    "trovit.com", "totaljobs.com",
}

_DDG_URL = "https://html.duckduckgo.com/html/"


def _check_linkedin_status(url: str, session: requests.Session) -> str:
    """Return 'active', 'closed', or 'unknown' for a LinkedIn job URL."""
    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        if resp.status_code == 404:
            return "closed"
        text = resp.text.lower()
        if any(phrase in text for phrase in _CLOSED_PHRASES):
            return "closed"
        return "active"
    except Exception:
        return "unknown"


def _check_company_listing(
    company: str, title: str, session: requests.Session
) -> Optional[str]:
    """
    Return the company careers URL if found on a non-aggregator site, else None.

    Queries DuckDuckGo HTML search for the job title + company on careers/jobs
    pages, excluding the major aggregators.  The real destination URL is
    extracted from the ``uddg=`` query parameter embedded in each result link.

    A fresh session is used for every DDG call so that cookies accumulated
    during LinkedIn scraping do not trigger DDG's bot detection.
    """
    query = (
        f'"{title}" "{company}" (careers OR jobs) '
        "-site:linkedin.com -site:indeed.com -site:glassdoor.com"
    )
    ddg_session = requests.Session()
    ddg_session.headers.update(
        {k: v for k, v in session.headers.items() if k.lower() != "referer"}
    )
    try:
        resp = ddg_session.post(
            _DDG_URL,
            data={"q": query, "b": "", "kl": "us-en"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://duckduckgo.com/",
            },
            timeout=15,
        )
        # 202 = bot-detection challenge; treat as no results
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        for result in soup.select(".result"):
            if "result--no-result" in result.get("class", []):
                break  # DDG returned a "no results found" sentinel
            # Prefer the real URL from the uddg= redirect param; fall back to
            # display text with https:// prepended.
            link_el = result.select_one("a.result__a")
            display_el = result.select_one(".result__url")
            real_url = ""
            if link_el:
                href = link_el.get("href", "")
                if href.startswith("//"):
                    href = "https:" + href
                params = parse_qs(urlparse(href).query)
                real_url = params.get("uddg", [""])[0]
            if not real_url and display_el:
                display = display_el.get_text(strip=True)
                real_url = "https://" + display if display else ""
            if real_url and not any(agg in real_url for agg in _AGGREGATOR_DOMAINS):
                return real_url
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Verification pass
# ---------------------------------------------------------------------------

_STATUS_ORDER = {
    STATUS_GOLDEN: 0,
    STATUS_ACTIVE: 1,
    STATUS_UNKNOWN: 2,
    STATUS_LIKELY_CLOSED: 3,
}


def verify_jobs(
    jobs: list[Job],
    session: requests.Session,
    delay: float = 2.0,
) -> list[Job]:
    """
    Second-pass verification: check each job's LinkedIn page and optionally
    the company's own careers site.  Mutates job.status in place and returns
    the same list.
    """
    for job in jobs:
        linkedin_status = _check_linkedin_status(job.url, session)
        time.sleep(delay)

        if linkedin_status == "closed":
            job.status = STATUS_LIKELY_CLOSED
        elif linkedin_status == "unknown":
            job.status = STATUS_UNKNOWN
        else:  # "active"
            company_url = _check_company_listing(job.company, job.title, session)
            time.sleep(delay)
            if company_url:
                job.status = STATUS_GOLDEN
                job.company_url = company_url
            else:
                job.status = STATUS_ACTIVE

    return jobs


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

import csv as _csv
from dataclasses import asdict as _asdict


def save_to_csv(jobs: list[Job], path: str = "jobs.csv") -> None:
    """
    Write jobs to a CSV file.

    - ``status`` is the first column (most important signal).
    - Rows are sorted: Golden → Active → Unknown → Likely Closed.
    - ``fake_reasons`` list is joined with ' | ' for readability.
    """
    sorted_jobs = sorted(jobs, key=lambda j: _STATUS_ORDER.get(j.status, 99))

    if not sorted_jobs:
        print("[info] No jobs to save.")
        return

    fieldnames = ["status"] + [
        f for f in _asdict(sorted_jobs[0]).keys() if f != "status"
    ]

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for job in sorted_jobs:
            row = _asdict(job)
            row["fake_reasons"] = " | ".join(row["fake_reasons"])
            # Reorder so status is first
            ordered = {k: row[k] for k in fieldnames}
            writer.writerow(ordered)

    print(f"[info] Saved {len(sorted_jobs)} jobs to {path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

import argparse as _argparse
import json as _json


def _main() -> None:
    parser = _argparse.ArgumentParser(
        prog="job_scraper",
        description="Scrape cybersecurity/IT job listings from LinkedIn and filter out fake postings.",
    )
    parser.add_argument(
        "--location",
        default="Salt Lake City, UT",
        help='Location filter (default: "Salt Lake City, UT")',
    )
    parser.add_argument(
        "--max-per-term",
        type=int,
        default=25,
        metavar="N",
        help="Max results per search term (default: 25)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Seconds to wait between requests (default: 2.0)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the verification pass (faster, no status ranking)",
    )
    parser.add_argument(
        "--output",
        default="jobs.csv",
        metavar="PATH",
        help="CSV output path (default: jobs.csv)",
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="Also save results as JSON to this path",
    )
    args = parser.parse_args()

    scraper = JobScraper(location=args.location, delay=args.delay)

    print(f"[info] Scraping LinkedIn for jobs in {args.location!r} ...")
    jobs = scraper.search(max_per_term=args.max_per_term)
    print(f"[info] {len(jobs)} listings passed the fake-job filter")

    if not args.no_verify:
        print("[info] Running verification pass ...")
        jobs = verify_jobs(jobs, scraper.session, delay=args.delay)
        golden = sum(1 for j in jobs if j.status == STATUS_GOLDEN)
        active = sum(1 for j in jobs if j.status == STATUS_ACTIVE)
        closed = sum(1 for j in jobs if j.status == STATUS_LIKELY_CLOSED)
        print(f"[info] Golden: {golden}  Active: {active}  Likely Closed: {closed}")

    save_to_csv(jobs, path=args.output)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            _json.dump([_asdict(j) for j in jobs], fh, indent=2)
        print(f"[info] Saved {len(jobs)} jobs to {args.json}")


if __name__ == "__main__":
    _main()
