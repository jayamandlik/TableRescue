"""
Reservation Confirmation Agent
==============================
Contacts guests during a reservation-platform outage, classifies their replies,
records the result, and escalates anything uncertain to restaurant staff.

Role:  CONTACT -> UNDERSTAND -> RECORD -> ESCALATE

Safety rules enforced in code:
  * A requested change is never applied. Only staff_approve_change() applies one.
  * Silence never cancels. Only a clear, unambiguous cancel does.
  * Ambiguous or mixed-intent replies become NEEDS_REVIEW; the booking is untouched.
  * The agent never invents availability.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    AWAITING_RESPONSE = "AWAITING_RESPONSE"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    CHANGE_REQUESTED = "CHANGE_REQUESTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class Reservation:
    reservation_id: str
    customer_name: str
    time: str                       # e.g. "7:30 PM"
    party_size: int
    restaurant_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    status: Status = Status.AWAITING_RESPONSE
    requested_party_size: Optional[int] = None
    requested_time: Optional[str] = None
    staff_review_required: bool = False
    original_retained: bool = True  # stays True until a guest clearly cancels
    last_reply: Optional[str] = None
    review_reason: Optional[str] = None

    @property
    def channel(self) -> Optional[str]:
        if self.phone:
            return "sms"
        if self.email:
            return "email"
        return None

    @property
    def address(self) -> Optional[str]:
        return self.phone or self.email


@dataclass
class Classification:
    status: Status
    requested_party_size: Optional[int] = None
    requested_time: Optional[str] = None
    reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
NUM = r"(\d{1,2}|" + "|".join(WORD_NUMS) + r")"


def to_int(token: str) -> int:
    token = token.lower()
    return WORD_NUMS[token] if token in WORD_NUMS else int(token)


def to_minutes(hour: int, minute: int = 0, ampm: Optional[str] = None) -> Optional[int]:
    """Clock time -> minutes since midnight. With no am/pm, assume dinner (PM)."""
    if not (0 <= minute < 60) or hour > 24:
        return None
    if ampm:
        pm = ampm.lower().startswith("p")
        if not 1 <= hour <= 12:
            return None
        hour = (hour % 12) + (12 if pm else 0)
    elif 1 <= hour <= 11:
        hour += 12
    return hour * 60 + minute


def fmt_minutes(total: int) -> str:
    h, m = divmod(total, 60)
    suffix = "PM" if h >= 12 else "AM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


def parse_time_string(text: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)\s*", text, re.I)
    if not m:
        raise ValueError(f"Unrecognised reservation time: {text!r}")
    minutes = to_minutes(int(m.group(1)), int(m.group(2) or 0), m.group(3).replace(".", ""))
    if minutes is None:
        raise ValueError(f"Invalid reservation time: {text!r}")
    return minutes


# --------------------------------------------------------------------------- #
# Reply classifier (deterministic, conservative)
# --------------------------------------------------------------------------- #
CONFIRM_RE = re.compile(
    r"\b(yes|yep|yeah|yup|yea|confirm(?:ed)?|sounds good|see you|still on|all good|"
    r"we(?:'ll| will) be there|works for us|no changes?|no problem)\b"
)
NEUTRALISE_RE = re.compile(r"\b(no changes?|no problem)\b")  # contain "no" but are not cancels
CANCEL_RE = re.compile(
    r"(^\s*(no|nope|nah)\b)|\bcancel\w*|\bno longer\b|\bnot (coming|joining|going to make)\b|"
    r"\b(can't|cannot|can not|won't|will not|unable to)\s+(make|come|be|join|attend)\b"
)
# Anything here means the reply carries something the playbook doesn't cover.
REVIEW_TRIGGER_RE = re.compile(
    r"\b(allerg\w*|dietary|vegan|vegetarian|gluten|birthday|anniversary|celebrat\w*|"
    r"wheelchair|accessib\w*|stroller|high ?chair|kids?|child\w*|baby|patio|outside|window|"
    r"late|later|earlier|early|minutes?|mins?|plus|extra|another|more|"
    r"maybe|might|probably|not sure|unsure|think|tentative)\b"
)
# Hedging words only matter when no concrete change was parsed ("coming, but 5 of us" is fine).
SOFT_TRIGGER_RE = re.compile(r"\b(but|however|unless)\b")

PARTY_PATTERNS = [
    re.compile(rf"\b{NUM}\s+(?:of us|people|guests|persons|adults|ppl)\b"),
    re.compile(rf"\bparty of {NUM}\b"),
    re.compile(rf"\b(?:table|reservation|for) {NUM}\b(?!\s*(?:pm|am|:|min))"),
    re.compile(rf"\bwe(?:'re| are) (?:now |actually )?{NUM}\b(?!\s*(?:pm|am|:|min))"),
]
TIME_PATTERNS = [
    re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?"),
    re.compile(r"\b(\d{1,2})()\s*([ap]\.?m\.?)"),
    re.compile(rf"\b(?:at|around|about|by)\s+{NUM}()()\b"),
    re.compile(rf"\b{NUM}()()\s+instead\b"),
]
INSTEAD_OF_RE = re.compile(r"\b(instead of|rather than)\s+\d{1,2}(:\d{2})?\s*([ap]\.?m\.?)?")


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def classify_reply(reply: str, original_time: str, original_party: int) -> Classification:
    text = reply.lower().replace("\u2019", "'").strip()
    work = text
    original_minutes = parse_time_string(original_time)

    # 1. Party-size requests
    party_values: set[int] = set()
    for pat in PARTY_PATTERNS:
        for m in pat.finditer(work):
            party_values.add(to_int(m.group(1)))
        work = pat.sub(lambda m: " " * len(m.group(0)), work)
    # 2. Time requests
    time_values: set[int] = set()
    for pat in TIME_PATTERNS:
        for m in list(pat.finditer(work)):
            hour_tok, minute_tok, ampm = m.group(1), m.group(2), m.group(3)
            minutes = to_minutes(to_int(hour_tok), int(minute_tok or 0),
                                 ampm.replace(".", "") if ampm else None)
            if minutes is None:
                return Classification(Status.NEEDS_REVIEW, reason="Could not read a time in the reply")
            time_values.add(minutes)
        work = pat.sub(lambda m: " " * len(m.group(0)), work)
    work = INSTEAD_OF_RE.sub(" ", work)

    # 3. Mentions of the original values are not changes
    party_values.discard(original_party)
    time_values.discard(original_minutes)

    if len(party_values) > 1 or len(time_values) > 1:
        return Classification(Status.NEEDS_REVIEW, reason="Reply contains conflicting party sizes or times")

    new_party = next(iter(party_values), None)
    new_time = fmt_minutes(next(iter(time_values))) if time_values else None
    has_change = new_party is not None or new_time is not None

    # 4. Intent signals
    cancel = bool(CANCEL_RE.search(NEUTRALISE_RE.sub(" ", work)))
    confirm = bool(CONFIRM_RE.search(work))

    # 5. Escalation conditions (checked before anything is recorded)
    if REVIEW_TRIGGER_RE.search(work):
        return Classification(Status.NEEDS_REVIEW, reason="Reply mentions something outside the standard flow")
    if not has_change and SOFT_TRIGGER_RE.search(work):
        return Classification(Status.NEEDS_REVIEW, reason="Reply is conditional or hedged")
    if re.search(r"\d", work):
        return Classification(Status.NEEDS_REVIEW, reason="Reply contains numbers that could not be interpreted")
    if cancel and (confirm or has_change):
        return Classification(Status.NEEDS_REVIEW, reason="Reply mixes cancellation with confirmation or a change")
    if "?" in text and not has_change:
        return Classification(Status.NEEDS_REVIEW, reason="Guest asked a question")

    # 6. Clear outcomes
    if cancel:
        return Classification(Status.CANCELLED)
    if has_change:
        return Classification(Status.CHANGE_REQUESTED, new_party, new_time)
    if confirm:
        return Classification(Status.CONFIRMED)
    return Classification(Status.NEEDS_REVIEW, reason="Intent could not be determined")


# --------------------------------------------------------------------------- #
# Messaging (plug in Twilio / SendGrid / etc. by implementing Messenger)
# --------------------------------------------------------------------------- #
class Messenger(Protocol):
    def send(self, channel: str, address: str, body: str, subject: Optional[str] = None) -> None: ...


class ConsoleMessenger:
    """Prints messages and keeps a record. Replace with a real provider in production."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, channel: str, address: str, body: str, subject: Optional[str] = None) -> None:
        self.sent.append({"channel": channel, "to": address, "subject": subject, "body": body})
        head = f"[{channel.upper()} -> {address}]" + (f" Subject: {subject}" if subject else "")
        print(f"{head}\n  {body}\n")


