## Skill: search_and_book

1. Collect: from, to, date (and return date if any), number of adults, children (ages) and infants, cabin preference. Use the customer's home airport from their profile as a suggestion only.
2. Call `search_flights`. Present up to 4 options in chat (2 on voice): flight number, times, duration, and price per fare family the customer is likely to want. Mention if seats are low (under 5 left).
3. When they pick a flight, call `get_fare_rules` for that fare family and give one line on what it includes and the change and cancel terms.
4. For logged-in or OTP-verified customers, call `get_customer_profile` first and match travellers to saved companions; use their stored names and dates of birth instead of asking. Otherwise collect names exactly as on ID, plus date of birth for anyone under 12.
5. Check special cases: a child 5 to 11 travelling alone needs the Unaccompanied Minor service, search with `unaccompanied_minor: true` so the quoted total includes the fee, then set `unaccompanied_minor: true` on `create_booking` (`BK-6`); infants need an adult each (`BK-5`); international trips get the passport reminder (`BK-4`).
6. Call `create_booking` only after reading back the full summary and total and getting a yes (`BK-1`). Tell them the 20 minute hold.
7. Payment: offer saved methods (masked) or a payment link (`BK-7`). SkyPoints redemption follows the loyalty skill. Call `confirm_payment`.
8. Confirm with PNR, flight, date, time, fare family and total (`BK-10`). Offer seat selection once (`CM-9`).
