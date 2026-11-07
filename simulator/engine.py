"""A simulated charger that behaves like a real one, in OCPP 1.6J or 2.0.1.

Every message is built and checked by the same `ocpp` library the backend uses, so what it sends is valid by
construction: unlike several online simulators, it cannot put a field where the schema does not allow it.

The charger outlives its connection, as real hardware does. Three layers:
  - Charger: what a person at the charger sees and does (connectors, cable, card reader, meter), the running
    transactions, and an outbox of messages waiting for a connection.
  - Messages16 / Messages201: build the OCPP payloads for one version.
  - Link16 / Link201: one WebSocket connection. They answer what the backend asks and carry calls. A
    supervisor opens a new link whenever one drops, until the charger is removed.

Behaviour, in both versions:
  - A transaction starts when a connector has both a cable and an accepted token (card tap or the app's
    remote start), in either order. Starting needs the backend; no offline starts, as the backend configures.
  - The meter is a cumulative register per connector. It never goes down; readings carry real timestamps.
  - A remote stop ends the transaction with reason Remote and the cable stays in. Unplugging while charging
    ends it with EVDisconnected; "Stop" is the driver's own stop (Local).

Losing the connection, the cases a CSMS must survive:
  - Network drop: the link dies, charging continues, readings and stops wait in the outbox. The charger
    reconnects without a BootNotification (it never rebooted) and sends the outbox in order; 2.0.1 events
    that happened offline say so (offline=true).
  - The backend drops the link (restart, idle timeout): the charger reconnects with backoff, 1 s doubling to
    30 s, and carries on as after a network drop.
  - Power cut, transaction lost: charging stops, the outbox is gone, and after the reboot the charger knows
    nothing of its transactions. The backend's power-loss recovery closes them.
  - Power cut, transaction saved: the charger kept its transactions and outbox in flash. After the reboot it
    sends the outbox, then ends each saved transaction with reason PowerLoss and its final reading.
  - Reconnect: a clean close and a new connection; nothing is lost.
"""

import asyncio
import base64
import itertools
import logging
import uuid
from collections import deque
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ocpp import v16, v201
from ocpp.routing import on
from ocpp.v16.enums import Action as Action16
from ocpp.v201.enums import Action as Action201
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger("simulator")

VERSIONS = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1"}
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
LOG_LIMIT = 500
OUTBOX_LIMIT = 5000  # a real charger's storage is finite too; the oldest readings go first
BACKOFF_MAX_S = 30.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Offline(RuntimeError):
    """The action needs the backend and there is no connection, or the connection died mid-call."""


