# Reservation Confirmation Agent

Contacts guests during a reservation-platform outage (e.g. a Resy outage), works out whether they still plan to attend, records any requested changes, and gives restaurant staff an accurate view of tonight's reservations.

**Role:** `CONTACT → UNDERSTAND → RECORD → ESCALATE`

This is not a replacement for the restaurant's reservation inventory system. It never invents availability and never approves a change on its own.

## Requirements

- Python 3.9+
- No third-party packages

## Quick start

```bash
python reservation_agent.py sample_reservations.json
```

On start, the agent prepares the confirmation message for every guest. Out of the box these are **printed to the console, not actually sent** (see [Sending real messages](#sending-real-messages)). It then reads commands from the terminal:

| Command | What it does |
|---|---|
| `RES-003 \| Can we come at 8 instead?` | Records a guest reply (reservation ID, a pipe, the reply text) |
| `board` | Shows the staff board, with items needing action at the top |
| `approve RES-003` | Staff approves a pending change request |
| `decline RES-003` | Staff declines a pending change request |
| `quit` | Exits and prints the final board |

If a record has no `restaurantName`, pass one on the command line:

```bash
python reservation_agent.py reservations.json "ABC"
```

## Input format

A JSON array. Each guest needs a phone or an email. If both are present, SMS is used.

```json
[
  {
    "reservationId": "RES-001",
    "customerName": "Sarah Johnson",
    "phone": "+15550000001",
    "reservationTime": "6:30 PM",
    "partySize": 2,
    "restaurantName": "ABC"
  }
]
```

`reservationTime` must look like `7:30 PM` (hour, optional minutes, AM/PM). Guests with no phone or email are flagged for staff to contact manually.

## Statuses

| Status | Meaning | Original reservation |
|---|---|---|
| `CONFIRMED` | Guest confirmed with no changes | Active |
| `CANCELLED` | Guest clearly cancelled | Released |
| `CHANGE_REQUESTED` | Guest asked for a party-size and/or time change | **Retained** until staff approve |
| `NEEDS_REVIEW` | Intent unclear, mixed, or unusual | Unchanged |
| `AWAITING_RESPONSE` | No reply yet | Active |

## How replies are handled

| Guest says | Result |
|---|---|
| "Yes", "Yep, we'll be there", "Confirmed" | `CONFIRMED` |
| "No", "We can't make it", "Please cancel" | `CANCELLED` |
| "There will be 5 of us instead of 4" | `CHANGE_REQUESTED` (party size 4 → 5) |
| "Can we come at 8 instead?" | `CHANGE_REQUESTED` (time → 8:00 PM) |
| "We're actually 6 people and would prefer 8 PM" | `CHANGE_REQUESTED` (both captured) |
| "maybe", "ok", "Yes, do you have a patio?", "Yes, plus 1 more" | `NEEDS_REVIEW` |
| "No, we'll be 5 instead", "Yes and cancel" | `NEEDS_REVIEW` (mixed intent) |
| *(no reply)* | `AWAITING_RESPONSE`, never auto-cancelled |

Replies are flagged for staff when they contain a question, a hedge, numbers the agent can't interpret, or a special request (allergies, birthdays, accessibility, running late, extra guests, and similar).

## Safety rules enforced in code

- A requested change is **not** a confirmed change. Party size and time on the booking are only updated by `staff_approve_change()`.
- A reservation is cancelled **only** on a clear cancel. Silence and ambiguous replies never cancel.
- Mixed-intent replies go to `NEEDS_REVIEW` and the booking is left untouched.
- Staff declining a change returns the guest to `AWAITING_RESPONSE`, and the guest is asked to reply YES to keep the original or NO to cancel, since the agent can't assume what they want.

## Project layout

```
reservation_agent.py        Agent, classifier, message templates, CLI
test_reservation_agent.py   Unit tests
sample_reservations.json    Example data for a 7-reservation night
README.md                   This file
```

Run the tests:

```bash
python -m unittest -v
```

## Using it as a library

```python
from reservation_agent import ConsoleMessenger, ReservationAgent, load_reservations
import json

records = json.load(open("sample_reservations.json"))
agent = ReservationAgent(load_reservations(records), ConsoleMessenger())

agent.send_outreach()
agent.handle_reply("RES-003", "Can we come at 8 instead?")
print(agent.board())
agent.staff_approve_change("RES-003")   # only after staff confirm availability
```

## Sending real messages

`ConsoleMessenger` only prints. To send real texts and emails, write a class with this method and pass it to `ReservationAgent` instead:

```python
class MyMessenger:
    def send(self, channel, address, body, subject=None):
        # channel is "sms" or "email"
        ...  # call Twilio, SendGrid, etc.
```

## Customising wording

All guest-facing text lives in the template functions near the top of `reservation_agent.py`:
`outreach_text`, `reply_confirmed`, `reply_cancelled`, `reply_change_requested`, `reply_needs_review`.

Note: the playbook doesn't specify guest wording for `NEEDS_REVIEW`, so `reply_needs_review` is a neutral holding message. Edit it to match your restaurant's voice.

## Known limitations

- **Rule-based classifier.** It is deliberately conservative: unusual phrasing is sent to staff rather than guessed at. It handles English only.
- **In-memory state.** Reservations and statuses are lost when the program exits. For live service, persist them to a database or a JSON file.
- **No inbound webhook.** Replies are entered by hand (or by your own integration calling `handle_reply`). There's no built-in listener for SMS/email replies.
- **Times without AM/PM** (e.g. "at 8") are assumed to be PM.
- **Same-day only.** There is no date handling; all reservations are treated as tonight's.
