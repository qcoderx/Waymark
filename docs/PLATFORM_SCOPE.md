# Waymark platform scope

Waymark converts a shared conversation into observable, controlled work. A conversation can
belong to delivery, banking, telecom, fintech, business, or a later vertical. The core owns the
audio room, speaker attribution, transcript, intent selection, action policy, artifacts, and
audit history. A vertical contributes customer data, tools, and rules.

## Core loop

1. Two participants join a private Daily audio room with separate role grants.
2. Their microphone streams remain speaker-labeled and are transcribed by Sahara.
3. Customer-care audio is committed in short segments so the agent can act before the call ends.
4. The current session, attached sandbox customer, and recent messages are sent to the OpenAI
   Responses API with a strict function-tool catalog.
5. Waymark validates tool arguments and executes tools against its own application code.
6. Read and low-risk work can complete immediately. Sensitive state changes create a pending
   action with a unique confirmation token.
7. Replies, actions, cases, and artifacts are written to the shared timeline. Both call pages
   see new results and can download generated documents.

OpenAI selects a tool but never directly accesses the database or filesystem. The application
executes every tool and records its parameters, status, result, risk level, and timestamp. The
API uses `store=false` for model responses. The initial records are explicitly fake and are safe
for product demonstrations.

## Included sandbox verticals

| Vertical | Seed record | Read tools | Action tools |
| --- | --- | --- | --- |
| Banking | Amina Yusuf | Balance, account, KYC, card state | Open case, request card freeze |
| Telecom | Chidi Okafor | Plan, airtime, data, SIM state | Open case, request line suspension |
| Fintech | Bisi Adeyemi | Wallet, tier, limits, transactions | Open case |
| Business | Kemi Bello | Company and billing profile | Generate invoice PDF |

`freeze_card` and `suspend_line` require confirmation. Money movement, refunds, lending,
identity changes, and irreversible external operations are not part of this sandbox.

## API objects

- `care_sessions` stores vertical, organization, attached customer, subject, and status.
- `care_messages` stores both human speakers and Waymark responses.
- `care_actions` is the durable execution ledger and confirmation boundary.
- `care_cases` stores created support cases.
- `care_artifacts` stores generated file bytes in PostgreSQL for durable downloads on Render.
- `care_calls` maps a session to its private Daily room.

## Adding a real organization

Replace the seeded customer adapter with an authenticated organization connector and expose only
the fields needed for a tool. Each new action must define its JSON schema, executor, risk level,
confirmation rule, idempotency key, and audit result. Production deployments also need staff and
customer authentication, role-based data access, consent and retention controls, organization
tenant isolation, and connector-specific rate limits.
