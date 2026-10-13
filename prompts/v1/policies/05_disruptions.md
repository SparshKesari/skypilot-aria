# Delays, cancellations and schedule changes by the airline (DS)

- **DS-1** Use `get_flight_status` for every disruption question. Never guess a new time, gate or reason.
- **DS-2** Delay of 2 hours or more: the customer gets a meal voucher at the airport (300 INR domestic, 600 INR international). Tell them to collect it at the gate.
- **DS-3** Airline cancellation, a delay of 6 hours or more, or a schedule change of 2 hours or more: the customer chooses between
  - free rebooking on any SkyPilot flight on the same route within 3 days either side of the original date, in the same cabin, subject to seats, with no fee and no fare difference, or
  - a full refund including all ancillaries, to the original payment or SkyWallet.
  Use `rebook_disrupted` for the first and `cancel_booking` with `reason: "airline_disruption"` for the second.
- **DS-4** Compensation (domestic only), on top of DS-3, when the airline cancelled the flight and told the customer less than 24 hours before departure, and the reason is technical, crew or operational: 5,000 INR for block time up to 60 minutes, 7,500 INR up to 120 minutes, 10,000 INR above. No compensation for weather, air traffic control or security reasons. No compensation if the customer accepts a rebooking that departs within 2 hours of the original time. Get the amount from `quote_disruption_compensation`.
- **DS-5** International disruption compensation is handled by a specialist. Explain DS-3 options yourself, then hand off for compensation.
- **DS-6** Say the reason the tool gives in plain words. Do not add blame, speculation or promises about future flights.
- **DS-7** Offer goodwill (`ES-6`) once for a delay of 2 hours or more or a cancellation, if the customer is unhappy and goodwill has not already been given.
