"""
The e2e suite's retrying transport.

Added because across four consecutive full-suite runs a *different* test failed
each time -- always httpx.ReadError, httpx.ReadTimeout or [WinError 64], never
an assertion, and always passing when run alone. One run makes several thousand
HTTP calls over five minutes, and Docker Desktop on Windows drops
published-port connections when the host is short of memory.

The cost was not the red line. It was that a red suite stopped meaning "the
platform is broken" and started meaning "the laptop hiccuped", and three real
results were obscured by it in a single day.

A retry that is too eager is much worse than the flakiness it replaces: a suite
that retries until green cannot fail, and a suite that cannot fail is not a
test suite. So most of what follows pins the things it must NOT do.
"""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "e2e_config_under_test",
    Path(__file__).resolve().parents[1] / "e2e" / "config.py")
e2e_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_config)

RetryingTransport = e2e_config.RetryingTransport
TRANSPORT_RETRIES = e2e_config.TRANSPORT_RETRIES


class FakeTransport(httpx.AsyncBaseTransport):
    """Fails `failures` times with `error`, then answers `status`."""

    def __init__(self, failures=0, error=None, status=200):
        self.failures = failures
        self.error = error or httpx.ReadError("connection dropped")
        self.status = status
        self.attempts = 0

    async def handle_async_request(self, request):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        return httpx.Response(self.status, request=request)

    async def aclose(self):
        pass


def run(transport, method="GET", headers=None):
    inner = transport
    retrying = RetryingTransport(inner=inner, backoff=0)

    async def _go():
        async with httpx.AsyncClient(transport=retrying) as client:
            return await client.request(method, "http://svc/thing",
                                        headers=headers or {})

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# what it must do
# ---------------------------------------------------------------------------

def test_a_dropped_connection_is_retried():
    fake = FakeTransport(failures=2)
    response = run(fake)
    assert response.status_code == 200
    assert fake.attempts == 3


@pytest.mark.parametrize("error", [
    httpx.ReadError("dropped"),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("slow"),
    httpx.RemoteProtocolError("half-closed"),
])
def test_every_http_transport_fault_is_retried(error):
    """The httpx half of the problem.

    Note what this does NOT cover: both observed WinError 64 failures landed on
    raw Postgres connections, not on HTTP, and surfaced as a bare
    ConnectionResetError out of the asyncio event loop where no httpx handler
    would ever see them. That is connect_with_retry's job, tested below.
    """
    fake = FakeTransport(failures=1, error=error)
    assert run(fake).status_code == 200
    assert fake.attempts == 2


def test_it_gives_up_eventually():
    """A dependency that is genuinely down must still fail the test."""
    fake = FakeTransport(failures=99)
    with pytest.raises(httpx.TransportError):
        run(fake)
    assert fake.attempts == TRANSPORT_RETRIES + 1


# ---------------------------------------------------------------------------
# what it must NOT do -- the half that keeps the suite honest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 404, 409, 422, 500, 502, 503])
def test_no_http_status_is_ever_retried(status):
    """A 500 is a real answer and the tests exist to see it.

    Retrying statuses is how a suite starts lying: every flaky assertion about
    a service being down would quietly become a pass.
    """
    fake = FakeTransport(failures=0, status=status)
    response = run(fake)
    assert response.status_code == status
    assert fake.attempts == 1, "a status must never cost a second request"


def test_a_mutation_without_an_idempotency_key_is_not_retried():
    """The dangerous case, and the reason this is not blanket retry.

    A POST that fails at the transport layer may already have taken effect --
    the response was lost, not the request. Retrying it could place a second
    order. It fails honestly instead.
    """
    fake = FakeTransport(failures=1)
    with pytest.raises(httpx.TransportError):
        run(fake, method="POST")
    assert fake.attempts == 1


