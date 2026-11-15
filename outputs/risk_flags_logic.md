# Risk flags – logic and assumptions

Run: `python risk_flags.py` (reads the warehouse; no personal data is involved).

## Rule A – shared IP
An IP address used by **3 or more different players** in October (`logins`, `COUNT(DISTINCT player_id) >= 3`).
Every player who logged in from such an IP is flagged.
**Result:** 6 IP addresses, 25 players (`rule_a_shared_ip_addresses.csv`).

## Rule B – deposit, then withdraw, without playing
An **approved** deposit followed by a withdrawal **request** from the same player **0–60 minutes later**, with **no betting activity in between**.
**Result:** 109 deposit→withdrawal pairs, 107 players (`rule_b_deposit_withdraw_no_play.csv`).

Choices I made (each is one line in the SQL, easy to change):
* **Withdrawal status is not filtered.** The rule is about the *request*, so approved, rejected, cancelled and pending requests all count. The status is shown in the file.
* **Window is inclusive** (0 to 60 minutes). Timestamps are to the minute, so a same-minute deposit and request counts.
* **"Betting activity in between"** = any bet session that overlaps the period from deposit to request, i.e. it started before the request and had not already ended before the deposit. A session that started *before* the deposit but was still running when it landed counts as betting.
  * 7 more pairs would be flagged if only sessions *starting* inside the window counted. They are in `rule_b_borderline_session_in_progress.csv` for a human to judge, but are **not** in the main list.
* One deposit can match several withdrawals (and vice-versa), so the file is at pair level; `risk_flags_players.csv` rolls it up to one row per player.

## Combined list
`risk_flags_players.csv`: one row per flagged player (132 in total: 25 from Rule A, 107 from Rule B, 0 on both), with which rule fired, the IPs, and the deposit→withdrawal pairs as evidence.

## Verification
`tests/test_assistant.py` re-computes both rules with independent code (pandas for Rule A, a plain Python brute-force loop over every deposit/withdrawal/session for Rule B) and checks that the output files match exactly.

## Caveat
These are screening flags, not proof of fraud. Shared IPs can be households, offices or mobile carrier gateways; a quick deposit-and-withdraw can be a player who changed their mind. They are meant to prioritise human review.
