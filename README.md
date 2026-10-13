# SkyPilot Air: flight booking agent (benchmark target)

A realistic airline customer-service agent ("Aria") built to be tested by Agent Testing and run across different LLMs. Everything here is synthetic.

## Run it

Python 3.10+ only, no packages to install.

```bash
cp .env.example .env          # add API keys for the models you want to compare
python agent/server.py        # http://localhost:8080
```

Open http://localhost:8080 for the chat site. In the test console you can:

- log in as any of the 50 users (★ marks users with ground-truth scenarios)
- flip guardrails between strict and off
- reset the data

The trace panel shows every tool call, the policy IDs cited, latency and tokens.

**Model.** `agent/models.json` is the roster; it holds a single entry, Claude Opus 5.5 (`claude-opus-5-5`), called natively through the Anthropic Messages API with `ANTHROPIC_API_KEY`. It is exposed at `/api/claude-opus-5-5/a2a`; `GET /api/models` lists it with readiness. To benchmark another model, add an entry to the roster and a matching provider branch in `agent/llm.py`.

**Model selection is part of the JSON.** Every API request may carry `"model": "<id>"` (ids in `agent/models.json`; `claude-opus` is accepted as an alias). When it is absent, the `default` in `agent/models.json` is used; the chat site never sends one, so it always runs the default. Unknown ids return HTTP 400 with the valid list.

Tests:

```bash
python data/validate.py                         # data integrity
python -m unittest tests/test_tools.py -v       # 28 tool tests against ground_truth.json
NODE_PATH=$(npm root -g) node tests/ui_test.mjs # UI smoke test (needs Playwright)
```

Live prompt test results: `tests/live/RESULTS.md`.

## API for Agent Testing

| Endpoint | Use |
| --- | --- |
| `POST /api/sessions` `{customer_id, model?, guardrails, channel}` | Start a conversation (fresh copy of the data); `model` defaults to `models.json` |
| `POST /api/sessions/{id}/messages` `{text, model?}` | One customer turn; `model` switches the model from this turn on |
| `POST /converse` `{text, session_id?, model?, customer_id?}` | Same, in one call |
| `POST /a2a` `{input, session_id?, customer_id?, model?, guardrails?}` | TestMu AI Agent Testing contract; returns `output: [{role: "assistant", content}]` plus `session_id` to pass back on the next turn. Headers `x-session-id`, `x-customer-id`, `x-model`, `x-guardrails` work too |
| `POST /api/{model_id}/a2a` | Same contract with the model fixed by the URL: `/api/claude-opus-5-5/a2a`. Use this URL in the TestMu endpoint profile |
| `GET /api/models` | Benchmark catalog: every model id, provider, pinned version, readiness (key present) and its endpoint URL |
| `GET /api/sessions/{id}/trace` | All turns, tool calls, SMS/email outbox, handoffs |
| `GET /api/sessions/{id}/db/{collection}?pnr=...` | Read data state after the conversation (ground-truth checks) |
| `POST /api/sessions/{id}/reset` | Restore the original data |
| `POST /api/pay/{id}/{link_id}` | Simulate the customer paying a payment link |
| `GET /api/sessions/{id}/system_prompt` | The exact prompt and tools the model sees |

## What's in this repo

```text
prompts/v1/
  system.md               Main system prompt with placeholders, filled at runtime
  policies/               13 policy files, every rule has an ID (CX-2, ID-5, ...)
  skills/                 11 playbooks the agent follows per task
  channels/chat.md        Chat widget rules
  channels/voice.md       Phone and browser voice rules
  output_contract.md      What every turn must return
  output_schema.json      JSON schema for the turn record
tools/
  tools.json              36 tool definitions (OpenAI function format, works through LiteLLM)
  fare_rules.json         Every number in the policies: fees, cutoffs, allowances, tiers
  rules.py                Deterministic fee, refund, compensation and look-in engine
agent/
  server.py               HTTP server (stdlib) for the API and chat site
  loop.py                 Agent loop: model call, tool calls, JSON turn record, trace
  llm.py                  Model gateway: Anthropic Messages API
  models.json             Model roster (Claude Opus 5.5); pin the version here
  tools_impl.py           All 36 tools over a per-session copy of the data
  store.py                Session store, simulated clock, reset
  assemble_prompt.py      Builds the full system prompt for a chat or voice session
channels/chat/            Chat website (index.html) and simulated payment page
tests/                    Tool tests, UI test, live prompt transcripts and results
data/
  seed.py                 Generates the database (fixed seed, same output every run)
  db.json                 The dummy database
  ground_truth.json       23 known-answer test scenarios with correct outcomes
  auth_test_credentials.json  Logins and test OTPs for all 50 users (testers only)
  validate.py             Integrity checks
  kb/kb_articles.json     30 help-centre articles for RAG
```

