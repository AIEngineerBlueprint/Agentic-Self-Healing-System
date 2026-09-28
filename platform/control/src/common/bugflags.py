"""Runtime bug injection for controlled fault scenarios.

DEMO ONLY. Gated behind DEMO_BUGS_ENABLED, which defaults to off. This endpoint
must never ship beyond the demo -- stated here in the code so nobody has to
guess later.

Flags are applied at runtime with no restart, default to off, and reset when the
container restarts. That is what makes a scenario repeatable on cue: it is a
flag toggle on a running deployment, not a git revert.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)


class BugFlags:
    def __init__(self, available: dict[str, str]) -> None:
        self._available = available
        self._enabled: set[str] = set()
        self._lock = threading.Lock()

    @property
    def demo_enabled(self) -> bool:
        return os.environ.get("DEMO_BUGS_ENABLED", "false").lower() == "true"

    def is_on(self, name: str) -> bool:
        if not self.demo_enabled:
            return False
        with self._lock:
            return name in self._enabled

    def set(self, name: str, enabled: bool) -> None:
        if name not in self._available:
            raise KeyError(name)
        with self._lock:
            if enabled:
                self._enabled.add(name)
            else:
                self._enabled.discard(name)
        log.warning(
            "demo bug flag %s -> %s", name, "ENABLED" if enabled else "disabled"
        )

    def reset(self) -> None:
        with self._lock:
            self._enabled.clear()

    def state(self) -> dict:
        with self._lock:
            return {
                "enabled_flags": sorted(self._enabled),
                "available_flags": dict(self._available),
            }
