"""
net_utils.py

One shared requests.Session for the whole project, configured with:
  - automatic retry + exponential backoff on transient failures (connection
    errors, timeouts, and 429/500/502/503/504 responses)
  - a consistent, descriptive User-Agent
  - a sane default timeout applied everywhere it's used

Previously, a single transient network blip (very common on free CI
runners) would hard-fail a whole run with no retry. Centralizing this means
every fetch in the project -- PAGASA's page, QC's feed, geoBoundaries --
gets the same resilience instead of three separate copies of the same
requests.get() boilerplate.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "Mozilla/5.0 (compatible; pagasa-ncr-advisory-bot/1.1; personal weather-alert project)"

DEFAULT_TIMEOUT = 30  # seconds


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,  # 0s, 1.5s, 3s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# One shared session for the whole process -- connection pooling across
# calls to the same host (e.g. geoBoundaries' two-step API+geometry fetch)
# is a small but free win on top of the retry behavior.
session = build_session()


def get(url: str, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """requests.get, via the shared retrying session, with a default timeout."""
    return session.get(url, timeout=timeout, **kwargs)