# --------------------------------------------------------------------------- #
# Message templates
# --------------------------------------------------------------------------- #
def outreach_text(r: Reservation) -> str:
    return (
        f"Hi {r.customer_name}, this is {r.restaurant_name}. We have your reservation tonight at "
        f"{r.time} for {r.party_size} guests. Our reservation platform is temporarily unavailable, "
        f"so we're confirming tonight's reservations directly. Please reply YES to confirm, NO if "
        f"you won't be joining us, or let us know if you'd like to request a change."
    )


def reply_confirmed(r: Reservation) -> str:
    return (f"Thank you. Your reservation for {r.party_size} guests at {r.time} is confirmed. "
            f"We look forward to seeing you tonight.")


def reply_cancelled(_: Reservation) -> str:
    return "Thank you for letting us know. Your reservation has been marked as cancelled."


def reply_change_requested(r: Reservation, new_party: Optional[int], new_time: Optional[str]) -> str:
    if new_party is not None and new_time is not None:
        return (f"Thanks. We've received your request to change your party size from {r.party_size} to "
                f"{new_party} and move your reservation from {r.time} to {new_time}. Your original "
                f"reservation for {r.party_size} at {r.time} remains in place while the restaurant "
                f"reviews the changes. We'll let you know once we can confirm.")
    if new_party is not None:
        return (f"Thanks. We've received your request to change your party size from {r.party_size} to "
                f"{new_party}. Your original reservation for {r.party_size} remains in place while the "
                f"restaurant reviews the change. We'll let you know once the updated party size is confirmed.")
    return (f"Thanks. We've received your request to move your reservation from {r.time} to {new_time}. "
            f"Your original {r.time} reservation remains in place while the restaurant checks "
            f"availability. We'll let you know if the new time can be accommodated.")


