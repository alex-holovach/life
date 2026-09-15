"""Authenticated batch ingestion; ACK means durable backend storage."""
import base64
import hmac
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
import storage
import temperature
import hrv
import vitals
import respiration
import wellness
import analysis_quality
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128)
    deviceId: str = Field(min_length=1, max_length=128)
    sessionId: str = Field(min_length=1, max_length=128)
    receivedAt: float = Field(ge=1577836800, le=4102444800)
    source: str = Field(min_length=1, max_length=128)
    rawBase64: str = Field(max_length=32768)
    sampleAt: float | None = Field(default=None, ge=1577836800, le=4102444800)
    hrBpm: int | None = Field(default=None, ge=0, le=65535)
    rrMs: list[float] | None = Field(default=None, max_length=512)
    intervalWords: list[int] | None = Field(default=None, max_length=512)
    intervalStatus: str | None = Field(default=None, max_length=128)
    acceleration: list[float] | None = Field(default=None, min_length=3, max_length=3)
    batteryPercent: float | None = Field(default=None, ge=0, le=100)
    quality: str | None = Field(default=None, max_length=128)
    decoderVersion: str = Field(max_length=128)

    @field_validator("intervalWords")
    @classmethod
    def valid_words(cls, value):
        if value is not None and any(not 0 <= word <= 65535 for word in value):
            raise ValueError("Invalid interval word")
        return value

    @field_validator("rawBase64")
    @classmethod
    def valid_base64(cls, value):
        base64.b64decode(value, validate=True)
        return value


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    captures: list[Capture] = Field(min_length=1, max_length=500)


def authorize(authorization):
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected and os.environ.get("INGEST_TOKEN_FILE"):
        expected = Path(os.environ["INGEST_TOKEN_FILE"]).read_text().strip()
    if len(expected) < 32: raise HTTPException(503, "Server token is not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized")


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    storage.initialize()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    with storage.connect() as db: db.execute("SELECT count(*) FROM settings")
    return {"ok": True}


@app.post("/v1/batches", status_code=204)
async def ingest(request: Request, authorization: str | None = Header(default=None)):
    authorize(authorization)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2 * 1024 * 1024: raise HTTPException(413, "Batch exceeds 2 MiB")
        body.extend(chunk)
    try:
        batch = Batch.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid capture batch") from None
    now = time.time()
    if any(c.receivedAt > now + 300 or (c.sampleAt is not None and c.sampleAt > min(now, c.receivedAt) + 300) for c in batch.captures):
        raise HTTPException(422, "Capture clock is in the future")
    try:
        await run_in_threadpool(storage.persist, batch)
    except storage.Conflict as error:
        raise HTTPException(409, str(error)) from None
    return Response(status_code=204)


@app.get("/v1/status")
def status(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return storage.status()


@app.get("/metrics")
def metrics():
    # Expose this only to localhost / the private compose network, never through Serve.
    s = storage.status()
    values = {"whoop_export_pending_samples": s["samples"].get("pending", 0),
              "whoop_export_blocked_samples": s["samples"].get("rejected", 0) + s["samples"].get("historical_import", 0),
              "whoop_export_conflicts": s["conflicts"], "whoop_archived_records": s["captured_records"]}
    with storage.connect() as db:
        device = db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
        supported = device and temperature.firmware_at(db, device[0], time.time()) == temperature.FIRMWARE
        values['whoop_skin_temperature_decoder_supported'] = int(bool(supported))
        values.update(hrv.metrics(db, time.time()))
        values.update(vitals.metrics(db, time.time()))
        values.update(wellness.metrics(db, time.time()))
        values.update(analysis_quality.metrics(db, time.time()))
    for metric, key in (("whoop_last_received_timestamp_seconds", "last_received"),
                        ("whoop_battery_last_observed_timestamp_seconds", "battery_observed_at"),
                        ("whoop_prometheus_last_success_timestamp_seconds", "last_prometheus_success")):
        if s[key] is not None: values[metric] = s[key]
    lines = []
    for k, v in values.items():
        labels = f'device="strap",algorithm="{hrv.ALGORITHM}"' if k.startswith('whoop_hrv_') else 'device="strap"'
        if k.startswith('whoop_spo2_'): labels += f',decoder="{vitals.OXYGEN_DECODER}"'
        if k.startswith('whoop_respiratory_rate_'): labels += f',algorithm="{respiration.ALGORITHM}"'
        if k.startswith('life_strain_') or k=='life_cardio_load': labels += f',algorithm="{wellness.ALGORITHM}"'
        if k.startswith('life_sleep_'): labels += f',algorithm="{wellness.SLEEP_ALGORITHM}"'
        lines.append(f'{k}{{{labels}}} {v}\n')
    return Response("".join(lines), media_type="text/plain; version=0.0.4")


@app.get("/v1/sensors")
def sensors(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return {"sensors": storage.sensor_inventory(), "health_calibration": "not_validated"}


@app.get("/v1/temperature")
def skin_temperature(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with storage.connect() as db:
        return temperature.status(db, storage.directory(), time.time())


@app.get("/v1/hrv")
def heart_rate_variability(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with storage.connect() as db:
        now=time.time()
        return hrv.status(db, now) | dict(quality_report=analysis_quality.status(db,now))


@app.get("/v1/vitals")
def respiratory_and_oxygen(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with storage.connect() as db:
        return vitals.status(db, time.time())


class WellnessProfile(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    timezone: str = Field(min_length=1,max_length=100)
    max_hr: float | None = Field(default=None,ge=80,le=250)
    sleep_goal_minutes: float | None = Field(default=None,ge=180,le=900)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('Unknown time zone') from None
        return value


class SleepEdit(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    revision: int = Field(ge=1,le=1000000)
    start: float = Field(ge=1577836800,le=4102444800)
    end: float | None = Field(default=None,ge=1577836800,le=4102444800)
    awake_seconds: float = Field(default=0,ge=0,le=86400)


@app.get('/v1/wellness')
def wellness_status(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with storage.connect() as db:return wellness.status(db,time.time())


@app.put('/v1/profile')
def update_profile(value: WellnessProfile,authorization: str | None = Header(default=None)):
    authorize(authorization)
    with storage.connect() as db:wellness.save_profile(db,value.model_dump(),time.time())
    return value


@app.put('/v1/sleep/{identifier}')
def update_sleep(identifier: str,value: SleepEdit,authorization: str | None = Header(default=None)):
    authorize(authorization)
    from uuid import UUID
    try: identifier=str(UUID(identifier))
    except ValueError:raise HTTPException(422,'Expected a sleep-session UUID') from None
    now=time.time()
    if value.start>now+5 or (value.end is not None and (value.end>now+5 or not 0<value.end-value.start<=86400 or value.awake_seconds>=value.end-value.start)):
        raise HTTPException(422,'Check the sleep times and awake duration')
    if value.end is None and value.awake_seconds!=0:raise HTTPException(422,'Finish the session before adding awake time')
    try:
        with storage.connect() as db:return wellness.save_sleep(db,identifier,value.model_dump(),now)
    except wellness.EditConflict as error:raise HTTPException(409,str(error)) from None
