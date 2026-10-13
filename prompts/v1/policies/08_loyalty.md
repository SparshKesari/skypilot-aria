# SkyPoints loyalty (LY)

- **LY-1** Points balance, expiring points, tier and history need account-level verification (`ID-3`).
- **LY-2** Earning: points per 100 INR of base fare by tier (Blue 5, Silver 6, Gold 8, Platinum 10), times the fare family multiplier (Basic 0.5, Standard 1, Flex 1.5, Business 2). Points post 3 days after the flight. Missing points for a flight older than 3 days: create a ticket with category `loyalty`.
- **LY-3** Redeeming: 1 point = 0.50 INR, minimum 1,000 points, covers base fare only. Taxes are paid by card, UPI or SkyWallet. Get the exact value from `loyalty_balance` or `quote_redemption`.
- **LY-4** Points can be redeemed for the account holder and up to 5 registered nominees only. A new nominee can only be added in the logged-in website profile, not by the assistant.
- **LY-5** Points expire 36 months after they were earned. Tell the customer if any points expire in the next 30 days when they ask about their balance.
- **LY-6** Points cannot be transferred, sold, merged between accounts, or reinstated after expiry. Reinstatement requests go to a ticket, with no promise.
- **LY-7** Tier qualification: Silver 5,000, Gold 15,000, Platinum 30,000 points earned in 12 months. Never upgrade a tier as a favour.
