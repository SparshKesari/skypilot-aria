## Skill: cancel_and_refund

1. Verify at booking level. Call `get_booking`.
2. If the flight was cancelled or heavily delayed by the airline, switch to disruption_recovery. Customers should not lose money cancelling a disrupted flight.
3. Call `quote_cancellation` (with `reason: "no_show"` if the flight has departed or the cutoff has passed). Tell them: amount paid, fee, refund amount, refund method and timeline (`CX-1`, `CX-5`). Offer instant SkyWallet credit as an alternative.
4. Partial cancellation of one passenger or segment is fine; quote it the same way (`CX-7`).
5. On an explicit yes, call `cancel_booking` with the chosen refund method. Give the cancellation reference and refund timeline.
6. Refund status questions: call `get_refund_status`. If overdue (`CX-8`), apologise once, create a `refund_overdue` ticket, and offer goodwill (`ES-6`) and a human.
