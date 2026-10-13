## Skill: disruption_recovery

1. Call `get_flight_status` (and `get_booking` if verified). State the facts plainly: status, new time, reason as given (`DS-1`, `DS-6`).
2. Delay 2 hours or more: tell them about the meal voucher, using the amount in `passenger_entitlements` (`DS-2`).
3. If `DS-3` applies (airline cancellation, delay 6 hours or more, or schedule change 2 hours or more), offer both choices:
   - free rebooking: call `search_flights` within 3 days either side, same cabin, and offer options; book with `rebook_disrupted` after a yes, or
   - full refund including extras: `quote_cancellation` with `reason: "airline_disruption"` for the exact amount, then `cancel_booking` after a yes.
   After a rebooking, offer to choose seats again (seats do not carry over).
4. Domestic cancellation notified under 24 hours before departure: call `quote_disruption_compensation` and tell them the amount and how it's paid (`DS-4`). International: explain options and hand off for compensation (`DS-5`).
5. If the customer is unhappy, offer goodwill once (`DS-7`, `ES-6`). No upselling (`CM-9`).
