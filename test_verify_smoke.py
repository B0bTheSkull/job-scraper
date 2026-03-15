"""
Smoke test for the cross-verification additions to job_scraper.py.
No real network calls — uses a minimal mock session.
"""
import sys
import csv
import tempfile
import os
from unittest.mock import MagicMock, patch

from job_scraper import (
    Job,
    STATUS_GOLDEN, STATUS_ACTIVE, STATUS_LIKELY_CLOSED, STATUS_UNKNOWN,
    _check_linkedin_status,
    _check_company_listing,
    verify_jobs,
    save_to_csv,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, condition):
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}")
    results.append(condition)


def mock_response(status_code=200, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


# ---------------------------------------------------------------------------
print("\n=== _check_linkedin_status ===")

session = MagicMock()

# 404 → closed
session.get.return_value = mock_response(404)
check("404 → closed", _check_linkedin_status("http://x", session) == "closed")

# closed phrase in body → closed
session.get.return_value = mock_response(200, "Sorry, no longer accepting applications here.")
check("closed phrase → closed", _check_linkedin_status("http://x", session) == "closed")

# normal page → active
session.get.return_value = mock_response(200, "Apply now for this exciting role!")
check("normal page → active", _check_linkedin_status("http://x", session) == "active")

# exception → unknown
session.get.side_effect = Exception("timeout")
check("exception → unknown", _check_linkedin_status("http://x", session) == "unknown")
session.get.side_effect = None  # reset


# ---------------------------------------------------------------------------
print("\n=== _check_company_listing ===")

# uddg= param carries the real destination URL
DDG_HTML_NON_AGGREGATOR = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcareers.acmecorp.com%2Fjobs%2F123">Job</a>
    <span class="result__url">careers.acmecorp.com/jobs/123</span>
  </div>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2F456">Job2</a>
    <span class="result__url">linkedin.com/jobs/456</span>
  </div>
</body></html>
"""

DDG_HTML_ONLY_AGGREGATORS = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2F1">J</a>
    <span class="result__url">linkedin.com/jobs/1</span>
  </div>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.indeed.com%2Fjobs%2F2">J</a>
    <span class="result__url">indeed.com/jobs/2</span>
  </div>
</body></html>
"""

DDG_HTML_EMPTY = "<html><body></body></html>"

outer_session = MagicMock()  # passed in (not used for DDG in the new code)

def ddg_check(html_text, *, exc=None):
    """Run _check_company_listing with a mocked internal DDG session."""
    mock_ddg = MagicMock()
    if exc:
        mock_ddg.post.side_effect = exc
    else:
        mock_ddg.post.return_value = mock_response(200, html_text)
    with patch("job_scraper.requests.Session", return_value=mock_ddg):
        return _check_company_listing("Acme Corp", "SOC Analyst", outer_session)

# Non-aggregator URL → returns the URL string
result = ddg_check(DDG_HTML_NON_AGGREGATOR)
check("non-aggregator URL → returns URL string",
      isinstance(result, str) and "acmecorp.com" in result)

# Only aggregators → None
check("only aggregators → None", ddg_check(DDG_HTML_ONLY_AGGREGATORS) is None)

# Empty results → None
check("empty results → None", ddg_check(DDG_HTML_EMPTY) is None)

# Exception → None
check("exception → None", ddg_check("", exc=Exception("network error")) is None)

# 202 bot-block → None
mock_ddg_202 = MagicMock()
mock_ddg_202.post.return_value = mock_response(202, "anomaly modal")
with patch("job_scraper.requests.Session", return_value=mock_ddg_202):
    check("202 bot-block → None",
          _check_company_listing("Acme Corp", "SOC Analyst", outer_session) is None)


# ---------------------------------------------------------------------------
print("\n=== verify_jobs ===")

def make_job(title="SOC Analyst", company="Acme", url="http://li/1"):
    return Job(title=title, company=company, location="SLC", snippet="Great role", url=url)

session3 = MagicMock()

# LinkedIn closed → Likely Closed (no DDG call)
session3.get.return_value = mock_response(200, "no longer accepting applications")
j = make_job()
verify_jobs([j], session3, delay=0)
check("LinkedIn closed → Likely Closed", j.status == STATUS_LIKELY_CLOSED)
check("DDG not called when closed", session3.post.call_count == 0)

session3.reset_mock()

# LinkedIn active + DDG finds non-aggregator → Golden + company_url set
session3.get.return_value = mock_response(200, "Apply now!")
mock_ddg_golden = MagicMock()
mock_ddg_golden.post.return_value = mock_response(200, DDG_HTML_NON_AGGREGATOR)
j = make_job()
with patch("job_scraper.requests.Session", return_value=mock_ddg_golden):
    verify_jobs([j], session3, delay=0)
check("active + company found → Golden", j.status == STATUS_GOLDEN)
check("company_url populated for Golden", j.company_url is not None and "acmecorp.com" in j.company_url)

session3.reset_mock()

# LinkedIn active + DDG only aggregators → Active, company_url stays None
session3.get.return_value = mock_response(200, "Apply now!")
mock_ddg_agg = MagicMock()
mock_ddg_agg.post.return_value = mock_response(200, DDG_HTML_ONLY_AGGREGATORS)
j = make_job()
with patch("job_scraper.requests.Session", return_value=mock_ddg_agg):
    verify_jobs([j], session3, delay=0)
check("active + no company listing → Active", j.status == STATUS_ACTIVE)
check("company_url is None for Active", j.company_url is None)

session3.reset_mock()

# LinkedIn unknown → Unknown (no DDG call)
session3.get.side_effect = Exception("timeout")
j = make_job()
verify_jobs([j], session3, delay=0)
check("LinkedIn error → Unknown", j.status == STATUS_UNKNOWN)
check("DDG not called on unknown", session3.post.call_count == 0)


# ---------------------------------------------------------------------------
print("\n=== save_to_csv ===")

jobs_csv = [
    Job("Analyst", "Corp A", "SLC", "Good job", "http://a", status=STATUS_ACTIVE),
    Job("Engineer", "Corp B", "SLC", "Great job", "http://b", status=STATUS_LIKELY_CLOSED),
    Job("Architect", "Corp C", "SLC", "Top job", "http://c", status=STATUS_GOLDEN),
    Job("Admin", "Corp D", "SLC", "OK job", "http://d", status=STATUS_UNKNOWN),
]

with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
    tmp_path = f.name

save_to_csv(jobs_csv, tmp_path)

with open(tmp_path, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

os.unlink(tmp_path)

check("status is first column", list(rows[0].keys())[0] == "status")
check("4 rows written", len(rows) == 4)
check("Golden is first row", rows[0]["status"] == STATUS_GOLDEN)
check("Active is second row", rows[1]["status"] == STATUS_ACTIVE)
check("Unknown is third row", rows[2]["status"] == STATUS_UNKNOWN)
check("Likely Closed is last row", rows[3]["status"] == STATUS_LIKELY_CLOSED)


# ---------------------------------------------------------------------------
print()
passed = sum(results)
total = len(results)
if passed == total:
    print(f"\033[32mAll {total} checks passed.\033[0m")
    sys.exit(0)
else:
    print(f"\033[31m{total - passed}/{total} checks FAILED.\033[0m")
    sys.exit(1)
