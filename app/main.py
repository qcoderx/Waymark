from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .care import (
    ActionConfirmation,
    CareAction,
    CareAgent,
    CareCallJoin,
    CareCallLinks,
    CareCustomer,
    CareRepository,
    CareSession,
    CareSessionCreate,
    CareTimeline,
    CareTurnCreate,
    CareTurnResult,
    CareVertical,
    InvoiceCreate,
)
from .config import Settings
from .daily import DailyAPIError, DailyClient, create_access_token, validate_access_token
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
    WebRTCCallLinks,
    WebRTCJoin,
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
        app.state.care = CareRepository(store, settings.public_base_url)
        app.state.care_agent = CareAgent(settings, app.state.care)
        app.state.twilio = TwilioTelephony(settings)
        app.state.infobip = InfobipTelephony(settings)
        app.state.daily = DailyClient(settings)
        yield
        store.close()

    app = FastAPI(
        title="Waymark Core API",
        summary="Turn shared conversations into useful, auditable actions.",
        description=(
            "Live two-sided speech, customer-care tools, shared documents, delivery "
            "guidance, and the Human Address Graph learning loop."
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

    @app.get("/", include_in_schema=False)
    async def landing_page() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "landing.html")

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

    @app.get(
        "/v1/care/customers",
        response_model=list[CareCustomer],
        tags=["customer care"],
    )
    async def list_care_customers(
        request: Request,
        vertical: CareVertical | None = None,
        query: str | None = Query(default=None, max_length=120),
    ) -> list[CareCustomer]:
        return request.app.state.care.list_customers(
            vertical=vertical.value if vertical else None, query=query
        )

    @app.get(
        "/v1/care/customers/{customer_id}",
        response_model=CareCustomer,
        tags=["customer care"],
    )
    async def get_care_customer(customer_id: str, request: Request) -> CareCustomer:
        return request.app.state.care.get_customer(customer_id)

    @app.post(
        "/v1/care/sessions",
        response_model=CareSession,
        status_code=201,
        tags=["customer care"],
    )
    async def create_care_session(
        payload: CareSessionCreate, request: Request
    ) -> CareSession:
        return request.app.state.care.create_session(payload)

    @app.get(
        "/v1/care/sessions/{session_id}",
        response_model=CareTimeline,
        tags=["customer care"],
    )
    async def get_care_session(session_id: str, request: Request) -> CareTimeline:
        return request.app.state.care.timeline(session_id)

    @app.post(
        "/v1/care/sessions/{session_id}/turns",
        response_model=CareTurnResult,
        tags=["customer care"],
    )
    async def process_care_turn(
        session_id: str, payload: CareTurnCreate, request: Request
    ) -> CareTurnResult:
        return await request.app.state.care_agent.process_turn(session_id, payload)

    @app.post(
        "/v1/care/sessions/{session_id}/invoices",
        response_model=CareTurnResult,
        tags=["customer care"],
    )
    async def create_care_invoice(
        session_id: str, payload: InvoiceCreate, request: Request
    ) -> CareTurnResult:
        return request.app.state.care_agent.create_invoice(session_id, payload)

    @app.post(
        "/v1/care/actions/{action_id}/confirm",
        response_model=CareAction,
        tags=["customer care"],
    )
    async def confirm_care_action(
        action_id: str, payload: ActionConfirmation, request: Request
    ) -> CareAction:
        return request.app.state.care_agent.confirm(
            action_id, payload.confirmation_token
        )

    @app.get("/v1/care/artifacts/{artifact_id}/download", tags=["customer care"])
    async def download_care_artifact(artifact_id: str, request: Request) -> Response:
        artifact, content = request.app.state.care.artifact_content(artifact_id)
        return Response(
            content=content,
            media_type=artifact.mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.file_name}"'
            },
        )

    def authorize_care_access(
        session_id: str, role: str, access: str, request_or_websocket: Request | WebSocket
    ):
        if not settings.daily_api_key:
            return None
        grant = validate_access_token(settings.daily_api_key, access)
        if not grant or grant.delivery_id != session_id or grant.role != role:
            return None
        try:
            call = request_or_websocket.app.state.care.get_call(grant.call_id)
        except NotFoundError:
            return None
        if call["session_id"] != session_id:
            return None
        return grant

    @app.post(
        "/v1/care/sessions/{session_id}/webrtc",
        response_model=CareCallLinks,
        tags=["customer care"],
    )
    async def create_care_webrtc_call(
        session_id: str, request: Request
    ) -> CareCallLinks:
        if not request.app.state.daily.configured:
            raise HTTPException(status_code=503, detail="Daily is not configured")
        session = request.app.state.care.get_session(session_id)
        expires_at_unix = int(time.time()) + settings.daily_room_ttl_minutes * 60
        room_subject = f"care-{session_id}"
        try:
            room = await request.app.state.daily.ensure_room(
                room_subject, expires_at_unix
            )
        except DailyAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        call = request.app.state.care.ensure_call(
            session_id, f"daily:{room['name']}", str(room["url"])
        )
        if not settings.daily_api_key:
            raise HTTPException(status_code=503, detail="DAILY_API_KEY is not configured")
        roles = (
            ("employee", "counterparty")
            if session.vertical == CareVertical.BUSINESS
            else ("agent", "customer")
        )
        base = f"{settings.public_base_url}/care-call/{session_id}"
        links: dict[str, str] = {}
        for role in roles:
            access = create_access_token(
                settings.daily_api_key,
                session_id,
                call["id"],
                role,
                expires_at_unix,
            )
            links[role] = f"{base}#role={role}&access={access}"
        return CareCallLinks(
            session_id=session_id,
            call_id=call["id"],
            links=links,
            expires_at=datetime.fromtimestamp(expires_at_unix, timezone.utc),
        )

    @app.post(
        "/v1/care/sessions/{session_id}/webrtc/join",
        response_model=CareCallJoin,
        tags=["customer care"],
    )
    async def join_care_webrtc_call(
        session_id: str,
        request: Request,
        response: Response,
        role: str = Query(pattern="^(customer|agent|employee|counterparty)$"),
    ) -> CareCallJoin:
        authorization = request.headers.get("authorization", "")
        access = authorization.removeprefix("Bearer ").strip()
        grant = authorize_care_access(session_id, role, access, request)
        if not grant:
            raise HTTPException(status_code=403, detail="invalid or expired call link")
        room_name = DailyClient.room_name(f"care-{session_id}")
        try:
            meeting_token = await request.app.state.daily.create_meeting_token(
                room_name,
                role,
                f"{role}-{session_id}"[:36],
                grant.expires_at,
            )
        except DailyAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        response.set_cookie(
            key="waymark_call",
            value=access,
            max_age=max(1, grant.expires_at - int(time.time())),
            httponly=True,
            secure=settings.public_base_url.startswith("https://"),
            samesite="strict",
            path=f"/v1/care/sessions/{session_id}",
        )
        return CareCallJoin(
            room_url=f"https://{settings.daily_domain}.daily.co/{room_name}",
            meeting_token=meeting_token,
            audio_websocket_url=(
                f"{settings.websocket_base_url}/v1/care/sessions/{session_id}"
                f"/daily-audio/{role}"
            ),
            role=role,
            disclosure=settings.care_disclosure,
            expires_at=datetime.fromtimestamp(grant.expires_at, timezone.utc),
        )

    @app.get("/care-call/{session_id}", include_in_schema=False)
    async def care_call_page(session_id: str, request: Request) -> FileResponse:
        request.app.state.care.get_session(session_id)
        return FileResponse(Path(__file__).parent / "static" / "call.html")

    @app.websocket("/v1/care/sessions/{session_id}/daily-audio/{role}")
    async def care_daily_audio_stream(
        websocket: WebSocket, session_id: str, role: str
    ) -> None:
        access = websocket.cookies.get("waymark_call", "")
        grant = authorize_care_access(session_id, role, access, websocket)
        if not grant:
            await websocket.close(code=4403, reason="invalid or expired call link")
            return
        await websocket.accept()
        stream: SaharaStream | None = None
        transcript_task: asyncio.Task | None = None
        agent_tasks: set[asyncio.Task] = set()

        async def receive_segment(active_stream: SaharaStream) -> None:
            async for message in active_stream.messages():
                kind = message.get("message_type")
                if kind == "COMMITTED_TRANSCRIPT":
                    transcript = message.get("transcript_text", "").strip()
                    if transcript:
                        task = asyncio.create_task(
                            websocket.app.state.care_agent.process_turn(
                                session_id,
                                CareTurnCreate(speaker=role, text=transcript),
                            )
                        )
                        agent_tasks.add(task)
                        task.add_done_callback(agent_tasks.discard)
                    return
                if kind in {
                    "ERROR",
                    "AUTHENTICATION_ERROR",
                    "RESOURCE_EXHAUSTED",
                    "QUOTA_EXCEEDED",
                    "INSUFFICIENT_AUDIO_ACTIVITY",
                    "SESSION_TIME_LIMIT_EXCEEDED",
                }:
                    return

        async def start_segment() -> None:
            nonlocal stream, transcript_task
            if not settings.intron_api_key:
                return
            stream = SaharaStream(settings)
            await stream.connect()
            transcript_task = asyncio.create_task(receive_segment(stream))

        async def finish_segment() -> None:
            nonlocal stream, transcript_task
            active_stream = stream
            active_task = transcript_task
            stream = None
            transcript_task = None
            if not active_stream:
                return
            with contextlib.suppress(Exception):
                await active_stream.commit()
            if active_task:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(active_task, timeout=12)
                if not active_task.done():
                    active_task.cancel()
            await active_stream.close()

        try:
            await start_segment()
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                if audio and stream:
                    await stream.send_pcm16(audio, source_rate=16_000)
                command = message.get("text")
                if command == "flush":
                    await finish_segment()
                    await start_segment()
                elif command == "stop":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("customer-care Daily audio stream failed")
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
        finally:
            await finish_segment()

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
        "/v1/deliveries/{delivery_id}/webrtc",
        response_model=WebRTCCallLinks,
        tags=["telephony"],
    )
    async def create_webrtc_call(delivery_id: str, request: Request) -> WebRTCCallLinks:
        if settings.telephony_provider != "daily" and not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Daily WebRTC is disabled")
        if not request.app.state.daily.configured:
            raise HTTPException(status_code=503, detail="Daily is not configured")
        delivery = request.app.state.store.get_delivery(delivery_id)
        expires_at_unix = int(time.time()) + settings.daily_room_ttl_minutes * 60
        try:
            room = await request.app.state.daily.ensure_room(delivery_id, expires_at_unix)
        except DailyAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        provider_call_id = f"daily:{room['name']}"
        try:
            call = request.app.state.store.call_for_provider_id(provider_call_id)
            call_id = call["id"]
        except NotFoundError:
            call_id = request.app.state.store.create_call(
                delivery_id, "daily", provider_call_id, str(room["url"])
            )
        if not settings.daily_api_key:
            raise HTTPException(status_code=503, detail="DAILY_API_KEY is not configured")
        tokens = {
            role: create_access_token(
                settings.daily_api_key, delivery_id, call_id, role, expires_at_unix
            )
            for role in ("rider", "customer")
        }
        await request.app.state.events.publish(
            EventType.CALL_STATUS,
            delivery_id,
            {"status": CallStatus.RINGING, "provider": "daily"},
            call_id=call_id,
            trace_id=request.state.trace_id,
        )
        base = f"{settings.public_base_url}/call/{delivery_id}"
        return WebRTCCallLinks(
            delivery_id=delivery.id,
            call_id=call_id,
            rider_url=f"{base}#role=rider&access={tokens['rider']}",
            customer_url=f"{base}#role=customer&access={tokens['customer']}",
            expires_at=datetime.fromtimestamp(expires_at_unix, timezone.utc),
        )

    def authorize_daily_access(
        delivery_id: str, role: str, access: str, request_or_websocket: Request | WebSocket
    ):
        if not settings.daily_api_key:
            return None
        grant = validate_access_token(settings.daily_api_key, access)
        if not grant or grant.delivery_id != delivery_id or grant.role != role:
            return None
        try:
            call = request_or_websocket.app.state.store.get_call(grant.call_id)
        except NotFoundError:
            return None
        if call["delivery_id"] != delivery_id or call["provider"] != "daily":
            return None
        return grant

    @app.post(
        "/v1/deliveries/{delivery_id}/webrtc/join",
        response_model=WebRTCJoin,
        tags=["telephony"],
    )
    async def join_webrtc_call(
        delivery_id: str,
        request: Request,
        response: Response,
        role: str = Query(pattern="^(rider|customer)$"),
    ) -> WebRTCJoin:
        authorization = request.headers.get("authorization", "")
        access = authorization.removeprefix("Bearer ").strip()
        grant = authorize_daily_access(delivery_id, role, access, request)
        if not grant:
            raise HTTPException(status_code=403, detail="invalid or expired call link")
        delivery = request.app.state.store.get_private_delivery(delivery_id)
        room_name = DailyClient.room_name(delivery_id)
        user_ref = delivery["rider_ref"] if role == "rider" else delivery["customer_ref"]
        try:
            meeting_token = await request.app.state.daily.create_meeting_token(
                room_name, role, user_ref, grant.expires_at
            )
        except DailyAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        response.set_cookie(
            key="waymark_call",
            value=access,
            max_age=max(1, grant.expires_at - int(time.time())),
            httponly=True,
            secure=settings.public_base_url.startswith("https://"),
            samesite="strict",
            path=f"/v1/deliveries/{delivery_id}",
        )
        ws_url = f"{settings.websocket_base_url}/v1/deliveries/{delivery_id}/daily-audio/{role}"
        return WebRTCJoin(
            room_url=f"https://{settings.daily_domain}.daily.co/{room_name}",
            meeting_token=meeting_token,
            audio_websocket_url=ws_url,
            role=role,
            disclosure=settings.call_disclosure,
            expires_at=datetime.fromtimestamp(grant.expires_at, timezone.utc),
        )

    @app.get("/call/{delivery_id}", include_in_schema=False)
    async def call_page(delivery_id: str, request: Request) -> FileResponse:
        request.app.state.store.get_delivery(delivery_id)
        return FileResponse(Path(__file__).parent / "static" / "call.html")

    @app.websocket("/v1/deliveries/{delivery_id}/daily-audio/{role}")
    async def daily_audio_stream(
        websocket: WebSocket, delivery_id: str, role: str
    ) -> None:
        access = websocket.cookies.get("waymark_call", "")
        grant = authorize_daily_access(delivery_id, role, access, websocket)
        if not grant:
            await websocket.close(code=4403, reason="invalid or expired call link")
            return
        await websocket.accept()
        store = websocket.app.state.store
        events: EventHub = websocket.app.state.events
        pipeline: ResolutionPipeline = websocket.app.state.pipeline
        trace_id = f"trace_{uuid.uuid4().hex}"
        stream: SaharaStream | None = None
        transcript_task: asyncio.Task | None = None

        async def receive_transcripts() -> None:
            assert stream is not None
            async for message in stream.messages():
                kind = message.get("message_type")
                if kind == "PARTIAL_TRANSCRIPT":
                    await pipeline.publish_partial(
                        delivery_id,
                        message.get("transcript", ""),
                        call_id=grant.call_id,
                        trace_id=trace_id,
                    )
                elif kind == "COMMITTED_TRANSCRIPT":
                    transcript = message.get("transcript_text", "").strip()
                    if transcript:
                        await pipeline.process_utterance(
                            delivery_id,
                            SimulationUtterance(
                                transcript=transcript,
                                speaker=role,
                                confidence=0.88,
                                timestamp_ms=int(float(message.get("audio_len", 0)) * 1000),
                                language_mix=[settings.intron_language, "en"],
                            ),
                            call_id=grant.call_id,
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
                        delivery_id,
                        {"stage": "stt", "provider": "sahara", "detail": message},
                        call_id=grant.call_id,
                        trace_id=trace_id,
                    )
                    if kind != "INPUT_ERROR":
                        return

        try:
            store.update_call(grant.call_id, CallStatus.CONNECTED, consent_state="disclosed")
            await events.publish(
                EventType.CALL_STATUS,
                delivery_id,
                {"status": CallStatus.CONNECTED, "provider": "daily", "speaker": role},
                call_id=grant.call_id,
                trace_id=trace_id,
            )
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
                    await stream.send_pcm16(audio, source_rate=16_000)
                if message.get("text") == "stop":
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.exception("Daily audio stream failed", extra={"call_id": grant.call_id})
            await events.publish(
                EventType.ERROR,
                delivery_id,
                {"stage": "media", "provider": "daily", "message": str(exc)},
                call_id=grant.call_id,
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

    @app.post(
        "/v1/deliveries/{delivery_id}/proxy",
        response_model=ProxyAssignment,
        tags=["telephony"],
    )
    async def assign_proxy(delivery_id: str, request: Request) -> ProxyAssignment:
        if settings.telephony_provider == "daily":
            raise HTTPException(
                status_code=409,
                detail=f"Daily uses POST /v1/deliveries/{delivery_id}/webrtc",
            )
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
