## Skill: change_booking

1. Verify at booking level. Call `get_booking`.
2. Check the basics before searching: segment not flown or departed (`CH-10`), not inside the cutoff (`CH-5`), fare family allows changes (`CH-1`), unless the look-in option applies (`CH-4`). If not allowed, say why in one sentence and offer the alternative (cancel under `CX`, or a new booking).
3. Ask for the new date or flight. Route changes are cancel and rebook (`CH-9`).
4. Call `search_flights` for the new date, then `quote_change` for the chosen flight. Tell them the change fee, fare difference and total separately (`CH-6`). If the new fare is lower, explain `CH-3`.
5. Changing only some passengers splits the booking. Say so (`CH-7`).
6. On an explicit yes, call `change_booking`. Confirm the new flight, time and amount charged or credited.
7. Name fixes go through `correct_name` under `CH-8`, not this flow.
8. Upgrades to Business: same flight, `quote_change` with `new_fare_family: "BUSINESS"` (`CH-11`).
9. Adding passengers: not possible on an existing booking (`CH-12`); offer a new booking.
