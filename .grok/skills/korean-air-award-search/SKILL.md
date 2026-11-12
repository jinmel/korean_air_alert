---
name: korean-air-award-search
description: Search Korean Air SKYPASS award seats (prestige/first-class mileage awards and public mileage-upgrade candidates) on given routes and dates, optionally confirming award seats against the live booking search. Read-only; never books or pays.
when-to-use: Use when the user asks for Korean Air mileage award seats, 대한항공 마일리지 보너스 좌석, 프레스티지석/일등석 보너스, 마일리지 승급 좌석, or whether an award seat is actually bookable on specific routes or dates.
argument-hint: "routes=ICN-JFK,ICN-FRA start=YYYY-MM-DD end=YYYY-MM-DD types=award,upgrade live=yes|no"
allowed-tools: Bash(python3 korean_award.py *), Bash(python3 -m unittest *), Read
metadata:
  repository: https://github.com/jinmel/korean_air_alert
---

# Korean Air award-seat search

Read-only lookup of Korean Air award seats. The script lives in the root of the
`jinmel/korean_air_alert` repository. Run every command from that directory.

## Inputs

Before you run anything, work out these four inputs from the user's request:

| Input | Flag | Default | Notes |
|---|---|---|---|
| Routes | `--routes ICN-JFK,ICN-FRA` | required | IATA codes, comma separated, `ORIGIN-DEST` |
| Date range | `--start YYYY-MM-DD --end YYYY-MM-DD` | today → about 360 days | Inclusive. Dates are in Korea time (KST). The end date is cut off at the booking horizon. |
| Seat types | `--seat-types award,upgrade` | `award` | `award` = prestige + first-class mileage awards. `upgrade` = public mileage-upgrade candidates. Economy awards are not supported. |
| Live check | `--verify-live` | off | Re-checks award candidates in the authenticated booking search. Needs a Korean Air session. |

- Turn a month such as "January" into the nearest future month. For example, "1월" becomes `--start 2027-01-01 --end 2027-01-31`.
- Ask the user only if the route is missing. Otherwise use the defaults.
- A live check sends one booking search per candidate date. For wide ranges or many routes, tell the user the scope before you start.

## Run

Always use `--json` and parse stdout as one JSON object:

```bash
python3 korean_award.py --routes ICN-JFK,ICN-FRA \
  --start 2027-01-01 --end 2027-01-31 \
  --seat-types award,upgrade --verify-live --json
```

Exit code 0 means every route was checked and nothing came back unknown. Exit code 1 means at least one route failed or at least one result is unknown. Even with exit code 1, still read the JSON and report what did succeed.

## Session for `--verify-live`

The live booking search needs Korean Air's session cookie `T`. The script looks for it in this order:

1. The session file: `--session FILE`, or the `KOREANAIR_SESSION_FILE` environment variable, or `~/.local/state/korean_air_alert/session.json`. The script refreshes the cookie and writes it back to this file with mode 0600.
2. The `KOREANAIR_SESSION_T` environment variable. Get this value from a Vault secret, never from chat.
3. If the stored session has expired and `--cdp URL` or `KOREANAIR_CDP_URL` is set, the script takes a fresh `T` from a Chrome that is logged in to koreanair.com and exposes a remote-debugging (CDP) port.

If login fails, the script reports an error. When that happens:

- Report the public results as public only and the live results as unknown.
- Ask the user to log in to koreanair.com again, in the browser or with a refreshed secret.
- Never ask the user to paste the cookie or password into chat.
- Never solve a CAPTCHA or extra-verification step on the user's behalf. The user has to do it.

## Reading the JSON

Results are grouped per route (`routes[].results[]`), and each result has a `live_status`:

| `live_status` | Meaning | How to report it |
|---|---|---|
| `selectable` | Shown as selectable in the live booking search (`seats` = seats in the response) | "예매 가능" |
| `not_selectable` | Live booking search shows it sold out or not selectable | "예매 불가" |
| `unknown` | The live search errored, the session failed, or the flight was missing from the response | "미확인" |
| `unsupported` | Upgrade candidate. No live check exists for upgrades. | "공개 현황 기준 승급 후보 (실제 신청 가능 여부 미확인)" |
| `not_checked` | Live check was off | "공개 현황 후보 (예매 가능 여부 미확인)" |

A route with `status: "error"` was not checked. Say so, and give the error message.

## Hard rules

- **Never turn unknown into "no seats".** Treat each of these as "미확인", not as "좌석 없음" or "운항 안 함":
  - an `unknown` result
  - a route with `status: "error"`
  - booking-search error `ERT.7190`
- An empty `results` list on an `ok` route only means there are no public candidates. Add that the public calendar updates about once a day, so seats opened after the last update can be missing.
- Report public candidates and live results as separate states.
- An upgrade candidate is not a confirmed upgrade. Nothing has been verified about it beyond the public calendar.
- Read-only. Never do any of the following, even if the user asks:
  - create a reservation, PNR, or ticket
  - submit passenger details
  - pay or deduct mileage
  - request an upgrade
  - log in with the user's password on their behalf
- Never print, log, store in chat, or summarize the session cookie `T`, passwords, SKYPASS number, or traveler identity. Do not paste raw authenticated responses.
- Do not push request rates higher than the script does. Do not run many copies of the script in parallel.

## Answer format

Reply in the user's language, in Korean by default.

1. Lead with a one-line verdict, for example: "1월 ICN→FRA 프레스티지석 예매 가능 2일".
2. Then a compact table per route:

   | 날짜 | 편 | 출발시각 | 좌석 | 상태 (좌석 수) |
   |---|---|---|---|---|

3. Then a line listing everything that is unknown or errored, with the reason.
4. End with the effective date window from `window`. This can be shorter than requested, because the end date is cut at the booking horizon.
