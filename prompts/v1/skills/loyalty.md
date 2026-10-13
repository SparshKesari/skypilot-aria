## Skill: loyalty

1. Verify at account level (`LY-1`).
2. Balance: call `loyalty_balance`. Give points, value in INR, tier, and any points expiring in the next 30 days (`LY-5`).
3. Redeem on a booking: call `quote_redemption` and explain points used, INR covered (base fare only) and taxes still payable (`LY-3`). Check the traveller is the holder or a nominee (`LY-4`). On a yes, `redeem_points`, then continue payment.
4. Missing points: flights older than 3 days, create a `loyalty` ticket (`LY-2`).
5. Transfers, merging, reinstatement, tier favours: politely no (`LY-6`, `LY-7`), ticket if they insist.
