"""Failures, retries without hammering, and a circuit breaker.

Real rate-limited third-party APIs (FIRMS, GEE, NOAA) fail in three shapes:

  * transient  — a 503, a timeout, a reset connection. Retry with backoff.
  * permanent  — a 401, a bad request, a schema you will never satisfy by
                 retrying. Fail immediately with the reason.
  * systemic   — the source is down for everyone. Keep retrying and you burn the
                 whole workflow's budget for nothing.

The first two are handled by :func:`retry_call`; the third by
:class:`CircuitBreaker`, which trips after N consecutive failed attempts and
then refuses further requests so a run ends in seconds with an honest
"source unavailable" status instead of a 6-hour timeout.

Both take injectable clocks/sleepers so their behaviour is unit-testable without
waiting in real time.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "CollectorError",
    "PermanentError",
    "RetryPolicy",
    "TransactionBudget",
    "TransientError",
    "compute_backoff",
    "retry_call",
]

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class CollectorError(RuntimeError):
    """Base class for every expected failure inside the collection layer."""


class TransientError(CollectorError):
    """Retryable: timeouts, connection resets, 429s, 5xx."""


class PermanentError(CollectorError):
    """Not retryable: bad credentials, malformed request, unacceptable schema."""


class CircuitBreakerOpen(CollectorError):
    """The source has failed too often this run — stop calling it."""


class SchemaValidationError(CollectorError):
    """An input or output frame violated its Pandera contract."""


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------

def compute_backoff(
    attempt: int,
    *,
    base_delay: float = 1.0,
    factor: float = 2.0,
    max_delay: float = 60.0,
    jitter: str = "full",
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with jitter, in seconds.

    ``attempt`` is 1-based. ``jitter="full"`` draws uniformly from
    ``[0, delay]``, which decorrelates retries across the parallel jobs GitHub
    Actions runs — the documented reason full jitter beats fixed backoff.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if base_delay <= 0:
        raise ValueError(f"base_delay must be > 0, got {base_delay}")
    if factor < 1:
        raise ValueError(f"factor must be >= 1, got {factor}")

    delay = min(base_delay * (factor ** (attempt - 1)), max_delay)
    source = rng if rng is not None else random

    if jitter == "none":
        return delay
    if jitter == "equal":
        return delay / 2.0 + source.uniform(0.0, delay / 2.0)
    if jitter == "full":
        return source.uniform(0.0, delay)
    raise ValueError(f"unknown jitter mode {jitter!r}; expected none|equal|full")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 1.0
    factor: float = 2.0
    max_delay: float = 60.0
    jitter: str = "full"

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts must be >= 1, got {self.attempts}")

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        return compute_backoff(
            attempt,
            base_delay=self.base_delay,
            factor=self.factor,
            max_delay=self.max_delay,
            jitter=self.jitter,
            rng=rng,
        )


def retry_call[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    description: str = "call",
) -> T:
    """Run ``fn`` with bounded retries, consulting the breaker before each try."""
    policy = policy or RetryPolicy()
    last_error: BaseException | None = None

    for attempt in range(1, policy.attempts + 1):
        if breaker is not None and not breaker.allow():
            raise CircuitBreakerOpen(
                f"circuit breaker '{breaker.name}' is open after "
                f"{breaker.failure_count} consecutive failures; refusing {description}"
            )
        try:
            result = fn()
        except retry_on as exc:
            last_error = exc
            if breaker is not None:
                breaker.record_failure(exc)
            if attempt >= policy.attempts:
                break
            delay = policy.delay_for(attempt, rng=rng)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            else:
                LOGGER.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.2fs",
                    description,
                    attempt,
                    policy.attempts,
                    exc,
                    delay,
                )
            sleep(delay)
        except Exception as exc:  # noqa: BLE001 - non-retryable by construction
            if breaker is not None:
                breaker.record_failure(exc)
            raise
        else:
            if breaker is not None:
                breaker.record_success()
            return result

    assert last_error is not None
    raise last_error


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------

class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Trip after ``failure_threshold`` consecutive failures; probe to close.

    State machine::

        closed --N consecutive failures--> open --reset_timeout elapsed--> half_open
        half_open --success--> closed        half_open --failure--> open (timer reset)

    The breaker is per source per run (constructed fresh by each collector), so
    "stop hammering it for the rest of the run" is exactly what it does.
    """

    name: str
    #: Failures are counted per *attempt*, and one logical request may make several
    #: attempts, so this should be at least ``retry_attempts x tolerated_request_failures``.
    #: At the default of 4 attempts, a threshold of 5 trips after roughly one bad
    #: request — too twitchy for a source that returns sporadic throttling errors.
    failure_threshold: int = 5
    reset_timeout: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.reset_timeout <= 0:
            raise ValueError("reset_timeout must be > 0")

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def state(self) -> BreakerState:
        """Current state, promoting ``open`` to ``half_open`` once the timer expires."""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and self.clock() - self._opened_at >= self.reset_timeout
        ):
            self._state = BreakerState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Whether a request may be attempted right now (one probe in half-open)."""
        with self._lock:
            return self._state_locked() is not BreakerState.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._success_count += 1
            self._failure_count = 0
            self._state = BreakerState.CLOSED
            self._opened_at = None

    def record_failure(self, error: BaseException | None = None) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.OPEN
                self._opened_at = self.clock()
                LOGGER.error("circuit breaker '%s' re-opened by probe failure", self.name)
                return
            if self._failure_count >= self.failure_threshold:
                if self._state is not BreakerState.OPEN:
                    LOGGER.error(
                        "circuit breaker '%s' opened after %d consecutive failures (last: %s)",
                        self.name,
                        self._failure_count,
                        error,
                    )
                self._state = BreakerState.OPEN
                self._opened_at = self.clock()

    def call[T](self, fn: Callable[[], T], *, description: str = "") -> T:
        label = description or self.name
        if not self.allow():
            raise CircuitBreakerOpen(
                f"circuit breaker '{self.name}' is open; refusing {label}"
            )
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - state must reflect every failure
            self.record_failure(exc)
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe state for pipeline_runs / health reporting."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.failure_threshold,
        }


# --------------------------------------------------------------------------
# Quota pacing
# --------------------------------------------------------------------------

@dataclass
class TransactionBudget:
    """Sliding-window limiter for a documented per-interval quota.

    Exists because "sleep a fixed amount between requests" is not the same as
    "stay inside the quota". FIRMS documents 5000 transactions per 10 minutes and
    notes that *a single request may cost several transactions* — measured
    behaviour is that a 5-day window costs about 5. A fixed inter-request pause
    therefore looks polite while spending the entire budget inside one backfill.

    ``state_path`` matters more than the limit does. The quota window lives on the
    server, so a second run starting two minutes after the first inherits a nearly
    full window — but a fresh process would begin with an empty local budget and
    throttle immediately. Persisting reservations means successive runs share the
    accounting, which is exactly the case that produced repeated HTTP 400s
    ("Invalid MAP_KEY") that were really throttle responses.
    """

    limit: int = 4200
    window_seconds: float = 600.0
    #: Wall-clock by default, so persisted state stays meaningful across processes.
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    state_path: Path | None = None
    #: Retained only as a safety valve for tests and for callers that inject a clock.
    _spent: deque[tuple[float, int]] = field(default_factory=deque, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    total_spent: int = field(default=0, init=False)
    total_slept: float = field(default=0.0, init=False)
    hydrated: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if self.state_path is not None:
            self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        assert self.state_path is not None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.hydrated = True
            return
        now = self.clock()
        for entry in raw if isinstance(raw, list) else []:
            try:
                stamp, cost = float(entry[0]), int(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if now - stamp < self.window_seconds and cost > 0:
                self._spent.append((stamp, cost))
        self.total_spent = sum(cost for _, cost in self._spent)
        self._spent = deque(sorted(self._spent))
        self.hydrated = True

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [[stamp, cost] for stamp, cost in self._spent]
            self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - a read-only disk must not stop collection
            LOGGER.warning("could not persist quota state to %s: %s", self.state_path, exc)

    # -- accounting --------------------------------------------------------

    def _prune(self, now: float) -> int:
        while self._spent and now - self._spent[0][0] >= self.window_seconds:
            self._spent.popleft()
        return sum(cost for _, cost in self._spent)

    def spend(self, transactions: int = 1) -> float:
        """Reserve ``transactions``, sleeping if the window is nearly full.

        Returns the seconds slept, so the caller can log or assert on it.
        """
        if transactions < 1:
            raise ValueError(f"transactions must be >= 1, got {transactions}")
        slept = 0.0
        with self._lock:
            while True:
                now = self.clock()
                used = self._prune(now)
                if used + transactions <= self.limit:
                    self._spent.append((now, transactions))
                    self.total_spent += transactions
                    self._save()
                    return slept
                # Wait until the oldest reservation leaves the window.
                oldest_at = self._spent[0][0] if self._spent else now
                wait = max(self.window_seconds - (now - oldest_at), 0.05)
                self.sleep(wait)
                slept += wait
                self.total_slept += wait

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limit": self.limit,
                "window_seconds": self.window_seconds,
                "used_in_window": self._prune(self.clock()),
                "total_spent": self.total_spent,
                "total_slept_s": round(self.total_slept, 3),
                "persisted": self.state_path is not None,
            }
