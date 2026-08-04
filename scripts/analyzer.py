"""
analyzer.py — Website Audit & Report Engine for B2B Lead Generation.

Audits a business website across Security, Performance, Mobile Readiness,
SEO and Trust signals, and produces a structured, scored report. The same
report object powers three things:
  1. The dashboard's "Audit Context" column (triage).
  2. email_generator.py's personalisation (mentions real, specific flaws).
  3. The demo landing page's "Your Free Website Audit" section.

Implements retry adapters for flaky servers and gracefully handles WAF /
bot-protection responses instead of treating them as hard failures.
"""

import logging
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check registry — each check contributes a weight to the overall score.
# Keeping this data-driven (rather than a pile of if/else) makes it trivial
# to add new checks later (e.g. Core Web Vitals via PageSpeed API) without
# touching the scoring logic.
# ---------------------------------------------------------------------------
CHECK_WEIGHTS: dict[str, int] = {
    "ssl": 20,
    "speed": 15,
    "mobile": 20,
    "seo_title": 10,
    "seo_meta": 10,
    "seo_h1": 10,
    "favicon": 5,
    "social_tags": 10,
}


class WebsiteAnalyzer:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        """Create a resilient requests session with automatic retries."""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 504],  # do NOT retry on WAF (401/403/503)
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        return session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def analyze(self, url: str) -> dict[str, object]:
        """Audit the target URL and return a structured report.

        Returns a dict shaped like::

            {
                "score": 62,                     # 0-100, higher = healthier site
                "grade": "C",                     # A-F letter grade for quick scanning
                "is_protected": False,
                "error": None,
                "checks": [
                    {"id": "ssl", "title": "Missing SSL Certificate", "status": "fail",
                     "detail": "...", "recommendation": "..."},
                    ...
                ],
                # legacy fields kept for backward compatibility with older
                # call sites / CSVs that only look at flaw_count/primary_flaw:
                "flaw_count": 3,
                "primary_flaw": "Missing SSL Certificate (Not Secure)",
            }
        """
        if not url.startswith("http"):
            url = f"http://{url}"

        checks: list[dict[str, str]] = []
        result: dict[str, object] = {
            "score": None,
            "grade": None,
            "is_protected": False,
            "error": None,
            "checks": checks,
            "flaw_count": 0,
            "primary_flaw": "",
        }

        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True)

            if response.status_code in (401, 403, 503):
                result["is_protected"] = True
                result["primary_flaw"] = "protected_asset"
                result["score"] = 70  # can't fully audit; assume roughly average, don't punish
                result["grade"] = self._grade(70)
                return result

            response.raise_for_status()
            final_url = response.url
            ttfb = response.elapsed.total_seconds()
            soup = BeautifulSoup(response.content, "html.parser")

            checks.append(self._check_ssl(final_url))
            checks.append(self._check_speed(ttfb))
            checks.append(self._check_mobile(soup))
            checks.append(self._check_title(soup))
            checks.append(self._check_meta_description(soup))
            checks.append(self._check_h1(soup))
            checks.append(self._check_favicon(soup, final_url))
            checks.append(self._check_social_tags(soup))

        except requests.exceptions.SSLError:
            checks.append(self._fail("ssl", "Broken SSL Certificate",
                                      "The SSL certificate is invalid or expired.",
                                      "Reissue and correctly install a valid TLS certificate."))
        except requests.exceptions.ConnectionError:
            result["error"] = "Connection Failed"
            checks.append(self._fail("uptime", "Website is unreachable or down",
                                      "The server refused the connection.",
                                      "Verify hosting/DNS is active; migrate to reliable hosting."))
        except requests.exceptions.Timeout:
            result["error"] = "Timeout"
            checks.append(self._fail("uptime", "Server connection timed out",
                                      "The server did not respond within the timeout window.",
                                      "Investigate server/hosting performance or switch providers."))
        except Exception as e:  # noqa: BLE001
            logger.debug("Analysis failed for %s: %s", url, e)
            result["error"] = str(e)
            checks.append(self._fail("uptime", "Website is unreachable",
                                      str(e), "Investigate hosting/DNS configuration."))

        score, grade = self._score(checks)
        result["score"] = score
        result["grade"] = grade

        failing = [c for c in checks if c["status"] == "fail"]
        result["flaw_count"] = len(failing)
        if failing:
            # Preserve original priority ordering: security > mobile > speed > SEO
            priority = ["ssl", "uptime", "mobile", "speed", "seo_title", "seo_meta", "seo_h1", "favicon", "social_tags"]
            failing_sorted = sorted(failing, key=lambda c: priority.index(c["id"]) if c["id"] in priority else 99)
            result["primary_flaw"] = failing_sorted[0]["title"]

        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    @staticmethod
    def _pass(check_id: str, title: str, detail: str) -> dict[str, str]:
        return {"id": check_id, "title": title, "status": "pass", "detail": detail, "recommendation": ""}

    @staticmethod
    def _fail(check_id: str, title: str, detail: str, recommendation: str) -> dict[str, str]:
        return {"id": check_id, "title": title, "status": "fail", "detail": detail, "recommendation": recommendation}

    def _check_ssl(self, final_url: str) -> dict[str, str]:
        if final_url.startswith("https://"):
            return self._pass("ssl", "Valid HTTPS", "Site is served securely over HTTPS.")
        return self._fail(
            "ssl", "Missing SSL Certificate (Not Secure)",
            "The site is served over plain HTTP.",
            "Install a free TLS certificate (e.g. Let's Encrypt) and force HTTPS redirects.",
        )

    def _check_speed(self, ttfb: float) -> dict[str, str]:
        if ttfb <= 1.2:
            return self._pass("speed", "Fast server response", f"Time-to-first-byte was {ttfb:.2f}s.")
        if ttfb <= 2.5:
            return self._fail(
                "speed", f"Sluggish load time ({ttfb:.1f}s)",
                "Server response is slower than the ~1s users expect.",
                "Enable caching/CDN and compress assets to cut response time.",
            )
        return self._fail(
            "speed", f"Extremely slow load time ({ttfb:.1f}s)",
            "Visitors are likely abandoning the page before it loads.",
            "Move to modern static hosting (e.g. Vercel/Netlify/CDN) — this alone can 10x load speed.",
        )

    def _check_mobile(self, soup: BeautifulSoup) -> dict[str, str]:
        viewport = soup.find("meta", attrs={"name": "viewport"})
        if viewport:
            return self._pass("mobile", "Mobile responsive", "Viewport meta tag is present.")
        return self._fail(
            "mobile", "Not mobile responsive",
            "No viewport meta tag — the site will render broken/tiny on phones.",
            "Rebuild with a responsive, mobile-first layout (most local searches are on mobile).",
        )

    def _check_title(self, soup: BeautifulSoup) -> dict[str, str]:
        title = soup.find("title")
        if title and title.get_text(strip=True):
            return self._pass("seo_title", "Page title present", f"Title: \"{title.get_text(strip=True)[:60]}\"")
        return self._fail(
            "seo_title", "Missing page title",
            "No <title> tag — hurts click-through rate on Google.",
            "Add a descriptive, keyword-rich <title> for every page.",
        )

    def _check_meta_description(self, soup: BeautifulSoup) -> dict[str, str]:
        description = soup.find("meta", attrs={"name": "description"})
        if description and description.get("content", "").strip():
            return self._pass("seo_meta", "Meta description present", "Search snippet is controlled, not auto-generated.")
        return self._fail(
            "seo_meta", "Missing meta description",
            "Google will auto-generate a snippet, often poorly.",
            "Write a compelling 150-character meta description per page.",
        )

    def _check_h1(self, soup: BeautifulSoup) -> dict[str, str]:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return self._pass("seo_h1", "Primary heading (H1) present", "Clear page hierarchy for SEO.")
        return self._fail(
            "seo_h1", "Missing primary heading (H1)",
            "No H1 — search engines can't tell what the page is about.",
            "Add one clear, keyword-relevant H1 per page.",
        )

    def _check_favicon(self, soup: BeautifulSoup, final_url: str) -> dict[str, str]:
        icon = soup.find("link", attrs={"rel": lambda v: v and "icon" in v.lower()})
        if icon:
            return self._pass("favicon", "Favicon present", "Browser tab branding is set.")
        return self._fail(
            "favicon", "Missing favicon",
            "No favicon — looks unfinished/unprofessional in browser tabs.",
            "Add a favicon.ico / SVG icon matching the brand.",
        )

    def _check_social_tags(self, soup: BeautifulSoup) -> dict[str, str]:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og:
            return self._pass("social_tags", "Social share tags present", "Links preview correctly on social/chat apps.")
        return self._fail(
            "social_tags", "Missing social preview tags",
            "No Open Graph tags — shared links show a blank/ugly preview.",
            "Add og:title, og:description and og:image meta tags.",
        )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------
    def _score(self, checks: list[dict[str, str]]) -> tuple[int, str]:
        if not checks:
            return 0, "F"
        total_weight = sum(CHECK_WEIGHTS.get(c["id"], 5) for c in checks) or 1
        earned = sum(CHECK_WEIGHTS.get(c["id"], 5) for c in checks if c["status"] == "pass")
        score = round((earned / total_weight) * 100)
        return score, self._grade(score)

    @staticmethod
    def _grade(score: int) -> str:
        if score >= 90:
            return "A"
        if score >= 75:
            return "B"
        if score >= 60:
            return "C"
        if score >= 40:
            return "D"
        return "F"


if __name__ == "__main__":
    import json
    analyzer = WebsiteAnalyzer()
    print(json.dumps(analyzer.analyze("http://example.com"), indent=2))
