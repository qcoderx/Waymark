from __future__ import annotations

import base64
import io
import json
import re
import uuid
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .config import Settings
from .store import ConflictError, NotFoundError, SQLiteStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CareVertical(StrEnum):
    BANKING = "banking"
    TELECOM = "telecom"
    FINTECH = "fintech"
    BUSINESS = "business"
    GENERAL = "general"


class CareCustomer(BaseModel):
    id: str
    vertical: CareVertical
    name: str
    phone: str
    email: str
    organization: str
    status: str
    data: dict[str, Any]


class CareSessionCreate(BaseModel):
    vertical: CareVertical
    organization: str = Field(min_length=2, max_length=120)
    customer_id: str | None = None
    subject: str = Field(min_length=2, max_length=240)


class CareSession(BaseModel):
    id: str
    vertical: CareVertical
    organization: str
    customer_id: str | None
    subject: str
    status: str
    created_at: datetime
    updated_at: datetime


class CareTurnCreate(BaseModel):
    speaker: Literal["customer", "agent", "employee", "counterparty"]
    text: str = Field(min_length=1, max_length=8000)


class CareMessage(BaseModel):
    id: str
    session_id: str
    speaker: str
    text: str
    created_at: datetime


class CareAction(BaseModel):
    id: str
    session_id: str
    customer_id: str | None
    action_type: str
    status: str
    risk_level: str
    requires_confirmation: bool
    confirmation_token: str | None = None
    parameters: dict[str, Any]
    result: dict[str, Any]
    created_at: datetime
    executed_at: datetime | None = None


class CareArtifact(BaseModel):
    id: str
    session_id: str
    artifact_type: str
    file_name: str
    mime_type: str
    download_url: str
    created_at: datetime


class CareTurnResult(BaseModel):
    session_id: str
    reply: str
    actions: list[CareAction]
    artifacts: list[CareArtifact]


class CareTimeline(BaseModel):
    session: CareSession
    customer: CareCustomer | None
    messages: list[CareMessage]
    actions: list[CareAction]
    artifacts: list[CareArtifact]


class CareCallLinks(BaseModel):
    session_id: str
    call_id: str
    provider: str = "daily"
    links: dict[str, str]
    expires_at: datetime


class CareCallJoin(BaseModel):
    room_url: str
    meeting_token: str
    audio_websocket_url: str
    role: str
    disclosure: str
    expires_at: datetime


class ActionConfirmation(BaseModel):
    confirmation_token: str = Field(min_length=12)


class InvoiceLine(BaseModel):
    description: str = Field(min_length=3, max_length=300)
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        normalized = " ".join(value.split())
        meaningful = {
            character.casefold() for character in normalized if character.isalnum()
        }
        letter_count = sum(character.isalpha() for character in normalized)
        if len(meaningful) < 3 or letter_count < 3:
            raise ValueError("describe the actual product or service being invoiced")
        return normalized


class InvoiceCreate(BaseModel):
    seller: str = Field(min_length=1, max_length=200)
    buyer: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    items: list[InvoiceLine] = Field(min_length=1, max_length=50)
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        return normalized


CARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS care_customers (
    id TEXT PRIMARY KEY,
    vertical TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT NOT NULL,
    organization TEXT NOT NULL,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS care_sessions (
    id TEXT PRIMARY KEY,
    vertical TEXT NOT NULL,
    organization TEXT NOT NULL,
    customer_id TEXT REFERENCES care_customers(id),
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS care_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(id),
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS care_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(id),
    customer_id TEXT REFERENCES care_customers(id),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    requires_confirmation INTEGER NOT NULL,
    confirmation_token TEXT,
    parameters_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    executed_at TEXT
);
CREATE TABLE IF NOT EXISTS care_cases (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(id),
    customer_id TEXT REFERENCES care_customers(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS care_artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(id),
    artifact_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content_base64 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS care_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(id),
    provider_call_id TEXT NOT NULL UNIQUE,
    room_url TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_care_messages_session ON care_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_care_actions_session ON care_actions(session_id, created_at);
"""


SEED_CUSTOMERS = (
    (
        "cust_bank_amina",
        "banking",
        "Amina Yusuf",
        "+2348010001001",
        "amina@example.test",
        "Waymark Demo Bank",
        "active",
        {
            "account_number": "0123456789",
            "account_type": "Business Current",
            "balance": 485250.75,
            "currency": "NGN",
            "kyc_tier": 3,
            "card_status": "active",
            "last_transaction": "POS - Iya Basira Foods - NGN 8,500",
        },
    ),
    (
        "cust_tel_chidi",
        "telecom",
        "Chidi Okafor",
        "+2348030002002",
        "chidi@example.test",
        "Waymark Demo Mobile",
        "active",
        {
            "plan": "Everyday Plus",
            "airtime_balance": 2300.5,
            "data_balance_mb": 4872,
            "sim_status": "active",
            "last_recharge": "NGN 2,000 on 2026-09-10",
        },
    ),
    (
        "cust_fintech_bisi",
        "fintech",
        "Bisi Adeyemi",
        "+2348050003003",
        "bisi@example.test",
        "Waymark Demo Pay",
        "active",
        {
            "wallet_id": "WMP-203944",
            "wallet_balance": 92750.0,
            "currency": "NGN",
            "verification_tier": 2,
            "daily_transfer_limit": 200000.0,
            "last_transaction": "Transfer received - NGN 25,000",
        },
    ),
    (
        "cust_biz_kemi",
        "business",
        "Kemi Bello",
        "+2348070004004",
        "kemi@example.test",
        "Bello Creative Studio",
        "active",
        {
            "company": "Bello Creative Studio",
            "tax_id": "TIN-DEMO-88401",
            "default_currency": "NGN",
            "billing_address": "12 Demo Street, Lagos",
        },
    ),
)


class CareRepository:
    def __init__(self, store: SQLiteStore, public_base_url: str) -> None:
        self.store = store
        self.public_base_url = public_base_url.rstrip("/")
        self._create_schema()
        self._seed()

    def _create_schema(self) -> None:
        statements = [item.strip() for item in CARE_SCHEMA.split(";") if item.strip()]
        with self.store._tx() as db:
            for statement in statements:
                db.execute(statement)

    def _seed(self) -> None:
        with self.store._tx() as db:
            for customer in SEED_CUSTOMERS:
                db.execute(
                    """
                    INSERT OR IGNORE INTO care_customers(
                        id, vertical, name, phone, email, organization, status,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*customer[:7], json.dumps(customer[7]), _iso()),
                )

    @staticmethod
    def _customer(row: Any) -> CareCustomer:
        item = dict(row)
        return CareCustomer(
            id=item["id"],
            vertical=item["vertical"],
            name=item["name"],
            phone=item["phone"],
            email=item["email"],
            organization=item["organization"],
            status=item["status"],
            data=json.loads(item["data_json"]),
        )

    @staticmethod
    def _session(row: Any) -> CareSession:
        item = dict(row)
        return CareSession(
            **{key: item[key] for key in (
                "id", "vertical", "organization", "customer_id", "subject", "status"
            )},
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
        )

    @staticmethod
    def _message(row: Any) -> CareMessage:
        item = dict(row)
        return CareMessage(
            id=item["id"],
            session_id=item["session_id"],
            speaker=item["speaker"],
            text=item["text"],
            created_at=datetime.fromisoformat(item["created_at"]),
        )

    @staticmethod
    def _action(row: Any) -> CareAction:
        item = dict(row)
        return CareAction(
            id=item["id"],
            session_id=item["session_id"],
            customer_id=item["customer_id"],
            action_type=item["action_type"],
            status=item["status"],
            risk_level=item["risk_level"],
            requires_confirmation=bool(item["requires_confirmation"]),
            confirmation_token=item["confirmation_token"],
            parameters=json.loads(item["parameters_json"]),
            result=json.loads(item["result_json"]),
            created_at=datetime.fromisoformat(item["created_at"]),
            executed_at=(
                datetime.fromisoformat(item["executed_at"])
                if item["executed_at"]
                else None
            ),
        )

    def _artifact(self, row: Any) -> CareArtifact:
        item = dict(row)
        return CareArtifact(
            id=item["id"],
            session_id=item["session_id"],
            artifact_type=item["artifact_type"],
            file_name=item["file_name"],
            mime_type=item["mime_type"],
            download_url=f"{self.public_base_url}/v1/care/artifacts/{item['id']}/download",
            created_at=datetime.fromisoformat(item["created_at"]),
        )

    def list_customers(
        self, *, vertical: str | None = None, query: str | None = None
    ) -> list[CareCustomer]:
        clauses: list[str] = []
        params: list[str] = []
        if vertical:
            clauses.append("vertical = ?")
            params.append(vertical)
        if query:
            clauses.append("LOWER(name || ' ' || phone || ' ' || email) LIKE ?")
            params.append(f"%{query.lower()}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.store._lock:
            rows = self.store._connection.execute(
                f"SELECT * FROM care_customers{where} ORDER BY name", params
            ).fetchall()
        return [self._customer(row) for row in rows]

    def get_customer(self, customer_id: str) -> CareCustomer:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_customers WHERE id = ?", (customer_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"customer {customer_id!r} was not found")
        return self._customer(row)

    def update_customer_data(self, customer_id: str, updates: dict[str, Any]) -> CareCustomer:
        customer = self.get_customer(customer_id)
        data = {**customer.data, **updates}
        with self.store._tx() as db:
            db.execute(
                "UPDATE care_customers SET data_json = ? WHERE id = ?",
                (json.dumps(data), customer_id),
            )
        return self.get_customer(customer_id)

    def create_session(self, payload: CareSessionCreate) -> CareSession:
        if payload.customer_id:
            customer = self.get_customer(payload.customer_id)
            if customer.vertical != payload.vertical:
                raise ConflictError("customer does not belong to the selected vertical")
        session_id = _id("care")
        now = _iso()
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_sessions(
                    id, vertical, organization, customer_id, subject, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_id,
                    payload.vertical,
                    payload.organization,
                    payload.customer_id,
                    payload.subject,
                    now,
                    now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> CareSession:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"care session {session_id!r} was not found")
        return self._session(row)

    def add_message(self, session_id: str, speaker: str, text: str) -> CareMessage:
        self.get_session(session_id)
        message_id = _id("msg")
        now = _iso()
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_messages(id, session_id, speaker, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, session_id, speaker, text, now),
            )
            db.execute(
                "UPDATE care_sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._message(row)

    def messages(self, session_id: str, limit: int = 50) -> list[CareMessage]:
        self.get_session(session_id)
        with self.store._lock:
            rows = self.store._connection.execute(
                """
                SELECT * FROM care_messages WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._message(row) for row in reversed(rows)]

    def create_action(
        self,
        session_id: str,
        action_type: str,
        parameters: dict[str, Any],
        *,
        customer_id: str | None,
        risk_level: str,
        requires_confirmation: bool,
    ) -> CareAction:
        self.get_session(session_id)
        action_id = _id("act")
        token = uuid.uuid4().hex if requires_confirmation else None
        status = "pending_confirmation" if requires_confirmation else "running"
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_actions(
                    id, session_id, customer_id, action_type, status, risk_level,
                    requires_confirmation, confirmation_token, parameters_json,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    action_id,
                    session_id,
                    customer_id,
                    action_type,
                    status,
                    risk_level,
                    int(requires_confirmation),
                    token,
                    json.dumps(parameters),
                    _iso(),
                ),
            )
        return self.get_action(action_id)

    def complete_action(
        self, action_id: str, result: dict[str, Any], *, status: str = "completed"
    ) -> CareAction:
        self.get_action(action_id)
        with self.store._tx() as db:
            db.execute(
                """
                UPDATE care_actions SET status = ?, result_json = ?, executed_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(result), _iso(), action_id),
            )
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> CareAction:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"care action {action_id!r} was not found")
        return self._action(row)

    def actions(self, session_id: str) -> list[CareAction]:
        self.get_session(session_id)
        with self.store._lock:
            rows = self.store._connection.execute(
                "SELECT * FROM care_actions WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [self._action(row) for row in rows]

    def create_case(
        self,
        session_id: str,
        customer_id: str | None,
        title: str,
        description: str,
        priority: str,
    ) -> dict[str, Any]:
        case_id = _id("case")
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_cases(
                    id, session_id, customer_id, title, description,
                    priority, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (case_id, session_id, customer_id, title, description, priority, _iso()),
            )
        return {"case_id": case_id, "status": "open", "priority": priority}

    def create_artifact(
        self,
        session_id: str,
        artifact_type: str,
        file_name: str,
        mime_type: str,
        content: bytes,
    ) -> CareArtifact:
        artifact_id = _id("file")
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_artifacts(
                    id, session_id, artifact_type, file_name, mime_type,
                    content_base64, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    session_id,
                    artifact_type,
                    file_name,
                    mime_type,
                    base64.b64encode(content).decode(),
                    _iso(),
                ),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> CareArtifact:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"artifact {artifact_id!r} was not found")
        return self._artifact(row)

    def artifact_content(self, artifact_id: str) -> tuple[CareArtifact, bytes]:
        artifact = self.get_artifact(artifact_id)
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT content_base64 FROM care_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return artifact, base64.b64decode(row["content_base64"])

    def artifacts(self, session_id: str) -> list[CareArtifact]:
        self.get_session(session_id)
        with self.store._lock:
            rows = self.store._connection.execute(
                "SELECT * FROM care_artifacts WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def timeline(self, session_id: str) -> CareTimeline:
        session = self.get_session(session_id)
        customer = self.get_customer(session.customer_id) if session.customer_id else None
        return CareTimeline(
            session=session,
            customer=customer,
            messages=self.messages(session_id),
            actions=self.actions(session_id),
            artifacts=self.artifacts(session_id),
        )

    def ensure_call(
        self, session_id: str, provider_call_id: str, room_url: str
    ) -> dict[str, Any]:
        self.get_session(session_id)
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_calls WHERE provider_call_id = ?",
                (provider_call_id,),
            ).fetchone()
        if row:
            return dict(row)
        call_id = _id("carecall")
        with self.store._tx() as db:
            db.execute(
                """
                INSERT INTO care_calls(
                    id, session_id, provider_call_id, room_url, status, created_at
                ) VALUES (?, ?, ?, ?, 'ready', ?)
                """,
                (call_id, session_id, provider_call_id, room_url, _iso()),
            )
        return self.get_call(call_id)

    def get_call(self, call_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store._connection.execute(
                "SELECT * FROM care_calls WHERE id = ?", (call_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"care call {call_id!r} was not found")
        return dict(row)


class InvoiceDocument:
    @staticmethod
    def render(invoice: InvoiceCreate, invoice_number: str) -> bytes:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=18 * mm,
            leftMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=f"Invoice {invoice_number}",
        )
        styles = getSampleStyleSheet()
        ink = colors.HexColor("#171716")
        yellow = colors.HexColor("#F7C94B")
        red = colors.HexColor("#E9483F")
        muted = colors.HexColor("#625C56")
        story: list[Any] = []
        heading = ParagraphStyle(
            "InvoiceHeading",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=30,
            leading=32,
            textColor=ink,
            spaceAfter=4 * mm,
        )
        right = ParagraphStyle(
            "Right",
            parent=styles["BodyText"],
            alignment=TA_RIGHT,
            textColor=muted,
            fontSize=9,
            leading=13,
        )
        label = ParagraphStyle(
            "Label",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            textColor=red,
            fontSize=8,
            leading=10,
            spaceAfter=2 * mm,
        )
        body = ParagraphStyle(
            "InvoiceBody",
            parent=styles["BodyText"],
            textColor=ink,
            fontSize=10,
            leading=14,
        )
        header = Table(
            [
                [
                    Paragraph("WAYMARK", label),
                    Paragraph(
                        f"{invoice_number}<br/>{_now().date().isoformat()}", right
                    ),
                ],
                [Paragraph("INVOICE", heading), ""],
            ],
            colWidths=[115 * mm, 55 * mm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F6F1E5")),
                    ("BOX", (0, 0), (-1, -1), 0, colors.white),
                    ("LINEABOVE", (0, 0), (-1, 0), 5, yellow),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
                ]
            )
        )
        story.extend([header, Spacer(1, 9 * mm)])
        due = invoice.due_date.isoformat() if invoice.due_date else "Due on receipt"
        currency_names = {
            "NGN": "Nigerian naira (NGN)",
            "USD": "US dollar (USD)",
            "GBP": "British pound (GBP)",
            "EUR": "Euro (EUR)",
        }
        currency_label = currency_names.get(invoice.currency, invoice.currency)
        parties = Table(
            [
                [Paragraph("FROM", label), Paragraph("BILL TO", label)],
                [Paragraph(invoice.seller, body), Paragraph(invoice.buyer, body)],
                [Paragraph("CURRENCY", label), Paragraph("DUE DATE", label)],
                [Paragraph(currency_label, body), Paragraph(due, body)],
            ],
            colWidths=[85 * mm, 85 * mm],
        )
        parties.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.HexColor("#D7DFE2")),
                    ("TOPPADDING", (0, 2), (-1, -1), 5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ]
            )
        )
        story.extend([parties, Spacer(1, 10 * mm)])
        table_data: list[list[Any]] = [["DESCRIPTION", "QTY", "RATE", "AMOUNT"]]
        total = 0.0
        for item in invoice.items:
            amount = item.quantity * item.unit_price
            total += amount
            table_data.append(
                [
                    Paragraph(item.description, body),
                    f"{item.quantity:g}",
                    f"{invoice.currency} {item.unit_price:,.2f}",
                    f"{invoice.currency} {amount:,.2f}",
                ]
            )
        table_data.append(["", "", "TOTAL", f"{invoice.currency} {total:,.2f}"])
        items = Table(table_data, colWidths=[86 * mm, 18 * mm, 32 * mm, 34 * mm])
        items.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), ink),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (2, -1), (-1, -1), yellow),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.5, colors.HexColor("#D7DFE2")),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ]
            )
        )
        story.append(items)
        if invoice.notes:
            story.extend(
                [
                    Spacer(1, 9 * mm),
                    Paragraph("NOTES", label),
                    Paragraph(invoice.notes, body),
                ]
            )
        story.extend(
            [
                Spacer(1, 16 * mm),
                Paragraph(
                    "Generated during a shared Waymark conversation. "
                    "Review payment details before settlement.",
                    ParagraphStyle(
                        "Footer",
                        parent=body,
                        textColor=muted,
                        fontSize=8,
                        leading=11,
                    ),
                ),
            ]
        )
        doc.build(story)
        return buffer.getvalue()


