"""Generic connect/run/cleanup/retry loop, decoupled from any concrete BLE,
HID, or audio backend so it is unit-testable with fake async callables (see
tests/test_connection_supervisor.py) rather than requiring real hardware or
a live WinRT/hidapi/PortAudio runtime.

Added after XRBM-014 review RETRY P1 #2: the previous app.py connected once
and waited forever, with no reconnect loop and no guarantee that cleanup ran
on every failure path. This module makes both properties structural:

- ``cleanup`` always runs after every ``connect``/``wait`` attempt, whether
  that attempt succeeded, raised, or was ended by ``request_reconnect()`` -
  it is the only ``finally`` in the loop body.
- A failed or ended attempt always leads to another ``connect`` attempt
  (after ``retry_delay``), until ``stop()`` is called.

Thread safety (fixed after XRBM-014 review round 2 P1 #5): ``asyncio.Event``
is documented as not thread-safe - only code running on the event loop's own
thread may call ``.set()`` directly. ``request_reconnect()`` is invoked from
WinRT/worker-thread callbacks (a BLE disconnect notification, an ATVV
protocol error decoded on ble_transport_winrt.py's worker thread, a failed
audio-playback write), so it now always hops onto the owning loop via
``loop.call_soon_threadsafe()`` instead of mutating the event directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from . import reconnect_policy

ConnectFn = Callable[[], Awaitable[None]]
CleanupFn = Callable[[], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]


class ConnectionSupervisor:
    def __init__(
        self,
        connect: ConnectFn,
        cleanup: CleanupFn,
        retry_delay: float = 3.0,
        max_retry_delay: Optional[float] = 60.0,
        logger: Optional[logging.Logger] = None,
        sleep: SleepFn = asyncio.sleep,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        policy: Optional[reconnect_policy.ReconnectPolicy] = None,
    ) -> None:
        self._connect = connect
        self._cleanup = cleanup
        # ``retry_delay`` / ``max_retry_delay`` are kept as constructor args
        # for backward compatibility with existing callers (app.py, tests),
        # but the actual delay between attempts is owned by ``ReconnectPolicy``
        # - the constructor args are now used only to build a default policy
        # if the caller did not inject one. Callers that need a custom
        # config (different base, jitter ratio, etc.) should pass ``policy``
        # directly; the policy's clock is injectable for deterministic tests.
        self._retry_delay = max(0.0, retry_delay)
        if max_retry_delay is None:
            max_retry_delay = self._retry_delay
        self._max_retry_delay = max(self._retry_delay, max_retry_delay)
        if policy is None:
            policy = reconnect_policy.ReconnectPolicy(
                config=reconnect_policy.ReconnectPolicyConfig(
                    base_delay_seconds=self._retry_delay,
                    maximum_delay_seconds=self._max_retry_delay,
                )
            )
        self._policy = policy
        self._logger = logger
        self._sleep = sleep
        # Captured at construction time (must happen on the loop that will
        # run run_forever()/stop()) so request_reconnect() can safely hop
        # back onto it from any other thread - see module docstring.
        self._loop = loop or asyncio.get_event_loop()
        self._disconnect_event = asyncio.Event()
        self._stopping = False

        # Exposed for tests to assert on, without needing to intercept the
        # injected connect/cleanup callables themselves.
        self.attempt_count = 0
        self.cleanup_count = 0
        self.last_error: Optional[BaseException] = None

    @property
    def policy(self) -> reconnect_policy.ReconnectPolicy:
        """The reconnect policy in use. Read-only view for tests; the
        supervisor mutates the policy's internal counter directly."""
        return self._policy

    def request_reconnect(self) -> None:
        """Call from any callback (a BLE disconnect notification, a protocol
        error, a dead HID listener, a failed playback write) to end the
        current attempt early and start a fresh connect/cleanup cycle.

        Safe to call from ANY thread, not only the event loop's own thread
        (XRBM-014 review round 2 P1 #5): hops onto the owning loop via
        ``call_soon_threadsafe`` rather than setting the ``asyncio.Event``
        directly, which is only safe from the loop thread itself.
        """

        self._loop.call_soon_threadsafe(self._disconnect_event.set)

    async def run_forever(self) -> None:
        while not self._stopping:
            self.attempt_count += 1
            self._disconnect_event.clear()
            connected = False
            try:
                self.last_error = None
                await self._connect()
                connected = True
                await self._disconnect_event.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - deliberately broad: any
                # connect()/wait() failure must still fall through to cleanup
                # and a retry, never propagate out of the supervisor.
                self.last_error = exc
                if self._logger is not None:
                    self._logger.info("connection attempt failed: %s", exc)
            finally:
                await self._cleanup()
                self.cleanup_count += 1

            if self._stopping:
                break
            if connected:
                # Mac parity (HD838A BluetoothReconnectPolicy.reset): a
                # truly Ready device collapses the consecutive-failure
                # counter to zero so a freshly recovered link is not
                # treated as "the 47th consecutive failure". The first
                # delay after a successful connect is the policy's base.
                self._policy.reset()
                delay = self._retry_delay
            else:
                # Mac parity (HD838A BluetoothReconnectPolicy.nextAutomaticDelay):
                # each consecutive failure advances the exponential backoff
                # by one exponent with +/- 10% jitter, capped at max_delay.
                delay = self._policy.next_automatic_delay()
            await self._sleep(delay)

    async def stop(self) -> None:
        """Ends the loop after its current cleanup finishes. Safe to call
        even if run_forever() was never started or already finished.
        """

        self._stopping = True
        self._disconnect_event.set()
