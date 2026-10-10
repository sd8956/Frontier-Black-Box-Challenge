"""HTTP client for the original service (the black box).

Standard library only. The token is never printed, logged or returned: it lives
inside the instance and is used only to build the ``Authorization`` header.

Not to be confused with the replacement: this package measures, it does not
implement, and nothing in the API should import it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
TIMEOUT = 5.0
MAX_PER_SECOND = 8


class ConfigurationError(RuntimeError):
    """BLACK_BOX_URL or BLACK_BOX_TOKEN is missing in the environment and in reto-2/.env."""


def _read_env(path):
    """Reads a .env-like file without depending on the working directory."""
    data = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _configuration():
    """Resolves URL and token: environment first, then ``reto-2/.env``.

    Returns ``(url, token)`` for internal use only. It is never printed.
    """
    url = os.environ.get("BLACK_BOX_URL") or ""
    token = os.environ.get("BLACK_BOX_TOKEN") or ""
    if not url or not token:
        data = _read_env(ENV_PATH)
        url = url or data.get("BLACK_BOX_URL", "")
        token = token or data.get("BLACK_BOX_TOKEN", "")
    if not url:
        raise ConfigurationError(
            "BLACK_BOX_URL is missing in the environment and in reto-2/.env"
        )
    if not token:
        raise ConfigurationError(
            "BLACK_BOX_TOKEN is missing in the environment and in reto-2/.env"
        )
    return url, token


class RateLimiter:
    """Hard cap of 8 requests per second, in the client.

    It uses a one-second sliding window. The clock and sleep are injectable so
    it can be tested with a fake clock, without real sleeps.
    """

    def __init__(
        self,
        max_per_second=MAX_PER_SECOND,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.max_per_second = max_per_second
        self._clock = clock
        self._sleep = sleep
        self._marks = deque()

    def wait(self):
        """Blocks until there is room and consumes one slot."""
        while True:
            now = self._clock()
            while self._marks and now - self._marks[0] >= 1.0:
                self._marks.popleft()
            if len(self._marks) < self.max_per_second:
                self._marks.append(now)
                return
            pause = 1.0 - (now - self._marks[0])
            if pause <= 0:
                pause = 0.0
            self._sleep(pause)


class HttpTransport:
    """Real transport with urllib. 5 s timeout, no retries."""

    def __init__(self, timeout=TIMEOUT):
        self.timeout = timeout

    def send(self, method, url, headers, body):
        data = None
        headers = dict(headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _decode(data):
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", errors="replace")
    else:
        text = str(data)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


class Client:
    """Client of the original. The rate limit is applied to every request."""

    def __init__(
        self,
        base_url=None,
        token=None,
        transport=None,
        rate_limiter=None,
        timeout=TIMEOUT,
    ):
        if not base_url or not token:
            env_url, env_token = _configuration()
            base_url = base_url or env_url
            token = token or env_token
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._transport = (
            transport if transport is not None else HttpTransport(timeout=timeout)
        )
        self._rate_limiter = (
            rate_limiter if rate_limiter is not None else RateLimiter()
        )

    def __repr__(self):
        return "Client(base_url=%r, token=***)" % (self.base_url,)

    def _request(self, method, path, body=None):
        self._rate_limiter.wait()
        url = self.base_url + path
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
        }
        status, response_headers, data = self._transport.send(
            method, url, headers, body
        )
        return {
            "status": int(status),
            "body": _decode(data),
            "x_req": _header(response_headers, "X-Req"),
            "infra": _header(response_headers, "X-Frontier-Infra") is not None,
        }

    def post_msg(self, payload):
        """POST /msg. It is never retried: a retry can deposit twice."""
        return self._request("POST", "/msg", {"m": payload})

    def reset(self):
        return self._request("POST", "/__reset")

    def health(self):
        return self._request("GET", "/health")
