"""Retry policy, backoff maths, and the circuit breaker state machine."""

from __future__ import annotations

import random

import pytest

from ops.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpen,
    PermanentError,
    RetryPolicy,
    TransactionBudget,
    TransientError,
    compute_backoff,
    retry_call,
)
from tests.conftest import FakeMonotonic, RecordingSleep


class TestComputeBackoff:
    def test_first_attempt_equals_base_without_jitter(self) -> None:
        assert compute_backoff(1, base_delay=3.0, jitter="none") == 3.0

    def test_grows_exponentially(self) -> None:
        delays = [compute_backoff(n, base_delay=1.0, factor=2.0, jitter="none") for n in (1, 2, 3, 4)]
        assert delays == [1.0, 2.0, 4.0, 8.0]

    def test_is_capped_at_max_delay(self) -> None:
        assert compute_backoff(20, base_delay=1.0, factor=2.0, max_delay=60.0, jitter="none") == 60.0

    def test_full_jitter_stays_within_bounds(self) -> None:
        rng = random.Random(1234)
        for attempt in range(1, 8):
            delay = compute_backoff(attempt, base_delay=1.0, jitter="full", rng=rng)
            assert 0.0 <= delay <= min(2 ** (attempt - 1), 60.0)

    def test_equal_jitter_stays_within_bounds(self) -> None:
        rng = random.Random(99)
        for attempt in range(1, 6):
            delay = compute_backoff(attempt, base_delay=2.0, jitter="equal", rng=rng)
            ceiling = min(2.0 * 2 ** (attempt - 1), 60.0)
            assert ceiling / 2 <= delay <= ceiling

    def test_jitter_decorrelates_replicas(self) -> None:
        left = [compute_backoff(n, jitter="full", rng=random.Random(1)) for n in (1, 2, 3)]
        right = [compute_backoff(n, jitter="full", rng=random.Random(2)) for n in (1, 2, 3)]
        assert left != right

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"attempt": 0},
            {"attempt": 1, "base_delay": 0.0},
            {"attempt": 1, "factor": 0.5},
            {"attempt": 1, "jitter": "chaotic"},
        ],
    )
    def test_rejects_invalid_parameters(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            compute_backoff(**kwargs)


class TestRetryPolicy:
    def test_policy_delays_use_its_own_settings(self) -> None:
        policy = RetryPolicy(attempts=3, base_delay=5.0, factor=3.0, jitter="none")
        assert policy.delay_for(1) == 5.0
        assert policy.delay_for(2) == 15.0

    def test_rejects_zero_attempts(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(attempts=0)


class TestRetryCall:
    def test_returns_immediately_on_success(self) -> None:
        sleeper = RecordingSleep()
        assert retry_call(lambda: "ok", sleep=sleeper) == "ok"
        assert sleeper.delays == []

    def test_retries_transient_failures_then_succeeds(self) -> None:
        sleeper = RecordingSleep()
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError(f"attempt {calls['n']} failed")
            return "recovered"

        result = retry_call(
            flaky, policy=RetryPolicy(attempts=5, jitter="none", base_delay=1.0), sleep=sleeper
        )
        assert result == "recovered"
        assert calls["n"] == 3
        assert sleeper.delays == [1.0, 2.0]

    def test_raises_after_exhausting_attempts(self) -> None:
        sleeper = RecordingSleep()
        calls = {"n": 0}

        def always_fails() -> None:
            calls["n"] += 1
            raise TransientError("nope")

        with pytest.raises(TransientError, match="nope"):
            retry_call(always_fails, policy=RetryPolicy(attempts=3), sleep=sleeper)
        assert calls["n"] == 3
        assert len(sleeper.delays) == 2

    def test_non_retryable_error_propagates_immediately(self) -> None:
        sleeper = RecordingSleep()
        calls = {"n": 0}

        def bad_credentials() -> None:
            calls["n"] += 1
            raise PermanentError("401")

        with pytest.raises(PermanentError):
            retry_call(bad_credentials, sleep=sleeper)
        assert calls["n"] == 1
        assert sleeper.delays == []

    def test_on_retry_callback_receives_attempt_and_delay(self) -> None:
        seen: list[tuple[int, float, str]] = []
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientError("boom")
            return "ok"

        retry_call(
            flaky,
            policy=RetryPolicy(attempts=3, jitter="none", base_delay=2.0),
            sleep=RecordingSleep(),
            on_retry=lambda attempt, delay, exc: seen.append((attempt, delay, str(exc))),
        )
        assert seen == [(1, 2.0, "boom")]


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = CircuitBreaker("firms")
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow() is True

    def test_opens_after_threshold_failures(self) -> None:
        breaker = CircuitBreaker("firms", failure_threshold=3)
        for _ in range(2):
            breaker.record_failure(TransientError("down"))
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure(TransientError("down"))
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow() is False

    def test_success_resets_the_failure_run(self) -> None:
        breaker = CircuitBreaker("firms", failure_threshold=3)
        breaker.record_failure(TransientError("x"))
        breaker.record_failure(TransientError("x"))
        breaker.record_success()
        breaker.record_failure(TransientError("x"))
        assert breaker.state is BreakerState.CLOSED
        assert breaker.failure_count == 1

    def test_half_opens_after_reset_timeout_then_closes_on_success(self) -> None:
        clock = FakeMonotonic(1000.0)
        breaker = CircuitBreaker("firms", failure_threshold=1, reset_timeout=60.0, clock=clock)
        breaker.record_failure(TransientError("down"))
        assert breaker.state is BreakerState.OPEN

        clock.advance(59.0)
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow() is False

        clock.advance(2.0)
        assert breaker.state is BreakerState.HALF_OPEN
        assert breaker.allow() is True

        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.failure_count == 0

    def test_probe_failure_reopens_and_restarts_the_timer(self) -> None:
        clock = FakeMonotonic(0.0)
        breaker = CircuitBreaker("firms", failure_threshold=1, reset_timeout=30.0, clock=clock)
        breaker.record_failure(TransientError("down"))
        clock.advance(30.0)
        assert breaker.state is BreakerState.HALF_OPEN

        breaker.record_failure(TransientError("still down"))
        assert breaker.state is BreakerState.OPEN
        clock.advance(10.0)
        assert breaker.state is BreakerState.OPEN
        clock.advance(25.0)
        assert breaker.state is BreakerState.HALF_OPEN

    def test_call_records_success_and_returns_value(self) -> None:
        breaker = CircuitBreaker("firms")
        assert breaker.call(lambda: 42) == 42
        assert breaker.success_count == 1

    def test_call_records_failure_and_reraises(self) -> None:
        breaker = CircuitBreaker("firms")
        with pytest.raises(TransientError):
            breaker.call(lambda: (_ for _ in ()).throw(TransientError("boom")))
        assert breaker.failure_count == 1

    def test_call_refuses_when_open(self) -> None:
        breaker = CircuitBreaker("firms", failure_threshold=1)
        breaker.record_failure(TransientError("down"))
        with pytest.raises(CircuitBreakerOpen, match="refusing"):
            breaker.call(lambda: "never")

    def test_snapshot_is_serialisable(self) -> None:
        breaker = CircuitBreaker("firms", failure_threshold=2)
        breaker.record_failure(TransientError("x"))
        snapshot = breaker.snapshot()
        assert snapshot["name"] == "firms"
        assert snapshot["state"] == "closed"
        assert snapshot["failure_count"] == 1
        assert snapshot["failure_threshold"] == 2

    @pytest.mark.parametrize(
        "kwargs",
        [{"failure_threshold": 0}, {"reset_timeout": 0.0}, {"reset_timeout": -1.0}],
    )
    def test_rejects_invalid_configuration(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker("firms", **kwargs)


class TestRetryWithBreaker:
    def test_breaker_stops_the_retry_loop_early(self) -> None:
        """A dead source must end the run, not burn every retry on it."""
        sleeper = RecordingSleep()
        breaker = CircuitBreaker("firms", failure_threshold=2)
        calls = {"n": 0}

        def always_fails() -> None:
            calls["n"] += 1
            raise TransientError("source down")

        with pytest.raises(CircuitBreakerOpen):
            retry_call(
                always_fails,
                policy=RetryPolicy(attempts=10, jitter="none"),
                breaker=breaker,
                sleep=sleeper,
            )
        assert calls["n"] == 2
        assert breaker.state is BreakerState.OPEN

    def test_breaker_closes_again_after_a_success(self) -> None:
        breaker = CircuitBreaker("firms", failure_threshold=2)
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientError("blip")
            return "ok"

        assert retry_call(flaky, breaker=breaker, sleep=RecordingSleep()) == "ok"
        assert breaker.state is BreakerState.CLOSED
        assert breaker.failure_count == 0


class TestTransactionBudget:
    """Pacing against a documented quota, not a guessed fixed pause.

    FIRMS charges about 5 transactions for a 5-day window against 5000 per 10
    minutes. A fixed inter-request sleep looks polite while spending the whole
    budget inside one backfill, which is why the next run starts throttled.
    """

    def test_spends_under_the_limit_without_sleeping(self) -> None:
        clock = FakeMonotonic(0.0)
        sleeper = RecordingSleep()
        budget = TransactionBudget(limit=100, window_seconds=60.0, clock=clock, sleep=sleeper)

        assert budget.spend(5) == 0.0
        assert budget.spend(5) == 0.0
        assert sleeper.delays == []
        assert budget.total_spent == 10

    def test_sleeps_until_the_window_frees_up(self) -> None:
        clock = FakeMonotonic(0.0)
        sleeper = RecordingSleep()
        budget = TransactionBudget(limit=10, window_seconds=60.0, clock=clock, sleep=sleeper)

        assert budget.spend(10) == 0.0

        # The next 10 requires the first reservation to age out. The fake sleep
        # advances the clock so the loop terminates.
        def advance(seconds: float) -> None:
            sleeper.delays.append(seconds)
            clock.advance(seconds)

        budget.sleep = advance  # type: ignore[method-assign]
        slept = budget.spend(10)

        assert slept >= 60.0
        assert budget.total_slept >= 60.0

    def test_spending_exactly_the_limit_is_allowed(self) -> None:
        clock = FakeMonotonic(0.0)
        budget = TransactionBudget(limit=50, window_seconds=60.0, clock=clock, sleep=RecordingSleep())
        assert budget.spend(50) == 0.0

    def test_window_expiry_is_sliding_not_fixed(self) -> None:
        clock = FakeMonotonic(0.0)
        sleeper = RecordingSleep()
        budget = TransactionBudget(limit=10, window_seconds=60.0, clock=clock, sleep=sleeper)

        budget.spend(10)
        clock.advance(59.0)
        # Still inside the window: a further spend would have to wait.
        assert budget.snapshot()["used_in_window"] == 10
        clock.advance(2.0)
        assert budget.snapshot()["used_in_window"] == 0
        assert budget.spend(10) == 0.0

    def test_bigger_requests_cost_more(self) -> None:
        clock = FakeMonotonic(0.0)
        budget = TransactionBudget(limit=12, window_seconds=60.0, clock=clock, sleep=RecordingSleep())
        budget.spend(5)
        budget.spend(5)
        assert budget.snapshot()["used_in_window"] == 10
        # 2 more fits exactly; the budget is now full.
        assert budget.spend(2) == 0.0
        assert budget.total_spent == 12
        assert budget.snapshot()["used_in_window"] == 12

    @pytest.mark.parametrize("transactions", [0, -1])
    def test_rejects_non_positive_spends(self, transactions: int) -> None:
        budget = TransactionBudget(limit=10, clock=FakeMonotonic(0.0), sleep=RecordingSleep())
        with pytest.raises(ValueError):
            budget.spend(transactions)

    @pytest.mark.parametrize(
        "kwargs", [{"limit": 0}, {"window_seconds": 0.0}, {"window_seconds": -5.0}]
    )
    def test_rejects_invalid_configuration(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            TransactionBudget(**kwargs)

    def test_snapshot_is_serialisable(self) -> None:
        import json

        budget = TransactionBudget(limit=10, clock=FakeMonotonic(0.0), sleep=RecordingSleep())
        budget.spend(3)
        json.dumps(budget.snapshot())


class TestTransactionBudgetPersistence:
    """Successive runs must share the server's sliding window.

    The window lives on the server, so a second run minutes after the first
    inherits a nearly full window. Without persistence it starts with an empty
    local budget, overspends immediately, and the resulting HTTP 400s look like a
    bad credential rather than throttling.
    """

    def test_state_survives_a_new_process(self, tmp_path) -> None:
        state = tmp_path / "quota.json"
        clock = FakeMonotonic(1000.0)

        first = TransactionBudget(
            limit=100, window_seconds=600.0, clock=clock, sleep=RecordingSleep(), state_path=state
        )
        first.spend(80)
        assert state.is_file()

        # A fresh instance reads the previous run's usage back.
        second = TransactionBudget(
            limit=100, window_seconds=600.0, clock=clock, sleep=RecordingSleep(), state_path=state
        )
        assert second.snapshot()["used_in_window"] == 80
        assert second.hydrated is True

    def test_expired_reservations_are_not_restored(self, tmp_path) -> None:
        state = tmp_path / "quota.json"
        clock = FakeMonotonic(1000.0)
        TransactionBudget(
            limit=100, window_seconds=600.0, clock=clock, sleep=RecordingSleep(), state_path=state
        ).spend(80)

        clock.advance(700.0)
        restored = TransactionBudget(
            limit=100, window_seconds=600.0, clock=clock, sleep=RecordingSleep(), state_path=state
        )
        assert restored.snapshot()["used_in_window"] == 0

    def test_corrupt_state_is_ignored_rather_than_fatal(self, tmp_path) -> None:
        state = tmp_path / "quota.json"
        state.write_text("{not json", encoding="utf-8")
        budget = TransactionBudget(limit=10, clock=FakeMonotonic(0.0), sleep=RecordingSleep(), state_path=state)
        assert budget.snapshot()["used_in_window"] == 0
        assert budget.spend(1) == 0.0

    def test_partially_corrupt_entries_are_skipped(self, tmp_path) -> None:
        state = tmp_path / "quota.json"
        state.write_text('[[1000.0, 5], ["nonsense"], [1001.0, 7]]', encoding="utf-8")
        budget = TransactionBudget(
            limit=100, window_seconds=600.0, clock=FakeMonotonic(1005.0), sleep=RecordingSleep(), state_path=state
        )
        assert budget.snapshot()["used_in_window"] == 12

    def test_unwritable_state_does_not_stop_collection(self, tmp_path) -> None:
        # A read-only state directory must not fail an expensive collection run.
        budget = TransactionBudget(
            limit=10,
            clock=FakeMonotonic(0.0),
            sleep=RecordingSleep(),
            state_path=tmp_path / "nested",
        )
        budget.state_path = tmp_path / "nested" / "quota.json"  # type: ignore[assignment]
        assert budget.spend(1) == 0.0

    def test_snapshot_reports_whether_state_is_persisted(self, tmp_path) -> None:
        persisted = TransactionBudget(
            limit=10, clock=FakeMonotonic(0.0), sleep=RecordingSleep(), state_path=tmp_path / "q.json"
        )
        assert persisted.snapshot()["persisted"] is True
        assert TransactionBudget(limit=10, clock=FakeMonotonic(0.0), sleep=RecordingSleep()).snapshot()["persisted"] is False