## Prompt layer: how it fits together

1. `system.md` defines Aria's role, the 7-step working loop (understand, pick skill, verify, get facts from tools, explain and confirm, act, close), tool rules and final reminders.
2. At conversation start `assemble_prompt.py` inserts the runtime context (simulated now, channel, session, caller ID), all skills, all policies, the channel rules and the output contract.
3. The model sees `tools.json` as its tools. Read tools never change data; action tools need an explicit customer "yes".
4. Every turn returns a JSON record (`output_schema.json`) with the reply, intent, skill, verification level, actions, policy IDs, handoff and injection flags. Testers score this record plus the database state.

Assembled prompt size: about 36k characters (roughly 9k tokens), plus about 6k tokens of tool definitions. Anthropic prompt caching is on.

## Design decisions

- **Money is never computed by the model.** Tools call `rules.py`, which reads `fare_rules.json`. The ground truth is generated by the same engine.
- **Guardrail switch.** Business rules (fees, cutoffs, availability) are always enforced by the tools. Identity checks and the fraud hold are enforced by the tools only with `GUARDRAILS=strict` (public demo); with `off` (benchmark runs) only the model protects private data.
- **Simulated clock.** "Now" is `2026-10-06T10:00:00+05:30` (in `fare_rules.json` and `db.json` meta). Flights exist from 3 Oct to 5 Dec 2026; history goes back 3 years.
- **Per-session data.** Each test session should load a fresh copy of `db.json` (or reset), because bookings, cancellations and refunds change it.

## The data

| Collection | Rows | Notes |
| --- | --- | --- |
| customers | 50 | 12 personas, deep histories (1 to 70 bookings each) |
| airports / routes | 20 / 74 | 15 Indian + DXB, LHR, SIN, BKK, DOH; hubs DEL and BOM |
| flights | ~7,550 | 64-day window, fares per fare family and seats left embedded |
| disruptions | ~100 | delays, cancellations, schedule changes with reasons and notice times |
| bookings | 800 | completed, confirmed, cancelled, no-show |
| booking_passengers / segments / segment_passengers | ~1,100 / ~1,400 / ~2,000 | seats, bags, meals, assistance per passenger per flight |
| payments / refunds | 800 / ~70 | |
| loyalty_accounts / loyalty_transactions | 50 / ~960 | FIFO points with expiry |
| travel_documents, payment_methods, companions | ~80, ~90, ~40 | masked cards only, synthetic passports |
| support_tickets / conversation_history | ~150 / ~370 | memory for the agent |
| wallet_transactions, goodwill_credits | small | |

Run `python data/seed.py && python data/patch_disruptions.py && python data/validate.py` to regenerate and check. The patch adds three long delays (5 h, 6 h, 7 h) on upcoming flights so the DS-3 threshold can be tested with real data.

## Ground truth scenarios (data/ground_truth.json)

Same-name customers, domestic cancellation with compensation, weather cancellation without compensation, 3h delay, points expiring tomorrow, overdue refund, Basic fare change refused, look-in free cancel, look-in not eligible, unaccompanied minor, name corrections (allowed and refused), duplicate booking, expired saved card, fraud-flagged account, expired passport on an international trip, prompt injection inside booking notes, Flex late cancellation, inside-cutoff change, exit row with infant, international cancellation handoff, goodwill already given, authority-claim jailbreak, third party without verification.

Each scenario gives the login (password SkyPilot@123), test OTP, PNR, a sample opening message, the expected outcome (amounts from `rules.py`) and the policy IDs that should appear.
