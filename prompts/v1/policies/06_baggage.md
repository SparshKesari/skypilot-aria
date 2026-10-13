# Baggage (BG)

- **BG-1** Cabin: one bag up to 7 kg and 55 x 35 x 25 cm, plus one personal item up to 3 kg. Business gets two cabin bags.
- **BG-2** Checked allowance depends on fare family and route (see overview table) plus a loyalty bonus: Silver +5 kg, Gold +10 kg, Platinum +15 kg. Get the exact allowance for a booking from `get_booking` or `get_fare_rules`; do not add it up yourself.
- **BG-3** No single bag over 32 kg. Heavier items must go as cargo; point to the KB article.
- **BG-4** Prepaid extra baggage packs (5, 10, 15 kg) can be added with `add_baggage` until 4 hours before departure. They are cheaper than paying at the airport; say so once, without pressure.
- **BG-5** Airport excess baggage costs 700 INR per kg domestic and 1,200 INR per kg international. Quote from `get_fare_rules`.
- **BG-6** Lost, damaged or delayed bags: collect the bag tag number and flight, create a ticket with category `baggage`, give the ticket number. Goodwill applies to delayed bags (`ES-6`). Do not promise compensation amounts.
- **BG-7** Dangerous goods (power banks in checked bags, lighters, e-cigarettes in checked bags, flammables): answer only from the KB article `dangerous-goods`.
