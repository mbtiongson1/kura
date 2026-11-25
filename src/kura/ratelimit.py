"""Cross-process request spacing for remote services, persisted in ``rate_limits``.

Several Kura processes (a resolver run, a list sync, a SeaDex sync) may talk to the same
service. Each one reserves its start slot in the shared database before it sleeps, so the
combined request rate stays inside the service's limit. The limiter opens its own SQLite
connection per operation: it never joins, commits or rolls back a transaction on the
caller's main connection, and it holds no connection while sleeping. The database uses
rollback-journal mode, so a caller that holds a write lock (or an unfinished read) on the
same file makes the limiter wait out the 5 s busy timeout and then fail closed with
``limiter_unavailable``; call it between the caller's transactions, as every Kura service does.

Stored times are wall-clock epoch seconds (monotonic time is per process). Anything
implausibly far in the future (clock skew, a corrupted row) is clamped to ``max_penalty``
so a bad value can never lock a service out for good. Fail closed: if the shared state
cannot be read or written the limiter raises instead of letting a request through unpaced.
"""
from pathlib import Path
import sqlite3
import time

from .models import KuraError
from .safety import regular_file

SERVICE_MAX = 32


class SharedLimiter:
    """Cross-process request spacing persisted in rate_limits.

    ``interval`` is the minimum spacing between request starts. A service-advertised value
    passed to :meth:`observe` (``limit`` together with ``interval``) replaces it for every
    process; without an observation the constructor's ``interval`` is authoritative.
    """

    def __init__(self, store_root: Path, service: str, *, interval: float, max_penalty: float = 3600,
                 wall=time.time, sleep=time.sleep):
        if (not isinstance(service, str) or not 1 <= len(service) <= SERVICE_MAX or
                not service.isascii() or not service.isprintable()):
            raise KuraError("invalid_argument", "Invalid rate-limit service name")
        if not interval > 0 or not max_penalty > 0 or interval == float("inf") or max_penalty == float("inf"):
            raise KuraError("invalid_argument", "Invalid rate-limit parameters")
        self.path = Path(store_root) / "state.sqlite3"
        self.service, self.interval, self.max_penalty = service, float(interval), float(max_penalty)
        self.wall, self.sleep = wall, sleep

    # -- storage ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            regular_file(self.path)
            return sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except (OSError, KuraError, sqlite3.Error) as e:
            raise KuraError("limiter_unavailable", "Shared rate-limit state is unavailable") from e

    def _transaction(self, work):
        """Run `work(db, now)` inside BEGIN IMMEDIATE; commit on success, roll back otherwise."""
        db = self._connect()
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = work(db, self.wall())
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
                return result
            except sqlite3.Error as e:
                try:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise KuraError("limiter_unavailable", "Shared rate-limit state is unavailable") from e
        finally:
            db.close()

    def _row(self, db, now: float) -> dict:
        """The service row with every stored time clamped into [0, now + max_penalty]."""
        row = db.execute(
            "SELECT next_ok, interval, penalty_until, observed_limit, remaining, reset_at "
            "FROM rate_limits WHERE service=?", (self.service,)).fetchone()
        ceiling = now + self.max_penalty
        if row is None:
            return {"next_ok": 0.0, "interval": self.interval, "penalty_until": 0.0,
                    "observed_limit": None, "remaining": None, "reset_at": None}
        next_ok, interval, penalty, limit, remaining, reset = row

        def clamp(value):
            return min(max(value, 0.0), ceiling) if isinstance(value, (int, float)) and value == value else 0.0

        if limit is None or not isinstance(interval, (int, float)) or not 0 < interval <= self.max_penalty:
            interval = self.interval        # no usable advertised value: configuration wins
        return {"next_ok": clamp(next_ok), "interval": float(interval), "penalty_until": clamp(penalty),
                "observed_limit": limit, "remaining": remaining, "reset_at": reset}

    def _store(self, db, now: float, row: dict):
        db.execute(
            "INSERT INTO rate_limits(service, next_ok, interval, penalty_until, observed_limit, "
            "remaining, reset_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(service) DO UPDATE SET next_ok=excluded.next_ok, interval=excluded.interval, "
            "penalty_until=excluded.penalty_until, observed_limit=excluded.observed_limit, "
            "remaining=excluded.remaining, reset_at=excluded.reset_at, updated_at=excluded.updated_at",
            (self.service, row["next_ok"], row["interval"], row["penalty_until"],
             row["observed_limit"], row["remaining"], row["reset_at"], now))

    # -- API ----------------------------------------------------------------------

    def acquire(self, budget: float) -> float:
        """Reserve the next request start and sleep until it. Returns seconds waited.

        Raises ``rate_limited`` (with ``retry_after``) without reserving anything when the
        wait would exceed `budget` seconds. After waking it re-reads the shared penalty, so a
        429 recorded by another process during the sleep still holds this request back (or
        refuses it when the extra wait no longer fits the budget; the slot is then spent).
        """
        if not budget >= 0:
            raise KuraError("invalid_argument", "Invalid rate-limit budget")

        def reserve(db, now):
            row = self._row(db, now)
            start = max(now, row["next_ok"], row["penalty_until"])
            wait = start - now
            if wait > budget:
                raise KuraError("rate_limited", "Rate limit window is still open",
                                retry_after=round(wait, 3), service=self.service)
            row["next_ok"] = start + row["interval"]
            self._store(db, now, row)
            return wait

        waited = self._transaction(reserve)
        if waited > 0:
            self.sleep(waited)
        # A penalty (429, exhausted window) recorded by another process while we slept
        # outranks our reservation: wait it out too, still inside the caller's budget.
        while True:
            def recheck(db, now):
                remaining = self._row(db, now)["penalty_until"] - now
                if remaining > budget - waited:
                    raise KuraError("rate_limited", "Rate limit window is still open",
                                    retry_after=round(remaining, 3), service=self.service)
                return max(remaining, 0.0)
            extra = self._transaction(recheck)
            if extra <= 0:
                return waited
            self.sleep(extra)
            waited += extra

    def done(self):
        """Request finished: the next one may start no sooner than one interval from now."""
        def finish(db, now):
            row = self._row(db, now)
            row["next_ok"] = max(row["next_ok"], now + row["interval"])
            self._store(db, now, row)
        self._transaction(finish)

    def penalize(self, until: float):
        """Block every process until the wall-clock time `until` (capped at ``max_penalty``)."""
        if not isinstance(until, (int, float)) or until != until:
            raise KuraError("invalid_argument", "Invalid penalty time")

        def apply(db, now):
            row = self._row(db, now)
            row["penalty_until"] = max(row["penalty_until"], min(float(until), now + self.max_penalty))
            self._store(db, now, row)
        self._transaction(apply)

    def observe(self, *, limit=None, remaining=None, reset=None, interval=None):
        """Record service-advertised limits. An `interval` only sticks together with a `limit`."""
        def numbers(*values):
            return all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)
                                     and v == v and abs(v) != float("inf")) for v in values)
        if not numbers(limit, remaining, reset, interval):
            raise KuraError("invalid_argument", "Invalid rate-limit observation")

        def apply(db, now):
            row = self._row(db, now)
            if limit is not None and limit > 0:
                row["observed_limit"] = int(limit)
                if interval is not None and 0 < interval <= self.max_penalty:
                    # A larger interval also pushes out the slot already reserved by the last start.
                    if interval > row["interval"] and row["next_ok"] > now:
                        row["next_ok"] = min(row["next_ok"] + (interval - row["interval"]),
                                             now + self.max_penalty)
                    row["interval"] = float(interval)
            if remaining is not None:
                row["remaining"] = int(remaining)
            if reset is not None:
                row["reset_at"] = float(reset)
            self._store(db, now, row)
        self._transaction(apply)

    def state(self) -> dict:
        def read(db, now):
            row = self._row(db, now)
            return {"service": self.service,
                    "next_ok_in_s": max(row["next_ok"] - now, 0.0), "interval": row["interval"],
                    "observed_limit": row["observed_limit"], "remaining": row["remaining"],
                    "penalty_in_s": max(row["penalty_until"] - now, 0.0)}
        return self._transaction(read)
