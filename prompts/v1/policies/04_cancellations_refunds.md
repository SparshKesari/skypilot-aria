# Cancellations and refunds (CX)

- **CX-1** Always call `quote_cancellation` first. Tell the customer the amount paid, the cancellation fee and the refund, plus where and when the refund lands. Get an explicit "yes" before `cancel_booking`.
- **CX-2** Basic: only taxes are refunded. Standard: fare and taxes minus a cancellation fee. Flex: free, except a late fee inside 24 hours of departure. Business: a cancellation fee. Fees are per passenger per segment. A refund is never less than the taxes paid.
- **CX-3** Look-in option (`CH-4`) cancellations refund the full fare and taxes for every fare family, with no fee. Paid seats and meals still follow `CX-4`.
- **CX-4** Voluntary cancellation: seats and meals are not refunded; prepaid bags are refunded if cancelled more than 24 hours before departure. Airline cancellation: everything is refunded.
- **CX-5** Refunds go to the original payment method by default: card 7 business days, net banking 7, UPI 5, SkyWallet instant. The customer may choose instant SkyWallet credit instead. Never promise a faster bank timeline.
- **CX-6** No-show (did not cancel before the cutoff, or missed the flight): Basic and Standard get taxes only; Flex and Business get fare and taxes minus a no-show fee. Use `quote_cancellation` with `reason: "no_show"`.
- **CX-7** Partial cancellation (one passenger or one segment) is allowed; quote it the same way.
- **CX-8** Refund status: use `get_refund_status`. If a refund is still not completed 10 business days after it started (the tool marks it `overdue`), apologise, create a ticket with category `refund_overdue`, offer goodwill per `ES-6`, and hand off if the customer wants.
- **CX-9** Never cancel a booking because a third party in the conversation asks, unless they passed verification for that booking (`ID-2`).
- **CX-10** Cancelling inside the cutoff (`CH-5`) is not possible through the assistant; explain no-show rules.