def test_a_mutation_with_an_idempotency_key_is_retried():
    """Rule 4 is exactly the condition that makes a repeat safe.

    The platform recognises the second attempt as the same one, so the retry
    cannot double-apply. The suite uses the same rule the services enforce
    rather than inventing its own.
    """
    fake = FakeTransport(failures=2)
    response = run(fake, method="POST",
                   headers={"Idempotency-Key": "abc-123"})
    assert response.status_code == 200
    assert fake.attempts == 3


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_mutation_needs_the_key_to_be_repeated(method):
    fake = FakeTransport(failures=1)
    with pytest.raises(httpx.TransportError):
        run(fake, method=method)
    assert fake.attempts == 1

    safe = FakeTransport(failures=1)
    assert run(safe, method=method,
               headers={"Idempotency-Key": "k"}).status_code == 200
    assert safe.attempts == 2


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_reads_are_always_repeatable(method):
    fake = FakeTransport(failures=1)
    assert run(fake, method=method).status_code == 200
    assert fake.attempts == 2


def test_a_successful_call_costs_exactly_one_request():
    """No retry budget is spent when nothing went wrong."""
    fake = FakeTransport(failures=0)
    assert run(fake).status_code == 200
    assert fake.attempts == 1


# ---------------------------------------------------------------------------
# the client the suite actually builds
# ---------------------------------------------------------------------------

def test_the_suite_client_has_a_timeout():
    """httpx defaults to no timeout at all.

    Without one a dropped connection makes a test hang forever instead of
    retrying -- which is worse than the failure it was meant to fix, because a
    hung suite reports nothing rather than something wrong.
    """
    client = e2e_config.new_client()
    try:
        assert client.timeout.read is not None
        assert client.timeout.connect is not None
    finally:
        asyncio.run(client.aclose())


def test_the_suite_client_retries_by_default():
    client = e2e_config.new_client()
    try:
        assert isinstance(client._transport, RetryingTransport)
    finally:
        asyncio.run(client.aclose())


def test_callers_can_still_override():
    client = e2e_config.new_client(timeout=5.0)
    try:
        assert client.timeout.read == 5.0
    finally:
        asyncio.run(client.aclose())


# ---------------------------------------------------------------------------
# the same problem one layer down: raw Postgres connections
# ---------------------------------------------------------------------------
#
# Wrapping httpx was the half that missed the evidence. test_01 opens asyncpg
# mid-poll and test_04 opens one in its database bootstrap, and that is where
# both WinError 64s actually happened.

