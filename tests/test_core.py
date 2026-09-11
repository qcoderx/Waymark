from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import Settings
from app.daily import create_access_token, validate_access_token
from app.domain import (
    Coordinate,
    DeliveryComplete,
    DeliveryCreate,
    LandmarkPhrase,
    SimulationUtterance,
)
from app.events import EventHub
from app.extraction import DirectionExtractor
from app.grounding import MapboxGrounder
from app.pipeline import ResolutionPipeline
from app.store import SQLiteStore
from app.stt import mulaw_to_pcm16_16khz, resample_pcm16
from app.telephony import (
    TwilioTelephony,
    infobip_phone,
    validate_basic_authorization,
    validate_twilio_signature,
)


class APISmokeTests(unittest.TestCase):
    def test_customer_care_daily_links_join_shared_action_call(self) -> None:
        from app.main import create_app

        class FakeDaily:
            configured = True

            async def ensure_room(self, subject_id: str, expires_at: int) -> dict:
                return {
                    "name": f"waymark-{subject_id}",
                    "url": f"https://waymark-test.daily.co/waymark-{subject_id}",
                }

            async def create_meeting_token(
                self, room_name: str, role: str, user_ref: str, expires_at: int
            ) -> str:
                return f"care-daily-token-{role}"

        database_path = Path("data") / f"care-daily-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            public_base_url="https://waymark.example",
            daily_api_key="test-secret",
            daily_domain="waymark-test",
            care_agent_enabled=False,
            demo_mode=False,
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                client.app.state.daily = FakeDaily()
                session = client.post(
                    "/v1/care/sessions",
                    json={
                        "vertical": "telecom",
                        "organization": "Waymark Demo Mobile",
                        "customer_id": "cust_tel_chidi",
                        "subject": "Data plan help",
                    },
                ).json()
                response = client.post(
                    f"/v1/care/sessions/{session['id']}/webrtc"
                )
                self.assertEqual(response.status_code, 200, response.text)
                links = response.json()["links"]
                self.assertEqual(set(links), {"agent", "customer"})
                agent_access = parse_qs(urlparse(links["agent"]).fragment)["access"][0]
                join = client.post(
                    f"/v1/care/sessions/{session['id']}/webrtc/join",
                    params={"role": "agent"},
                    headers={"Authorization": f"Bearer {agent_access}"},
                )
                self.assertEqual(join.status_code, 200, join.text)
                self.assertEqual(join.json()["meeting_token"], "care-daily-token-agent")
                self.assertIn("/v1/care/sessions/", join.json()["audio_websocket_url"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)

    def test_customer_care_reads_data_and_confirms_sensitive_action(self) -> None:
        from app.main import create_app

        database_path = Path("data") / f"care-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            demo_mode=True,
            care_agent_enabled=False,
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                customers = client.get(
                    "/v1/care/customers", params={"vertical": "banking"}
                ).json()
                self.assertEqual([item["id"] for item in customers], ["cust_bank_amina"])
                session = client.post(
                    "/v1/care/sessions",
                    json={
                        "vertical": "banking",
                        "organization": "Waymark Demo Bank",
                        "customer_id": "cust_bank_amina",
                        "subject": "Account help",
                    },
                ).json()
                balance = client.post(
                    f"/v1/care/sessions/{session['id']}/turns",
                    json={"speaker": "customer", "text": "What is my account balance?"},
                )
                self.assertEqual(balance.status_code, 200, balance.text)
                self.assertIn("485250.75", balance.json()["reply"])
                freeze = client.post(
                    f"/v1/care/sessions/{session['id']}/turns",
                    json={"speaker": "customer", "text": "Freeze my card, it is missing."},
                ).json()
                action = freeze["actions"][0]
                self.assertEqual(action["status"], "pending_confirmation")
                confirmed = client.post(
                    f"/v1/care/actions/{action['id']}/confirm",
                    json={"confirmation_token": action["confirmation_token"]},
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["result"]["card_status"], "frozen")
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)

    def test_customer_care_creates_downloadable_invoice_pdf(self) -> None:
        from app.main import create_app

        database_path = Path("data") / f"invoice-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            demo_mode=True,
            care_agent_enabled=False,
            public_base_url="https://waymark.example",
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                session = client.post(
                    "/v1/care/sessions",
                    json={
                        "vertical": "business",
                        "organization": "Bello Creative Studio",
                        "customer_id": "cust_biz_kemi",
                        "subject": "Design invoice",
                    },
                ).json()
                result = client.post(
                    f"/v1/care/sessions/{session['id']}/invoices",
                    json={
                        "seller": "Bello Creative Studio",
                        "buyer": "Northwind Traders",
                        "currency": "NGN",
                        "items": [
                            {
                                "description": "Brand identity design",
                                "quantity": 1,
                                "unit_price": 350000,
                            }
                        ],
                        "due_date": "2026-09-30",
                        "notes": "Thank you for your business.",
                    },
                )
                self.assertEqual(result.status_code, 200, result.text)
                artifact = result.json()["artifacts"][0]
                download = client.get(
                    f"/v1/care/artifacts/{artifact['id']}/download"
                )
                self.assertEqual(download.status_code, 200)
                self.assertTrue(download.content.startswith(b"%PDF"))
                self.assertIn("attachment", download.headers["content-disposition"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)

    def test_daily_links_are_private_and_joinable(self) -> None:
        from app.main import create_app

        class FakeDaily:
            configured = True

            async def ensure_room(self, delivery_id: str, expires_at: int) -> dict:
                return {
                    "name": f"waymark-{delivery_id}",
                    "url": f"https://waymark-test.daily.co/waymark-{delivery_id}",
                }

            async def create_meeting_token(
                self, room_name: str, role: str, user_ref: str, expires_at: int
            ) -> str:
                return f"daily-token-{role}"

        database_path = Path("data") / f"daily-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            public_base_url="https://waymark.example",
            telephony_provider="daily",
            daily_api_key="test-secret",
            daily_domain="waymark-test",
            demo_mode=False,
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                client.app.state.daily = FakeDaily()
                delivery = client.post(
                    "/v1/deliveries",
                    json={
                        "external_order_id": "daily-order",
                        "rider_ref": "rider",
                        "rider_phone": "+2348011111111",
                        "customer_ref": "customer",
                        "customer_phone": "+2348022222222",
                        "coarse_location": {"lat": 6.5155, "lng": 3.3857},
                    },
                ).json()
                response = client.post(f"/v1/deliveries/{delivery['id']}/webrtc")
                self.assertEqual(response.status_code, 200, response.text)
                links = response.json()
                rider_query = parse_qs(urlparse(links["rider_url"]).fragment)
                self.assertNotEqual(links["rider_url"], links["customer_url"])
                join = client.post(
                    f"/v1/deliveries/{delivery['id']}/webrtc/join",
                    params={"role": "rider"},
                    headers={"Authorization": f"Bearer {rider_query['access'][0]}"},
                )
                self.assertEqual(join.status_code, 200, join.text)
                self.assertEqual(join.json()["meeting_token"], "daily-token-rider")
                self.assertIn("waymark_call=", join.headers["set-cookie"])
                self.assertIn("HttpOnly", join.headers["set-cookie"])
                self.assertNotIn("test-secret", response.text + join.text)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)

    def test_demo_endpoint_proves_learn_and_reuse(self) -> None:
        from app.main import create_app

        database_path = Path("data") / f"api-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            demo_mode=True,
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                response = client.post("/v1/demo/run", json={})
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["learned_guidance"]["source"], "simulated_call")
                self.assertEqual(body["reused_guidance"]["source"], "human_address_graph")
                self.assertEqual(
                    body["learned_guidance"]["trail"], body["reused_guidance"]["trail"]
                )
                self.assertTrue(body["second_delivery"]["known_route_available"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)

    def test_infobip_dialog_discloses_then_starts_both_streams(self) -> None:
        from app.main import create_app

        class FakeInfobip:
            def __init__(self) -> None:
                self.disclosures: list[tuple[str, str]] = []
                self.streams: list[str] = []

            async def create_dialog(self, *args) -> dict:
                return {
                    "id": "dialog-1",
                    "parentCall": {"id": "ib-parent"},
                    "childCall": {"id": "ib-child"},
                }

            async def say_dialog(self, dialog_id: str, text: str) -> dict:
                self.disclosures.append((dialog_id, text))
                return {}

            async def start_media_stream(self, provider_call_id: str) -> dict:
                self.streams.append(provider_call_id)
                return {}

        database_path = Path("data") / f"infobip-test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=database_path,
            database_url=None,
            demo_mode=True,
            telephony_provider="infobip",
            infobip_proxy_numbers=("+2342012345678",),
            mapbox_access_token=None,
            intron_api_key=None,
        )
        try:
            with TestClient(create_app(settings)) as client:
                fake = FakeInfobip()
                client.app.state.infobip = fake
                delivery = client.post(
                    "/v1/deliveries",
                    json={
                        "external_order_id": "ib-order",
                        "rider_ref": "rider",
                        "rider_phone": "+2348011111111",
                        "customer_ref": "customer",
                        "customer_phone": "+2348022222222",
                        "coarse_location": {"lat": 6.5155, "lng": 3.3857},
                    },
                ).json()
                proxy = client.post(f"/v1/deliveries/{delivery['id']}/proxy").json()
                response = client.post(
                    "/v1/telephony/infobip/events",
                    json={
                        "type": "CALL_RECEIVED",
                        "callId": "ib-parent",
                        "from": "2348011111111",
                        "to": proxy["proxy_number"],
                    },
                )
                self.assertEqual(response.status_code, 204, response.text)
                self.assertEqual(fake.streams, [])
                response = client.post(
                    "/v1/telephony/infobip/events",
                    json={"type": "DIALOG_ESTABLISHED", "dialogId": "dialog-1"},
                )
                self.assertEqual(response.status_code, 204, response.text)
                self.assertEqual(fake.disclosures, [("dialog-1", settings.call_disclosure)])
                response = client.post(
                    "/v1/telephony/infobip/events",
                    json={"type": "SAY_FINISHED", "dialogId": "dialog-1"},
                )
                self.assertEqual(response.status_code, 204, response.text)
                self.assertEqual(set(fake.streams), {"ib-parent", "ib-child"})
                client.post(
                    "/v1/telephony/infobip/events",
                    json={"type": "SAY_FINISHED", "dialogId": "dialog-1"},
                )
                self.assertEqual(len(fake.streams), 2)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)


