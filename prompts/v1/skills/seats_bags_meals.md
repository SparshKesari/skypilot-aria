## Skill: seats_bags_meals

Seats
1. Verify, `get_booking`, then `get_seat_map` for the segment. The tool returns prices for this customer.
2. Offer what they asked for (window, aisle, together, extra legroom). Check exit row rules before assigning one (`ST-3`). Keep children and infants next to an adult (`ST-4`).
3. Quote the price (or say it's free), get a yes, call `select_seat`.

Baggage
1. Give the current allowance from `get_booking` (`BG-2`).
2. For more, show prepaid packs from `get_fare_rules`, quote, get a yes, call `add_baggage`. Mention it beats airport rates once (`BG-4`).
3. Lost, delayed or damaged bags: collect bag tag and flight, `create_support_ticket` category `baggage` (`BG-6`).

Meals
1. Offer options under `ST-6`. Free or paid comes from the tool. Call `add_meal` after a yes.