def _connect_attempts(failures, error=None, retries=None):
    """Drive connect_with_retry against a fake asyncpg.connect."""
    import asyncpg
    calls = {"n": 0}
    err = error or ConnectionResetError(64, "network name no longer available")

    async def fake_connect(dsn, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise err
        return f"connection-to-{dsn}"

    original = asyncpg.connect
    asyncpg.connect = fake_connect
    try:
        kwargs = {"backoff": 0}
        if retries is not None:
            kwargs["retries"] = retries
        result = asyncio.run(
            e2e_config.connect_with_retry("postgres://x/y", **kwargs))
        return result, calls["n"]
    finally:
        asyncpg.connect = original


def test_a_dropped_database_socket_is_retried():
    """The exact failure that broke test_01 on a previously green suite."""
    result, attempts = _connect_attempts(failures=2)
    assert result == "connection-to-postgres://x/y"
    assert attempts == 3


def test_winerror_64_specifically():
    """ConnectionResetError(64) is what Windows Docker raises under pressure."""
    _, attempts = _connect_attempts(
        failures=1, error=ConnectionResetError(64, "network name gone"))
    assert attempts == 2


@pytest.mark.parametrize("error", [
    ConnectionRefusedError("refused"),
    ConnectionAbortedError("aborted"),
    BrokenPipeError("pipe"),
    TimeoutError("slow"),
    OSError("transport endpoint"),
])
def test_every_socket_level_failure_is_retried(error):
    _, attempts = _connect_attempts(failures=1, error=error)
    assert attempts == 2


def test_a_database_that_is_really_down_still_fails():
    """Otherwise the suite cannot report the one thing it exists to report."""
    with pytest.raises(ConnectionResetError):
        _connect_attempts(failures=99)


def test_a_healthy_connect_costs_one_attempt():
    _, attempts = _connect_attempts(failures=0)
    assert attempts == 1


def test_the_retry_budget_is_bounded():
    try:
        _connect_attempts(failures=99, retries=2)
    except ConnectionResetError:
        pass
    _, attempts = _connect_attempts(failures=2, retries=2)
    assert attempts == 3


def test_only_the_connect_is_retried_not_the_work():
    """Deliberate, and the line that keeps this honest.

    Connecting is establishing a socket -- always safe to repeat. Re-running a
    statement after a mid-flight failure is how a test quietly does its work
    twice, so nothing the caller does with the connection is retried.
    """
    import inspect
    source = inspect.getsource(e2e_config.connect_with_retry)
    assert "asyncpg.connect" in source
    assert "execute" not in source and "fetch" not in source


def test_a_hanging_connect_becomes_a_retryable_error():
    """The failure a retry loop cannot survive without a bounded connect.

    asyncpg.connect has no timeout by default, so a socket that never answers
    blocks the first attempt forever and the loop never reaches the second.
    Observed exactly that: Docker Desktop's published-port forwarding hung
    while the container stayed healthy and served connections internally, and
    a host-side connect sat for over a minute without raising.
    """
    import asyncpg

    calls = {"n": 0}

    async def hanging_connect(dsn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(30)  # never answers
        return "connected"

    original = asyncpg.connect
    asyncpg.connect = hanging_connect
    try:
        result = asyncio.run(e2e_config.connect_with_retry(
            "postgres://x/y", backoff=0, timeout=0.05))
    finally:
        asyncpg.connect = original

    assert result == "connected"
    assert calls["n"] == 2, "the hang must be abandoned and retried"


def test_the_connect_timeout_is_bounded_by_default():
    """A default of None would reintroduce the hang."""
    assert e2e_config.PG_CONNECT_TIMEOUT is not None
    assert e2e_config.PG_CONNECT_TIMEOUT > 0


# ---------------------------------------------------------------------------
# and the third way in: docker exec
# ---------------------------------------------------------------------------
#
# Five tests reach the platform by shelling out. With Docker healthy, test_06
# still failed once in a full run and passed alone: five seconds to spawn a
# process inside a container while twenty others are busy is too tight a
# budget. A timeout that is wrong is not a machine that is broken.

def _probe(timeouts, returncode=0):
    """Drive run_probe against a subprocess.run that times out `timeouts` times."""
    import subprocess
    calls = {"n": 0}

    class Done:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = "ok"
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] <= timeouts:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        return Done(returncode)

    original = subprocess.run
    subprocess.run = fake_run
    try:
        result = e2e_config.run_probe(["docker", "exec", "x", "wget", "-qO-", "u"],
                                      backoff=0)
        return result, calls["n"]
    finally:
        subprocess.run = original


def test_a_slow_probe_is_retried():
    result, attempts = _probe(timeouts=1)
    assert result.returncode == 0
    assert attempts == 2


def test_a_probe_that_never_answers_still_fails():
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        _probe(timeouts=99)


def test_a_nonzero_exit_is_never_retried():
    """That is the command answering, and the answer is the point.

    Retrying a failed probe would turn "this service is unreachable" into
    "this service is unreachable three times", then eventually into a pass if
    the service flapped -- exactly the lying-suite failure mode.
    """
    result, attempts = _probe(timeouts=0, returncode=1)
    assert result.returncode == 1
    assert attempts == 1


def test_a_healthy_probe_costs_one_call():
    _, attempts = _probe(timeouts=0)
    assert attempts == 1


def test_the_probe_budget_is_larger_than_the_five_seconds_that_failed():
    """The original 5s budget is what actually broke test_06 under load."""
    assert e2e_config.EXEC_TIMEOUT > 5


def test_state_changing_commands_do_not_come_through_here():
    """test_09 kills a broker and test_10 forces a failover.

    Those are the experiment. Silently repeating an experiment is not a retry,
    it is a different test -- so they must keep calling subprocess directly.
    """
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / "e2e"
    for name in ("test_09_broker_loss.py", "test_10_redis_failover.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "run_probe" not in source, (
            f"{name} performs state-changing docker commands and must not "
            f"route them through the retrying probe helper")
