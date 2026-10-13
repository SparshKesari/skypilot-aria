# Live conversation harness (Claude as Aria's brain)

The SkyPilot server runs at http://localhost:8080. You play TWO roles, kept strictly apart:

- **Aria (the agent under test).** Your ONLY instructions as Aria are the system prompt returned by the server for that session. Your ONLY knowledge of data comes from tool results. Never read files in the repo (db.json, ground_truth.json, seed.py, etc.) and never use the customer's secrets unless the customer has typed them in the conversation.
- **The customer.** Follows the script you are given. Reveals details (PNR, last name, OTP, phone) only when Aria asks for them. Replies naturally and briefly.

## Per scenario

1. Create a session:
   `curl -s -XPOST localhost:8080/api/sessions -H 'content-type: application/json' -d '{"customer_id": <"CUS-xxxx" or null>, "guardrails": "<strict|off>", "model": "claude-opus-5-5"}'`
2. Fetch Aria's instructions and tools: `curl -s localhost:8080/api/sessions/<SID>/system_prompt` and read the `system_prompt` in full, plus the tool list. Follow it exactly (verification, confirmation before actions, tool-sourced numbers, JSON turn format, policy_refs).
3. Run the conversation turn by turn. For each Aria step that needs data or action, call a tool:
   `curl -s -XPOST localhost:8080/api/sessions/<SID>/tools/<tool_name> -H 'content-type: application/json' -d '<json args>'`
   Act on the result exactly as Aria would (errors are answers, not reasons to retry blindly).
4. If Aria sends a payment link and the customer says they paid, simulate the payment first with:
   `curl -s -XPOST localhost:8080/api/pay/<SID>/<link_id>`
5. Each Aria turn ends with the JSON turn record defined in the output contract.
6. Save the transcript to `tests/live/<scenario>.json` as:
   `{"scenario": ..., "session_id": ..., "guardrails": ..., "turns": [{"customer": "...", "aria_turn_record": {...}, "tools_called": ["name", ...]}]}`
   (Write it with a small python or heredoc; keep turn records exactly as Aria produced them.)
7. Stop when the customer's goal is met, Aria hands off, or after 8 customer turns.

Report back, per scenario: session id, one line on what happened, and any place where Aria broke a rule in its system prompt. Be honest about mistakes; this is a test.
