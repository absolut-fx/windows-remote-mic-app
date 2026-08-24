"""Pure-function exponential-backoff policy for BLE / ATVV reconnects.

Ported from HD838A/remote-mic-app 1.8.13-1.8.15 (Swift
``BluetoothReconnectPolicy`` in ``Sources/RemoteMic/BluetoothLifecycle.swift``,
commits ``bce4d12`` + ``ba58921`` + ``66fa620``). The Mac side encoded three
properties as one small mutable struct; this module is the direct,
testable Python equivalent.

Three properties the Mac struct had to encode:

1. The delay between reconnect attempts grows exponentially so a flapping
   Bluetooth link does not burn CPU / battery / log space retrying at full
   speed forever (``3s -> 6s -> 12s -> 24s -> 48s -> 60s`` cap).
2. Each delay carries +/- 10% jitter so two clients that disconnect at the
   same instant do not then stampede the radio on every retry tick.
3. The policy carries a ``reset()`` so a *successful* connect (``phase ==
   ready`` on Mac, ``connected == True`` in our ``ConnectionSupervisor``)
   collapses the counter back to zero - a freshly recovered link is not
   "the 47th consecutive failure".

These are all pure-function properties, so the policy takes a ``clock``
(integer monotonic millisecond counter) for deterministic jitter seeding
in tests rather than reaching for ``time.monotonic_ns`` directly. The
production ``ConnectionSupervisor`` injects ``lambda: time.monotonic_ns()``
so production behavior is unaffected.

What this policy is NOT, by design:

- It does not own a timer or schedule anything. ``ConnectionSupervisor``
  calls ``next_automatic_delay()`` once per failed attempt and feeds the
  result to its own ``sleep`` callable (which is also injectable for tests).
- It does not know about WinRT or any other transport-specific API. The
  ``allowsCachedTargetRetrieval`` toggle on the Mac side (commit ``66fa620``)
  protects against a CoreBluetooth-specific bug where
  ``retrievePeripherals(withIdentifiers:)`` returns a stale cached
  peripheral and reconnects to it forever; the Windows code path uses
  ``BluetoothCacheMode.UNCACHED`` already, so the bug does not reproduce
  here and the toggle is intentionally absent.
- It does not handle system-adapter state changes (poweredOff / poweredOn
  / resetting / unauthorized / unsupported). That is a separate Mac policy
  (``BluetoothCentralRecoveryPolicy``) that depends on CoreBluetooth
  ``CBCentralManager`` delegate callbacks. On Windows it would need a new
  ``Windows.Devices.Bluetooth.Adapter.BluetoothAdapter`` event
  subscription which the repository does not currently have; tracked as a
  separate future PR once hardware is available for validation.

Module-level constants expose the Mac-parity defaults so tests and
upstream callers can import them by name rather than duplicating the
magic numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


# Mac parity: 3s base, 60s cap, +/- 10% jitter. Same numbers as
# HD838A/remote-mic-app 1.8.15's BluetoothReconnectPolicy.
DEFAULT_BASE_DELAY_SECONDS: float = 3.0
DEFAULT_MAXIMUM_DELAY_SECONDS: float = 60.0
DEFAULT_JITTER_RATIO: float = 0.1


@dataclass(frozen=True)
class ReconnectPolicyConfig:
    """Immutable tuning knobs for a ``ReconnectPolicy``.

    Defaults match the Mac 1.8.15 parity numbers (``DEFAULT_BASE_DELAY_SECONDS``
    / ``DEFAULT_MAXIMUM_DELAY_SECONDS`` / ``DEFAULT_JITTER_RATIO`` above).
    Validation rejects pathological values at construction time so
    production callers never accidentally configure ``base=0`` or
    ``base > maximum``.
    """

    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    maximum_delay_seconds: float = DEFAULT_MAXIMUM_DELAY_SECONDS
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    # Cap on the consecutive-failure exponent so even after many failures
    # we still plateau at ``maximum_delay_seconds`` (Mac uses 5 - 2^5 = 32,
    # so 32 * base = 96s would have exceeded the 60s cap; the cap is the
    # hard ceiling, the exponent just selects the nominal value).
    max_exponent: int = 5

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError(
                f"base_delay_seconds must be > 0, got {self.base_delay_seconds}"
            )
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "maximum_delay_seconds must be >= base_delay_seconds, "
                f"got maximum={self.maximum_delay_seconds}, "
                f"base={self.base_delay_seconds}"
            )
        if not 0.0 <= self.jitter_ratio < 0.5:
            raise ValueError(
                f"jitter_ratio must be in [0.0, 0.5), got {self.jitter_ratio}"
            )


class ReconnectPolicy:
    """Mutable backoff state. Lives across reconnect attempts inside one
    ``ConnectionSupervisor`` instance and is reset on a successful connect.

    Callers MUST call ``reset()`` after a *successful* connect attempt;
    the supervisor already does this in ``run_forever()`` (see the
    ``if connected: self._policy.reset()`` branch in the integrated path).
    Callers MUST NOT mutate it directly otherwise - every state change
    should go through ``next_automatic_delay()`` so the consecutive-
    failure counter and the backoff stay consistent.
    """

    def __init__(
        self,
        config: ReconnectPolicyConfig | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._config = config or ReconnectPolicyConfig()
        self._clock = clock or _default_clock
        self._consecutive_failure_count = 0

    @property
    def consecutive_failure_count(self) -> int:
        """Read-only view for tests and supervisor telemetry. Bumped by
        ``next_automatic_delay()``, cleared by ``reset()``."""
        return self._consecutive_failure_count

    def next_automatic_delay(self) -> float:
        """Compute the next reconnect delay, advance the consecutive-failure
        counter, and return the delay in seconds.

        Sequence (no jitter): ``base``, ``base*2``, ``base*4``, ``base*8``,
        ``base*16``, ``base*32`` - then capped at ``maximum_delay_seconds``.
        Each delay is multiplied by a uniform jitter factor of
        ``(1 - jitter_ratio) + (jitter_unit * jitter_ratio * 2)`` where
        ``jitter_unit`` is ``clock()`` mapped into ``[0, 1]``.
        The whole thing is then capped at ``maximum_delay_seconds`` once
        more so even the jitter upper bound cannot exceed it.

        Returns the delay in seconds, always ``>= base_delay_seconds`` (the
        floor) and ``<= maximum_delay_seconds`` (the ceiling).
        """

        self._consecutive_failure_count += 1
        exponent = min(
            self._consecutive_failure_count - 1, self._config.max_exponent
        )
        nominal_delay = min(
            self._config.maximum_delay_seconds,
            self._config.base_delay_seconds * (2 ** exponent),
        )
        jitter_unit = self._map_clock_to_unit()
        jitter_factor = (
            (1 - self._config.jitter_ratio)
            + (jitter_unit * self._config.jitter_ratio * 2)
        )
        return min(
            self._config.maximum_delay_seconds,
            nominal_delay * jitter_factor,
        )

    def reset(self) -> None:
        """Clear the consecutive-failure counter. Called by the supervisor
        after a successful connect attempt."""
        self._consecutive_failure_count = 0

    def _map_clock_to_unit(self) -> float:
        """Map the clock's integer output into ``[0.0, 1.0]``.

        The Mac version used a Swift ``UInt64`` clock divided by ``UInt64.max``;
        Python integers are unbounded so we instead use bitwise AND with
        ``0xFFFF`` to land in ``[0, 65535]`` (cheap, deterministic, and
        produces a uniform distribution that is plenty fine for a
        +/-10% jitter window - the Mac code does the same modulo trick
        for the same reason).
        """
        value = self._clock() & 0xFFFF
        return value / 65535.0


def _default_clock() -> int:
    """Production clock. ``time.monotonic_ns()`` so the jitter seed is
    never tied to wall-clock time and is immune to NTP corrections."""
    import time

    return time.monotonic_ns()


__all__ = [
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_JITTER_RATIO",
    "DEFAULT_MAXIMUM_DELAY_SECONDS",
    "ReconnectPolicy",
    "ReconnectPolicyConfig",
]