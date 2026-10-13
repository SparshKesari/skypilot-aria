"""Tool layer tests against data/ground_truth.json.  Run: python -m unittest tests/test_tools.py -v"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
from store import Session  # noqa: E402
from tools_impl import Tools  # noqa: E402

GT = {s["scenario"]: s for s in json.loads((ROOT / "data" / "ground_truth.json").read_text())["scenarios"]}


def session(scn=None, login=False, guardrails="strict"):
    s = Session(customer_id=GT[scn]["customer_id"] if (scn and login) else None, guardrails=guardrails)
    return s, Tools(s)


def verify(t, scn):
    g = GT[scn]
    r = t.call("verify_booking", {"pnr": g["pnr"], "last_name": g["last_name"]})
    assert r.get("verified"), r


def first_seg(t, pnr):
    return t.call("get_booking", {"pnr": pnr})["segments"][0]


class Identity(unittest.TestCase):
    def test_booking_needs_verification(self):
        s, t = session()
        r = t.call("get_booking", {"pnr": GT["basic_fare_change_not_allowed"]["pnr"]})
        self.assertEqual(r["error"], "NOT_VERIFIED")

    def test_guardrails_off_skips_identity(self):
        s, t = session(guardrails="off")
        r = t.call("get_booking", {"pnr": GT["basic_fare_change_not_allowed"]["pnr"]})
        self.assertNotIn("error", r)

    def test_two_failures_lock(self):
        s, t = session()
        pnr = GT["basic_fare_change_not_allowed"]["pnr"]
        self.assertFalse(t.call("verify_booking", {"pnr": pnr, "last_name": "Wrong"})["verified"])
        r = t.call("verify_booking", {"pnr": pnr, "last_name": "Wronger"})
        self.assertTrue(r["locked"])
        self.assertEqual(t.call("get_booking", {"pnr": pnr})["error"], "LOCKED")

    def test_otp_flow_and_same_name(self):
        s, t = session()
        g = GT["same_name"]
        r = t.call("lookup_customer", {"phone": g["phone"]})
        self.assertEqual(r["matches"], 1)
        cid = r["customers"][0]["customer_id"]
        t.call("send_otp", {"customer_id": cid, "channel": "sms"})
        self.assertTrue(t.call("verify_otp", {"customer_id": cid, "code": g["test_otp"]})["verified"])
        self.assertNotIn("error", t.call("list_bookings", {"customer_id": cid}))
        names = [c for c in s.db["customers"] if (c["first_name"], c["last_name"]) == ("Rahul", "Sharma")]
        self.assertEqual(len(names), 2)

    def test_fraud_flag_blocks_cancel(self):
        g = GT["fraud_flag_handoff"]
        s, t = session("fraud_flag_handoff", login=True)
        r = t.call("quote_cancellation", {"pnr": g["pnr"], "reason": "voluntary"})
        self.assertEqual(r["error"], "ACCOUNT_REVIEW")


class Money(unittest.TestCase):
    def check_cancel(self, scn, key, reason="voluntary"):
        g = GT[scn]
        s, t = session()
        verify(t, scn)
        r = t.call("quote_cancellation", {"pnr": g["pnr"], "reason": reason})
        self.assertNotIn("error", r, r)
        self.assertEqual(r["total_refund_inr"], g["expected"][key]["total_refund"], scn)
        return t, r

    def test_basic_cancel(self):
        self.check_cancel("basic_fare_change_not_allowed", "cancel_option")

    def test_look_in(self):
        self.check_cancel("look_in_free_cancel", "quote")

    def test_look_in_not_eligible(self):
        self.check_cancel("look_in_not_eligible_short_notice", "quote")

    def test_flex_late(self):
        self.check_cancel("flex_late_cancellation", "quote")

    def test_injection_quote(self):
        self.check_cancel("prompt_injection_in_data", "cancel_quote")

    def test_inside_cutoff(self):
        g = GT["inside_cutoff"]
        s, t = session()
        verify(t, "inside_cutoff")
        self.assertEqual(t.call("quote_cancellation", {"pnr": g["pnr"], "reason": "voluntary"})["error"], "CUTOFF_PASSED")
        r = t.call("quote_cancellation", {"pnr": g["pnr"], "reason": "no_show"})
        self.assertEqual(r["total_refund_inr"], g["expected"]["no_show_quote"]["total_refund"])

    def test_cancel_executes_and_refunds_wallet(self):
        t, q = self.check_cancel("look_in_free_cancel", "quote")
        r = t.call("cancel_booking", {"pnr": GT["look_in_free_cancel"]["pnr"], "quote_id": q["quote_id"], "refund_to": "skywallet"})
        self.assertEqual(r["status"], "CANCELLED")
        self.assertEqual(r["refund"]["status"], "completed")

    def test_basic_change_refused(self):
        g = GT["basic_fare_change_not_allowed"]
        s, t = session()
        verify(t, "basic_fare_change_not_allowed")
        sg = first_seg(t, g["pnr"])
        day = sg["departs_local"][:10]
        flights = t.call("search_flights", {"origin": sg["origin"], "destination": sg["destination"], "date": day, "adults": 1})["flights"]
        other = next(f for f in flights if f["flight_id"] != sg["flight_id"]) if len(flights) > 1 else flights[0]
        r = t.call("quote_change", {"pnr": g["pnr"], "segment_id": sg["segment_id"], "new_flight_id": other["flight_id"]})
        self.assertEqual(r["error"], "NOT_ALLOWED")

    def test_standard_change_quote_and_apply(self):
        g = GT["look_in_not_eligible_short_notice"]
        s, t = session("look_in_not_eligible_short_notice", login=True)
        sg = first_seg(t, g["pnr"])
        import datetime as dt
        day = (dt.date.fromisoformat(sg["departs_local"][:10]) + dt.timedelta(days=2)).isoformat()
        fl = t.call("search_flights", {"origin": sg["origin"], "destination": sg["destination"], "date": day, "adults": 1})["flights"][0]
        q = t.call("quote_change", {"pnr": g["pnr"], "segment_id": sg["segment_id"], "new_flight_id": fl["flight_id"]})
        self.assertEqual(q["change_fee_total_inr"], 3000)
        prof = t.call("get_customer_profile", {"customer_id": g["customer_id"]})
        pm = next(m for m in prof["saved_payment_methods"] if not m.get("expired"))
        r = t.call("change_booking", {"pnr": g["pnr"], "segment_id": sg["segment_id"], "new_flight_id": fl["flight_id"], "quote_id": q["quote_id"],
                                      "payment_method": "saved_method", "payment_method_id": pm["id"]})
        self.assertEqual(r["status"], "CHANGED", r)
        self.assertEqual(t.call("get_booking", {"pnr": g["pnr"]})["segments"][0]["flight_id"], fl["flight_id"])


class Disruptions(unittest.TestCase):
    def test_domestic_compensation(self):
        g = GT["airline_cancelled_domestic_compensation"]
        s, t = session()
        verify(t, "airline_cancelled_domestic_compensation")
        sg = first_seg(t, g["pnr"])
        c = t.call("quote_disruption_compensation", {"pnr": g["pnr"], "segment_id": sg["segment_id"]})
        self.assertEqual(c["amount_per_passenger_inr"], g["expected"]["compensation"]["amount"])
        q = t.call("quote_cancellation", {"pnr": g["pnr"], "reason": "airline_disruption"})
        self.assertEqual(q["total_refund_inr"], g["expected"]["full_refund_amount"])
        alt = g["expected"]["eligible_rebooking_flight_ids"][0]
        r = t.call("rebook_disrupted", {"pnr": g["pnr"], "segment_id": sg["segment_id"], "new_flight_id": alt})
        self.assertEqual(r["status"], "REBOOKED", r)

    def test_weather_no_compensation(self):
        g = GT["airline_cancelled_weather_no_compensation"]
        s, t = session()
        verify(t, "airline_cancelled_weather_no_compensation")
        sg = first_seg(t, g["pnr"])
        self.assertFalse(t.call("quote_disruption_compensation", {"pnr": g["pnr"], "segment_id": sg["segment_id"]})["due"])

    def test_international_handoff(self):
        g = GT["international_cancellation_handoff"]
        s, t = session()
        verify(t, "international_cancellation_handoff")
        sg = first_seg(t, g["pnr"])
        self.assertEqual(t.call("quote_disruption_compensation", {"pnr": g["pnr"], "segment_id": sg["segment_id"]})["due"], "handoff")

    def test_delay_entitlements(self):
        g = GT["delay_190_minutes"]
        s, t = session()
        verify(t, "delay_190_minutes")
        sg = first_seg(t, g["pnr"])
        st = t.call("get_flight_status", {"flight_number": sg["flight_number"], "date": sg["departs_local"][:10]})
        self.assertTrue(st["passenger_entitlements"]["meal_voucher"])
        self.assertFalse(st["passenger_entitlements"]["free_rebook_or_full_refund"])


class Booking(unittest.TestCase):
    def test_umnr_required_then_ok(self):
        g = GT["minor_alone_needs_umnr"]
        s, t = session("minor_alone_needs_umnr", login=True)
        prof = t.call("get_customer_profile", {"customer_id": g["customer_id"]})
        kid = next(c for c in prof["companions"] if c["companion_id"] == g["companion_id"])
        home = prof["home_airport"]
        dest = next(r["destination"] for r in s.db["routes"] if r["origin"] == home and r["route_type"] == "domestic")
        fl = t.call("search_flights", {"origin": home, "destination": dest, "date": "2026-10-17", "adults": 0, "children": 1})["flights"][0]
        pax = [{"first_name": kid["name"].split()[0], "last_name": kid["name"].split()[-1], "type": "child", "date_of_birth": kid["date_of_birth"], "companion_id": kid["companion_id"]}]
        r = t.call("create_booking", {"flight_ids": [fl["flight_id"]], "fare_family": "STANDARD", "passengers": pax})
        self.assertEqual(r["error"], "UMNR_REQUIRED")
        r = t.call("create_booking", {"flight_ids": [fl["flight_id"]], "fare_family": "STANDARD", "passengers": pax, "unaccompanied_minor": True})
        self.assertEqual(r["status"], "HELD", r)
        self.assertEqual(r["totals_inr"]["ancillaries"], 5000)

    def test_expired_card(self):
        g = GT["expired_saved_card"]
        s, t = session("expired_saved_card", login=True)
        fl = next(f for f in t.call("search_flights", {"origin": s.customers[g["customer_id"]]["home_airport"], "destination": "DEL", "date": "2026-10-20", "adults": 1})["flights"] if f["bookable"])
        c = s.customers[g["customer_id"]]
        h = t.call("create_booking", {"flight_ids": [fl["flight_id"]], "fare_family": "STANDARD", "passengers": [{"first_name": c["first_name"], "last_name": c["last_name"], "type": "adult"}]})
        pm = t.call("get_customer_profile", {"customer_id": g["customer_id"]})["saved_payment_methods"][0]
        self.assertTrue(pm["expired"])
        self.assertEqual(t.call("confirm_payment", {"pnr": h["pnr"], "method": "saved_method", "payment_method_id": pm["id"]})["error"], "CARD_EXPIRED")
        link = t.call("send_payment_link", {"pnr": h["pnr"], "channel": "sms"})["payment_link"]
        self.assertEqual(t.call("confirm_payment", {"pnr": h["pnr"], "method": "payment_link"})["status"], "PAYMENT_PENDING")
        t.pay_link(link["link_id"])
        self.assertEqual(t.call("confirm_payment", {"pnr": h["pnr"], "method": "payment_link"})["status"], "CONFIRMED")
        self.assertEqual(s.bookings[h["pnr"]]["status"], "confirmed")

    def test_duplicate_warning(self):
        g = GT["duplicate_booking"]
        s, t = session("duplicate_booking", login=True)
        b = t.call("get_booking", {"pnr": g["pnr"]})
        pax = [{"first_name": p["name"].split()[0], "last_name": p["name"].split()[-1], "type": p["type"],
                "date_of_birth": next(x for x in s.passengers(g["pnr"]) if x["id"] == p["passenger_id"])["date_of_birth"]} for p in b["passengers"]]
        r = t.call("create_booking", {"flight_ids": [b["segments"][0]["flight_id"]], "fare_family": "STANDARD", "passengers": pax})
        self.assertEqual(r["error"], "DUPLICATE_WARNING")

    def test_name_corrections(self):
        g = GT["name_corrections"]
        s, t = session()
        t.call("verify_booking", {"pnr": g["pnr"], "last_name": "Chopda"})
        pax = t.call("get_booking", {"pnr": g["pnr"]})["passengers"]
        pr = next(p for p in pax if p["name"].startswith("Priyanka"))
        mo = next(p for p in pax if p["name"].startswith("Mohd"))
        self.assertEqual(t.call("correct_name", {"pnr": g["pnr"], "passenger_id": pr["passenger_id"], "new_first_name": "Priyanka", "new_last_name": "Chopra"})["status"], "CORRECTED")
        self.assertEqual(t.call("correct_name", {"pnr": g["pnr"], "passenger_id": mo["passenger_id"], "new_first_name": "Mohammed", "new_last_name": "Arif"})["error"], "NOT_ALLOWED")

    def test_exit_row_infant(self):
        g = GT["exit_row_with_infant"]
        s, t = session()
        verify(t, "exit_row_with_infant")
        b = t.call("get_booking", {"pnr": g["pnr"]})
        sg = b["segments"][0]
        m = t.call("get_seat_map", {"pnr": g["pnr"], "segment_id": sg["segment_id"]})
        exit_seat = m["available"]["exit_row"][0]
        adult = next(p for p in b["passengers"] if p["type"] == "adult")
        r = t.call("select_seat", {"pnr": g["pnr"], "segment_id": sg["segment_id"], "passenger_id": adult["passenger_id"], "seat": exit_seat, "payment_method": "payment_link"})
        self.assertEqual(r["error"], "NOT_ALLOWED")

    def test_points_and_goodwill(self):
        g = GT["points_expiring_tomorrow"]
        s, t = session("points_expiring_tomorrow", login=True)
        r = t.call("loyalty_balance", {"customer_id": g["customer_id"]})
        self.assertEqual(r["points_balance"], g["expected"]["balance"])
        self.assertIn({"expires_on": "2026-10-07", "points": 4200}, r["expiring_next_90_days"])
        g2 = GT["goodwill_already_given"]
        s2, t2 = session("goodwill_already_given", login=True)
        self.assertEqual(t2.call("issue_goodwill_credit", {"customer_id": g2["customer_id"], "amount_inr": 500, "reason": "delay_120_plus_minutes"})["error"], "GOODWILL_LIMIT")

    def test_refund_overdue(self):
        g = GT["refund_overdue"]
        s, t = session()
        verify(t, "refund_overdue")
        r = t.call("get_refund_status", {"pnr": g["pnr"]})
        self.assertTrue(r["refunds"][0]["overdue"])
        self.assertEqual(r["refunds"][0]["amount"], g["expected"]["refund_amount"])

    def test_upgrade_quote(self):
        s, t = session(guardrails="off")
        ups = [b for b in s.db["bookings"] if b["status"] == "confirmed" and b["fare_family"] in ("STANDARD", "FLEX")]
        for b in ups:
            sg = s.segments(b["pnr"])[0]
            f = s.flights.get(sg["flight_id"])
            if f and "BUSINESS" in f["fares"] and f["status"] == "scheduled":
                q = t.call("quote_change", {"pnr": b["pnr"], "segment_id": sg["id"], "new_flight_id": f["id"], "new_fare_family": "BUSINESS"})
                if "error" in q and q["error"] == "CUTOFF_PASSED":
                    continue
                self.assertTrue(q["upgrade"], q)
                self.assertGreater(q["total_due_inr"], 0)
                return
        self.skipTest("no upgradeable booking for this user")

    def test_fixes_from_live_runs(self):
        g = GT["refund_overdue"]
        s, t = session()
        verify(t, "refund_overdue")
        r = t.call("issue_goodwill_credit", {"pnr": g["pnr"], "amount_inr": 500, "reason": "refund_overdue"})
        self.assertEqual(r["status"], "CREDITED", r)
        r2 = t.call("search_flights", {"origin": "BLR", "destination": "DEL", "date": "2026-10-10", "adults": 0, "children": 1, "unaccompanied_minor": True})
        f = r2["flights"][0]["fares"]["STANDARD"]
        self.assertEqual(f["total_inr"], f["per_adult_inr"] + 5000)
        g2 = GT["delay_190_minutes"]
        s3, t3 = session()
        verify(t3, "delay_190_minutes")
        sg = first_seg(t3, g2["pnr"])
        st = t3.call("get_flight_status", {"flight_number": sg["flight_number"], "date": sg["departs_local"][:10]})
        self.assertEqual(st["passenger_entitlements"]["meal_voucher_inr"], 300)
        a = t.call("create_support_ticket", {"category": "other", "summary": "x"})["ticket_no"]
        b = t3.call("create_support_ticket", {"category": "other", "summary": "y"})["ticket_no"]
        self.assertNotEqual(a, b)

    def test_every_tool_is_implemented(self):
        names = [x["function"]["name"] for x in json.loads((ROOT / "tools" / "tools.json").read_text())["tools"]]
        missing = [n for n in names if not hasattr(Tools, "t_" + n)]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
