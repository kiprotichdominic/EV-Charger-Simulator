# OCPP versions and how the simulator works

This document has two parts:

1. **[How OCPP 1.6J, 2.0.1 and 2.1 differ](#part-1-how-the-ocpp-versions-differ)**: what changes between the
   versions, and how each difference shows up on the wire.
2. **[How the simulator works](#part-2-how-the-simulator-works)**: its structure, what it sends in each
   version, and how it behaves when the connection or the power fails.

The [README](README.md) covers installing, running and using the page. This document explains why the simulator
behaves the way it does.

---

## Part 1: How the OCPP versions differ

### At a glance

| | OCPP 1.6J | OCPP 2.0.1 | OCPP 2.1 |
| --- | --- | --- | --- |
| Released | 2015 (security extension 2020) | 2020 | 2025 |
| WebSocket subprotocol | `ocpp1.6` | `ocpp2.0.1` | `ocpp2.1` |
| Messages (in the `ocpp` library) | 39 | 64 | 90 |
| Where charging happens | `connectorId` | EVSE, then connector within it | Same as 2.0.1 |
| Transactions | `StartTransaction` / `StopTransaction` | One `TransactionEvent` (Started, Updated, Ended) | Same as 2.0.1 |
| Transaction ID assigned by | Backend (integer) | Charger (string) | Charger (string) |
| Configuration | Flat key/value list | Device model (components and variables) | Same as 2.0.1 |
| Card or token | `idTag`: string, max 20 chars | `idToken`: object with value (max 36) and type (fixed list) | `idToken`: value max 255, type is free text (max 20) |
| Meter values | Strings (`"1234"`) | Numbers (`1234`) | Numbers |
| Connector statuses | 9 (Preparing, Charging, Finishing…) | 5 (Available, Occupied…) | Same as 2.0.1 |
| Security | Optional extension | Built in (security profiles 1 to 3) | Same as 2.0.1, plus newer certificate handling |
| Backward compatible with the previous version | n/a | No: a redesign | Yes, mostly: 2.0.1 plus additions |

The key point: **going from 1.6 to 2.0.1 is a rewrite, while 2.1 is 2.0.1 with additions.** Nothing was
removed in 2.1. Every 2.0.1 message still exists, and every value the simulator sends is still valid.

### Connection and message framing

All three versions use JSON over a WebSocket. The charger connects to the backend's URL with its charge point
ID at the end (for example `ws://host/ocpp/SIM-001`) and names the version as the WebSocket subprotocol. A
backend that does not support that version refuses the handshake.

Messages use the same frame shapes in every version:

```text
[2, "<message id>", "<Action>", {payload}]      Call (a request)
[3, "<message id>", {payload}]                  CallResult (the answer)
[4, "<message id>", "<errorCode>", "<description>", {details}]   CallError
```

2.1 also adds unconfirmed message types (for example the `SEND` type used by periodic event streams) that need
no answer. The simulator does not use them.

**Field naming.** On the wire, every version uses camelCase (`connectorId`, `idToken`). The Python `ocpp`
library uses snake_case (`connector_id`) and converts between the two.

**Security.** In 1.6, security (HTTP Basic auth, TLS, certificates) was bolted on later as an optional
"security extension". From 2.0.1 it is part of the standard, as three security profiles:

| Profile | Transport | Charger authenticates with |
| --- | --- | --- |
| 1 | `ws://` (no TLS) | HTTP Basic auth |
| 2 | `wss://` (TLS) | HTTP Basic auth |
| 3 | `wss://` (TLS) | Client certificate |

The simulator supports profile 1, and profile 2 if you give a `wss://` URL. It does not support client
certificates (profile 3).

### Booting and heartbeats

| | 1.6J | 2.0.1 and 2.1 |
| --- | --- | --- |
| Boot message | `BootNotification` with flat fields: `chargePointVendor`, `chargePointModel`, `firmwareVersion` | `BootNotification` with a nested `chargingStation` object (`vendorName`, `model`, `firmwareVersion`) and a required `reason` (`PowerUp`, `RemoteReset`, …) |
| Reply | `status` (Accepted, Pending, Rejected) and `interval` | Same |
| Heartbeat | `Heartbeat`, no fields | Same |

The `interval` in the boot reply is the heartbeat interval in seconds. The simulator uses it as-is.

### Where charging happens: connectors vs EVSEs

- **1.6J** numbers connectors `1..n`. Connector `0` means "the whole charger".
- **2.0.1 and 2.1** add a level: a charger has **EVSEs** (each can charge one car at a time), and each EVSE
  has one or more **connectors** (for example a Type 2 socket and a CCS cable on the same EVSE). Messages name
  both: `evseId` and `connectorId`, or an `evse: {id, connectorId}` object.

The simulator gives each of its connectors its own EVSE with one connector. "Connector 2" in the simulator is
therefore `connectorId: 2` in 1.6, and `evseId: 2, connectorId: 1` in 2.x.

### Connector status

In 1.6, `StatusNotification` tells you what is happening at the connector. In 2.x it only says whether the
connector is in use; the charging state moves into `TransactionEvent`.

| Situation | 1.6J `status` | 2.x `connectorStatus` | 2.x `chargingState` (in TransactionEvent) |
| --- | --- | --- | --- |
| Free, nothing plugged | `Available` | `Available` | none |
| Cable in or card tapped, not charging yet | `Preparing` | `Occupied` (cable in) | none yet |
| Charging | `Charging` | `Occupied` | `Charging` |
| Car paused charging | `SuspendedEV` | `Occupied` | `SuspendedEV` |
| Charger paused charging | `SuspendedEVSE` | `Occupied` | `SuspendedEVSE` |
| Transaction over, cable still in | `Finishing` | `Occupied` | `Idle` (when it ends) |
| Reserved / out of service / broken | `Reserved` / `Unavailable` / `Faulted` | Same names | |

1.6 also sends an `errorCode` with every status (`NoError` when all is well). 2.x reports errors through
separate messages (`NotifyEvent`).

### Cards and tokens (authorization)

| | 1.6J | 2.0.1 | 2.1 |
| --- | --- | --- | --- |
| Field | `idTag` (a string) | `idToken: {idToken, type}` | `idToken: {idToken, type}` |
| Max length | 20 | 36 | 255 |
| Token type | Not sent | Fixed list: `ISO14443` (RFID card), `Central` (backend or app), `eMAID`, `KeyCode`, `MacAddress`, `NoAuthorization`, … | Free text, max 20 characters (the 2.0.1 values still work) |
| Authorize reply | `idTagInfo.status` | `idTokenInfo.status` | Same as 2.0.1 |

In 2.x the token type tells the backend how the driver identified themselves. The simulator sends `ISO14443`
for a card tap and `Central` for a remote start from the app.

### Transactions: the biggest change

**1.6J** uses separate messages, and the **backend** names the transaction:

```text
Charger                                   Backend
  StartTransaction {connectorId, idTag, meterStart, timestamp}  ─►
  ◄─  {transactionId: 42, idTagInfo: {status: Accepted}}
  MeterValues {connectorId, transactionId: 42, meterValue}      ─►   (repeated)
  StopTransaction {transactionId: 42, meterStop, reason}        ─►
```

The charger cannot report anything about a transaction until the backend has replied with its ID.

**2.0.1 and 2.1** use a single `TransactionEvent` message with an `eventType`, and the **charger** names the
transaction (any string up to 36 characters; the simulator uses a UUID):

```text
Charger                                   Backend
  TransactionEvent {eventType: Started, transactionInfo: {transactionId: "a1b2…"},
                    triggerReason, seqNo: 0, idToken, evse, meterValue}   ─►
  ◄─  {idTokenInfo: {status: Accepted}}
  TransactionEvent {eventType: Updated, seqNo: 1, …}             ─►   (repeated)
  TransactionEvent {eventType: Ended, seqNo: n,
                    transactionInfo: {stoppedReason}, …}        ─►
```

What else changes in 2.x:

- **`seqNo`** numbers each event of a transaction from 0, so the backend can spot gaps and put events in
  order, even when they arrive late after an outage.
- **`triggerReason`** says *why* each event was sent (`Authorized`, `RemoteStart`, `MeterValuePeriodic`,
  `RemoteStop`, …). 1.6 has nothing like it.
- **`offline: true`** marks an event that happened while the charger had no connection.
- **When a transaction starts is configurable** in 2.x (when the cable goes in, when the card is accepted, or
  both). In 1.6 it is always a `StartTransaction`. The simulator always starts once it has both a cable and an
  accepted token.

### Stop reasons

| Reason | 1.6J | 2.0.1 | 2.1 |
| --- | --- | --- | --- |
| `Local`, `Remote`, `EVDisconnected`, `PowerLoss`, `DeAuthorized`, `EmergencyStop`, `HardReset`, `SoftReset`, `Reboot`, `UnlockCommand`, `Other` | Yes | Yes (`HardReset`/`SoftReset` become `ImmediateReset`) | Yes |
| `EnergyLimitReached`, `SOCLimitReached`, `TimeLimitReached`, `StoppedByEV`, `GroundFault`, `OvercurrentFault`, `PowerQuality`, `LocalOutOfCredit`, `MasterPass`, `Timeout` | No | Yes | Yes |
| `ReqEnergyTransferRejected` | No | No | Yes |

In 1.6 the reason goes in `StopTransaction.reason`. In 2.x it goes in `transactionInfo.stoppedReason` on the
`Ended` event, alongside a `triggerReason`.

### Meter values

| | 1.6J | 2.x |
| --- | --- | --- |
| Value | String: `"value": "1234"` | Number: `"value": 1234` |
| Unit | `"unit": "Wh"` | `"unitOfMeasure": {"unit": "Wh"}` |
| During a transaction | `MeterValues` with `transactionId` | Inside `TransactionEvent` (`Updated`) |
| Outside a transaction | `MeterValues` without `transactionId` | `MeterValues` with `evseId` |

Both use the same measurand names (`Energy.Active.Import.Register`, `Power.Active.Import`, …) and the same
`context` values (`Transaction.Begin`, `Sample.Periodic`, `Transaction.End`, `Trigger`).

### Remote control from the backend

| What the backend wants | 1.6J | 2.0.1 and 2.1 |
| --- | --- | --- |
| Start a session (for example from the app) | `RemoteStartTransaction {idTag, connectorId?}` | `RequestStartTransaction {idToken, remoteStartId, evseId?}` |
| Stop a session | `RemoteStopTransaction {transactionId: int}` | `RequestStopTransaction {transactionId: string}` |
| Ask for a message now | `TriggerMessage {requestedMessage, connectorId?}` | `TriggerMessage {requestedMessage, evse?}` (2.1 adds `CustomTrigger`) |
| Restart | `Reset {type: Hard or Soft}` | `Reset {type: Immediate or OnIdle, evseId?}` |

In 2.x the backend picks a `remoteStartId`, and the charger repeats it in the `Started` event. That is how the
backend matches a transaction to the start request that caused it. 1.6 has no such link, so backends have to
match by connector and `idTag`.

### Configuration: keys vs the device model

**1.6J** has a flat list of named settings:

```text
ChangeConfiguration {key: "HeartbeatInterval", value: "60"}
GetConfiguration {key: ["HeartbeatInterval"]}  →  configurationKey: [{key, value, readonly}], unknownKey: [...]
```

**2.x** replaces it with a **device model**: the charger is described as *components* (for example
`OCPPCommCtrlr`, `EVSE 1`, `Connector 1`), each with *variables*. Settings are read and written in batches:

```text
SetVariables {setVariableData: [{component: {name: "OCPPCommCtrlr"}, variable: {name: "HeartbeatInterval"}, attributeValue: "60"}]}
GetVariables {getVariableData: [{component, variable}]}  →  getVariableResult: [{attributeStatus, attributeValue, ...}]
```

The device model also covers reports (`GetBaseReport`), monitoring (`SetVariableMonitoring`) and events
(`NotifyEvent`). The simulator only stores and returns values.

### Smart charging, security and the rest of 2.0.1

Beyond the changes above, 2.0.1 added features that are outside the simulator's scope:

- **Smart charging** reworked: charging profiles per EVSE, external limits (`NotifyChargingLimit`), and the
  car sending its own charging needs over ISO 15118 (`NotifyEVChargingNeeds`).
- **ISO 15118 Plug & Charge**: the car identifies itself through a certificate, with no card or app.
- **Certificate management**: `SignCertificate`, `CertificateSigned`, `InstallCertificate`, and so on.
- **Display messages**: the backend can show text on the charger's screen.
- **Local authorization lists** and an authorization cache with clearer rules.

### What 2.1 adds on top of 2.0.1

2.1 adds 26 messages and removes none. Grouped by purpose:

| Area | New messages | What it is for |
| --- | --- | --- |
| Bidirectional charging (V2X) | `NotifyAllowedEnergyTransfer`, plus discharge settings in charging profiles | Sending power from the car back to the grid or a building (ISO 15118-20) |
| Grid control (DER) | `SetDERControl`, `GetDERControl`, `ClearDERControl`, `ReportDERControl`, `NotifyDERAlarm`, `NotifyDERStartStop` | Following grid-code rules for voltage and frequency, curtailment |
| Grid services | `AFRRSignal`, `OpenPeriodicEventStream`, `AdjustPeriodicEventStream`, `ClosePeriodicEventStream`, `GetPeriodicEventStream` | Fast grid-balancing signals and high-frequency data streams |
| Tariffs and cost | `SetDefaultTariff`, `GetTariffs`, `ClearTariffs`, `ChangeTransactionTariff` | The charger calculates and shows the running cost itself; new trigger reasons `RunningCost`, `TariffChanged`, `CostLimitReached` |
| Payment | `NotifyWebPaymentStarted`, `NotifySettlement`, `VatNumberValidation` | Card terminals at the charger, dynamic QR codes that start a web payment, invoicing |
| Smart charging | `UpdateDynamicSchedule`, `PullDynamicScheduleUpdate`, `UsePriorityCharging`, `NotifyPriorityCharging` | Limits that change during a session; "charge now at full power" requests |
| Battery swapping | `BatterySwap`, `RequestBatterySwap` | Battery swap stations |
| Security | `GetCertificateChainStatus` | Checking certificate chains |

Existing messages also gain optional fields. For example `TransactionEvent` can carry `costDetails`,
`preconditioningStatus` and `numberOfPhasesUsed`, and there are new trigger reasons (`TxResumed`,
`SoCLimitReached`, `LimitSet`, `OperationModeChanged`, …).

**Upgrading a backend from 2.0.1 to 2.1** mostly means: accept the `ocpp2.1` subprotocol, validate against the
2.1 schemas (longer `idToken` values, free-text token types), and ignore or handle the new optional fields.
The new features can then be added one at a time.

---

## Part 2: How the simulator works

### What it is for

The simulator stands in for a real charger when testing the CCMS backend and app. It does what a person at a
charger does (plug in, tap a card, stop, unplug), answers the backend's requests, and fails in the ways real
chargers fail (network drops, power cuts). It shows every frame on the wire as it goes.

Every message is built and validated by the [`ocpp`](https://pypi.org/project/ocpp/) library, the same one
the backend uses. If the simulator tried to send a field the schema does not allow, the library would raise an
error before it reached the wire. So whatever the backend receives is valid OCPP.

### The files

```text
simulator/
  __main__.py   entry point: parses --port and starts uvicorn
  web.py        FastAPI app: serves page.html and the JSON API, holds chargers in memory
  engine.py     the charger itself
  page.html     the control page (plain HTML and JS that polls the API)
```

### The three layers in `engine.py`

```text
            web.py (API)  ──►  Charger  ──── outlives any connection
                                  │  connectors, cable, card reader, meter,
                                  │  running transactions, outbox
                                  │
                   ┌──────────────┴──────────────┐
                   ▼                             ▼
     Messages16 / Messages201 / Messages21   Link16 / Link201 / Link21
     build payloads for one version          one WebSocket connection:
                                             answers the backend, carries calls
```

- **`Charger`** is the hardware: what a person sees and does, plus the running transactions and an outbox of
  messages waiting for a connection. It lives until the charger is removed, through any number of connections.
- **`Messages16`, `Messages201`, `Messages21`** turn a charger event ("connector 1 started charging") into
  the payload for one version. Everything version-specific in what the charger *sends* lives here.
- **`Link16`, `Link201`, `Link21`** are one WebSocket connection each. They subclass the library's
  `ChargePoint`, answer what the backend asks (remote start, set variables, …) and carry the charger's calls.
  A new link is created for each connection.

The `Charger` never checks the version itself. It looks up the right classes once:

```python
MESSAGES = {"1.6": Messages16, "2.0.1": Messages201, "2.1": Messages21}
LINKS = {"1.6": Link16, "2.0.1": Link201, "2.1": Link21}
```

**How 2.1 is built.** 2.1 keeps 2.0.1's model for everything the simulator does, so the 2.1 classes are the
2.0.1 ones pointed at the 2.1 schemas:

- `Messages201` builds through a class attribute (`self.ocpp.call.TransactionEvent(...)`), and `Messages21`
  only sets `ocpp = v21`.
- `Link201`'s handlers answer through `self._call_result`, and `Link21` only changes the three class
  attributes that the library's own `v21.ChargePoint` sets (`_call`, `_call_result`, `_ocpp_version`). Those
  decide which schemas each message is validated against.

### Connecting and staying connected

`Charger.start()` launches a **supervisor** task that keeps the charger online until it is removed:

```text
          ┌──────────────► connecting ──── handshake refused (401, 400…) ──► failed (stops trying)
          │                    │
          │                    ▼
          │               connected ──── come online (below)
          │                    │
          │              link drops
          │                    ▼
          └─── wait (backoff, or the outage length) ◄── offline
```

- **Backoff**: after an unplanned drop the charger waits 1 s, then 2, 4, … up to 30 s, and resets to 1 s once
  a connection succeeds.
- **Planned outages** (network drop, power cut) set a time before which the charger stays offline. After it,
  the charger reconnects at once.
- **A refused handshake** (wrong password, unknown charger, unsupported version) marks the charger `failed`.
  Retrying would only repeat the same refusal.
- Basic auth is sent as `Authorization: Basic base64("<charge point ID>:<password>")` when a password is set.

**Coming online** (`_come_online`) happens on every new connection, in this order:

1. `BootNotification`, **only if the charger has powered up since its last boot**. A network drop is not a
   reboot, so after one the charger does not boot again. The heartbeat interval comes from the boot reply.
2. Send the outbox, oldest first.
3. End any transaction saved through a power cut, with reason `PowerLoss`.
4. Send a `StatusNotification` for every connector.
5. Start heartbeats.
6. Start any transaction that was only waiting for the connection (a cable and an accepted token).

### Three ways to send a message

The charger sends each message in one of three ways, depending on what should happen when there is no
connection:

| Method | Used for | When offline |
| --- | --- | --- |
| `_call` | `Authorize`, starting a transaction: anything that needs the backend's answer right now | Fails: the action is refused |
| `_deliver` | Meter readings, stops: anything the backend must eventually receive | Queued in the outbox |
| `_status` | `StatusNotification` | Skipped: the next connection reports every connector's current status anyway |

`_deliver` also queues a message when it is already waiting behind others in the outbox, so messages always
arrive in order. If the link dies mid-send, the message is queued and sent again; it may arrive twice, and
the backend is expected to treat a repeat as the same message.

The outbox holds up to 5000 messages; when it is full the oldest are dropped first, like a real charger's
finite storage.

Every call goes through `_ask`, which gives up as soon as its link dies. Without that, a call in flight when
the network drops would wait out the full 30-second response timeout on a socket nobody reads.

### A charging session

A transaction starts when a connector has **both a cable and an accepted token**, in either order:

```text
  tap card ──► Authorize ──► Accepted ─┐
                                       ├──► start transaction ──► charge ──► stop
  remote start (from the backend) ─────┤
                                       │
  plug in ─────────────────────────────┘
```

- Tapping the card that is already charging stops the session (reason `Local`), as on real chargers.
- Starting always needs the backend; the simulator never starts a session offline.
- If the backend refuses the start (for example the card is blocked), the charger ends it at once with reason
  `DeAuthorized`. This matches the `StopTransactionOnInvalidId` (1.6) and `StopTxOnInvalidId` (2.x) settings.

**The meter** is a cumulative energy register per connector, in Wh, that never goes down, like a real meter.
While charging, it adds `power × elapsed time × speed` at every reading interval. Energy below 1 Wh is carried
over to the next reading, so nothing is lost to rounding. With speed `60`, an hour's energy is charged in a
minute.

**How the session ends** decides its stop reason:

| What happened | Reason | 1.6J sends | 2.x sends |
| --- | --- | --- | --- |
| Stop pressed, or same card tapped again | `Local` | `StopTransaction reason=Local` | `Ended`, `triggerReason=StopAuthorized` |
| Remote stop from the backend | `Remote` | `reason=Remote` | `triggerReason=RemoteStop` |
| Unplugged while charging | `EVDisconnected` | `reason=EVDisconnected` | `triggerReason=EVCommunicationLost` |
| Backend refused the start | `DeAuthorized` | `reason=DeAuthorized` | `triggerReason=Deauthorized` |
| Power cut, transaction saved | `PowerLoss` | `reason=PowerLoss` | `triggerReason=AbnormalCondition` |

### What the simulator sends, per version

The same session, as each version puts it on the wire:

| Event | 1.6J | 2.0.1 and 2.1 |
| --- | --- | --- |
| Power-up | `BootNotification` (vendor `CCMS`, model `Simulator`, firmware `sim-1.0`) | `BootNotification` with `reason: PowerUp` |
| Cable in / card tapped | `StatusNotification Preparing` | `StatusNotification Occupied` (cable in) |
| Card tapped | `Authorize {idTag}` | `Authorize {idToken, type: ISO14443}` |
| Session starts | `StartTransaction`; the backend replies with the integer ID | `TransactionEvent Started`, UUID transaction ID, `seqNo 0`, `triggerReason Authorized` (or `RemoteStart` with `remoteStartId` and token type `Central`), reading with context `Transaction.Begin` |
| Charging | `StatusNotification Charging` | (status stays `Occupied`; `chargingState: Charging`) |
| Every reading interval | `MeterValues` with `transactionId`, energy and power as strings | `TransactionEvent Updated`, `triggerReason MeterValuePeriodic`, context `Sample.Periodic`, energy and power as numbers |
| Session ends | `StopTransaction {meterStop, reason}` | `TransactionEvent Ended`, `chargingState Idle`, `stoppedReason`, context `Transaction.End` |
| After the session, cable still in | `StatusNotification Finishing` | `StatusNotification Occupied` |
| Unplugged | `StatusNotification Available` | `StatusNotification Available` |
| Happened while offline | (no marker in 1.6) | `offline: true` on each `TransactionEvent` |

### What the simulator answers, per version

| 1.6J | 2.0.1 and 2.1 | Behaviour |
| --- | --- | --- |
| `RemoteStartTransaction` | `RequestStartTransaction` | Accepted unless the connector already has a session. With no connector given, picks a free one. Charging starts once the cable is in. |
| `RemoteStopTransaction` | `RequestStopTransaction` | Accepted if the transaction is running; stops it with reason `Remote`. |
| `ChangeConfiguration` / `GetConfiguration` | `SetVariables` / `GetVariables` | Stores and returns values in memory. In 2.x, values are stored under `Component.Variable`. Unknown keys are reported as unknown. Setting `HeartbeatInterval` does not yet change the heartbeat. |
| `TriggerMessage` (MeterValues) | `TriggerMessage` (MeterValues) | Sends a reading now (outside the transaction: `MeterValues` with no transaction ID). Other triggers return `NotImplemented`. |
| `Reset` | `Reset` | Accepted, but the simulator does not reboot. Use a power cut instead. |

Any other request the backend sends gets a `NotImplemented` CallError from the library.

### Choosing 2.1 on the page

2.1 brings no new controls to the page. The only change is a third **2.1** button in the OCPP version
selector. Every connector control, failure button and backend request works exactly as it does in 2.0.1.

The messages 2.1 adds (see [What 2.1 adds on top of 2.0.1](#what-21-adds-on-top-of-201)) are not handled
yet. If the backend sends one, for example `SetDefaultTariff`, `UsePriorityCharging` or `SetDERControl`, the
charger answers with a `NotImplemented` CallError, the same as for any other unsupported request. Its own
`TransactionEvent`s never carry the new 2.1 fields (`costDetails`, `numberOfPhasesUsed`, …) or the new
trigger reasons (`RunningCost`, `TariffChanged`, …).

So choosing 2.1 tests two things:

- the backend accepts the `ocpp2.1` subprotocol
- the backend accepts 2.0.1-style messages checked against the 2.1 schemas

The easiest 2.1 features to add first are tariffs (`SetDefaultTariff`, then `TransactionEvent`s with
`costDetails` and trigger reason `RunningCost`) and priority charging (`UsePriorityCharging`). Both belong on
`Link21`; see [Adding to the simulator](#adding-to-the-simulator).

### Breaking the connection

These simulate the failures a backend has to survive.

| Action | The link | Charging | Outbox | On reconnect |
| --- | --- | --- | --- | --- |
| **Reconnect** | Closed cleanly | Carries on | Kept | No boot; sends the outbox and statuses |
| **Network drop** (for N s) | Dies with no close frame, as when a cable is cut | Carries on; readings and stops are queued | Kept and grows | No boot (it never rebooted); sends the outbox in order; 2.x events marked `offline: true` |
| **Backend drops the link** | Closed by the backend | Carries on | Kept | Reconnects with backoff, then as after a network drop |
| **Power cut, transaction lost** | Dies at once, before anything else is sent | Stops | Lost | Boots again; knows nothing of its old sessions. The backend's power-loss recovery must close them |
| **Power cut, transaction saved** | Dies at once | Stops | Kept, as if in flash | Boots, sends the outbox, then ends each saved session with `PowerLoss` and its last reading |

### The web layer and the page

`web.py` is a small FastAPI app on `127.0.0.1` that keeps chargers in memory, keyed by charge point ID. Adding
a charger with an existing ID replaces it. The page is a thin client: it polls the API for charger state and
for new frames (`/frames?after=N`). Every frame is recorded as it goes in or out of the socket, keeping the
last 500 per charger. The full list of endpoints is in the [README](README.md#http-api).

### Adding to the simulator

- **Supporting a new message from the backend**: add an `@on(Action.…)` handler to the link class. For 2.x,
  put it on `Link201` and answer through `self._call_result`, and 2.1 inherits it. If the message only exists
  in 2.1 (for example `SetDefaultTariff`), put it on `Link21`.
- **Sending something new**: add a method to the `Messages` classes that builds the payload, and call it from
  `Charger` through `_call`, `_deliver` or `_status`, depending on what should happen when offline.
- **Adding a version**: add `Messages` and `Link` classes for it, then add it to `VERSIONS`, `MESSAGES`,
  `LINKS`, the `version` field in `web.py`, and the version buttons in `page.html`.

### Known limits

- **2.1 is 2.0.1 behaviour on 2.1 schemas.** None of 2.1's new features (tariffs and cost, payments,
  bidirectional charging, DER, periodic streams, battery swap) are simulated yet. Choosing 2.1 tests that the
  backend accepts the `ocpp2.1` subprotocol and 2.1 messages.
- **Card IDs longer than 20 characters fail in 1.6.** The API accepts up to 36 characters (the 2.0.1 limit),
  but 1.6 allows only 20, so the library rejects longer ones before they are sent.
- Each connector is its own EVSE with one connector; multi-connector EVSEs are not modelled.
- No reservations, availability changes (`ChangeAvailability`), firmware updates, diagnostics, local
  authorization lists, smart charging profiles or certificates.
- No suspended states (`SuspendedEV`, `SuspendedEVSE`): power is either full or zero.
- No security profile 3 (client certificates).
- Configuration values are stored but do not change behaviour (for example `HeartbeatInterval`).
