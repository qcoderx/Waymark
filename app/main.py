from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .domain import (
    CallStatus,
    DeliveryComplete,
    DeliveryCreate,
    DeliveryOutcome,
    DeliverySession,
    DemoRunRequest,
    DemoRunResponse,
    EventType,
    Guidance,
    ProxyAssignment,
    ResolveResponse,
    SimulationUtterance,
    WaymarkEvent,
)
from .events import EventHub
from .pipeline import ResolutionPipeline
from .store import ConflictError, NotFoundError, PostgresStore, SQLiteStore
from .stt import SaharaStream
from .telephony import (
    InfobipTelephony,
    TwilioTelephony,
    validate_basic_authorization,
    validate_twilio_signature,
)


logger = logging.getLogger("waymark")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        store = (
            PostgresStore(
                settings.database_url,
                guidance_threshold=settings.guidance_confidence_threshold,
                freshness_half_life_days=settings.landmark_freshness_half_life_days,
            )
            if settings.database_url
            else SQLiteStore(
                settings.database_path,
                guidance_threshold=settings.guidance_confidence_threshold,
                freshness_half_life_days=settings.landmark_freshness_half_life_days,
            )
        )
        events = EventHub(store)
        app.state.settings = settings
        app.state.store = store
        app.state.events = events
        app.state.pipeline = ResolutionPipeline(settings, store, events)
        app.state.twilio = TwilioTelephony(settings)
        app.state.infobip = InfobipTelephony(settings)
        yield
        store.close()

    app = FastAPI(
        title="Waymark Core API",
        summary="Turn direction calls into reusable, machine-navigable addresses.",
        description=(
            "Dev 1 service for delivery sessions, proxy calls, live speech, landmark "
            "grounding, guidance events, and the Human Address Graph learning loop."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        trace_id = request.headers.get("x-request-id") or f"trace_{uuid.uuid4().hex}"
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["x-request-id"] = trace_id
        return response

    @app.get("/health", tags=["system"])
    async def health(request: Request) -> dict:
        readiness = settings.production_readiness()
        readiness["database_reachable"] = request.app.state.store.ping()
        return {
            "status": "ok",
            "version": __version__,
            "mode": "demo" if settings.demo_mode else "live",
            "production_ready": all(readiness.values()),
            "checks": readiness,
            "trace_id": request.state.trace_id,
        }

    @app.post(
        "/v1/deliveries",
        response_model=DeliverySession,
        status_code=201,
        tags=["deliveries"],
    )
    async def create_delivery(payload: DeliveryCreate, request: Request) -> DeliverySession:
        delivery = request.app.state.store.create_delivery(payload)
        await request.app.state.pipeline.reuse_known_route(delivery.id)
        return request.app.state.store.get_delivery(delivery.id)

    @app.get("/v1/deliveries", response_model=list[DeliverySession], tags=["deliveries"])
    async def list_deliveries(
        request: Request, limit: int = Query(default=50, ge=1, le=200)
    ) -> list[DeliverySession]:
        return request.app.state.store.list_deliveries(limit)

    @app.get(
        "/v1/deliveries/{delivery_id}",
        response_model=DeliverySession,
        tags=["deliveries"],
    )
    async def get_delivery(delivery_id: str, request: Request) -> DeliverySession:
        return request.app.state.store.get_delivery(delivery_id)

    @app.post(
        "/v1/deliveries/{delivery_id}/proxy",
        response_model=ProxyAssignment,
        tags=["telephony"],
    )
    async def assign_proxy(delivery_id: str, request: Request) -> ProxyAssignment:
        numbers = (
            settings.infobip_proxy_numbers
            if settings.telephony_provider == "infobip"
            else settings.twilio_proxy_numbers
        )
        if settings.demo_mode and not numbers:
            numbers = ("+15550000000",)
        if not numbers:
            variable = (
                "INFOBIP_PROXY_NUMBERS"
                if settings.telephony_provider == "infobip"
                else "TWILIO_PROXY_NUMBERS"
            )
            raise HTTPException(status_code=503, detail=f"{variable} is not configured")
        number, expires_at = request.app.state.store.assign_proxy(delivery_id, numbers)
        return ProxyAssignment(
            delivery_id=delivery_id, proxy_number=number, expires_at=expires_at
        )

    def verify_twilio(request: Request, form: dict[str, str]) -> None:
        if not settings.twilio_validate_signatures or settings.demo_mode:
            return
        signature = request.headers.get("x-twilio-signature")
        signed_url = f"{settings.public_base_url}{request.url.path}"
        if request.url.query:
            signed_url = f"{signed_url}?{request.url.query}"
        if not validate_twilio_signature(signed_url, form, signature, settings.twilio_auth_token):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")

    @app.post("/v1/telephony/inbound", tags=["telephony"])
    async def inbound_call(request: Request) -> Response:
        if settings.telephony_provider != "twilio" and not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Twilio telephony is disabled")
        raw_form = await request.form()
        form = {str(key): str(value) for key, value in raw_form.items()}
        verify_twilio(request, form)
        proxy_number = form.get("To", "")
        caller = form.get("From", "")
        provider_call_id = form.get("CallSid") or f"demo_{uuid.uuid4().hex}"
        delivery = request.app.state.store.delivery_for_proxy(proxy_number)
        if not settings.demo_mode and caller != delivery["rider_phone"]:
            raise HTTPException(status_code=403, detail="caller is not assigned to this delivery")
        call_id = request.app.state.store.create_call(
            delivery["id"], "twilio", provider_call_id, proxy_number
        )
        await request.app.state.events.publish(
            EventType.CALL_STATUS,
            delivery["id"],
            {"status": CallStatus.RINGING, "provider": "twilio"},
            call_id=call_id,
            trace_id=request.state.trace_id,
        )
        xml = request.app.state.twilio.inbound_twiml(call_id, delivery["customer_phone"])
        return Response(content=xml, media_type="application/xml")

    @app.post("/v1/telephony/call-status", tags=["telephony"])
    @app.post("/v1/telephony/stream-status", tags=["telephony"])
    async def call_status(request: Request) -> Response:
        raw_form = await request.form()
        form = {str(key): str(value) for key, value in raw_form.items()}
        verify_twilio(request, form)
        provider_call_id = form.get("CallSid", "")
        call = request.app.state.store.call_for_provider_id(provider_call_id)
        raw_status = (
            form.get("DialCallStatus")
            or form.get("StreamEvent")
            or form.get("CallStatus")
            or "completed"
        ).lower()
        status_map = {
            "ringing": CallStatus.RINGING,
            "in-progress": CallStatus.CONNECTED,
            "stream-started": CallStatus.CONNECTED,
            "completed": CallStatus.DISCONNECTED,
            "stream-stopped": CallStatus.DISCONNECTED,
            "busy": CallStatus.FAILED,
            "failed": CallStatus.FAILED,
            "no-answer": CallStatus.TIMEOUT,
            "canceled": CallStatus.FAILED,
            "stream-error": CallStatus.FAILED,
        }
        status = status_map.get(raw_status, CallStatus.FAILED)
        request.app.state.store.update_call(call["id"], status)
        await request.app.state.events.publish(
            EventType.CALL_STATUS,
            call["delivery_id"],
            {"status": status, "provider_status": raw_status},
            call_id=call["id"],
            trace_id=request.state.trace_id,
        )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
            media_type="application/xml",
        )

    def verify_infobip(request: Request) -> None:
        if settings.demo_mode:
            return
        if not validate_basic_authorization(
            request.headers.get("authorization"),
            settings.infobip_webhook_username,
            settings.infobip_webhook_password,
        ):
            raise HTTPException(status_code=403, detail="invalid Infobip webhook credentials")

    def event_phone(value: object) -> str:
        if isinstance(value, dict):
            value = value.get("phoneNumber") or value.get("number") or ""
        raw = str(value or "").strip()
        return f"+{raw.removeprefix('+')}" if raw else ""

    @app.post(
        "/v1/telephony/infobip/events",
        status_code=204,
        tags=["telephony"],
    )
    async def infobip_events(request: Request) -> Response:
        if settings.telephony_provider != "infobip" and not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Infobip telephony is disabled")
        verify_infobip(request)
        event = await request.json()
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="invalid Infobip event")
        event_type = str(event.get("type", "")).upper()
        provider_call_id = str(event.get("callId") or "")
        if not event_type:
            raise HTTPException(status_code=400, detail="event type is required")

        store: SQLiteStore = request.app.state.store
        events: EventHub = request.app.state.events
        if event_type == "CALL_RECEIVED":
            if not provider_call_id:
                raise HTTPException(status_code=400, detail="callId is required")
            proxy_number = event_phone(event.get("to"))
            caller = event_phone(event.get("from"))
            delivery = store.delivery_for_proxy(proxy_number)
            if not settings.demo_mode and caller != delivery["rider_phone"]:
                raise HTTPException(
                    status_code=403, detail="caller is not assigned to this delivery"
                )
            try:
                call = store.call_for_provider_id(provider_call_id)
                call_id = call["id"]
                if store.has_provider_call_leg(call_id, "customer"):
                    return Response(status_code=204)
                store.update_call(call_id, CallStatus.RINGING)
            except NotFoundError:
                call_id = store.create_call(
                    delivery["id"], "infobip", provider_call_id, proxy_number
                )
                await events.publish(
                    EventType.CALL_STATUS,
                    delivery["id"],
                    {"status": CallStatus.RINGING, "provider": "infobip"},
                    call_id=call_id,
                    trace_id=request.state.trace_id,
                )
            try:
                dialog = await request.app.state.infobip.create_dialog(
                    provider_call_id,
                    delivery["customer_phone"],
                    proxy_number,
                    call_id,
                )
                child_id = str((dialog.get("childCall") or {}).get("id") or "")
                if not child_id:
                    raise RuntimeError("Infobip dialog response omitted childCall.id")
                dialog_id = str(dialog.get("id") or "")
                if not dialog_id:
                    raise RuntimeError("Infobip dialog response omitted id")
                store.complete_provider_dialog(call_id, child_id, dialog_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                store.update_call(call_id, CallStatus.FAILED)
                await events.publish(
                    EventType.ERROR,
                    delivery["id"],
                    {
                        "stage": "telephony",
                        "provider": "infobip",
                        "detail": str(exc),
                    },
                    call_id=call_id,
                    trace_id=request.state.trace_id,
                )
                raise HTTPException(
                    status_code=502, detail="Infobip dialog creation failed"
                ) from exc
            return Response(status_code=204)

        dialog_id = str(event.get("dialogId") or "")
        if dialog_id and event_type in {
            "DIALOG_ESTABLISHED",
            "SAY_FINISHED",
            "DIALOG_FINISHED",
            "DIALOG_FAILED",
        }:
            try:
                dialog_call = store.call_for_provider_dialog(dialog_id)
            except NotFoundError:
                return Response(status_code=204)
            if event_type == "DIALOG_ESTABLISHED":
                if dialog_call["disclosure_requested"]:
                    return Response(status_code=204)
                try:
                    await request.app.state.infobip.say_dialog(
                        dialog_id, settings.call_disclosure
                    )
                    store.mark_dialog_disclosure_requested(dialog_id)
                except httpx.HTTPError as exc:
                    raise HTTPException(
                        status_code=502, detail="Infobip disclosure failed"
                    ) from exc
            elif event_type == "SAY_FINISHED":
                store.update_call(
                    dialog_call["id"],
                    CallStatus.CONNECTED,
                    consent_state="disclosed",
                )
                for provider_leg in store.provider_call_legs(dialog_call["id"]):
                    if provider_leg["stream_started"]:
                        continue
                    try:
                        await request.app.state.infobip.start_media_stream(
                            provider_leg["provider_call_id"]
                        )
                        store.mark_provider_stream_started(
                            provider_leg["provider_call_id"]
                        )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        raise HTTPException(
                            status_code=502,
                            detail="Infobip media stream start failed",
                        ) from exc
            else:
                dialog_status = (
                    CallStatus.DISCONNECTED
                    if event_type == "DIALOG_FINISHED"
                    else CallStatus.FAILED
                )
                store.update_call(dialog_call["id"], dialog_status)
            return Response(status_code=204)

        if not provider_call_id:
            return Response(status_code=204)

        try:
            leg = store.provider_call_leg(provider_call_id)
        except NotFoundError:
            return Response(status_code=204)

        status_map = {
            "CALL_RINGING": CallStatus.RINGING,
            "CALL_PRE_ESTABLISHED": CallStatus.RINGING,
            "CALL_ESTABLISHED": CallStatus.CONNECTED,
            "CALL_RECONNECTED": CallStatus.CONNECTED,
            "CALL_FINISHED": CallStatus.DISCONNECTED,
            "CALL_DISCONNECTED": CallStatus.DISCONNECTED,
            "CALL_FAILED": CallStatus.FAILED,
        }
        status = status_map.get(event_type)
        if status is not None:
            store.update_call(
                leg["call_id"],
                status,
            )
            await events.publish(
                EventType.CALL_STATUS,
                leg["delivery_id"],
                {
                    "status": status,
                    "provider": "infobip",
                    "provider_status": event_type,
                    "speaker": leg["speaker"],
                },
                call_id=leg["call_id"],
                trace_id=request.state.trace_id,
            )
        return Response(status_code=204)

    @app.websocket("/v1/media/{call_id}")
    async def media_stream(websocket: WebSocket, call_id: str) -> None:
        if settings.twilio_validate_signatures and not settings.demo_mode:
            signed_url = f"{settings.websocket_base_url}/v1/media/{call_id}"
            signature = websocket.headers.get("x-twilio-signature")
            if not validate_twilio_signature(
                signed_url, {}, signature, settings.twilio_auth_token
            ):
                await websocket.close(code=4403, reason="invalid Twilio signature")
                return
        await websocket.accept()
        store: SQLiteStore = websocket.app.state.store
        events: EventHub = websocket.app.state.events
        pipeline: ResolutionPipeline = websocket.app.state.pipeline
        try:
            call = store.get_call(call_id)
        except NotFoundError:
            await websocket.close(code=4404, reason="unknown call")
            return
        trace_id = f"trace_{uuid.uuid4().hex}"
        sahara_streams: dict[str, SaharaStream] = {}
        receiver_tasks: dict[str, asyncio.Task] = {}

        async def receive_transcripts(stream: SaharaStream, speaker: str) -> None:
            async for message in stream.messages():
                kind = message.get("message_type")
                if kind == "PARTIAL_TRANSCRIPT":
                    await pipeline.publish_partial(
                        call["delivery_id"],
                        message.get("transcript", ""),
                        call_id=call_id,
                        trace_id=trace_id,
                    )
                elif kind == "COMMITTED_TRANSCRIPT":
                    await pipeline.process_utterance(
                        call["delivery_id"],
                        SimulationUtterance(
                            transcript=message.get("transcript_text", ""),
                            speaker=speaker,
                            confidence=0.88,
                            timestamp_ms=int(float(message.get("audio_len", 0)) * 1000),
                            language_mix=[settings.intron_language, "en"],
                        ),
                        call_id=call_id,
                        trace_id=trace_id,
                    )
                    return
                elif kind in {
                    "ERROR",
                    "INPUT_ERROR",
                    "AUTHENTICATION_ERROR",
                    "RESOURCE_EXHAUSTED",
                    "QUOTA_EXCEEDED",
                    "INSUFFICIENT_AUDIO_ACTIVITY",
                    "SESSION_TIME_LIMIT_EXCEEDED",
                }:
                    await events.publish(
                        EventType.ERROR,
                        call["delivery_id"],
                        {"stage": "stt", "provider": "sahara", "detail": message},
                        call_id=call_id,
                        trace_id=trace_id,
                    )
                    if kind not in {"INPUT_ERROR"}:
                        return

        try:
            while True:
                message = await websocket.receive_json()
                event = message.get("event")
                if event == "start":
                    store.update_call(call_id, CallStatus.CONNECTED, consent_state="disclosed")
                    await events.publish(
                        EventType.CALL_STATUS,
                        call["delivery_id"],
                        {"status": CallStatus.CONNECTED, "provider": "twilio"},
                        call_id=call_id,
                        trace_id=trace_id,
                    )
                elif event == "media" and settings.intron_api_key:
                    media = message.get("media", {})
                    track = media.get("track", "inbound")
                    if track not in sahara_streams:
                        stream = SaharaStream(settings)
                        await stream.connect()
                        sahara_streams[track] = stream
                        speaker = "rider" if track == "inbound" else "customer"
                        receiver_tasks[track] = asyncio.create_task(
                            receive_transcripts(stream, speaker)
                        )
                    await sahara_streams[track].send_twilio_media(media.get("payload", ""))
                elif event == "transcript" and settings.demo_mode:
                    await pipeline.process_utterance(
                        call["delivery_id"],
                        SimulationUtterance(
                            transcript=message.get("transcript", ""),
                            speaker=message.get("speaker", "customer"),
                        ),
                        call_id=call_id,
                        trace_id=trace_id,
                    )
                elif event == "stop":
                    await asyncio.gather(
                        *(stream.commit() for stream in sahara_streams.values())
                    )
                    if receiver_tasks:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(
                                asyncio.gather(*receiver_tasks.values()), timeout=12
                            )
                    store.update_call(call_id, CallStatus.DISCONNECTED)
                    break
        except WebSocketDisconnect:
            store.update_call(call_id, CallStatus.DISCONNECTED)
        except Exception as exc:
            logger.exception("media stream failed", extra={"call_id": call_id})
            store.update_call(call_id, CallStatus.FAILED)
            await events.publish(
                EventType.ERROR,
                call["delivery_id"],
                {"stage": "media", "message": str(exc)},
                call_id=call_id,
                trace_id=trace_id,
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
        finally:
            for task in receiver_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *(stream.close() for stream in sahara_streams.values()),
                return_exceptions=True,
            )

    @app.websocket("/v1/telephony/infobip/media")
    async def infobip_media_stream(websocket: WebSocket) -> None:
        if not settings.demo_mode and not validate_basic_authorization(
            websocket.headers.get("authorization"),
            settings.infobip_media_stream_username,
            settings.infobip_media_stream_password,
        ):
            await websocket.close(code=4403, reason="invalid Infobip media credentials")
            return
        await websocket.accept()
        store: SQLiteStore = websocket.app.state.store
        events: EventHub = websocket.app.state.events
        pipeline: ResolutionPipeline = websocket.app.state.pipeline
        stream: SaharaStream | None = None
        transcript_task: asyncio.Task | None = None
        leg: dict | None = None
        trace_id = f"trace_{uuid.uuid4().hex}"

        async def receive_transcripts() -> None:
            assert stream is not None and leg is not None
            async for message in stream.messages():
                kind = message.get("message_type")
                if kind == "PARTIAL_TRANSCRIPT":
                    await pipeline.publish_partial(
                        leg["delivery_id"],
                        message.get("transcript", ""),
                        call_id=leg["call_id"],
                        trace_id=trace_id,
                    )
                elif kind == "COMMITTED_TRANSCRIPT":
                    transcript = message.get("transcript_text", "").strip()
                    if transcript:
                        await pipeline.process_utterance(
                            leg["delivery_id"],
                            SimulationUtterance(
                                transcript=transcript,
                                speaker=leg["speaker"],
                                confidence=0.88,
                                timestamp_ms=int(
                                    float(message.get("audio_len", 0)) * 1000
                                ),
                                language_mix=[settings.intron_language, "en"],
                            ),
                            call_id=leg["call_id"],
                            trace_id=trace_id,
                        )
                    return
                elif kind in {
                    "ERROR",
                    "INPUT_ERROR",
                    "AUTHENTICATION_ERROR",
                    "RESOURCE_EXHAUSTED",
                    "QUOTA_EXCEEDED",
                    "SESSION_TIME_LIMIT_EXCEEDED",
                }:
                    await events.publish(
                        EventType.ERROR,
                        leg["delivery_id"],
                        {"stage": "stt", "provider": "sahara", "detail": message},
                        call_id=leg["call_id"],
                        trace_id=trace_id,
                    )
                    if kind != "INPUT_ERROR":
                        return

        try:
            first = await websocket.receive()
            initial_text = first.get("text")
            if not initial_text:
                await websocket.close(code=4400, reason="missing Infobip stream metadata")
                return
            metadata = json.loads(initial_text)
            provider_call_id = str(metadata.get("callId", ""))
            sample_rate = int(metadata.get("sampleRate", 48_000))
            leg = store.provider_call_leg(provider_call_id)
            if settings.intron_api_key:
                stream = SaharaStream(settings)
                await stream.connect()
                transcript_task = asyncio.create_task(receive_transcripts())
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                if audio and stream:
                    await stream.send_pcm16(audio, source_rate=sample_rate)
        except (WebSocketDisconnect, NotFoundError):
            return
        except Exception as exc:
            logger.exception("Infobip media stream failed")
            if leg:
                await events.publish(
                    EventType.ERROR,
                    leg["delivery_id"],
                    {"stage": "media", "provider": "infobip", "message": str(exc)},
                    call_id=leg["call_id"],
                    trace_id=trace_id,
                )
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
        finally:
            if stream:
                with contextlib.suppress(Exception):
                    await stream.commit()
                if transcript_task:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(transcript_task, timeout=12)
                    if not transcript_task.done():
                        transcript_task.cancel()
                await stream.close()

    @app.get(
        "/v1/deliveries/{delivery_id}/guidance",
        response_model=Guidance,
        tags=["guidance"],
    )
    async def get_guidance(delivery_id: str, request: Request) -> Guidance:
        guidance = request.app.state.store.get_guidance(delivery_id)
        if not guidance:
            raise HTTPException(status_code=404, detail="guidance is not available yet")
        return guidance

    @app.websocket("/v1/deliveries/{delivery_id}/events")
    async def delivery_events(websocket: WebSocket, delivery_id: str) -> None:
        try:
            websocket.app.state.store.get_delivery(delivery_id)
        except NotFoundError:
            await websocket.close(code=4404, reason="unknown delivery")
            return
        await websocket.accept()
        try:
            async with websocket.app.state.events.subscribe(delivery_id) as queue:
                for event in websocket.app.state.store.event_history(delivery_id):
                    await websocket.send_json(event.model_dump(mode="json"))
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=25)
                        await websocket.send_json(event.model_dump(mode="json"))
                    except asyncio.TimeoutError:
                        await websocket.send_json({"type": "system.ping"})
        except WebSocketDisconnect:
            return

    @app.get(
        "/v1/deliveries/{delivery_id}/events/history",
        response_model=list[WaymarkEvent],
        tags=["guidance"],
    )
    async def event_history(delivery_id: str, request: Request) -> list[WaymarkEvent]:
        return request.app.state.store.event_history(delivery_id)

    @app.post(
        "/v1/deliveries/{delivery_id}/complete",
        response_model=DeliveryOutcome,
        tags=["deliveries"],
    )
    async def complete_delivery(
        delivery_id: str, payload: DeliveryComplete, request: Request
    ) -> DeliveryOutcome:
        return await request.app.state.pipeline.complete(delivery_id, payload)

    @app.get("/v1/resolve", response_model=ResolveResponse, tags=["guidance"])
    async def resolve_destination(destination_key: str, request: Request) -> ResolveResponse:
        guidance = request.app.state.store.resolve_destination(destination_key)
        return ResolveResponse(
            destination_key=destination_key, found=guidance is not None, guidance=guidance
        )

    @app.post(
        "/v1/demo/deliveries/{delivery_id}/utterances",
        response_model=Guidance,
        tags=["demo"],
    )
    async def simulate_utterance(
        delivery_id: str, payload: SimulationUtterance, request: Request
    ) -> Guidance:
        if not settings.demo_mode:
            raise HTTPException(status_code=404, detail="demo mode is disabled")
        return await request.app.state.pipeline.process_utterance(delivery_id, payload)

    @app.post("/v1/demo/run", response_model=DemoRunResponse, tags=["demo"])
    async def run_demo(payload: DemoRunRequest, request: Request) -> DemoRunResponse:
        if not settings.demo_mode:
            raise HTTPException(status_code=404, detail="demo mode is disabled")
        run_id = uuid.uuid4().hex[:10]
        destination_key = f"demo-yaba-{run_id}"
        first = request.app.state.store.create_delivery(
            DeliveryCreate(
                external_order_id=f"DEMO-LEARN-{run_id}",
                rider_ref="rider-ada",
                rider_phone="+2348011111111",
                customer_ref=f"customer-{run_id}",
                customer_phone="+2348022222222",
                coarse_location={"lat": 6.5155, "lng": 3.3857},
                coarse_address="Yaba, Lagos",
                destination_key=destination_key,
            )
        )
        learned_guidance = await request.app.state.pipeline.process_utterance(
            first.id, SimulationUtterance(transcript=payload.transcript)
        )
        await request.app.state.pipeline.complete(
            first.id,
            DeliveryComplete(delivered=True, final_location=payload.final_location),
        )
        second = request.app.state.store.create_delivery(
            DeliveryCreate(
                external_order_id=f"DEMO-REUSE-{run_id}",
                rider_ref="rider-femi",
                rider_phone="+2348033333333",
                customer_ref=f"customer-{run_id}",
                customer_phone="+2348022222222",
                coarse_location={"lat": 6.5155, "lng": 3.3857},
                coarse_address="Yaba, Lagos",
                destination_key=destination_key,
            )
        )
        reused = await request.app.state.pipeline.reuse_known_route(second.id)
        if reused is None:
            raise HTTPException(status_code=500, detail="the learned route could not be reused")
        return DemoRunResponse(
            first_delivery=request.app.state.store.get_delivery(first.id),
            learned_guidance=learned_guidance,
            second_delivery=request.app.state.store.get_delivery(second.id),
            reused_guidance=reused,
        )

    return app


app = create_app()
