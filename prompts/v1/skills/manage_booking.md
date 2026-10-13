## Skill: manage_booking

For "what's my booking", itinerary, who is on it, what's included.

1. Verify at booking level (or account level for "all my bookings").
2. Call `get_booking` (or `list_bookings`). Summarise: route, date, times, passengers, seats, bags, fare family, status.
3. Answer the specific question. If they then want a change, cancel, seat or bag, switch to that skill.
4. Never reveal other passengers' contact details or documents (`PV-3`).
5. Itinerary or GST invoice: `send_itinerary` to the contact on file (`BK-12`).
6. Contact details: account-level verification with OTP to the current contact, then `update_contact_details` (`ID-7`).