async def _ask(link: "Link16 | Link201", payload: Any) -> Any:
    """Call over one link, giving up the moment that link dies. Otherwise a call in flight when the network
    drops waits out the full response timeout on a socket nobody reads any more."""
    call = asyncio.ensure_future(link.call(payload))
    lost = asyncio.ensure_future(link.lost.wait())
    try:
        done, _ = await asyncio.wait({call, lost}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        call.cancel()  # never leave a call running on its own: it would hold the link's call lock
        raise
    finally:
        lost.cancel()
    if call in done:
        return call.result()
    call.cancel()
    raise Offline("the connection dropped")


@dataclass
class Connector:
    no: int
    status: str = "Available"  # as the charger reports it in its version
    plugged: bool = False
    id_tag: str | None = None  # an accepted token waiting for the cable, or the running transaction's
    remote_start_id: int | None = None  # 2.0.1: the app's start request this transaction answers
    transaction: str | int | None = None  # 1.6: the backend's integer; 2.0.1: our own name
    register_wh: int = 0
    start_wh: int | None = None  # the register when the running transaction started
    started_at: str | None = None
    power_w: int = 0
    stop: asyncio.Event | None = None  # set to end the running transaction
    stop_reason: str = "Local"
    saved: bool = False  # the transaction survived a power cut and must be ended after the reboot
    starting: bool = False  # a start is waiting for the backend's answer; never start twice
    seq: Iterator[int] = field(default_factory=lambda: itertools.count(0))  # 2.0.1 seqNo
    note: str = ""  # the last thing that happened, for the page


@dataclass
class Frame:
    seq: int
    at: str
    direction: str  # "out" (charger to backend) or "in"
    raw: str


@dataclass
class Settings:
    url: str  # backend base, e.g. ws://localhost:8000/ocpp; the charge point ID is appended
    charge_point_id: str
    version: str = "1.6"
    password: str | None = None
    connectors: int = 2
    power_w: int = 7400
    meter_interval_s: float = 5.0
    speed: float = 1.0  # energy fast-forward: 60 charges an hour's energy in a minute
    vendor: str = "CCMS"
    model: str = "Simulator"


class _LoggingConnection:
    """The socket as the ocpp library sees it, recording every frame for the page."""

    def __init__(self, ws: ClientConnection, charger: "Charger") -> None:
        self._ws = ws
        self._charger = charger

    async def recv(self) -> str:
        raw = await self._ws.recv()
        text = raw if isinstance(raw, str) else raw.decode(errors="replace")
        self._charger._record("in", text)
        return text

    async def send(self, raw: str) -> None:
        self._charger._record("out", raw)
        await self._ws.send(raw)


class Charger:
    def __init__(self, settings: Settings) -> None:
        if settings.version not in VERSIONS:
            raise ValueError(f"unsupported OCPP version {settings.version!r}")
        self.settings = settings
        self.connectors = {n: Connector(n) for n in range(1, settings.connectors + 1)}
        self.messages: Messages16 | Messages201 = (
            Messages16(settings) if settings.version == "1.6" else Messages201(settings)
        )
        self.frames: deque[Frame] = deque(maxlen=LOG_LIMIT)
        self.outbox: deque[Any] = deque(maxlen=OUTBOX_LIMIT)
        # connecting, connected, offline (will reconnect), disconnected (removed), failed (refused)
        self.state = "disconnected"
        self.error: str | None = None
        self.offline_reason: str | None = None
        self.retry_at: float | None = None  # loop time of the next connection attempt
        self.heartbeat_s = 60.0
        self.config: dict[str, str] = {}
        self.boots = 0
        self._seq = itertools.count(1)
        self._link: Link16 | Link201 | None = None
        self._ws: ClientConnection | None = None
        self._booted = False  # BootNotification sent since the last power-up
        self._want_online = False
        self._resume_at = 0.0  # loop time before which the link stays down (network drop, power cut)
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()  # outlive a link: charging loops
        self._link_tasks: set[asyncio.Task[Any]] = set()  # die with a link: heartbeats, flushing

    # --- tasks

    def _record(self, direction: str, raw: str) -> None:
        self.frames.append(Frame(next(self._seq), now(), direction, raw))

    def _spawn(self, coro: Coroutine[Any, Any, Any], *, with_link: bool = False) -> None:
        task = asyncio.create_task(coro)
        tasks = self._link_tasks if with_link else self._tasks
        tasks.add(task)
        task.add_done_callback(lambda t: self._done(t, tasks))

    def _done(self, task: asyncio.Task[Any], tasks: set[asyncio.Task[Any]]) -> None:
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            if isinstance(exc, ConnectionClosed | Offline | TimeoutError):
                return  # the link went away mid-call; the supervisor reports and reconnects
            log.warning("simulator task failed: %r", exc)
            self.error = _describe(exc) if exc else None

    @property
    def online(self) -> bool:
        return self._link is not None and self.state == "connected"

    # --- connection

    def start(self) -> None:
        """Go online and stay online: the supervisor reconnects whenever the link drops, until
        disconnect()."""
        self._want_online = True
        self.state, self.error = "connecting", None
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise())

    async def wait_connected(self, timeout: float = 10) -> None:
        async with asyncio.timeout(timeout):
            while not self.online:
                if self.state == "failed":
                    raise ConnectionError(self.error or "connection failed")
                await asyncio.sleep(0.02)

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        while self._want_online:
            wait = self._resume_at - loop.time()
            if wait > 0:
                self.retry_at = self._resume_at
                await self._sleep(wait)
                continue
            self.state, self.retry_at = "connecting", None
            connected = False
            try:
                connected = await self._connect_once()
            except InvalidStatus as exc:
                # The backend refused the handshake: retrying the same thing would only repeat it.
                self.state, self.error, self._want_online = "failed", _describe(exc), False
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = _describe(exc)
                self.offline_reason = self.offline_reason or "could not reach the backend"
            if not self._want_online:
                break
            if connected:
                backoff = 1.0
            if self._resume_at > loop.time():
                continue  # a deliberate outage: wait it out, then reconnect at once
            self.state = "offline"
            self.retry_at = loop.time() + backoff
            await self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
        self.state, self.retry_at = "disconnected", None

    async def _sleep(self, seconds: float) -> None:
        """Sleep, unless reconnect() asks to try now."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _connect_once(self) -> bool:
        """One connection, from handshake to drop. Returns whether it got as far as connected."""
        s = self.settings
        headers = {}
        if s.password:
            token = base64.b64encode(f"{s.charge_point_id}:{s.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        async with connect(
            f"{s.url.rstrip('/')}/{s.charge_point_id}",
            subprotocols=[VERSIONS[s.version]],  # type: ignore[list-item]
            additional_headers=headers,
            open_timeout=15,
            ping_interval=20,  # the link's own liveness, as most chargers use
        ) as ws:
            self._ws = ws
            link: Link16 | Link201 = (Link16 if s.version == "1.6" else Link201)(s.charge_point_id, ws, self)
            reader = asyncio.create_task(link.start())
            self._link, self.state, self.error = link, "connected", None
            # Coming online runs beside the reader, so a link that dies mid-boot fails its calls at once
            # instead of each waiting out the response timeout.
            self._spawn(self._come_online(link), with_link=True)
            try:
                await reader
            except ConnectionClosed as exc:
                self.offline_reason = self.offline_reason or _closed(exc)
            finally:
                link.lost.set()  # fails every call still waiting on this link
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                for task in list(self._link_tasks):
                    task.cancel()
                self._link, self._ws = None, None
                if self._want_online and self.state == "connected":
                    self.state = "offline"
            return True

    async def _come_online(self, link: "Link16 | Link201") -> None:
        """What a charger does on a fresh connection: boot only after a power-up, send what waited, say how
        each connector is, then heartbeat."""
        if not self._booted:
            reply = await _ask(link, self.messages.boot())
            self.heartbeat_s = float(reply.interval or 60)
            self._booted, self.boots = True, self.boots + 1
        await self._flush(link)
        for c in self.connectors.values():
            if c.saved:
                await self._end_saved(c)
        for c in self.connectors.values():
            await self._status(c)
        self.offline_reason = None
        self._spawn(self._heartbeats(link), with_link=True)
        for c in self.connectors.values():  # a start that was waiting for the backend
            if c.plugged and c.id_tag and c.transaction is None:
                self._spawn(self._charge(c))

    async def _flush(self, link: "Link16 | Link201") -> None:
        while self.outbox:
            await _ask(link, self.outbox[0])
            self.outbox.popleft()

    async def _heartbeats(self, link: "Link16 | Link201") -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            await _ask(link, self.messages.heartbeat())

    async def _call(self, payload: Any) -> Any:
        """A call that needs the backend's answer now: Authorize, a start. Fails when offline."""
        if self._link is None:
            raise Offline("the charger is offline")
        return await _ask(self._link, payload)

    async def _deliver(self, payload: Any) -> None:
        """A message the backend must get eventually: readings, stops. Queued when offline, or when the link
        dies while sending (it may have arrived; the backend treats a repeat as the same message)."""
        if self._link is not None and not self.outbox:
            try:
                await _ask(self._link, payload)
                return
            except (ConnectionClosed, TimeoutError, Offline):
                pass
        self.outbox.append(payload)

    async def _status(self, c: Connector, **how: bool) -> None:
        """Report a connector's status now. Offline it is skipped: the next connection reports every
        connector's current status anyway."""
        c.status = self.messages.status_name(c, **how)
        if self._link is not None:
            try:
                await _ask(self._link, self.messages.status(c))
            except (ConnectionClosed, TimeoutError, Offline):
                pass

    async def reconnect(self) -> None:
        """Close the connection cleanly and open a new one at once. Running transactions carry on."""
        self._resume_at = 0.0
        self.offline_reason = "reconnecting"
        if self._ws is not None:
            await self._ws.close()
        self._wake.set()
        if not self._want_online:
            self.start()

    async def network_drop(self, seconds: float = 30) -> None:
        """The network goes away for `seconds`. The charger keeps charging and queues what it would send."""
        self._resume_at = asyncio.get_running_loop().time() + seconds
        self.offline_reason = f"network drop ({seconds:g} s)"
        if self._ws is not None:
            self._ws.transport.abort()  # no close frame: the link simply dies

    async def power_cut(self, back_after_s: float = 3, *, keep_transactions: bool = False) -> None:
        """Lose power for `back_after_s`. Charging stops at once. With keep_transactions the charger saved its
        transactions and outbox and ends them with PowerLoss after the reboot; without, both are lost."""
        self._resume_at = asyncio.get_running_loop().time() + back_after_s
        self.offline_reason = "power cut, transactions " + ("saved" if keep_transactions else "lost")
        self._booted = False
        # The power goes first: the link dies before anything else can be sent on it.
        if self._link is not None:
            self._link.lost.set()
        if self._ws is not None:
            self._ws.transport.abort()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if not keep_transactions:
            self.outbox.clear()
        for c in self.connectors.values():
            c.power_w, c.stop, c.starting = 0, None, False
            if keep_transactions and c.transaction is not None:
                c.saved = True
                c.note = "power cut: transaction saved"
            else:
                if c.transaction is not None:
                    c.note = "power cut: transaction lost"
                c.transaction, c.id_tag, c.remote_start_id = None, None, None
                c.start_wh, c.started_at = None, None

    async def disconnect(self) -> None:
        """Remove the charger: go offline for good."""
        self._want_online = False
        self._wake.set()
        if self._ws is not None:
            await self._ws.close()
        for task in list(self._tasks):
            task.cancel()
        tasks = [*self._tasks, *([self._supervisor] if self._supervisor else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.state = "disconnected"

    # --- what a person does at the charger

    async def plug_in(self, no: int) -> None:
        c = self.connectors[no]
        if c.plugged:
            return
        c.plugged, c.note = True, "cable plugged in"
        await self._status(c)
        if c.id_tag and c.transaction is None and self.online:
            self._spawn(self._charge(c))

    async def unplug(self, no: int) -> None:
        c = self.connectors[no]
        if c.transaction is not None and c.stop is not None:
            c.stop_reason = "EVDisconnected"
            c.stop.set()
            while c.transaction is not None:
                await asyncio.sleep(0.05)
        c.plugged, c.id_tag, c.remote_start_id, c.note = False, None, None, "cable unplugged"
        await self._status(c)

    async def tap(self, no: int, id_tag: str) -> str:
        """A card on the reader. Returns the backend's answer, or "Offline"."""
        c = self.connectors[no]
        if c.transaction is not None and id_tag.upper() == (c.id_tag or "").upper():
            await self.stop(no)  # the same card again is the driver stopping, online or not
            return "Accepted"
        if not self.online:
            c.note = f"card {id_tag}: no connection, cannot authorize"
            return "Offline"
        reply = await self._call(self.messages.authorize(id_tag))
        status = self.messages.authorize_status(reply)
        c.note = f"card {id_tag}: {status}"
        if status != "Accepted" or c.transaction is not None:
            return status
        c.id_tag, c.remote_start_id = id_tag, None
        if c.plugged:
            self._spawn(self._charge(c))
        else:
            await self._status(c, waiting=True)
        return status

    async def stop(self, no: int) -> None:
        """The driver's own stop (the charger's button)."""
        c = self.connectors[no]
        if c.transaction is not None and c.stop is not None:
            c.stop_reason = "Local"
            c.stop.set()

    # --- what the backend asks, called by the links

    def remote_start(self, id_tag: str, no: int | None, remote_start_id: int | None = None) -> bool:
        free = [c for c in self.connectors.values() if c.transaction is None and not c.id_tag]
        c = self.connectors.get(no) if no else (free[0] if free else None)
        if c is None or c.transaction is not None:
            return False
        c.id_tag, c.remote_start_id = id_tag, remote_start_id
        c.note = "remote start: plug in to begin" if not c.plugged else "remote start"
        if c.plugged:
            self._spawn(self._charge(c))
        else:
            self._spawn(self._status(c, waiting=True))
        return True

    def remote_stop(self, transaction: str | int) -> bool:
        for c in self.connectors.values():
            if c.transaction is not None and str(c.transaction) == str(transaction) and c.stop is not None:
                c.stop_reason = "Remote"
                c.stop.set()
                return True
        return False

    def triggered_readings(self, no: int | None) -> None:
        for c in self.connectors.values():
            if no in (None, c.no):
                self._spawn(self._deliver(self.messages.reading(c, in_transaction=False, offline=False)))

    # --- a transaction

    async def _charge(self, c: Connector) -> None:
        if c.id_tag is None or c.transaction is not None or c.starting:
            return
        c.stop, c.stop_reason, c.seq, c.starting = asyncio.Event(), "Local", itertools.count(0), True
        try:
            tx, status = await self.messages.start(c, self._call)
        except (Offline, ConnectionClosed, TimeoutError):
            c.note = "start waits for the connection"
            return
        finally:
            c.starting = False
        if status != "Accepted":
            # StopTransactionOnInvalidId / StopTxOnInvalidId: a refused start ends at once.
            c.note = f"start refused: {status}"
            c.transaction = tx
            await self._deliver(self.messages.stop(c, "DeAuthorized", offline=not self.online))
            c.transaction, c.id_tag, c.remote_start_id, c.stop = None, None, None, None
            await self._status(c)
            return
        c.transaction, c.power_w = tx, self.settings.power_w
        c.start_wh, c.started_at = c.register_wh, now()
        c.note = f"charging: transaction {tx}"
        await self._status(c, charging=True)
        loop = asyncio.get_running_loop()
        last, carry = loop.time(), 0  # carry: energy below 1 Wh, in W·s, so nothing is lost to rounding
        while not c.stop.is_set():
            try:
                await asyncio.wait_for(c.stop.wait(), timeout=self.settings.meter_interval_s)
            except TimeoutError:
                pass
            t = loop.time()
            carry += round(c.power_w * (t - last) * self.settings.speed)
            last = t
            c.register_wh += carry // 3600
            carry %= 3600
            if not c.stop.is_set():
                await self._deliver(self.messages.reading(c, in_transaction=True, offline=not self.online))
        c.power_w = 0
        await self._deliver(self.messages.stop(c, c.stop_reason, offline=not self.online))
        c.note = f"stopped ({c.stop_reason}) at {c.register_wh} Wh" + (
            "" if self.online else ", sent when back online"
        )
        c.start_wh, c.started_at = None, None
        c.transaction, c.id_tag, c.remote_start_id, c.stop = None, None, None, None
        await self._status(c, finished=True)

    async def _end_saved(self, c: Connector) -> None:
        """After the reboot: end the transaction the power cut interrupted, with the register as it stood."""
        await self._deliver(self.messages.stop(c, "PowerLoss", offline=False))
        c.note = f"ended after the power cut (PowerLoss) at {c.register_wh} Wh"
        c.saved, c.start_wh, c.started_at = False, None, None
        c.transaction, c.id_tag, c.remote_start_id = None, None, None

    # --- for the page

    def snapshot(self) -> dict[str, Any]:
        try:
            loop_now = asyncio.get_running_loop().time()
        except RuntimeError:
            loop_now = 0.0
        return {
            "charge_point_id": self.settings.charge_point_id,
            "version": self.settings.version,
            "url": self.settings.url,
            "state": self.state,
            "error": self.error,
            "offline_reason": self.offline_reason,
            "retry_in_s": max(0.0, round(self.retry_at - loop_now, 1)) if self.retry_at else None,
            "queued": len(self.outbox),
            "boots": self.boots,
            "power_w": self.settings.power_w,
            "speed": self.settings.speed,
            "connectors": [
                {
                    "no": c.no,
                    "status": c.status,
                    "plugged": c.plugged,
                    "id_tag": c.id_tag,
                    "transaction": c.transaction,
                    "saved": c.saved,
                    "register_wh": c.register_wh,
                    "session_wh": c.register_wh - c.start_wh if c.start_wh is not None else None,
                    "started_at": c.started_at,
                    "power_w": c.power_w,
                    "note": c.note,
                }
                for c in self.connectors.values()
            ],
            "last_frame": self.frames[-1].seq if self.frames else 0,
        }


def _closed(exc: ConnectionClosed) -> str:
    if exc.rcvd is None:
        return "the connection dropped"
    reason = f": {exc.rcvd.reason}" if exc.rcvd.reason else ""
    return f"the backend closed the connection ({exc.rcvd.code}{reason})"


def _describe(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        reasons = {
            401: "wrong charge point ID or password",
            400: "subprotocol refused",
            403: "refused (TLS?)",
        }
        code = response.status_code
        return f"handshake rejected: HTTP {code} ({reasons.get(code, 'see the backend log')})"
    if isinstance(exc, OSError):
        return f"cannot reach the backend: {exc.strerror or exc}"
    return f"{type(exc).__name__}: {exc}"


def _reading(
    wh: int, *, context: str | None = None, power_w: int | None = None, v201: bool
) -> dict[str, Any]:
    """One meterValue entry: the energy register, and power while charging. 1.6J sends text, 2.0.1 numbers."""
    energy: dict[str, Any] = {"measurand": ENERGY}
    if context:
        energy["context"] = context
    sampled = [energy]
    if power_w is not None:
        sampled.append({"measurand": POWER})
    for s, value, unit in zip(sampled, (wh, power_w), ("Wh", "W"), strict=False):
        if v201:
            s["value"] = value
            s["unitOfMeasure"] = {"unit": unit}
        else:
            s["value"] = str(value)
            s["unit"] = unit
    return {"timestamp": now(), "sampledValue": sampled}


# --- messages, per version


class Messages16:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def boot(self) -> Any:
        s = self.settings
        return v16.call.BootNotification(
            charge_point_vendor=s.vendor, charge_point_model=s.model, firmware_version="sim-1.0"
        )

    def heartbeat(self) -> Any:
        return v16.call.Heartbeat()

    @staticmethod
    def status_name(
        c: Connector, *, charging: bool = False, waiting: bool = False, finished: bool = False
    ) -> str:
        if charging or c.transaction is not None:
            return "Charging"
        if c.plugged and finished:
            return "Finishing"  # the cable is still in after the transaction
        return "Preparing" if c.plugged or waiting or c.id_tag else "Available"

    def status(self, c: Connector) -> Any:
        return v16.call.StatusNotification(
            connector_id=c.no, error_code="NoError", status=c.status, timestamp=now()
        )

    def authorize(self, id_tag: str) -> Any:
        return v16.call.Authorize(id_tag=id_tag)

    @staticmethod
    def authorize_status(reply: Any) -> str:
        return str(reply.id_tag_info["status"])

    async def start(self, c: Connector, call: Any) -> tuple[int, str]:
        reply = await call(
            v16.call.StartTransaction(
                connector_id=c.no, id_tag=c.id_tag, meter_start=c.register_wh, timestamp=now()
            )
        )
        return reply.transaction_id, str(reply.id_tag_info["status"])

    def reading(self, c: Connector, *, in_transaction: bool, offline: bool) -> Any:
        return v16.call.MeterValues(
            connector_id=c.no,
            transaction_id=c.transaction if in_transaction else None,
            meter_value=[_reading(c.register_wh, power_w=c.power_w if in_transaction else None, v201=False)],
        )

    def stop(self, c: Connector, reason: str, *, offline: bool) -> Any:
        allowed = {"EVDisconnected", "Remote", "DeAuthorized", "PowerLoss"}
        return v16.call.StopTransaction(
            transaction_id=c.transaction,
            meter_stop=c.register_wh,
            timestamp=now(),
            reason=reason if reason in allowed else "Local",
            id_tag=c.id_tag,
        )


class Messages201:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def boot(self) -> Any:
        s = self.settings
        return v201.call.BootNotification(
            charging_station={"vendor_name": s.vendor, "model": s.model, "firmware_version": "sim-1.0"},
            reason="PowerUp",
        )

    def heartbeat(self) -> Any:
        return v201.call.Heartbeat()

    @staticmethod
    def status_name(
        c: Connector, *, charging: bool = False, waiting: bool = False, finished: bool = False
    ) -> str:
        # 2.0.1 reports only whether the connector is in use; charging shows in TransactionEvent.
        return "Occupied" if c.plugged or charging or c.transaction is not None else "Available"

    def status(self, c: Connector) -> Any:
        return v201.call.StatusNotification(
            timestamp=now(), connector_status=c.status, evse_id=c.no, connector_id=1
        )

    def authorize(self, id_tag: str) -> Any:
        return v201.call.Authorize(id_token={"id_token": id_tag, "type": "ISO14443"})

    @staticmethod
    def authorize_status(reply: Any) -> str:
        return str(reply.id_token_info["status"])

    @staticmethod
    def _token(c: Connector) -> dict[str, str]:
        return {"id_token": c.id_tag or "", "type": "Central" if c.remote_start_id else "ISO14443"}

    async def start(self, c: Connector, call: Any) -> tuple[str, str]:
        tx = str(uuid.uuid4())
        info: dict[str, Any] = {"transaction_id": tx, "charging_state": "Charging"}
        if c.remote_start_id is not None:
            info["remote_start_id"] = c.remote_start_id
        reply = await call(
            v201.call.TransactionEvent(
                event_type="Started",
                timestamp=now(),
                trigger_reason="RemoteStart" if c.remote_start_id else "Authorized",
                seq_no=next(c.seq),
                transaction_info=info,
                evse={"id": c.no, "connector_id": 1},
                id_token=self._token(c),
                meter_value=[_reading(c.register_wh, context="Transaction.Begin", v201=True)],
            )
        )
        return tx, str((reply.id_token_info or {}).get("status", "Accepted"))

    def reading(self, c: Connector, *, in_transaction: bool, offline: bool) -> Any:
        if not in_transaction or c.transaction is None:
            return v201.call.MeterValues(evse_id=c.no, meter_value=[_reading(c.register_wh, v201=True)])
        return v201.call.TransactionEvent(
            event_type="Updated",
            timestamp=now(),
            trigger_reason="MeterValuePeriodic",
            seq_no=next(c.seq),
            transaction_info={"transaction_id": str(c.transaction)},
            evse={"id": c.no, "connector_id": 1},
            meter_value=[_reading(c.register_wh, context="Sample.Periodic", power_w=c.power_w, v201=True)],
            offline=offline or None,
        )

    def stop(self, c: Connector, reason: str, *, offline: bool) -> Any:
        trigger = {
            "EVDisconnected": "EVCommunicationLost",
            "Remote": "RemoteStop",
            "DeAuthorized": "Deauthorized",
            "PowerLoss": "AbnormalCondition",
        }
        return v201.call.TransactionEvent(
            event_type="Ended",
            timestamp=now(),
            trigger_reason=trigger.get(reason, "StopAuthorized"),
            seq_no=next(c.seq),
            transaction_info={
                "transaction_id": str(c.transaction),
                "charging_state": "Idle",
                "stopped_reason": reason,
            },
            evse={"id": c.no, "connector_id": 1},
            meter_value=[_reading(c.register_wh, context="Transaction.End", v201=True)],
            offline=offline or None,
        )


# --- links: one connection each, answering the backend


class Link16(v16.ChargePoint):
    def __init__(self, cpid: str, ws: ClientConnection, charger: Charger) -> None:
        super().__init__(cpid, _LoggingConnection(ws, charger), response_timeout=30)
        self.charger = charger
        self.lost = asyncio.Event()

    @on(Action16.remote_start_transaction)
    async def on_remote_start(self, id_tag: str, connector_id: int | None = None, **kwargs: Any) -> Any:
        ok = self.charger.remote_start(id_tag, connector_id)
        return v16.call_result.RemoteStartTransaction(status="Accepted" if ok else "Rejected")

    @on(Action16.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id: int, **kwargs: Any) -> Any:
        ok = self.charger.remote_stop(transaction_id)
        return v16.call_result.RemoteStopTransaction(status="Accepted" if ok else "Rejected")

    @on(Action16.change_configuration)
    async def on_change_configuration(self, key: str, value: str, **kwargs: Any) -> Any:
        self.charger.config[key] = value
        return v16.call_result.ChangeConfiguration(status="Accepted")

    @on(Action16.get_configuration)
    async def on_get_configuration(self, key: list[str] | None = None, **kwargs: Any) -> Any:
        cfg = self.charger.config
        known = [{"key": k, "readonly": False, "value": v} for k, v in cfg.items() if not key or k in key]
        unknown = [k for k in key or [] if k not in cfg]
        return v16.call_result.GetConfiguration(configuration_key=known, unknown_key=unknown or None)

    @on(Action16.trigger_message)
    async def on_trigger_message(
        self, requested_message: str, connector_id: int | None = None, **kwargs: Any
    ) -> Any:
        if requested_message == "MeterValues":
            self.charger.triggered_readings(connector_id)
            return v16.call_result.TriggerMessage(status="Accepted")
        return v16.call_result.TriggerMessage(status="NotImplemented")

    @on(Action16.reset)
    async def on_reset(self, type: str, **kwargs: Any) -> Any:
        return v16.call_result.Reset(status="Accepted")  # accepted; the simulator does not reboot on its own


class Link201(v201.ChargePoint):
    def __init__(self, cpid: str, ws: ClientConnection, charger: Charger) -> None:
        super().__init__(cpid, _LoggingConnection(ws, charger), response_timeout=30)
        self.charger = charger
        self.lost = asyncio.Event()

    @on(Action201.request_start_transaction)
    async def on_request_start(
        self, id_token: dict[str, Any], remote_start_id: int, evse_id: int | None = None, **kwargs: Any
    ) -> Any:
        ok = self.charger.remote_start(id_token["id_token"], evse_id, remote_start_id)
        return v201.call_result.RequestStartTransaction(status="Accepted" if ok else "Rejected")

    @on(Action201.request_stop_transaction)
    async def on_request_stop(self, transaction_id: str, **kwargs: Any) -> Any:
        ok = self.charger.remote_stop(transaction_id)
        return v201.call_result.RequestStopTransaction(status="Accepted" if ok else "Rejected")

    @on(Action201.set_variables)
    async def on_set_variables(self, set_variable_data: list[dict[str, Any]], **kwargs: Any) -> Any:
        results = []
        for item in set_variable_data:
            self.charger.config[f"{item['component']['name']}.{item['variable']['name']}"] = item[
                "attribute_value"
            ]
            results.append(
                {"attribute_status": "Accepted", "component": item["component"], "variable": item["variable"]}
            )
        return v201.call_result.SetVariables(set_variable_result=results)

    @on(Action201.get_variables)
    async def on_get_variables(self, get_variable_data: list[dict[str, Any]], **kwargs: Any) -> Any:
        results = []
        for item in get_variable_data:
            key = f"{item['component']['name']}.{item['variable']['name']}"
            result = {"component": item["component"], "variable": item["variable"]}
            if key in self.charger.config:
                result.update(attribute_status="Accepted", attribute_value=self.charger.config[key])
            else:
                result["attribute_status"] = "UnknownVariable"
            results.append(result)
        return v201.call_result.GetVariables(get_variable_result=results)

    @on(Action201.trigger_message)
    async def on_trigger_message(
        self, requested_message: str, evse: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if requested_message == "MeterValues":
            self.charger.triggered_readings(evse.get("id") if evse else None)
            return v201.call_result.TriggerMessage(status="Accepted")
        return v201.call_result.TriggerMessage(status="NotImplemented")

    @on(Action201.reset)
    async def on_reset(self, type: str, **kwargs: Any) -> Any:
        return v201.call_result.Reset(status="Accepted")  # accepted; the simulator does not reboot on its own
