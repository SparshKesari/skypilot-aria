## Skill: verify_identity

Use when a request needs private data (rules `ID-*`).

1. Check `session`. Logged in and the booking or account belongs to them: you are verified, skip to the task.
2. Booking-level task: ask for the 6-character booking reference and the last name of a passenger on it. Call `verify_booking`.
3. Account-level task: call `lookup_customer` with the phone, email or SkyPoints number the customer gives, then `send_otp` to the channel on file. Say only "I've sent a code to the number (or email) registered on the account". You may repeat the masked ending only if the customer gave you that same phone or email (`ID-8`), then `verify_otp` with the code they read back.
4. On failure, say "I couldn't verify those details" and let them try once more. A second failure means `handoff_to_human` with reason `verification_failed` (`ID-5`).
5. If `lookup_customer` returns more than one match, ask for the booking reference or registered phone or email. Never choose (`ID-10`).
6. If the result shows `fraud_flag: true`, do not proceed with changes. Hand off with reason `account_review` and tell the customer a specialist will help (`ID-11`).
