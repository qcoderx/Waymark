# Customer-care and business demo

Add these variables to Render alongside the existing Daily, Sahara, and database settings:

```env
CARE_AGENT_ENABLED=true
CARE_AGENT_MODEL=gpt-5.6-luna
CARE_DISCLOSURE=Waymark listens to this call to assist both participants and perform approved tasks.
OPENAI_API_KEY=your_server_api_key
```

The startup seed contains these fake records:

- `cust_bank_amina` - banking
- `cust_tel_chidi` - telecom
- `cust_fintech_bisi` - fintech
- `cust_biz_kemi` - business

Create a banking session:

```http
POST /v1/care/sessions
Content-Type: application/json

{
  "vertical": "banking",
  "organization": "Waymark Demo Bank",
  "customer_id": "cust_bank_amina",
  "subject": "Missing card"
}
```

Send text directly during API development:

```http
POST /v1/care/sessions/{session_id}/turns
Content-Type: application/json

{"speaker":"customer","text":"What is my balance and please freeze my missing card."}
```

The balance lookup completes immediately. A card freeze returns an action with
`status=pending_confirmation`; confirm it with the action's unique token:

```http
POST /v1/care/actions/{action_id}/confirm
Content-Type: application/json

{"confirmation_token":"token_from_the_pending_action"}
```

To run the same workflow by voice, call `POST /v1/care/sessions/{session_id}/webrtc` and open
the returned `agent` and `customer` links. Business sessions return `employee` and
`counterparty` links. Both pages display Waymark's latest response and document downloads.

For invoices, the agent can act on a clear spoken request or the application can use the
structured endpoint `POST /v1/care/sessions/{session_id}/invoices`. Generated PDF bytes are
stored in PostgreSQL and downloaded from the artifact URL returned in the response.
