# Charger simulator

A simulated EV charger for manual testing of an OCPP backend (CSMS). It speaks **OCPP 1.6J** or **OCPP 2.0.1**,
runs as a small local web app, and lets you plug in, tap cards, charge, and break the connection in the ways a
real charger does, while showing every OCPP frame on the wire.

Every message is built and validated by the [`ocpp`](https://pypi.org/project/ocpp/) library, so what the
simulator sends always matches the OCPP schema.

## Requirements

- Python 3.12+
- The packages in `requirements.txt`

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run it

From this folder (the one that contains `simulator/`):

```bash
python -m simulator              # http://127.0.0.1:8001
python -m simulator --port 9000  # pick another port
```

Open the printed URL in a browser. The app binds to `127.0.0.1` only and keeps everything in memory: chargers
and any passwords you type disappear when you stop it.

## Using the page

### 1. Connect a charger

Fill in the form and press **Connect**:

| Field | Meaning |
| --- | --- |
| Backend URL | WebSocket base of the backend, e.g. `ws://localhost:8000/ocpp`. The charge point ID is appended, so the charger connects to `ws://localhost:8000/ocpp/SIM-001`. |
| Charge point ID | The charger's identity, e.g. `SIM-001`. Letters, digits and `_ . : -`. It must be registered in the backend. |
| OCPP version | `1.6J` or `2.0.1`. Sent as the WebSocket subprotocol (`ocpp1.6` / `ocpp2.0.1`). |
| Password | Optional. If set, sent as HTTP Basic auth (`<charge point ID>:<password>`). |
| Connectors | 1 to 8 connectors (EVSEs in 2.0.1). |
| Power (W) | Charging power per connector, default 7400 W. |
| Reading every (s) | How often MeterValues are sent while charging. |
| Energy speed | Fast-forward for energy. `60` charges an hour's worth of energy in one minute. |

On connect the charger sends BootNotification, StatusNotification for each connector, and then Heartbeats at
the interval the backend returns. You can add several chargers; pick one from the list to control it.

### 2. Charge

Each connector has these controls:

- **Plug in** / **Unplug**: the cable.
- **Tap**: present a card (id tag), e.g. `RFID0001`. The charger sends Authorize and shows the backend's answer.
- **Stop charging**: the driver stops at the charger (reason `Local`).

A transaction starts when a connector has both a cable and an accepted token, in either order:

- Tap then plug in, or plug in then tap.
- A remote start from the backend or app (RemoteStartTransaction in 1.6, RequestStartTransaction in 2.0.1)
  counts as the accepted token.

Starting always needs the backend; the simulator never starts a transaction offline.

While charging, the meter is a cumulative energy register per connector (Wh). It never goes down, and every
reading carries a real timestamp. How a transaction ends decides its stop reason:

| What happened | Stop reason | Cable |
| --- | --- | --- |
| Stop charging pressed | `Local` | stays in |
| Remote stop from the backend | `Remote` | stays in |
| Unplugged while charging | `EVDisconnected` | out |
| Power cut, transaction saved | `PowerLoss` | stays in |

### 3. Break the connection

These buttons simulate the failures a backend must survive. Set the outage length in seconds first.

| Button | What the charger does |
| --- | --- |
| **Reconnect** | Closes the connection cleanly and opens a new one. Nothing is lost. |
| **Network drop** | The connection dies but charging continues. Readings and stops queue in an outbox. On reconnect it does **not** send BootNotification (it never rebooted) and sends the outbox in order. In 2.0.1, events that happened offline are marked `offline=true`. |
| **Power cut (transaction lost)** | Charging stops, the outbox is lost, and after the reboot the charger knows nothing of its transactions. The backend's power-loss recovery has to close them. |
| **Power cut (transaction saved)** | The charger kept its transactions and outbox, as if in flash. After the reboot it sends the outbox, then ends each saved transaction with reason `PowerLoss` and its final reading. |

If the backend itself drops the connection (restart, idle timeout), the charger reconnects with backoff
(1 s, doubling up to 30 s) and carries on as after a network drop. If the backend can't be reached at all, it
keeps retrying. If the backend rejects the handshake (for example `401` for a wrong ID or password, or `400` for
a refused subprotocol), the charger is marked **failed** and stops retrying.

### 4. Watch the frames

The right-hand panel logs every frame sent (out) and received (in), up to the last 500 per charger. Filter by
text (e.g. `TransactionEvent`), show only **Requests** or **Errors**, **Pause** the live view, **Clear** it, or
**Copy** a single frame.

## Messages the charger answers

| OCPP 1.6J | OCPP 2.0.1 | Behaviour |
| --- | --- | --- |
| RemoteStartTransaction | RequestStartTransaction | Accepts unless the connector already has a transaction (with no connector given, picks a free one). Starts once a cable is in. |
| RemoteStopTransaction | RequestStopTransaction | Accepts if the transaction is running; stops with `Remote`. |
| ChangeConfiguration / GetConfiguration | SetVariables / GetVariables | Stores and returns values in memory. Unknown keys are reported as unknown. |
| TriggerMessage (MeterValues) | TriggerMessage (MeterValues) | Sends readings now. Other triggers return `NotImplemented`. |
| Reset | Reset | Accepted, but the simulator does not reboot. Use a power cut button for that. |

In 1.6 the backend assigns the integer transaction ID in its StartTransaction reply. In 2.0.1 the charger
names its own transactions and numbers events with `seqNo`.

## HTTP API

The page is a thin client over a JSON API, which you can also drive from scripts or tests. `{cpid}` is the
charge point ID.

| Method and path | Body | Purpose |
| --- | --- | --- |
| `GET /api/chargers` | | List chargers and their state. |
| `POST /api/chargers` | `{"url", "charge_point_id", "version", "password", "connectors", "power_w", "meter_interval_s", "speed"}` | Add and connect a charger. Replaces one with the same ID. |
| `DELETE /api/chargers/{cpid}` | | Disconnect and remove. |
| `POST /api/chargers/{cpid}/reconnect` | | Clean reconnect. |
| `POST /api/chargers/{cpid}/outage/{kind}` | `{"seconds": 30}` | `kind` is `network_drop`, `power_cut_lost` or `power_cut_saved`. |
| `POST /api/chargers/{cpid}/{action}` | `{"connector": 1, "id_tag": "RFID0001"}` | `action` is `plug_in`, `unplug`, `tap` (needs `id_tag`) or `stop`. |
| `GET /api/chargers/{cpid}/frames?after=N` | | Frames with sequence number greater than `N`. |

Example: connect a 1.6J charger and start a session.

```bash
curl -X POST localhost:8001/api/chargers -H 'content-type: application/json' \
  -d '{"url":"ws://localhost:8000/ocpp","charge_point_id":"SIM-001","version":"1.6","speed":60}'
curl -X POST localhost:8001/api/chargers/SIM-001/plug_in -H 'content-type: application/json' -d '{"connector":1}'
curl -X POST localhost:8001/api/chargers/SIM-001/tap -H 'content-type: application/json' \
  -d '{"connector":1,"id_tag":"RFID0001"}'
```

Actions that conflict with the charger's state (for example tapping while offline) return `409` with the
reason.

## How it is built

```
simulator/
  __main__.py   entry point: parses --port and starts uvicorn
  web.py        FastAPI app: serves page.html and the JSON API, holds chargers in memory
  engine.py     the charger itself
  page.html     the control page (plain HTML/JS, polls the API)
```

`engine.py` has three layers:

- **`Charger`**: what a person at the charger sees and does: connectors, cable, card reader, meter, running
  transactions, and an outbox of messages waiting for a connection. It outlives any single connection, as real
  hardware does.
- **`Messages16` / `Messages201`**: build the OCPP payloads for one version.
- **`Link16` / `Link201`**: one WebSocket connection. They answer the backend's requests and carry the
  charger's calls. A supervisor opens a new link whenever one drops, until the charger is removed.

The outbox holds up to 5000 messages; when it is full the oldest readings are dropped first, as on a real
charger with finite storage.
