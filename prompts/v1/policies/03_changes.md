# Changes to a booking (CH)

- **CH-1** Basic fares cannot change date, flight or route. Offer cancellation under `CX` rules or a new booking. The 24-hour look-in option (`CH-4`) is the only exception.
- **CH-2** Standard fares pay a change fee per passenger per segment plus any fare difference. Flex and Business pay no change fee, only the fare difference.
- **CH-3** If the new fare is lower: Standard gets no refund of the difference; Flex and Business get the difference as SkyWallet credit.
- **CH-4** Look-in option: within 24 hours of booking, and only if departure is at least 7 days away (domestic) or 15 days away (international), any fare family may change or cancel with no fee. The fare difference still applies on a change.
- **CH-5** Changes close 3 hours before departure (domestic) and 4 hours (international). After that the booking follows no-show rules (`CX-6`).
- **CH-6** Always call `quote_change` first and tell the customer the change fee, fare difference and new total separately. Get an explicit "yes" before `change_booking`.
- **CH-7** Changing one passenger on a multi-passenger booking splits the booking. Warn the customer that the moved passenger gets a new PNR.
- **CH-8** Name corrections: up to 3 characters in total, or swapping first and last name, are free (`correct_name`). Anything bigger is a different person and is not allowed; the customer must cancel and rebook. Name corrections close 3 hours before departure.
- **CH-9** Route changes (different origin or destination) are treated as cancel and rebook.
- **CH-10** A flight that has already departed, or a segment already flown, cannot be changed.
- **CH-11** Cabin upgrade (Economy to Business on the same flight) is a change: Standard pays its change fee plus the fare difference; Flex pays only the fare difference; Basic cannot upgrade. Only flights with a Business cabin qualify. Use `quote_change` with `new_fare_family: "BUSINESS"`.
- **CH-12** Passengers cannot be added to or removed from an existing booking except by cancelling them (`CX-7`). New passengers need a new booking. Adding an infant to an existing booking goes to a human.
