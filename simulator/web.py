"""The simulator's control page and its small JSON API. Dev tool: it binds to 127.0.0.1 and keeps chargers
(and any passwords typed in) in memory only."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from simulator.engine import Charger, Settings

PAGE = Path(__file__).with_name("page.html")


class ConnectIn(BaseModel):
    url: str = Field(min_length=5, pattern=r"^wss?://")
    charge_point_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    version: Literal["1.6", "2.0.1"] = "1.6"
    password: str | None = None
    connectors: int = Field(default=2, ge=1, le=8)
    power_w: int = Field(default=7400, gt=0, le=400_000)
    meter_interval_s: float = Field(default=5, ge=0.05, le=900)
    speed: float = Field(default=1, ge=1, le=3600)


class ActionIn(BaseModel):
    connector: int = 1
    id_tag: str | None = Field(default=None, max_length=36)


class OutageIn(BaseModel):
    seconds: float = Field(default=30, ge=0.1, le=3600)


def create_app() -> FastAPI:
    chargers: dict[str, Charger] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        for charger in chargers.values():
            await charger.disconnect()

    app = FastAPI(title="CCMS charger simulator", docs_url=None, redoc_url=None, lifespan=lifespan)

    def get(cpid: str) -> Charger:
        charger = chargers.get(cpid)
        if charger is None:
            raise HTTPException(404, "no such simulated charger")
        return charger

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return PAGE.read_text()

    @app.get("/api/chargers")
    async def list_chargers() -> list[dict[str, Any]]:
        return [c.snapshot() for c in chargers.values()]

    @app.post("/api/chargers", status_code=201)
    async def add(body: ConnectIn) -> dict[str, Any]:
        old = chargers.pop(body.charge_point_id, None)
        if old is not None:
            await old.disconnect()
        charger = Charger(Settings(**{**body.model_dump(), "password": body.password or None}))
        chargers[body.charge_point_id] = charger
        charger.start()
        return charger.snapshot()

    @app.delete("/api/chargers/{cpid}", status_code=204)
    async def remove(cpid: str) -> None:
        await get(cpid).disconnect()
        chargers.pop(cpid, None)

    @app.post("/api/chargers/{cpid}/reconnect")
    async def reconnect(cpid: str) -> dict[str, Any]:
        """A clean close and a new connection; running transactions carry on."""
        charger = get(cpid)
        await charger.reconnect()
        return charger.snapshot()

    @app.post("/api/chargers/{cpid}/outage/{kind}")
    async def outage(
        cpid: str, kind: Literal["network_drop", "power_cut_lost", "power_cut_saved"], body: OutageIn
    ) -> dict[str, Any]:
        charger = get(cpid)
        if kind == "network_drop":
            await charger.network_drop(body.seconds)
        else:
            await charger.power_cut(body.seconds, keep_transactions=kind == "power_cut_saved")
        return charger.snapshot()

    @app.post("/api/chargers/{cpid}/{action}")
    async def act(
        cpid: str, action: Literal["plug_in", "unplug", "tap", "stop"], body: ActionIn
    ) -> dict[str, Any]:
        charger = get(cpid)
        if body.connector not in charger.connectors:
            raise HTTPException(422, "no such connector")
        result: str | None = None
        try:
            if action == "plug_in":
                await charger.plug_in(body.connector)
            elif action == "unplug":
                await charger.unplug(body.connector)
            elif action == "tap":
                if not body.id_tag:
                    raise HTTPException(422, "id_tag is required")
                result = await charger.tap(body.connector, body.id_tag)
            else:
                await charger.stop(body.connector)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"result": result, "charger": charger.snapshot()}

    @app.get("/api/chargers/{cpid}/frames")
    async def frames(cpid: str, after: Annotated[int, Query(ge=0)] = 0) -> list[dict[str, Any]]:
        return [f.__dict__ for f in get(cpid).frames if f.seq > after]

    return app