class ExtractionTests(unittest.TestCase):
    def test_extracts_ordered_lagos_landmark_trail(self) -> None:
        result = DirectionExtractor().extract(
            "Pass the Mobil filling station, take the second right, "
            "then look for the black gate opposite the mosque.",
            0.92,
        )
        self.assertEqual(
            [item.normalized_name for item in result.landmarks],
            ["mobil filling station", "black gate", "mosque"],
        )
        self.assertEqual(
            [item.relation_type.value for item in result.relations],
            ["pass", "turn_right", "opposite"],
        )
        self.assertEqual(result.relations[1].ordinal, 2)

    def test_understands_pidgin_distance_and_left_turn(self) -> None:
        result = DirectionExtractor().extract(
            "When you reach FirstBank, pass am small, second street by your left, "
            "na the blue gate beside the church."
        )
        relations = {item.relation_type.value for item in result.relations}
        self.assertTrue({"pass", "turn_left", "continue", "beside"} <= relations)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = replace(
            Settings.from_env(),
            public_base_url="https://waymark.example",
            call_disclosure="Waymark disclosure.",
        )

    def test_twiml_bridges_and_streams_both_tracks(self) -> None:
        xml = TwilioTelephony(self.settings).inbound_twiml("call_123", "+2348022222222")
        self.assertIn("wss://waymark.example/v1/media/call_123", xml)
        self.assertIn('track="both_tracks"', xml)
        self.assertIn("+2348022222222", xml)
        self.assertIn("Waymark disclosure.", xml)

    def test_daily_audio_access_token_rejects_tampering(self) -> None:
        token = create_access_token(
            "secret", "del_123", "call_123", "rider", 4_102_444_800
        )
        grant = validate_access_token("secret", token)
        self.assertIsNotNone(grant)
        self.assertEqual(grant.role, "rider")
        self.assertIsNone(validate_access_token("secret", token + "x"))

    def test_twilio_signature_validation(self) -> None:
        url = "https://waymark.example/v1/telephony/inbound"
        params = {"CallSid": "CA123", "To": "+15550000000"}
        token = "secret"
        material = url + "".join(key + params[key] for key in sorted(params))
        signature = base64.b64encode(
            hmac.new(token.encode(), material.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertTrue(validate_twilio_signature(url, params, signature, token))
        self.assertFalse(validate_twilio_signature(url, params, "bad", token))

    def test_mulaw_conversion_has_expected_shape(self) -> None:
        pcm = mulaw_to_pcm16_16khz(bytes([0xFF]) * 160)
        self.assertEqual(len(pcm), 640)
        self.assertEqual(set(pcm), {0})

    def test_infobip_phone_and_basic_auth(self) -> None:
        self.assertEqual(infobip_phone("+2348012345678"), "2348012345678")
        encoded = base64.b64encode(b"waymark:secret").decode()
        self.assertTrue(
            validate_basic_authorization(
                f"Basic {encoded}", "waymark", "secret"
            )
        )
        self.assertFalse(
            validate_basic_authorization("Basic invalid", "waymark", "secret")
        )

    def test_infobip_pcm_is_resampled_for_sahara(self) -> None:
        pcm_48khz = (b"\x01\x00\x02\x00\x03\x00") * 960
        pcm_16khz = resample_pcm16(pcm_48khz, source_rate=48_000)
        self.assertEqual(len(pcm_16khz), len(pcm_48khz) // 3)

    def test_mapbox_geojson_is_normalized(self) -> None:
        grounder = MapboxGrounder(self.settings)
        results = grounder._mapbox_results(
            {
                "features": [
                    {
                        "id": "poi.1",
                        "geometry": {"coordinates": [3.3859, 6.5157]},
                        "properties": {
                            "mapbox_id": "mbx.1",
                            "name": "Mobil Filling Station",
                            "full_address": "Yaba, Lagos",
                        },
                    }
                ]
            },
            LandmarkPhrase(
                name="Mobil",
                normalized_name="mobil",
                landmark_type="business",
                confidence=0.9,
            ),
            "mapbox_search",
        )
        self.assertEqual(results[0].place_id, "mbx.1")
        self.assertEqual(results[0].location.lng, 3.3859)


class LearningLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_path = Path("data") / f"test-{uuid.uuid4().hex}.db"
        settings = replace(
            Settings.from_env(),
            database_path=self.database_path,
            demo_mode=True,
            mapbox_access_token=None,
        )
        self.store = SQLiteStore(settings.database_path)
        self.events = EventHub(self.store)
        self.pipeline = ResolutionPipeline(settings, self.store, self.events)

    async def asyncTearDown(self) -> None:
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.database_path) + suffix).unlink(missing_ok=True)

    async def test_successful_delivery_teaches_the_next_delivery(self) -> None:
        common = dict(
            rider_ref="rider-1",
            rider_phone="+2348011111111",
            customer_ref="customer-token",
            customer_phone="+2348022222222",
            coarse_location=Coordinate(lat=6.5155, lng=3.3857),
            destination_key="dest-yaba-42",
        )
        first = self.store.create_delivery(
            DeliveryCreate(external_order_id="order-1", **common)
        )
        guidance = await self.pipeline.process_utterance(
            first.id,
            SimulationUtterance(
                transcript=(
                    "Pass the Mobil filling station, take the second right, "
                    "black gate opposite the mosque."
                )
            ),
        )
        self.assertEqual(guidance.status, "resolved")
        outcome = await self.pipeline.complete(
            first.id,
            DeliveryComplete(
                delivered=True, final_location=Coordinate(lat=6.5162, lng=3.3862)
            ),
        )
        self.assertTrue(outcome.learned)
        repeated = await self.pipeline.complete(
            first.id,
            DeliveryComplete(
                delivered=True, final_location=Coordinate(lat=6.5162, lng=3.3862)
            ),
        )
        self.assertEqual(repeated.completed_at, outcome.completed_at)

        second = self.store.create_delivery(
            DeliveryCreate(external_order_id="order-2", **common)
        )
        reused = await self.pipeline.reuse_known_route(second.id)
        self.assertIsNotNone(reused)
        self.assertEqual(reused.source, "human_address_graph")
        self.assertEqual(
            [step.instruction for step in reused.trail],
            [step.instruction for step in guidance.trail],
        )
        first_confidence = self.store.resolve_destination("dest-yaba-42").confidence
        await self.pipeline.complete(
            second.id,
            DeliveryComplete(
                delivered=True, final_location=Coordinate(lat=6.5162, lng=3.3862)
            ),
        )
        same_rider_confidence = self.store.resolve_destination("dest-yaba-42").confidence
        self.assertEqual(same_rider_confidence, first_confidence)

        third_payload = {**common, "rider_ref": "rider-2", "rider_phone": "+2348044444444"}
        third = self.store.create_delivery(
            DeliveryCreate(external_order_id="order-3", **third_payload)
        )
        await self.pipeline.reuse_known_route(third.id)
        await self.pipeline.complete(
            third.id,
            DeliveryComplete(
                delivered=True, final_location=Coordinate(lat=6.5162, lng=3.3862)
            ),
        )
        independent_confidence = self.store.resolve_destination("dest-yaba-42").confidence
        self.assertGreater(independent_confidence, same_rider_confidence)


if __name__ == "__main__":
    unittest.main()
