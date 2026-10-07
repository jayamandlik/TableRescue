import unittest
from reservation_agent import (ConsoleMessenger, ReservationAgent, Reservation, Status,
                               classify_reply)


def c(reply, time="7:30 PM", party=4):
    return classify_reply(reply, time, party)


class ClassifierTests(unittest.TestCase):
    def test_confirmations(self):
        for text in ["Yes", "Yep, we'll be there.", "Confirmed.", "yes!", "Sounds good", "No changes, see you then"]:
            self.assertEqual(c(text).status, Status.CONFIRMED, text)

    def test_confirm_restating_original_details(self):
        self.assertEqual(c("Yes, 7:30 for 4 works").status, Status.CONFIRMED)

    def test_cancellations(self):
        for text in ["No", "We can't make it.", "Please cancel.", "Nope", "won't be joining you"]:
            self.assertEqual(c(text).status, Status.CANCELLED, text)

    def test_party_size_change(self):
        r = c("We're coming, but there will be 5 of us instead of 4.")
        self.assertEqual((r.status, r.requested_party_size, r.requested_time),
                         (Status.CHANGE_REQUESTED, 5, None))

    def test_time_change(self):
        r = c("Can we come at 8 instead?")
        self.assertEqual((r.status, r.requested_party_size, r.requested_time),
                         (Status.CHANGE_REQUESTED, None, "8:00 PM"))

    def test_multiple_changes(self):
        r = c("We're actually 6 people and would prefer 8 PM.")
        self.assertEqual((r.status, r.requested_party_size, r.requested_time),
                         (Status.CHANGE_REQUESTED, 6, "8:00 PM"))

    def test_ambiguous_goes_to_review(self):
        for text in ["maybe", "Not sure yet", "Who is this?", "ok", "Yes but we might be late",
                     "Yes, plus 1 more", "Yes, do you have a patio?", "Yes, it's my birthday",
                     "No, we'll be 5 instead", "Yes and cancel"]:
            self.assertEqual(c(text).status, Status.NEEDS_REVIEW, text)

    def test_question_never_confirms(self):
        self.assertEqual(c("Yes, is parking available?").status, Status.NEEDS_REVIEW)


def make_agent():
    res = [Reservation("RES-003", "Priya Shah", "7:30 PM", 4, "ABC", email="p@example.com"),
           Reservation("RES-009", "No Contact", "9:00 PM", 2, "ABC")]
    return ReservationAgent(res, ConsoleMessenger())


class AgentTests(unittest.TestCase):
    def test_change_does_not_alter_booking(self):
        a = make_agent()
        a.handle_reply("RES-003", "5 of us instead of 4 please")
        r = a.reservations["RES-003"]
        self.assertEqual((r.party_size, r.time, r.status), (4, "7:30 PM", Status.CHANGE_REQUESTED))
        self.assertTrue(r.staff_review_required and r.original_retained)

    def test_reply_text_matches_playbook(self):
        a = make_agent()
        body = a.handle_reply("RES-003", "We're coming, but there will be 5 of us instead of 4.")
        self.assertIn("from 4 to 5", body)
        self.assertIn("original reservation for 4 remains in place", body)

    def test_cancel_releases_only_on_clear_cancel(self):
        a = make_agent()
        a.handle_reply("RES-003", "maybe")
        self.assertTrue(a.reservations["RES-003"].original_retained)
        a.handle_reply("RES-003", "Please cancel")
        self.assertFalse(a.reservations["RES-003"].original_retained)

    def test_no_response_stays_active(self):
        a = make_agent()
        r = a.reservations["RES-003"]
        self.assertEqual((r.status, r.original_retained), (Status.AWAITING_RESPONSE, True))

    def test_staff_approval_is_only_way_to_apply_change(self):
        a = make_agent()
        a.handle_reply("RES-003", "Can we come at 8 instead?")
        a.staff_approve_change("RES-003")
        r = a.reservations["RES-003"]
        self.assertEqual((r.time, r.status), ("8:00 PM", Status.CONFIRMED))

    def test_staff_decline_keeps_original(self):
        a = make_agent()
        a.handle_reply("RES-003", "Can we come at 8 instead?")
        a.staff_decline_change("RES-003")
        r = a.reservations["RES-003"]
        self.assertEqual((r.time, r.status), ("7:30 PM", Status.AWAITING_RESPONSE))

    def test_unreachable_guest_flagged(self):
        a = make_agent()
        self.assertEqual(a.send_outreach(), ["RES-009"])
        self.assertTrue(a.reservations["RES-009"].staff_review_required)


if __name__ == "__main__":
    unittest.main()