TOOLS = [
    {
        "type": "function",
        "name": "lookup_customer",
        "description": "Find fake sandbox customers by name, phone, or email.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "vertical": {
                    "type": ["string", "null"],
                    "enum": ["banking", "telecom", "fintech", "business", None],
                },
            },
            "required": ["query", "vertical"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_customer_profile",
        "description": "Read the attached fake customer's current account or service data.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": ["string", "null"]}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "open_support_case",
        "description": "Open a support case for investigation or follow-up.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["title", "description", "priority"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "freeze_card",
        "description": "Request card freezing. This always requires explicit confirmation.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": ["string", "null"]},
                "reason": {"type": "string"},
            },
            "required": ["customer_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "suspend_line",
        "description": "Request telecom line suspension. Requires explicit confirmation.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": ["string", "null"]},
                "reason": {"type": "string"},
            },
            "required": ["customer_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "create_invoice",
        "description": "Create a downloadable PDF invoice requested by the participants.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "seller": {"type": "string"},
                "buyer": {"type": "string"},
                "currency": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["description", "quantity", "unit_price"],
                        "additionalProperties": False,
                    },
                },
                "due_date": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["seller", "buyer", "currency", "items", "due_date", "notes"],
            "additionalProperties": False,
        },
    },
]


class CareAgent:
    def __init__(self, settings: Settings, repository: CareRepository) -> None:
        self.settings = settings
        self.repository = repository

    async def process_turn(self, session_id: str, turn: CareTurnCreate) -> CareTurnResult:
        session = self.repository.get_session(session_id)
        self.repository.add_message(session_id, turn.speaker, turn.text)
        calls: list[tuple[str, dict[str, Any]]] = []
        reply = ""
        if self.settings.care_agent_enabled and self.settings.openai_api_key:
            try:
                calls, reply = await self._choose_tools(session)
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                calls, reply = self._fallback(session, turn.text)
        else:
            calls, reply = self._fallback(session, turn.text)
        actions: list[CareAction] = []
        artifacts: list[CareArtifact] = []
        summaries: list[str] = []
        for name, arguments in calls[:3]:
            action, artifact, summary = self._execute(session, name, arguments)
            if action:
                actions.append(action)
            if artifact:
                artifacts.append(artifact)
            if summary:
                summaries.append(summary)
        if summaries:
            reply = " ".join(summaries)
        if not reply:
            reply = "I heard the request, but I need one more detail before I can act."
        self.repository.add_message(session_id, "waymark", reply)
        return CareTurnResult(
            session_id=session_id,
            reply=reply,
            actions=actions,
            artifacts=artifacts,
        )

    async def _choose_tools(
        self, session: CareSession
    ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
        customer = (
            self.repository.get_customer(session.customer_id) if session.customer_id else None
        )
        messages = self.repository.messages(session.id, limit=12)
        context = {
            "session": session.model_dump(mode="json"),
            "attached_customer": customer.model_dump(mode="json") if customer else None,
            "conversation": [message.model_dump(mode="json") for message in messages],
        }
        instructions = (
            "You are Waymark, an action agent inside a shared customer-care or business "
            "conversation. This is a fake sandbox database. Use tools for factual customer "
            "data and actions. Never claim an action happened unless you call its tool. "
            "Card freezing and line suspension require confirmation; tell the user when an "
            "action is pending. Create an invoice when both parties have supplied seller, "
            "buyer, description, quantity, and price. Do not perform transfers, refunds, "
            "loans, or identity changes; open a support case instead. Keep replies concise."
        )
        body = {
            "model": self.settings.care_agent_model,
            "instructions": instructions,
            "input": json.dumps(context),
            "tools": TOOLS,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_output_tokens": 500,
            "store": False,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.openai_base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        payload = response.json()
        calls: list[tuple[str, dict[str, Any]]] = []
        text_parts: list[str] = []
        for item in payload.get("output", []):
            if item.get("type") == "function_call":
                calls.append((item["name"], json.loads(item.get("arguments", "{}"))))
            elif item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_parts.append(content.get("text", ""))
        return calls, " ".join(text_parts).strip()

    def _fallback(
        self, session: CareSession, text: str
    ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
        lowered = text.lower()
        customer_id = session.customer_id
        if "invoice" in lowered:
            amount_match = re.search(r"(?:ngn|₦)?\s*([\d,]+(?:\.\d{1,2})?)", lowered)
            amount = float(amount_match.group(1).replace(",", "")) if amount_match else 0.0
            buyer = (
                self.repository.get_customer(customer_id).name
                if customer_id
                else "Customer"
            )
            return [
                (
                    "create_invoice",
                    {
                        "seller": session.organization,
                        "buyer": buyer,
                        "currency": "NGN",
                        "items": [
                            {
                                "description": "Services discussed",
                                "quantity": 1,
                                "unit_price": amount,
                            }
                        ],
                        "due_date": None,
                        "notes": "Generated from the Waymark conversation.",
                    },
                )
            ], ""
        if "freeze" in lowered and "card" in lowered:
            return [("freeze_card", {"customer_id": customer_id, "reason": text})], ""
        if ("suspend" in lowered or "block" in lowered) and "line" in lowered:
            return [("suspend_line", {"customer_id": customer_id, "reason": text})], ""
        if any(word in lowered for word in ("balance", "plan", "data", "account", "wallet")):
            return [("get_customer_profile", {"customer_id": customer_id})], ""
        if any(word in lowered for word in ("complaint", "issue", "case", "failed")):
            return [
                (
                    "open_support_case",
                    {"title": session.subject, "description": text, "priority": "normal"},
                )
            ], ""
        return [], (
            "I can look up the customer, open a case, protect an account, "
            "or create an invoice."
        )

    def _customer_id(self, session: CareSession, arguments: dict[str, Any]) -> str | None:
        return arguments.get("customer_id") or session.customer_id

    def _execute(
        self, session: CareSession, name: str, arguments: dict[str, Any]
    ) -> tuple[CareAction | None, CareArtifact | None, str]:
        customer_id = self._customer_id(session, arguments)
        if name == "lookup_customer":
            customers = self.repository.list_customers(
                vertical=arguments.get("vertical"), query=arguments.get("query")
            )
            action = self.repository.create_action(
                session.id,
                name,
                arguments,
                customer_id=None,
                risk_level="read",
                requires_confirmation=False,
            )
            result = {"matches": [item.model_dump(mode="json") for item in customers[:5]]}
            action = self.repository.complete_action(action.id, result)
            return action, None, f"I found {len(customers)} matching customer record(s)."
        if name == "get_customer_profile":
            if not customer_id:
                return None, None, "Attach a customer before requesting account information."
            customer = self.repository.get_customer(customer_id)
            action = self.repository.create_action(
                session.id,
                name,
                arguments,
                customer_id=customer_id,
                risk_level="read",
                requires_confirmation=False,
            )
            action = self.repository.complete_action(
                action.id, {"customer": customer.model_dump(mode="json")}
            )
            facts = ", ".join(f"{key}: {value}" for key, value in customer.data.items())
            return action, None, f"{customer.name}'s current record shows {facts}."
        if name == "open_support_case":
            action = self.repository.create_action(
                session.id,
                name,
                arguments,
                customer_id=customer_id,
                risk_level="low",
                requires_confirmation=False,
            )
            case = self.repository.create_case(
                session.id,
                customer_id,
                arguments["title"],
                arguments["description"],
                arguments["priority"],
            )
            action = self.repository.complete_action(action.id, case)
            return action, None, f"Support case {case['case_id']} is open."
        if name in {"freeze_card", "suspend_line"}:
            if not customer_id:
                return None, None, "Attach a customer before requesting this action."
            action = self.repository.create_action(
                session.id,
                name,
                arguments,
                customer_id=customer_id,
                risk_level="high",
                requires_confirmation=True,
            )
            label = "card freeze" if name == "freeze_card" else "line suspension"
            return action, None, f"The {label} is ready and needs explicit confirmation."
        if name == "create_invoice":
            arguments = dict(arguments)
            raw_due_date = arguments.get("due_date")
            if raw_due_date:
                normalized_due_date: date | None = None
                for format_string in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
                    try:
                        normalized_due_date = datetime.strptime(
                            str(raw_due_date), format_string
                        ).date()
                        break
                    except ValueError:
                        continue
                arguments["due_date"] = (
                    normalized_due_date.isoformat() if normalized_due_date else None
                )
            arguments["currency"] = str(arguments.get("currency") or "NGN").upper()
            invoice = InvoiceCreate.model_validate(arguments)
            action = self.repository.create_action(
                session.id,
                name,
                invoice.model_dump(mode="json"),
                customer_id=customer_id,
                risk_level="low",
                requires_confirmation=False,
            )
            invoice_number = f"WM-{_now():%Y%m%d}-{action.id[-6:].upper()}"
            content = InvoiceDocument.render(invoice, invoice_number)
            artifact = self.repository.create_artifact(
                session.id,
                "invoice",
                f"invoice-{invoice_number}.pdf",
                "application/pdf",
                content,
            )
            action = self.repository.complete_action(
                action.id,
                {
                    "invoice_number": invoice_number,
                    "artifact_id": artifact.id,
                    "download_url": artifact.download_url,
                },
            )
            return action, artifact, f"Invoice {invoice_number} is ready to download."
        return None, None, f"The requested tool {name} is not available."

    def create_invoice(self, session_id: str, invoice: InvoiceCreate) -> CareTurnResult:
        session = self.repository.get_session(session_id)
        action, artifact, reply = self._execute(
            session, "create_invoice", invoice.model_dump(mode="json")
        )
        self.repository.add_message(session_id, "waymark", reply)
        return CareTurnResult(
            session_id=session_id,
            reply=reply,
            actions=[action] if action else [],
            artifacts=[artifact] if artifact else [],
        )

    def confirm(self, action_id: str, confirmation_token: str) -> CareAction:
        action = self.repository.get_action(action_id)
        if action.status != "pending_confirmation":
            raise ConflictError("action is not awaiting confirmation")
        if action.confirmation_token != confirmation_token:
            raise ConflictError("confirmation token is invalid")
        if action.action_type == "freeze_card" and action.customer_id:
            customer = self.repository.update_customer_data(
                action.customer_id, {"card_status": "frozen"}
            )
            result = {"card_status": customer.data["card_status"]}
        elif action.action_type == "suspend_line" and action.customer_id:
            customer = self.repository.update_customer_data(
                action.customer_id, {"sim_status": "suspended"}
            )
            result = {"sim_status": customer.data["sim_status"]}
        else:
            raise ConflictError("this action cannot be confirmed")
        return self.repository.complete_action(action.id, result)
