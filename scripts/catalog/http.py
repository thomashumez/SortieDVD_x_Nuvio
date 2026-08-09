from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from .config import (
    GUIDE_RAPIDE_HOST_SUFFIX,
    METADATA_FAST_HOSTS,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
)
from .utils import log

class HttpMixin:
    def request_profile(self, url: str) -> tuple[str, float, int]:
        host = (urlparse(url).hostname or "").lower()
        if host == GUIDE_RAPIDE_HOST_SUFFIX or host.endswith(f".{GUIDE_RAPIDE_HOST_SUFFIX}"):
            return (
                "guide_rapide",
                self.config.guide_rapide_delay_seconds,
                self.config.guide_rapide_request_timeout,
            )
        if host in METADATA_FAST_HOSTS:
            return (
                "metadata_api",
                self.config.metadata_api_delay_seconds,
                self.config.metadata_api_request_timeout,
            )
        return "default", REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT

    def throttle(self, bucket: str, delay_seconds: float) -> None:
        now = time.time()
        last_request_ts = self.last_request_ts_by_bucket.get(bucket, 0.0)
        wait_for = delay_seconds - (now - last_request_ts)
        if wait_for > 0:
            time.sleep(wait_for)

    def fetch_url(self, url: str) -> Optional[str]:
        bucket, delay_seconds, timeout_seconds = self.request_profile(url)
        if not self.reserve_request_budget(bucket):
            return None
        self.throttle(bucket, delay_seconds)
        self.last_request_ts_by_bucket[bucket] = time.time()
        try:
            response = self.session.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except requests.RequestException as exc:
            self.log_request_failure(url, exc)
            return None

    def fetch_json(self, url: str, params: Optional[dict[str, str]] = None) -> Optional[dict]:
        bucket, delay_seconds, timeout_seconds = self.request_profile(url)
        if not self.reserve_request_budget(bucket):
            return None
        self.throttle(bucket, delay_seconds)
        self.last_request_ts_by_bucket[bucket] = time.time()
        try:
            response = self.session.get(url, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, requests.RequestException):
                self.log_request_failure(url, exc)
            return None

        if not isinstance(payload, dict):
            return None
        return payload

    def log_request_failure(self, url: str, error: Exception) -> None:
        self.request_failures += 1
        host = (urlparse(url).hostname or "unknown-host").lower()
        log(
            f"[{self.elapsed()}] Request failed: host={host}, "
            f"error={type(error).__name__}"
        )

    def reserve_request_budget(self, bucket: str) -> bool:
        if bucket != "metadata_api":
            return True
        if self.metadata_api_requests >= self.config.max_metadata_api_lookups_per_run:
            if not self.metadata_budget_warning_emitted:
                log(
                    f"[{self.elapsed()}] Metadata API budget exhausted: "
                    f"{self.config.max_metadata_api_lookups_per_run} requests"
                )
                self.metadata_budget_warning_emitted = True
            return False
        self.metadata_api_requests += 1
        return True
