"""
state.py — persistent "seen jobs" store so we don't re-notify the same roles.

Stored as data/seen.json: { "<source:id>": "<iso-timestamp>" }.
Entries older than cfg['dedup_days'] are evicted on save.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


class SeenStore:
    def __init__(self, path: str, dedup_days: int):
        self.path = path
        self.dedup_days = dedup_days
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            if not isinstance(self._data, dict):
                self._data = {}
        except Exception as e:
            log.warning("state: failed to load %s: %s (starting fresh)", self.path, e)
            self._data = {}

    def has(self, key: str) -> bool:
        return key in self._data

    def mark(self, key: str) -> None:
        self._data[key] = datetime.now(timezone.utc).isoformat()

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.dedup_days)
        fresh: dict[str, str] = {}
        for k, v in self._data.items():
            try:
                ts = datetime.fromisoformat(v)
            except Exception:
                continue
            if ts >= cutoff:
                fresh[k] = v
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=2, sort_keys=True)
        self._data = fresh
        log.info("state: saved %d entries to %s", len(fresh), self.path)