def reply_needs_review(_: Reservation) -> str:
    # Not specified in the playbook; neutral holding message. Edit to taste.
    return ("Thanks for your reply. A member of our team will follow up with you shortly. "
            "Your original reservation remains in place.")


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ReservationAgent:
    def __init__(self, reservations: list[Reservation], messenger: Messenger) -> None:
        self.reservations = {r.reservation_id: r for r in reservations}
        self.messenger = messenger

    # CONTACT
    def send_outreach(self) -> list[str]:
        """Send the confirmation message to every guest. Returns IDs with no contact channel."""
        unreachable = []
        for r in self.reservations.values():
            if not r.channel:
                r.staff_review_required = True
                r.review_reason = "No phone or email on file"
                unreachable.append(r.reservation_id)
                continue
            subject = f"Confirming your reservation tonight at {r.restaurant_name}" if r.channel == "email" else None
            self.messenger.send(r.channel, r.address, outreach_text(r), subject)
        return unreachable

    # UNDERSTAND + RECORD
    def handle_reply(self, reservation_id: str, reply: str) -> str:
        r = self.reservations[reservation_id]
        r.last_reply = reply
        result = classify_reply(reply, r.time, r.party_size)

        r.status = result.status
        r.requested_party_size = result.requested_party_size
        r.requested_time = result.requested_time
        r.review_reason = result.reason
        # ESCALATE flag
        r.staff_review_required = result.status in (Status.CHANGE_REQUESTED, Status.NEEDS_REVIEW)

        if result.status is Status.CONFIRMED:
            body = reply_confirmed(r)
        elif result.status is Status.CANCELLED:
            r.original_retained = False
            body = reply_cancelled(r)
        elif result.status is Status.CHANGE_REQUESTED:
            body = reply_change_requested(r, result.requested_party_size, result.requested_time)
        else:
            body = reply_needs_review(r)

        if r.channel:
            subject = "Re: your reservation" if r.channel == "email" else None
            self.messenger.send(r.channel, r.address, body, subject)
        return body

    # Staff decisions (only path that ever changes a booking's details)
    def staff_approve_change(self, reservation_id: str) -> str:
        r = self.reservations[reservation_id]
        if r.status is not Status.CHANGE_REQUESTED:
            raise ValueError(f"{reservation_id} has no pending change request")
        if r.requested_party_size is not None:
            r.party_size = r.requested_party_size
        if r.requested_time is not None:
            r.time = r.requested_time
        r.requested_party_size = r.requested_time = None
        r.status, r.staff_review_required = Status.CONFIRMED, False
        body = (f"Good news. Your updated reservation for {r.party_size} guests at {r.time} "
                f"is confirmed. We look forward to seeing you tonight.")
        self._notify(r, body)
        return body

    def staff_decline_change(self, reservation_id: str) -> str:
        r = self.reservations[reservation_id]
        if r.status is not Status.CHANGE_REQUESTED:
            raise ValueError(f"{reservation_id} has no pending change request")
        r.requested_party_size = r.requested_time = None
        # We don't know whether the guest still wants the original, so ask.
        r.status, r.staff_review_required = Status.AWAITING_RESPONSE, False
        body = (f"Unfortunately we aren't able to accommodate that change. Your original reservation "
                f"for {r.party_size} guests at {r.time} is still in place. Please reply YES to keep it "
                f"or NO to cancel.")
        self._notify(r, body)
        return body

    def _notify(self, r: Reservation, body: str) -> None:
        if r.channel:
            self.messenger.send(r.channel, r.address, body,
                                "Your reservation update" if r.channel == "email" else None)

    # Staff view
    def board(self) -> str:
        order = {Status.CHANGE_REQUESTED: 0, Status.NEEDS_REVIEW: 0}
        rows = sorted(self.reservations.values(),
                      key=lambda r: (order.get(r.status, 1), parse_time_string(r.time)))
        lines = ["", "TONIGHT'S RESERVATIONS", "=" * 92,
                 f"{'ID':<8}{'Guest':<18}{'Time':<10}{'Party':<7}{'Status':<20}{'Staff?':<8}Details",
                 "-" * 92]
        for r in rows:
            detail = ""
            if r.status is Status.CHANGE_REQUESTED:
                bits = []
                if r.requested_party_size is not None:
                    bits.append(f"party {r.party_size}->{r.requested_party_size}")
                if r.requested_time is not None:
                    bits.append(f"time {r.time}->{r.requested_time}")
                detail = ", ".join(bits) + " (original retained)"
            elif r.status is Status.NEEDS_REVIEW or r.review_reason:
                detail = f"{r.review_reason or ''} | reply: {r.last_reply or ''}"
            lines.append(f"{r.reservation_id:<8}{r.customer_name:<18}{r.time:<10}{r.party_size:<7}"
                         f"{r.status.value:<20}{'YES' if r.staff_review_required else '-':<8}{detail}")
        counts: dict[str, int] = {}
        for r in self.reservations.values():
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        lines += ["-" * 92, "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())), ""]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Loading + CLI
# --------------------------------------------------------------------------- #
def load_reservations(records: list[dict], default_restaurant: str = "") -> list[Reservation]:
    out = []
    for rec in records:
        name = rec.get("restaurantName") or default_restaurant
        if not name:
            raise ValueError(f"{rec.get('reservationId')}: restaurantName is required")
        out.append(Reservation(
            reservation_id=rec["reservationId"], customer_name=rec["customerName"],
            time=rec["reservationTime"], party_size=int(rec["partySize"]),
            restaurant_name=name, phone=rec.get("phone"), email=rec.get("email")))
    return out


HELP = ("Commands:  <ID> | <guest reply>   record a reply (e.g. RES-002 | Yes, 5 of us)\n"
        "           board                  show the staff board\n"
        "           approve <ID>           staff approves a pending change\n"
        "           decline <ID>           staff declines a pending change\n"
        "           quit")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("Usage: python reservation_agent.py <reservations.json> [restaurant name]")
        return
    with open(argv[1], encoding="utf-8") as f:
        records = json.load(f)
    agent = ReservationAgent(load_reservations(records, " ".join(argv[2:])), ConsoleMessenger())
    missing = agent.send_outreach()
    if missing:
        print(f"WARNING: no contact info for {', '.join(missing)}. Staff must call these guests.")
    print(HELP)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cmd = line.split()[0].lower()
        try:
            if cmd == "quit":
                break
            elif cmd == "board":
                print(agent.board())
            elif cmd in ("approve", "decline"):
                rid = line.split()[1].upper()
                (agent.staff_approve_change if cmd == "approve" else agent.staff_decline_change)(rid)
            elif "|" in line:
                rid, reply = (p.strip() for p in line.split("|", 1))
                agent.handle_reply(rid.upper(), reply)
            else:
                print(HELP)
        except (KeyError, ValueError, IndexError) as e:
            print(f"Error: {e}")
    print(agent.board())


if __name__ == "__main__":
    main(sys.argv)
