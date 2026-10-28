"""Priority lanes, bounded background waits and metrics on the Flattrade data limiter.

Time is faked so the tests are exact: ``reserve()`` returns the wait a caller would sleep,
and ``now + wait`` is the absolute second its request leaves.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from broker.flattrade.api import rate_limit
from broker.flattrade.api.rate_limit import SlidingWindowLimiter
from utils import broker_priority
from utils.broker_backpressure import BrokerBusyError


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    return fake


def _limiter(**kw) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        "data", max_per_second=kw.pop("per_s", 9), max_per_minute=kw.pop("per_m", 110), **kw
    )


def _book(limiter, clock, priority=None):
    wait = limiter.reserve(priority) if priority else limiter.reserve()
    return clock.now + wait


def _assert_caps(slots, per_s, per_m):
    slots = sorted(slots)
    for i, start in enumerate(slots):
        in_second = sum(1 for s in slots[i:] if s < start + 1.0)
        in_minute = sum(1 for s in slots[i:] if s < start + 60.0)
        assert in_second <= per_s, f"{in_second} calls inside one second from {start}"
        assert in_minute <= per_m, f"{in_minute} calls inside one minute from {start}"


# -- Default behaviour is unchanged -------------------------------------------


def test_default_limiter_paces_exactly_as_before(clock):
    limiter = _limiter()
    waits = [limiter.reserve() for _ in range(10)]
    assert waits[:9] == [0.0] * 9
    assert waits[9] == pytest.approx(1.0)


def test_default_limiter_ignores_priority_and_never_refuses(clock):
    limiter = _limiter()
    with broker_priority.background():
        waits = [limiter.reserve() for _ in range(300)]
    assert max(waits) > 60.0  # queued, not refused: the legacy unbounded wait


# -- Reserved lane ------------------------------------------------------------


def test_critical_call_is_not_queued_behind_a_deep_background_queue(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    for _ in range(200):  # normal traffic queues far into the future
        limiter.reserve(broker_priority.NORMAL)
    assert limiter.reserve(broker_priority.NORMAL) > 30.0
    assert limiter.reserve(broker_priority.CRITICAL) == 0.0
    assert limiter.reserve(broker_priority.CRITICAL) == 0.0


def test_normal_traffic_alone_cannot_use_the_reserved_slots(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    waits = [limiter.reserve(broker_priority.NORMAL) for _ in range(8)]
    assert waits[:7] == [0.0] * 7  # 9 - 2 reserved = 7 per second for everyone else
    assert waits[7] == pytest.approx(1.0)


def test_critical_burst_beyond_its_lane_borrows_idle_general_capacity(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    waits = [limiter.reserve(broker_priority.CRITICAL) for _ in range(5)]
    assert waits == [0.0] * 5  # 2 from the lane, 3 borrowed from the idle general lane


def test_critical_traffic_never_exceeds_the_broker_total(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    slots = [_book(limiter, clock, broker_priority.CRITICAL) for _ in range(60)]
    _assert_caps(slots, 9, 110)


def test_random_mixed_traffic_never_exceeds_per_second_or_per_minute_caps(clock):
    rng = random.Random(20261028)
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    slots = []
    for _ in range(600):
        clock.now += rng.choice([0.0, 0.0, 0.0, 0.05, 0.3, 1.0])
        priority = rng.choice(
            [broker_priority.CRITICAL]
            + [broker_priority.NORMAL] * 4
            + [broker_priority.BACKGROUND] * 3
        )
        slots.append(_book(limiter, clock, priority))
    _assert_caps(slots, 9, 110)


def test_background_flood_cannot_delay_critical_when_the_minute_budget_is_gone(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    for _ in range(400):
        limiter.reserve(broker_priority.BACKGROUND)
    assert limiter.reserve(broker_priority.CRITICAL) == 0.0


def test_critical_lane_exhausted_waits_instead_of_exceeding_the_broker_quota(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    slots = [_book(limiter, clock, broker_priority.CRITICAL) for _ in range(300)]
    _assert_caps(slots, 9, 110)
    assert max(slots) - clock.now > 60.0  # the quota is real: an honest wait, not a breach


def test_reserve_comes_from_the_context_when_no_priority_is_passed(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    for _ in range(200):
        limiter.reserve(broker_priority.NORMAL)
    with broker_priority.critical():
        assert limiter.reserve() == 0.0


def test_priority_follows_a_call_into_a_pool_only_through_run_in_context(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20)
    for _ in range(200):
        limiter.reserve(broker_priority.NORMAL)
    with ThreadPoolExecutor(max_workers=1) as pool, broker_priority.critical():
        lost = pool.submit(limiter.reserve).result()
        kept = pool.submit(broker_priority.run_in_context(limiter.reserve)).result()
    assert lost > 30.0
    assert kept == 0.0


# -- Bounded background wait --------------------------------------------------


def test_background_call_is_refused_when_its_turn_is_too_far_away(clock):
    limiter = _limiter(background_max_wait=3.0)
    for _ in range(9 * 4):  # a 4 s queue
        limiter.reserve(broker_priority.NORMAL)
    booked = len(limiter._reserved)
    with pytest.raises(BrokerBusyError):
        limiter.reserve(broker_priority.BACKGROUND)
    assert len(limiter._reserved) == booked  # a refusal leaves no reservation behind


def test_background_call_inside_the_bound_is_served(clock):
    limiter = _limiter(background_max_wait=3.0)
    assert limiter.reserve(broker_priority.BACKGROUND) == 0.0


def test_bound_does_not_apply_to_normal_or_critical_calls(clock):
    limiter = _limiter(background_max_wait=3.0)
    for _ in range(9 * 4):
        limiter.reserve(broker_priority.NORMAL)
    assert limiter.reserve(broker_priority.NORMAL) > 3.0


# -- Adaptive clamp and configuration ----------------------------------------


def test_clamp_keeps_a_lane_to_a_third_of_the_lowered_cap(clock):
    limiter = _limiter(reserve_per_second=4, reserve_per_minute=20)
    limiter.clamp_per_second(4)
    waits = [limiter.reserve(broker_priority.NORMAL) for _ in range(4)]
    assert waits[:3] == [0.0, 0.0, 0.0]
    assert waits[3] == pytest.approx(1.0)  # general lane is 4 - min(4, 4 // 3) = 3


def test_a_half_configured_reserve_is_ignored(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=0)
    waits = [limiter.reserve(broker_priority.NORMAL) for _ in range(9)]
    assert waits == [0.0] * 9  # no lane carved out of the second budget


# -- Metrics ------------------------------------------------------------------


def test_snapshot_reports_waits_refusals_and_rejections_by_class(clock):
    limiter = _limiter(reserve_per_second=2, reserve_per_minute=20, background_max_wait=1.0)
    for _ in range(7):
        limiter.reserve(broker_priority.NORMAL)  # fills the general lane for this second
    limiter.reserve(broker_priority.NORMAL)  # waits 1.0
    with pytest.raises(BrokerBusyError):
        for _ in range(50):
            limiter.reserve(broker_priority.BACKGROUND)
    limiter.reserve(broker_priority.CRITICAL)
    limiter.record_rejection()
    limiter.record_latency(0.25)
    limiter.record_timeout()

    snap = limiter.snapshot()
    assert snap["normal"]["calls"] == 8
    assert snap["normal"]["max_wait"] == pytest.approx(1.0)
    assert snap["normal"]["delayed"] == 1
    assert snap["critical"]["calls"] == 1
    assert snap["background"]["refused"] == 1
    assert snap["rejections"] == 1
    assert snap["timeouts"] == 1
    assert snap["latency"]["calls"] == 1
    assert snap["latency"]["max"] == pytest.approx(0.25)


def test_a_broker_rejection_is_counted_by_clamp_from_response(clock):
    limiter = _limiter()
    response = {
        "stat": "Not_Ok",
        "emsg": "Invalid Input :  Order Recieved 11 in a current second exceeds Limit 10 for user",
    }
    assert rate_limit.clamp_from_response(response, limiter) is True
    assert limiter.snapshot()["rejections"] == 1


def test_summary_line_is_logged_once_per_interval(clock, caplog):
    limiter = _limiter()
    limiter.reserve()
    with caplog.at_level("INFO"):
        clock.now += 61
        limiter.reserve()
        clock.now += 10
        limiter.reserve()
    lines = [r.getMessage() for r in caplog.records if "limiter" in r.getMessage()]
    assert len(lines) == 1
    assert "normal" in lines[0]


# -- Priority reaches the broker call --------------------------------------


def test_quote_pool_keeps_the_callers_priority(monkeypatch):
    from broker.flattrade.api import data

    seen = []

    def fake_single(self, symbol, exchange, api_exchange, token, api_key, retry_count=0):
        seen.append(broker_priority.current())
        return {"symbol": symbol, "exchange": exchange, "data": {"ltp": 1.0}}

    monkeypatch.setenv("BROKER_API_KEY", "uid:::secret")
    monkeypatch.setattr(data, "USE_ASYNC", False)
    monkeypatch.setattr(data, "get_br_symbol", lambda s, e: f"{s}-EQ")
    monkeypatch.setattr(data, "get_token", lambda s, e: "1")
    monkeypatch.setattr(data.BrokerData, "_fetch_single_quote_sync", fake_single)

    broker = data.BrokerData("token")
    items = [{"symbol": f"S{i}", "exchange": "NSE"} for i in range(5)]
    with broker_priority.critical():
        broker._process_quotes_batch(items)
    broker._process_quotes_batch(items)

    assert seen[:5] == [broker_priority.CRITICAL] * 5
    assert seen[5:] == [broker_priority.NORMAL] * 5


def test_priority_follows_asyncio_tasks_started_in_the_same_thread():
    import asyncio

    async def probe():
        return await asyncio.gather(
            asyncio.to_thread(lambda: None), asyncio.sleep(0, broker_priority.current())
        )

    with broker_priority.background():
        assert asyncio.run(probe())[1] == broker_priority.BACKGROUND
    assert broker_priority.current() == broker_priority.NORMAL


def test_request_timeouts_and_latency_are_recorded_by_the_request_path(monkeypatch):
    import httpx

    from broker.flattrade.api import data

    limiter = _limiter()
    monkeypatch.setattr(data, "DATA_LIMITER", limiter)
    monkeypatch.setattr(data, "_apply_rate_limit", lambda: None)
    monkeypatch.setenv("BROKER_API_KEY", "uid:::secret")

    class Client:
        def __init__(self, outcome):
            self.outcome = outcome

        def request(self, *a, **k):
            if self.outcome == "timeout":
                raise httpx.ReadTimeout("slow")

            class R:
                text = '{"stat": "Ok"}'

            return R()

    monkeypatch.setattr(data, "get_httpx_client", lambda: Client("ok"))
    data.get_api_response("/PiConnectAPI/GetQuotes", "tok")
    monkeypatch.setattr(data, "get_httpx_client", lambda: Client("timeout"))
    with pytest.raises(httpx.ReadTimeout):
        data.get_api_response("/PiConnectAPI/GetQuotes", "tok")

    snap = limiter.snapshot()
    assert snap["latency"]["calls"] == 1
    assert snap["timeouts"] == 1


# -- Workload replays under the real configured limits ----------------------------
#
# The real figures: 9 per second and 110 per minute, with the suggested lanes: orders 2 per second
# and 10 per minute, background 1 per second and 20 per minute, and a 3 s background bound. Events are replayed on the fake clock in arrival order.

REAL = {"per_s": 9, "per_m": 110}
CONFIGURED = {
    "reserve_per_second": 2,
    "reserve_per_minute": 10,
    "background_per_second": 1,
    "background_per_minute": 20,
    "background_max_wait": 3.0,
}


def _replay(limiter, clock, events):
    """events: (arrival_seconds, priority, label). Returns [(label, priority, start, wait|None)]."""
    results = []
    base = clock.now
    for arrival, priority, label in sorted(events, key=lambda e: e[0]):
        clock.now = base + arrival
        try:
            wait = limiter.reserve(priority)
        except BrokerBusyError:
            results.append((label, priority, clock.now, None))
            continue
        results.append((label, priority, clock.now, wait))
    return results


def _bar_close_events(preload_calls=70):
    N, B, C = broker_priority.NORMAL, broker_priority.BACKGROUND, broker_priority.CRITICAL
    events = [(-60 + i * (60 / preload_calls), N, "steady") for i in range(preload_calls)]
    events += [(0.0 + i * 0.01, N, "history") for i in range(40)]  # EVWAP: 40 symbols at the bar
    events += [(0.5 + i * 0.01, N, "tp_watch") for i in range(13)]
    events += [(1.0 + i * 0.01, B, "mtm") for i in range(12)]
    events += [(2.0 + i * 0.01, B, "pnl_page") for i in range(8)]
    events += [(1.0, C, "order"), (1.5, C, "order"), (2.0, C, "order"), (3.0, C, "order")]
    return events


def _slots(results):
    return [start + wait for _, _, start, wait in results if wait is not None]


def test_bar_close_burst_with_simultaneous_orders_old_limiter_delays_the_orders(clock):
    old = _limiter(**REAL)
    clock.now += 60
    results = _replay(old, clock, _bar_close_events())
    order_waits = [w for label, _, _, w in results if label == "order"]
    assert max(order_waits) > 5.0  # the defect this change addresses, reproduced on the replay


def test_bar_close_burst_with_simultaneous_orders_new_limiter_serves_orders_at_once(clock):
    new = _limiter(**REAL, **CONFIGURED)
    clock.now += 60
    results = _replay(new, clock, _bar_close_events())
    order_waits = [w for label, _, _, w in results if label == "order"]
    assert order_waits == [0.0, 0.0, 0.0, 0.0]
    _assert_caps(_slots(results), 9, 110)  # the broker quota is never exceeded


def test_the_strategy_history_burst_still_completes_promptly_with_the_lane_reserved(clock):
    new = _limiter(**REAL, **CONFIGURED)
    clock.now += 60
    results = _replay(new, clock, _bar_close_events(preload_calls=30))
    history = [start + w for label, _, start, w in results if label == "history"]
    assert max(history) - min(start for label, _, start, _ in results if label == "history") < 8.0


def test_background_work_is_shed_during_the_burst_and_served_again_afterwards(clock):
    new = _limiter(**REAL, **CONFIGURED)
    clock.now += 60
    results = _replay(new, clock, _bar_close_events())
    background = [r for r in results if r[1] == broker_priority.BACKGROUND]
    assert any(w is None for _, _, _, w in background)  # refused rather than queued behind
    clock.now += 40  # the lane's minute window still holds the burst's calls
    with pytest.raises(BrokerBusyError):
        new.reserve(broker_priority.BACKGROUND)
    clock.now += 25  # a full minute after the burst
    assert new.reserve(broker_priority.BACKGROUND) == 0.0


def test_steady_load_does_not_starve_background_work(clock):
    new = _limiter(**REAL, **CONFIGURED)
    N, B = broker_priority.NORMAL, broker_priority.BACKGROUND
    events = []
    for cycle in range(5):  # five 5-minute bars: EVWAP burst, exit watcher, valuation reads
        t = cycle * 300
        events += [(t + i * 0.1, N, "history") for i in range(40)]
        events += [(t + 21 * k + i * 0.05, N, "tp_watch") for k in range(14) for i in range(13)]
        events += [(t + 10 * k, B, "mtm") for k in range(30)]
    results = _replay(new, clock, events)
    background = [r for r in results if r[1] == B]
    served = sum(1 for _, _, _, w in background if w is not None)
    assert served / len(background) >= 0.95
    _assert_caps(_slots(results), 9, 110)


def test_a_background_flood_leaves_orders_and_strategy_calls_untouched(clock):
    new = _limiter(**REAL, **CONFIGURED)
    N, B, C = broker_priority.NORMAL, broker_priority.BACKGROUND, broker_priority.CRITICAL
    events = [(i * 0.02, B, "flood") for i in range(1000)]  # 50 per second for 20 s
    events += [(5.0 + i * 0.5, C, "order") for i in range(6)]
    events += [(10.0 + i * 0.05, N, "history") for i in range(20)]
    results = _replay(new, clock, events)
    assert [w for label, _, _, w in results if label == "order"] == [0.0] * 6
    history_waits = [w for label, _, _, w in results if label == "history"]
    assert max(history_waits) < 5.0  # the flood is confined to its own lane
    _assert_caps(_slots(results), 9, 110)
