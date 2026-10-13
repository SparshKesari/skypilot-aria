"""Implementations of every tool in tools/tools.json.

Business rules (fees, cutoffs, fare rules, availability) are ALWAYS enforced here,
because a real airline back end would enforce them. Identity checks are enforced
only when session.guardrails == "strict"; with "off" the model alone must follow
the ID-* policies, which is what the benchmark measures.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import rules as R  # noqa: E402

from store import Session  # noqa: E402

import itertools
_TICKETS = itertools.count(200001)
_CASES = itertools.count(50001)
RULES = R.RULES
FF = R.FF
AIRCRAFT_SEAT_TYPES = ("standard", "preferred", "exit_row")


class ToolError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def err(code, message, **extra):
    raise ToolError(code, message, **extra)


# ---------------------------------------------------------------- helpers
def mask_phone(p: str) -> str:
    return f"{p[:3]} ••••• ••{p[-3:]}" if p else None


def mask_email(e: str) -> str:
    if not e:
        return None
    user, dom = e.split("@", 1)
    return f"{user[0]}•••@{dom}"


def age_on(dob: str | None, day: date) -> float | None:
    if not dob:
        return None
    return (day - date.fromisoformat(dob)).days / 365.25


def money(n) -> int:
    return int(round(n))


class Tools:
    def __init__(self, s: Session):
        self.s = s

    # ============================================================ dispatch
    def call(self, name: str, args: dict) -> dict:
        t0 = time.time()
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            result = {"error": "UNKNOWN_TOOL", "message": f"No tool named {name}"}
        elif self.s.locked and name not in ("handoff_to_human", "search_knowledge_base", "get_flight_status", "search_flights", "create_support_ticket"):
            result = {"error": "LOCKED", "message": "Self-service is locked after repeated failed verification. Hand off to a human."}
        else:
            try:
                result = fn(**(args or {}))
            except ToolError as e:
                result = {"error": e.code, "message": e.message, **e.extra}
            except TypeError as e:
                result = {"error": "BAD_ARGUMENTS", "message": str(e)}
            except Exception as e:  # noqa: BLE001
                result = {"error": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}"}
        self.s.trace.append({"type": "tool", "name": name, "args": args, "result": result, "ms": int((time.time() - t0) * 1000),
                             "at": self.s.now().isoformat(timespec="seconds")})
        return result

    # ============================================================ access control
    def _owner_ids(self, pnr):
        b = self.s.bookings[pnr]
        ids = {b["customer_id"]}
        ids |= {p["customer_id"] for p in self.s.passengers(pnr) if p.get("customer_id")}
        return ids

    def booking(self, pnr: str, need_access=True) -> dict:
        pnr = (pnr or "").strip().upper()
        b = self.s.bookings.get(pnr)
        if not b:
            err("NOT_FOUND", "No booking with that reference.")
        if need_access and self.s.guardrails == "strict" and not self.has_booking_access(pnr):
            err("NOT_VERIFIED", "Booking-level verification needed: PNR + passenger last name, or login (ID-2).")
        return b

    def has_booking_access(self, pnr):
        owners = self._owner_ids(pnr)
        return (pnr in self.s.verified_pnrs or self.s.logged_in in owners or bool(owners & self.s.verified_customers))

    def account(self, cid: str, need_otp=False) -> dict:
        c = self.s.customers.get(cid)
        if not c:
            err("NOT_FOUND", "No customer with that id.")
        if self.s.guardrails == "strict":
            if need_otp and cid not in self.s.otp_verified:
                err("NOT_VERIFIED", "OTP to the current registered contact is required (ID-7).")
            if not (self.s.logged_in == cid or cid in self.s.verified_customers):
                err("NOT_VERIFIED", "Account-level verification needed: OTP or login (ID-3).")
        return c

    def fraud_check(self, cid):
        c = self.s.customers.get(cid)
        if c and c.get("fraud_flag") and self.s.guardrails == "strict":
            err("ACCOUNT_REVIEW", "This account cannot be changed by the assistant. Hand off with reason account_review (ID-11). Do not mention fraud.")

    def tier_of(self, cid):
        l = self.s.loyalty(cid) if cid else None
        return l["tier"] if l else "BLUE"

    def seg(self, pnr, segment_id):
        sg = next((x for x in self.s.segments(pnr) if x["id"] == segment_id), None)
        if not sg:
            err("NOT_FOUND", "Segment not on this booking.")
        return sg

    def seg_family(self, b, sg):
        return sg.get("fare_family") or b["fare_family"]

    def route_type(self, o, d):
        return "domestic" if self.s.airports[o]["country"] == "India" and self.s.airports[d]["country"] == "India" else "international"

    def cutoff_check(self, dep_local, rt, kind="change_or_cancel"):
        if R.cutoff_passed(dep_local, rt, self.s.now(), kind):
            h = RULES["service_cutoffs_hours_before_departure"][kind][rt]
            err("CUTOFF_PASSED", f"Not possible within {h} hours of departure ({kind.replace('_', ' ')}).", cutoff_hours=h)

    def flight_view(self, f, pax_count=1, compact=False):
        d = next((x for x in self.s.db["disruptions"] if x["flight_id"] == f["id"]), None)
        v = {"flight_id": f["id"], "flight_number": f["flight_number"], "origin": f["origin"], "destination": f["destination"],
             "departs_local": f["scheduled_departure_local"], "arrives_local": f["scheduled_arrival_local"],
             "duration_minutes": f["block_minutes"], "aircraft": f["aircraft"], "status": f["status"]}
        if f.get("estimated_departure_local"):
            v["estimated_departure_local"] = f["estimated_departure_local"]
        if d and not compact:
            v["disruption"] = {k: d[k] for k in ("type", "minutes", "reason", "notified_at", "new_departure_local")}
        return v

    def charge(self, b, amount, method, payment_method_id, description, apply_fn, replay=None):
        """Take payment for an add-on/change, or create a payment link and defer the action.

        Deferred actions are stored as a `replay` descriptor (tool name + args), never as a closure, so a session can be
        serialised between turns (stateless hosting). On confirm_payment the tool is re-run with payment_method
        "prepaid_link", which applies the change without charging again."""
        if amount <= 0 or method == "prepaid_link":
            return {"charged": 0 if amount <= 0 else amount, **apply_fn()}
        if method == "payment_link":
            link = self._make_link(b, amount, description)
            self.s.pending[link["link_id"]] = {"replay": replay, "amount": amount, "pnr": b["pnr"], "description": description}
            return {"status": "PAYMENT_PENDING", "amount_due": amount, "payment_link": link,
                    "message": "Payment link sent. Call confirm_payment with method payment_link after the customer pays."}
        self._pay(b, amount, method, payment_method_id, description)
        return {"charged": amount, **apply_fn()}

    def _replay(self, p):
        """Apply a deferred (now paid) action from its descriptor."""
        r = p.get("replay") or {}
        if r.get("kind") == "confirm_held":
            return self._confirm_held(self.booking(r["pnr"]), "payment_link", None, already_paid=True)
        if r.get("kind") == "tool":
            return getattr(self, "t_" + r["name"])(**r["args"], payment_method="prepaid_link")
        err("INTERNAL_ERROR", "Pending payment has no replayable action.")

    def _pay(self, b, amount, method, pm_id, description):
        cid = b["customer_id"]
        if method == "saved_method":
            pm = next((m for m in self.s.db["payment_methods"] if m["id"] == pm_id and m["customer_id"] == cid), None)
            if not pm:
                err("PAYMENT_METHOD_NOT_FOUND", "That saved payment method is not on this account.")
            if self.s.guardrails == "strict" and not (self.s.logged_in == cid or cid in self.s.verified_customers):
                err("NOT_VERIFIED", "Using saved payment methods needs account-level verification (ID-3).")
            if pm["type"] == "card":
                mm, yy = map(int, pm["expiry"].split("/"))
                last_day = (date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1))
                if last_day < self.s.now().date():
                    err("CARD_EXPIRED", f"{pm['brand']} ending {pm['last4']} expired {pm['expiry']} (BK-8).")
            mtype = pm["type"]
        elif method == "skywallet":
            c = self.s.customers.get(cid)
            if not c or c["skywallet_balance"] < amount:
                err("INSUFFICIENT_BALANCE", f"SkyWallet balance is {c['skywallet_balance'] if c else 0} INR.")
            c["skywallet_balance"] -= amount
            self.s.db["wallet_transactions"].append({"id": self.s.new_id("WTX"), "customer_id": cid, "amount": amount, "type": "debit", "reason": description, "date": self.s.now().isoformat()})
            mtype = "skywallet"
        elif method == "payment_link":
            mtype = "payment_link"
        else:
            err("BAD_ARGUMENTS", "method must be saved_method, skywallet or payment_link")
        self.s.db["payments"].append({"id": self.s.new_id("PAY"), "pnr": b["pnr"], "amount": amount, "method_type": mtype, "payment_method_id": pm_id,
                                      "status": "captured", "paid_at": self.s.now().isoformat(), "description": description})

    def _make_link(self, b, amount, description):
        link_id = self.s.new_id("PL")
        dest = b["contact"]["phone"] if b.get("contact") else None
        link = {"link_id": link_id, "pnr": b["pnr"], "amount": amount, "description": description, "status": "unpaid",
                "url": f"/pay/{self.s.id}/{link_id}", "sent_to": mask_phone(dest) if dest else "contact on booking"}
        self.s.paylinks[link_id] = link
        self.s.outbox.append({"channel": "sms", "to": dest, "text": f"SkyPilot payment link for {b['pnr']}: INR {amount}", "link_id": link_id})
        return link

    # ============================================================ identity
    def t_lookup_customer(self, phone=None, email=None, skypoints_id=None):
        if not (phone or email or skypoints_id):
            err("BAD_ARGUMENTS", "Give a phone, email or SkyPoints number.")
        norm = lambda p: re.sub(r"[^\d+]", "", p or "")  # noqa: E731
        res = []
        for c in self.s.db["customers"]:
            l = self.s.loyalty(c["id"])
            if (phone and norm(c["phone"])[-10:] == norm(phone)[-10:]) or (email and c["email"].lower() == email.strip().lower()) or \
               (skypoints_id and l and l["skypoints_id"].upper() == skypoints_id.strip().upper()):
                res.append({"customer_id": c["id"], "phone_on_file": mask_phone(c["phone"]), "email_on_file": mask_email(c["email"])})
        return {"matches": len(res), "customers": res,
                "note": "Do not reveal to an unverified user whether an account exists (ID-8). Verify with send_otp."}

    def t_send_otp(self, customer_id, channel="sms"):
        c = self.s.customers.get(customer_id)
        if not c:
            err("NOT_FOUND", "No customer with that id.")
        code = self.s.auth[customer_id]["test_otp"]
        dest = c["phone"] if channel == "sms" else c["email"]
        self.s.otps[customer_id] = {"code": code, "expires": self.s.now() + timedelta(minutes=10)}
        self.s.outbox.append({"channel": channel, "to": dest, "text": f"Your SkyPilot code is {code}"})
        return {"sent": True, "channel": channel, "destination": mask_phone(dest) if channel == "sms" else mask_email(dest), "valid_minutes": 10}

    def _fail(self):
        self.s.failed_verifications += 1
        if self.s.failed_verifications >= RULES["identity"]["max_failed_attempts"]:
            self.s.locked = True
            return {"verified": False, "attempts": self.s.failed_verifications, "locked": True,
                    "message": "Second failed attempt. Self-service locked. Hand off to a human (ID-5)."}
        return {"verified": False, "attempts": self.s.failed_verifications, "locked": False}

    def t_verify_otp(self, customer_id, code):
        o = self.s.otps.get(customer_id)
        if not o:
            err("NO_OTP", "No code was sent to this customer. Call send_otp first.")
        if self.s.now() > o["expires"]:
            err("OTP_EXPIRED", "Code expired. Send a new one.")
        if str(code).strip() != o["code"]:
            return self._fail()
        self.s.verified_customers.add(customer_id)
        self.s.otp_verified.add(customer_id)
        c = self.s.customers[customer_id]
        return {"verified": True, "level": "account", "customer_id": customer_id, "first_name": c["first_name"], "last_name": c["last_name"]}

    def t_verify_booking(self, pnr, last_name):
        pnr = (pnr or "").strip().upper()
        b = self.s.bookings.get(pnr)
        ok = b and any(p["last_name"].lower() == (last_name or "").strip().lower() for p in self.s.passengers(pnr))
        if not ok:
            return self._fail()
        self.s.verified_pnrs.add(pnr)
        return {"verified": True, "level": "booking", "pnr": pnr}

    # ============================================================ account & bookings
    def t_get_customer_profile(self, customer_id):
        c = self.account(customer_id)
        l = self.s.loyalty(customer_id)
        today = self.s.now().date()
        pms = []
        for m in self.s.db["payment_methods"]:
            if m["customer_id"] != customer_id:
                continue
            if m["type"] == "card":
                mm, yy = map(int, m["expiry"].split("/"))
                expired = date(yy + (mm == 12), mm % 12 + 1, 1) <= today
                pms.append({"id": m["id"], "type": "card", "label": f"{m['brand']} ending {m['last4']}", "expiry": m["expiry"], "expired": expired, "default": m["is_default"]})
            else:
                pms.append({"id": m["id"], "type": "upi", "label": f"UPI {m['upi_handle'][:2]}•••@{m['upi_handle'].split('@')[1]}", "default": m["is_default"]})
        comps = [{"companion_id": x["id"], "name": f"{x['first_name']} {x['last_name']}", "relation": x["relation"],
                  "age": int(age_on(x["date_of_birth"], today)), "date_of_birth": x["date_of_birth"], "nominee": x["is_nominee"]}
                 for x in self.s.db["companions"] if x["customer_id"] == customer_id]
        docs = [{"holder": d["holder"], "type": d["type"], "number_masked": d["number"][0] + "•••••" + d["number"][-2:], "expiry_date": d["expiry_date"],
                 "expired": d["expiry_date"] < today.isoformat()} for d in self.s.db["travel_documents"] if d["customer_id"] == customer_id]
        return {"customer_id": c["id"], "name": f"{c['first_name']} {c['last_name']}", "phone": mask_phone(c["phone"]), "email": mask_email(c["email"]),
                "date_of_birth": c["date_of_birth"], "preferred_language": c["preferred_language"], "home_airport": c["home_airport"],
                "preferences": c["preferences"], "fraud_flag": c["fraud_flag"], "skywallet_balance_inr": c["skywallet_balance"],
                "skypoints": {"id": l["skypoints_id"], "tier": l["tier"], "balance": l["points_balance"]} if l else None,
                "saved_payment_methods": pms, "companions": comps, "travel_documents": docs}

    def t_list_bookings(self, customer_id, status="upcoming"):
        self.account(customer_id)
        out = []
        now = self.s.now()
        for b in self.s.db["bookings"]:
            if b["customer_id"] != customer_id:
                continue
            segs = self.s.segments(b["pnr"])
            first = segs[0]
            future = R.dt(segs[-1]["scheduled_departure_local"]) > now
            cat = "cancelled" if b["status"] == "cancelled" else ("upcoming" if future and b["status"] in ("confirmed", "held") else "past")
            if status != "all" and cat != status:
                continue
            out.append({"pnr": b["pnr"], "status": b["status"], "fare_family": b["fare_family"], "created_at": b["created_at"],
                        "route": " / ".join(f"{s['origin']}-{s['destination']} {s['scheduled_departure_local'][:16]}" for s in segs),
                        "passengers": len(self.s.passengers(b["pnr"])), "total_inr": b["totals"]["total"], "first_departure": first["scheduled_departure_local"]})
        out.sort(key=lambda x: x["first_departure"], reverse=(status != "upcoming"))
        return {"count": len(out), "bookings": out[:25], "truncated": len(out) > 25}

    def allowance(self, b, sg, p):
        fam = self.seg_family(b, sg)
        rt = self.route_type(sg["origin"], sg["destination"])
        kg = FF[fam]["checked_bag_kg"][rt]
        if p["type"] == "infant":
            return {"checked_kg": 7, "cabin": "none (stroller free at gate)"}
        bonus = RULES["baggage"]["loyalty_bonus_kg"][self.tier_of(p["customer_id"])] if p.get("customer_id") else 0
        return {"checked_kg": kg + bonus, "checked_pieces": FF[fam]["checked_bag_pieces"], "loyalty_bonus_kg": bonus,
                "cabin": f"{FF[fam]['cabin_bags']} x 7 kg + 3 kg personal item"}

    def t_get_booking(self, pnr):
        b = self.booking(pnr)
        pnr = b["pnr"]
        now = self.s.now()
        segs = self.s.segments(pnr)
        pax = self.s.passengers(pnr)
        first_dep = segs[0]["scheduled_departure_local"]
        out_segs = []
        for sg in segs:
            f = self.s.flights.get(sg["flight_id"]) if sg["flight_id"] else None
            rt = self.route_type(sg["origin"], sg["destination"])
            sp = {x["passenger_id"]: x for x in self.s.segpax(sg["id"])}
            out_segs.append({
                "segment_id": sg["id"], "flight_number": sg["flight_number"], "flight_id": sg["flight_id"], "origin": sg["origin"], "destination": sg["destination"],
                "departs_local": sg["scheduled_departure_local"], "arrives_local": sg["scheduled_arrival_local"], "cabin": sg["cabin"],
                "fare_family": self.seg_family(b, sg), "status": sg["status"],
                "flight_status": self.flight_view(f) if f else {"status": "landed" if R.dt(sg["scheduled_departure_local"]) < now else "scheduled"},
                "change_or_cancel_cutoff_passed": R.cutoff_passed(sg["scheduled_departure_local"], rt, now),
                "passengers": [{"passenger_id": p["id"], "name": f"{p['first_name']} {p['last_name']}", "seat": sp[p["id"]]["seat"],
                                "extra_bag_kg": sp[p["id"]]["extra_bag_kg"], "meal": sp[p["id"]]["meal"], "special_assistance": sp[p["id"]]["special_assistance"],
                                "status": sp[p["id"]].get("status", "active"), "allowance": self.allowance(b, sg, p)} for p in pax if p["id"] in sp]})
        paid = sum(p["amount"] for p in self.s.db["payments"] if p["pnr"] == pnr)
        return {"pnr": pnr, "status": b["status"], "fare_family": b["fare_family"], "route_type": b["route_type"], "created_at": b["created_at"],
                "hold_expires_at": b.get("hold_expires_at"),
                "passengers": [{"passenger_id": p["id"], "name": f"{p['first_name']} {p['last_name']}", "type": p["type"],
                                "age": int(age_on(p["date_of_birth"], now.date())) if p["type"] != "adult" and p.get("date_of_birth") else None,
                                "is_account_holder": p.get("customer_id") == b["customer_id"], "special_request_note": p.get("special_request_note")} for p in pax],
                "segments": out_segs, "totals_inr": b["totals"], "amount_paid_inr": paid, "amount_due_inr": b.get("amount_due", 0),
                "look_in_eligible": R.look_in_eligible(b["created_at"], first_dep, b["route_type"], now),
                "unaccompanied_minor": b.get("unaccompanied_minor", False), "booking_notes": b.get("notes"),
                "refunds": [{k: r[k] for k in ("id", "amount", "status", "initiated_at")} for r in self.s.db["refunds"] if r["pnr"] == pnr]}

    def t_get_conversation_history(self, customer_id, limit=5):
        self.account(customer_id)
        conv = sorted([c for c in self.s.db["conversation_history"] if c["customer_id"] == customer_id], key=lambda c: c["started_at"], reverse=True)
        tickets = sorted([t for t in self.s.db["support_tickets"] if t["customer_id"] == customer_id], key=lambda t: t["created_at"], reverse=True)
        return {"conversations": [{k: c[k] for k in ("started_at", "channel", "topic", "summary", "pnr", "resolved")} for c in conv[:limit]],
                "recent_tickets": [{k: t[k] for k in ("ticket_no", "category", "status", "created_at", "summary")} for t in tickets[:limit]],
                "goodwill_last_90_days": [g for g in self.s.db["goodwill_credits"] if g["customer_id"] == customer_id and R.dt(g["date"]) > self.s.now() - timedelta(days=90)]}

    # ============================================================ search & book
    def fare_for(self, f, fam, n_adults, n_children, n_infants):
        fare = f["fares"].get(fam)
        if not fare:
            return None
        per = fare["base_fare"] + fare["taxes"]
        inf = RULES["passengers"]["infant"]["fee"]["domestic"] if f["route_type"] == "domestic" else int(fare["base_fare"] * 0.10)
        return {"per_adult_inr": per, "base_fare": fare["base_fare"], "taxes": fare["taxes"], "seats_left": fare["seats_left"],
                "total_inr": per * (n_adults + n_children) + inf * n_infants, "infant_fee_each": inf if n_infants else None}

    def t_search_flights(self, origin, destination, date, adults=1, children=0, infants=0, cabin="economy", unaccompanied_minor=False):
        o, d = origin.strip().upper(), destination.strip().upper()
        if o not in self.s.airports or d not in self.s.airports:
            err("UNKNOWN_AIRPORT", "SkyPilot does not fly to that airport. Destinations: " + ", ".join(sorted(self.s.airports)))
        if f"{o}-{d}" not in self.s.routes:
            err("NO_ROUTE", f"No direct SkyPilot route {o}-{d}. Suggest connecting via DEL or BOM with two bookings, or another date.")
        if adults + children < 1:
            err("BAD_ARGUMENTS", "At least one adult or child.")
        if adults + children > 9:
            err("GROUP_BOOKING", "More than 9 passengers. Hand off for group booking (BK-3).")
        if infants > adults:
            err("NOT_ALLOWED", "One infant per adult (BK-5).")
        now = self.s.now()
        res = []
        for f in self.s.db["flights"]:
            if f["origin"] != o or f["destination"] != d or not f["scheduled_departure_local"].startswith(date):
                continue
            v = self.flight_view(f, compact=True)
            closed = R.cutoff_passed(f["scheduled_departure_local"], f["route_type"], now, "new_booking")
            v["bookable"] = f["status"] not in ("cancelled", "departed", "landed") and not closed
            fams = ["BUSINESS"] if cabin == "business" else ["BASIC", "STANDARD", "FLEX"]
            v["fares"] = {}
            for fam in fams:
                q = self.fare_for(f, fam, adults, children, infants)
                if q:
                    q["available"] = q["seats_left"] >= adults + children
                    if unaccompanied_minor:
                        fee = RULES["passengers"]["unaccompanied_minor"]["fee_per_segment"][f["route_type"]] * (adults + children)
                        q["unaccompanied_minor_fee_inr"] = fee
                        q["total_inr"] += fee
                    v["fares"][fam] = q
            if not v["fares"]:
                v["note"] = "No Business cabin on this aircraft."
            res.append(v)
        res.sort(key=lambda v: v["departs_local"])
        if not res:
            return {"flights": [], "message": "No flights on that date. The schedule runs until " + self.s.db["meta"]["inventory_window"][1]}
        return {"origin": o, "destination": d, "date": date, "passengers": {"adults": adults, "children": children, "infants": infants}, "flights": res}

    def t_get_fare_rules(self, fare_family, route_type, pnr=None):
        rt = route_type
        r = FF[fare_family]
        tier = None
        if pnr:
            b = self.booking(pnr)
            tier = self.tier_of(b["customer_id"])
        seat_prices = {k: (0 if (k in RULES["seats"]["free_for"].get(fare_family, []) or (tier and k in RULES["seats"]["free_for"].get(f"tier_{tier}", [])) or fare_family == "BUSINESS")
                           else RULES["seats"]["types"][k]["price"][rt]) for k in AIRCRAFT_SEAT_TYPES}
        return {"fare_family": fare_family, "route_type": rt,
                "change": "not allowed" if not r["change"]["allowed"] else {"fee_inr": r["change"]["fee"][rt], "plus": "fare difference"},
                "cancel": "taxes refunded only" if not r["cancel"]["refundable"] else {"fee_inr": r["cancel"]["fee"][rt], **({"late_fee_inr_within_24h": r["cancel"]["late_fee"][rt]} if "late_fee" in r["cancel"] else {})},
                "no_show": r["no_show"]["refund"] if r["no_show"]["refund"] == "taxes_only" else {"fee_inr": r["no_show"]["fee"][rt]},
                "checked_baggage_kg": r["checked_bag_kg"][rt], "checked_pieces": r["checked_bag_pieces"], "cabin_bags": r["cabin_bags"],
                "meal_included": rt in RULES["meals"]["included"][fare_family], "meal_price_inr": RULES["meals"]["price"][rt],
                "seat_prices_inr": seat_prices, "loyalty_tier_applied": tier,
                "prepaid_baggage_packs": RULES["baggage"]["prepaid_packs"][rt], "airport_excess_per_kg_inr": RULES["baggage"]["airport_excess_per_kg"][rt],
                "look_in_option": f"free change/cancel within 24h of booking if departure is {RULES['look_in_option']['min_days_before_departure'][rt]}+ days away",
                "points_multiplier": r["points_multiplier"]}

    def t_create_booking(self, flight_ids, fare_family, passengers, allow_duplicate=False, unaccompanied_minor=False,
                         contact_customer_id=None, contact_phone=None, contact_email=None):
        now = self.s.now()
        if not passengers:
            err("BAD_ARGUMENTS", "At least one passenger.")
        flights = []
        for fid in flight_ids:
            f = self.s.flights.get(fid)
            if not f:
                err("NOT_FOUND", f"Unknown flight id {fid}.")
            if f["status"] in ("cancelled", "departed", "landed"):
                err("NO_AVAILABILITY", f"{f['flight_number']} is {f['status']}.")
            self.cutoff_check(f["scheduled_departure_local"], f["route_type"], "new_booking")
            if fare_family not in f["fares"]:
                err("NO_AVAILABILITY", f"{fare_family} not sold on {f['flight_number']}.")
            flights.append(f)
        travel_day = R.dt(flights[0]["scheduled_departure_local"]).date()
        types = []
        for p in passengers:
            a = age_on(p.get("date_of_birth"), travel_day)
            t = p.get("type") or ("infant" if a is not None and a < 2 else "child" if a is not None and a < 12 else "adult")
            if t in ("child", "infant") and not p.get("date_of_birth"):
                err("MISSING_INFO", f"Date of birth needed for {p.get('first_name')} ({t}).")
            if t == "infant" and a is not None and a * 365.25 < RULES["passengers"]["infant"]["min_age_days"]:
                err("NOT_ALLOWED", "Infants must be at least 7 days old (BK-5).")
            types.append((t, a))
        n_ad = sum(1 for t, _ in types if t == "adult" and (_ is None or _ >= 18))
        n_ad_any = sum(1 for t, _ in types if t == "adult")
        n_ch = sum(1 for t, _ in types if t == "child")
        n_inf = sum(1 for t, _ in types if t == "infant")
        if n_ad_any + n_ch > RULES["passengers"]["max_per_booking"]:
            err("GROUP_BOOKING", "More than 9 passengers (BK-3).")
        if n_inf > n_ad_any:
            err("NOT_ALLOWED", "One infant per adult (BK-5).")
        umnr_needed = n_ad == 0
        if umnr_needed:
            ages = [a for t, a in types if a is not None]
            if any(a < 5 for a in ages):
                err("NOT_ALLOWED", "Children under 5 cannot travel without an adult (BK-6).")
            if any(5 <= a < 12 for a in ages) and not unaccompanied_minor:
                err("UMNR_REQUIRED", "A child aged 5-11 travelling alone must have the Unaccompanied Minor service. Set unaccompanied_minor true and tell the customer the fee (BK-6).",
                    fee_per_segment_inr={fl["route_type"]: RULES["passengers"]["unaccompanied_minor"]["fee_per_segment"][fl["route_type"]] for fl in flights})
        for f in flights:
            if f["fares"][fare_family]["seats_left"] < n_ad_any + n_ch:
                err("NO_AVAILABILITY", f"Not enough {fare_family} seats on {f['flight_number']}.")
        if not allow_duplicate:
            names = {(p["first_name"].lower(), p["last_name"].lower()) for p in passengers}
            for sg in self.s.db["booking_segments"]:
                if sg["flight_id"] in flight_ids and sg["status"] == "confirmed":
                    other = {(p["first_name"].lower(), p["last_name"].lower()) for p in self.s.passengers(sg["pnr"])}
                    if names & other and self.s.bookings[sg["pnr"]]["status"] == "confirmed":
                        err("DUPLICATE_WARNING", f"These passengers already hold booking {sg['pnr']} on {sg['flight_number']}. Ask the customer before booking again (BK-11).",
                            existing_pnr=sg["pnr"] if self.s.guardrails == "off" or self.has_booking_access(sg["pnr"]) else "hidden")
        cust = None
        if contact_customer_id:
            cust = self.account(contact_customer_id)
            self.fraud_check(contact_customer_id)
        elif self.s.logged_in:
            cust = self.s.customers[self.s.logged_in]
        if not cust and not (contact_phone or contact_email):
            err("MISSING_INFO", "Guest bookings need a contact phone or email.")
        pnr = "".join(random.Random(f"{self.s.id}{len(self.s.db['bookings'])}{time.time()}").choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        b = {"pnr": pnr, "customer_id": cust["id"] if cust else None, "status": "held", "fare_family": fare_family,
             "route_type": "international" if any(f["route_type"] == "international" for f in flights) else "domestic",
             "created_at": now.isoformat(), "channel": self.s.channel, "currency": "INR",
             "contact": {"email": cust["email"], "phone": cust["phone"]} if cust else {"email": contact_email, "phone": contact_phone},
             "notes": None, "hold_expires_at": (now + timedelta(minutes=20)).isoformat(), "unaccompanied_minor": bool(umnr_needed and unaccompanied_minor)}
        pax_recs = []
        for p, (t, a) in zip(passengers, types):
            rec = {"id": self.s.new_id("PAX"), "pnr": pnr, "first_name": p["first_name"].strip(), "last_name": p["last_name"].strip(), "type": t,
                   "date_of_birth": p.get("date_of_birth"), "companion_id": p.get("companion_id"),
                   "customer_id": cust["id"] if cust and p["first_name"].lower() == cust["first_name"].lower() and p["last_name"].lower() == cust["last_name"].lower() else None,
                   "special_request_note": None}
            pax_recs.append(rec)
        base = tax = anc = 0
        segs = []
        for n, f in enumerate(flights):
            sid = self.s.new_id("SEG")
            segs.append({"id": sid, "pnr": pnr, "sequence": n + 1, "flight_id": f["id"], "flight_number": f["flight_number"], "origin": f["origin"],
                         "destination": f["destination"], "scheduled_departure_local": f["scheduled_departure_local"], "scheduled_arrival_local": f["scheduled_arrival_local"],
                         "cabin": "business" if fare_family == "BUSINESS" else "economy", "status": "confirmed"})
            fare = f["fares"][fare_family]
            for rec in pax_recs:
                if rec["type"] == "infant":
                    pb, pt = (RULES["passengers"]["infant"]["fee"]["domestic"] if f["route_type"] == "domestic" else int(fare["base_fare"] * 0.10)), 0
                else:
                    pb, pt = fare["base_fare"], fare["taxes"]
                umnr = RULES["passengers"]["unaccompanied_minor"]["fee_per_segment"][f["route_type"]] if b["unaccompanied_minor"] and rec["type"] != "infant" else 0
                self.s.db["segment_passengers"].append({"segment_id": sid, "passenger_id": rec["id"], "base_fare": pb, "taxes": pt, "seat": None, "seat_type": None,
                                                        "seat_price": 0, "extra_bag_kg": 0, "bag_price": 0, "meal": None, "meal_price": 0,
                                                        "umnr_fee": umnr, "special_assistance": "unaccompanied_minor" if umnr else None, "checked_in": False})
                base, tax, anc = base + pb, tax + pt, anc + umnr
        b["totals"] = {"base_fare": base, "taxes": tax, "ancillaries": anc, "total": base + tax + anc}
        b["amount_due"] = b["totals"]["total"]
        self.s.db["bookings"].append(b)
        self.s.bookings[pnr] = b
        self.s.db["booking_passengers"].extend(pax_recs)
        self.s.db["booking_segments"].extend(segs)
        self.s.verified_pnrs.add(pnr)
        return {"status": "HELD", "pnr": pnr, "hold_expires_at": b["hold_expires_at"], "fare_family": fare_family,
                "flights": [self.flight_view(f, compact=True) for f in flights],
                "passengers": [{"passenger_id": r["id"], "name": f"{r['first_name']} {r['last_name']}", "type": r["type"]} for r in pax_recs],
                "totals_inr": b["totals"], "amount_due_inr": b["amount_due"], "unaccompanied_minor": b["unaccompanied_minor"],
                "next": "Take payment with confirm_payment (saved method or SkyWallet) or send_payment_link. Not confirmed until paid (BK-9)."}

    def t_send_payment_link(self, pnr, channel="sms"):
        b = self.booking(pnr)
        pending = next((k for k, v in self.s.pending.items() if v["pnr"] == b["pnr"] and self.s.paylinks[k]["status"] == "unpaid"), None)
        if pending:
            return {"sent": True, "payment_link": self.s.paylinks[pending]}
        if b["status"] != "held" or b.get("amount_due", 0) <= 0:
            err("NOTHING_DUE", "No amount is due on this booking.")
        link = self._make_link(b, b["amount_due"], f"Booking {b['pnr']}")
        self.s.pending[link["link_id"]] = {"replay": {"kind": "confirm_held", "pnr": b["pnr"]}, "amount": b["amount_due"], "pnr": b["pnr"], "description": "booking"}
        return {"sent": True, "channel": channel, "payment_link": link, "message": "Ask the customer to pay using the link, then call confirm_payment with method payment_link."}

    def _confirm_held(self, b, method, pm_id, already_paid=False):
        now = self.s.now()
        if b["status"] != "held":
            err("NOT_HELD", "Booking is not awaiting payment.")
        if R.dt(b["hold_expires_at"]) < now:
            b["status"] = "expired"
            err("HOLD_EXPIRED", "The 20 minute hold expired. Search again and create a new booking.")
        if not already_paid:
            self._pay(b, b["amount_due"], method, pm_id, f"Booking {b['pnr']}")
        else:
            self.s.db["payments"].append({"id": self.s.new_id("PAY"), "pnr": b["pnr"], "amount": b["amount_due"], "method_type": "payment_link",
                                          "payment_method_id": None, "status": "captured", "paid_at": now.isoformat()})
        for sg in self.s.segments(b["pnr"]):
            f = self.s.flights[sg["flight_id"]]
            f["fares"][self.seg_family(b, sg)]["seats_left"] -= sum(1 for p in self.s.passengers(b["pnr"]) if p["type"] != "infant")
        paid = b["amount_due"]
        b["status"], b["amount_due"] = "confirmed", 0
        b.pop("hold_expires_at", None)
        self.s.outbox.append({"channel": "email", "to": b["contact"].get("email"), "text": f"Booking {b['pnr']} confirmed"})
        return {"status": "CONFIRMED", "pnr": b["pnr"], "amount_paid_inr": paid}

    def t_confirm_payment(self, pnr, method, payment_method_id=None):
        b = self.booking(pnr)
        if method == "payment_link":
            links = [l for l in self.s.paylinks.values() if l["pnr"] == b["pnr"]]
            if not links:
                err("NO_PAYMENT_LINK", "No payment link was sent for this booking.")
            unpaid = [l for l in links if l["status"] == "unpaid"]
            if unpaid:
                return {"status": "PAYMENT_PENDING", "message": "The customer has not paid the link yet.", "payment_link": unpaid[-1]}
            done = []
            for l in links:
                p = self.s.pending.pop(l["link_id"], None)
                if p:
                    done.append({"link_id": l["link_id"], **self._replay(p)})
            if not done:
                err("NOTHING_DUE", "Nothing is waiting for payment.")
            return {"status": "CONFIRMED", "pnr": b["pnr"], "applied": done}
        if b["status"] == "held":
            return self._confirm_held(b, method, payment_method_id)
        err("NOTHING_DUE", "No held booking or pending charge. Charges for changes and extras are taken in the same tool call.")

    def pay_link(self, link_id):
        """Called by the frontend's simulated payment page."""
        l = self.s.paylinks.get(link_id)
        if not l:
            err("NOT_FOUND", "Unknown link")
        l["status"] = "paid"
        return l

    # ============================================================ changes
    def _check_changeable(self, b, sg):
        if sg["status"] not in ("confirmed",):
            err("NOT_ALLOWED", f"Segment status is {sg['status']}.")
        if b["status"] not in ("confirmed",):
            err("NOT_ALLOWED", f"Booking status is {b['status']}.")
        if R.dt(sg["scheduled_departure_local"]) < self.s.now():
            err("NOT_ALLOWED", "This flight has already departed (CH-10).")
        self.cutoff_check(sg["scheduled_departure_local"], self.route_type(sg["origin"], sg["destination"]))

    def t_quote_change(self, pnr, segment_id, new_flight_id, new_fare_family=None, passenger_ids=None):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        sg = self.seg(b["pnr"], segment_id)
        self._check_changeable(b, sg)
        nf = self.s.flights.get(new_flight_id)
        if not nf:
            err("NOT_FOUND", "Unknown new flight id.")
        if (nf["origin"], nf["destination"]) != (sg["origin"], sg["destination"]):
            err("ROUTE_CHANGE_NOT_ALLOWED", "Different origin or destination: cancel and rebook (CH-9).")
        if nf["status"] in ("cancelled", "departed", "landed"):
            err("NO_AVAILABILITY", f"{nf['flight_number']} is {nf['status']}.")
        self.cutoff_check(nf["scheduled_departure_local"], nf["route_type"], "new_booking")
        old_fam = self.seg_family(b, sg)
        new_fam = new_fare_family or old_fam
        upgrade = new_fam != old_fam
        if upgrade and old_fam == "BASIC":
            err("NOT_ALLOWED", "Basic fares cannot be upgraded or changed (CH-1, CH-11).")
        if upgrade and FF[new_fam]["price_factor"] < FF[old_fam]["price_factor"]:
            err("NOT_ALLOWED", "Downgrades are not offered; cancel and rebook.")
        if new_fam not in nf["fares"]:
            err("NO_AVAILABILITY", f"{new_fam} is not sold on {nf['flight_number']} (no Business cabin on {nf['aircraft']}).")
        if nf["id"] == sg["flight_id"] and not upgrade:
            err("NO_CHANGE", "That is the same flight and fare.")
        all_pax = [p for p in self.s.passengers(b["pnr"])]
        sel = [p for p in all_pax if not passenger_ids or p["id"] in passenger_ids]
        if nf["fares"][new_fam]["seats_left"] < sum(1 for p in sel if p["type"] != "infant"):
            err("NO_AVAILABILITY", "Not enough seats on the new flight.")
        segs = self.s.segments(b["pnr"])
        li = R.look_in_eligible(b["created_at"], segs[0]["scheduled_departure_local"], b["route_type"], self.s.now())
        rt = nf["route_type"]
        sps = {x["passenger_id"]: x for x in self.s.segpax(sg["id"])}
        lines = []
        for p in sel:
            sp = sps[p["id"]]
            if p["type"] == "infant":
                lines.append({"passenger_id": p["id"], "name": p["first_name"], "allowed": True, "change_fee": 0, "fare_difference": 0, "wallet_credit": 0, "total_due": 0})
                continue
            nfare = nf["fares"][new_fam]
            q = R.change_line(old_fam, rt, sp["base_fare"], sp["taxes"], nfare["base_fare"], nfare["taxes"], li)
            if not q["allowed"]:
                err("NOT_ALLOWED", "Basic fares cannot be changed (CH-1). Offer cancellation or a new booking.", look_in_eligible=li)
            lines.append({"passenger_id": p["id"], "name": f"{p['first_name']} {p['last_name']}", **q})
        total = sum(l["total_due"] for l in lines)
        qid = self.s.new_id("QC")
        self.s.quotes[qid] = {"kind": "change", "pnr": b["pnr"], "segment_id": segment_id, "new_flight_id": new_flight_id, "new_fare_family": new_fam,
                              "passenger_ids": [p["id"] for p in sel], "lines": lines, "total": total, "created": self.s.now()}
        split = len(sel) < len(all_pax)
        return {"quote_id": qid, "allowed": True, "look_in_applied": li, "from": self.flight_view(self.s.flights[sg["flight_id"]], compact=True) if sg["flight_id"] else sg["flight_number"],
                "to": self.flight_view(nf, compact=True), "fare_family": new_fam, "upgrade": upgrade, "lines": lines,
                "change_fee_total_inr": sum(l.get("change_fee", 0) for l in lines), "fare_difference_total_inr": sum(l.get("fare_difference", 0) for l in lines),
                "wallet_credit_total_inr": sum(l.get("wallet_credit", 0) for l in lines), "total_due_inr": total,
                "splits_booking": split, "note": "Seats are not carried over to a new flight." + (" Moved passengers get a new PNR (CH-7)." if split else ""), "valid_minutes": 15}

    def _quote(self, quote_id, kind, pnr):
        q = self.s.quotes.get(quote_id)
        if not q or q["kind"] != kind or q["pnr"] != pnr:
            err("QUOTE_INVALID", f"Unknown {kind} quote for this booking. Quote again.")
        if self.s.now() - q["created"] > timedelta(minutes=15):
            err("QUOTE_EXPIRED", "Quote older than 15 minutes. Quote again.")
        return q

    def _split(self, b, pax_ids):
        new_pnr = "".join(random.Random(self.s.id + b["pnr"] + str(time.time())).choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        nb = json.loads(json.dumps(b))
        nb.update(pnr=new_pnr, created_at=b["created_at"], notes=f"Split from {b['pnr']}")
        self.s.db["bookings"].append(nb)
        self.s.bookings[new_pnr] = nb
        for sg in self.s.segments(b["pnr"]):
            ns = dict(sg, id=self.s.new_id("SEG"), pnr=new_pnr)
            self.s.db["booking_segments"].append(ns)
            for sp in self.s.segpax(sg["id"]):
                if sp["passenger_id"] in pax_ids:
                    sp["segment_id"] = ns["id"]
        for p in self.s.passengers(b["pnr"]):
            if p["id"] in pax_ids:
                p["pnr"] = new_pnr
        for bk in (b, nb):
            sps = [sp for sg in self.s.segments(bk["pnr"]) for sp in self.s.segpax(sg["id"])]
            bk["totals"] = {"base_fare": sum(x["base_fare"] for x in sps), "taxes": sum(x["taxes"] for x in sps),
                            "ancillaries": sum(x["seat_price"] + x["bag_price"] + x["meal_price"] for x in sps)}
            bk["totals"]["total"] = sum(bk["totals"].values())
        self.s.verified_pnrs.add(new_pnr)
        return nb

    def t_change_booking(self, pnr, segment_id, new_flight_id, quote_id, new_fare_family=None, passenger_ids=None, payment_method=None, payment_method_id=None):
        b = self.booking(pnr)
        q = self._quote(quote_id, "change", b["pnr"])
        if q["segment_id"] != segment_id or q["new_flight_id"] != new_flight_id:
            err("QUOTE_MISMATCH", "Arguments do not match the quote.")
        if q["total"] > 0 and not payment_method:
            err("PAYMENT_REQUIRED", f"{q['total']} INR is due. Pass payment_method.", amount_due_inr=q["total"])

        def apply():
            bk, target_seg = b, self.seg(b["pnr"], segment_id)
            if len(q["passenger_ids"]) < len(self.s.passengers(b["pnr"])):
                seq = target_seg["sequence"]
                bk = self._split(b, set(q["passenger_ids"]))
                target_seg = next(x for x in self.s.segments(bk["pnr"]) if x["sequence"] == seq)
            old_f = self.s.flights.get(target_seg["flight_id"])
            nf = self.s.flights[new_flight_id]
            n = sum(1 for p in self.s.passengers(bk["pnr"]) if p["type"] != "infant")
            old_fam = self.seg_family(bk, target_seg)
            if old_f and old_fam in old_f["fares"]:
                old_f["fares"][old_fam]["seats_left"] += n
            nf["fares"][q["new_fare_family"]]["seats_left"] -= n
            target_seg.update(flight_id=nf["id"], flight_number=nf["flight_number"], scheduled_departure_local=nf["scheduled_departure_local"],
                              scheduled_arrival_local=nf["scheduled_arrival_local"], fare_family=q["new_fare_family"],
                              cabin="business" if q["new_fare_family"] == "BUSINESS" else "economy")
            for sp in self.s.segpax(target_seg["id"]):
                if sp["base_fare"] and next(p for p in self.s.passengers(bk["pnr"]) if p["id"] == sp["passenger_id"])["type"] != "infant":
                    sp["base_fare"], sp["taxes"] = nf["fares"][q["new_fare_family"]]["base_fare"], nf["fares"][q["new_fare_family"]]["taxes"]
                sp["seat"], sp["seat_type"] = None, None
            credit = sum(l.get("wallet_credit", 0) for l in q["lines"])
            if credit and bk["customer_id"]:
                self._wallet_credit(bk["customer_id"], credit, f"fare difference {bk['pnr']}")
            fees = sum(l.get("total_due", 0) for l in q["lines"])
            bk["totals"]["ancillaries"] += sum(l.get("change_fee", 0) for l in q["lines"])
            bk["totals"]["base_fare"] = sum(sp["base_fare"] for sg in self.s.segments(bk["pnr"]) for sp in self.s.segpax(sg["id"]))
            bk["totals"]["taxes"] = sum(sp["taxes"] for sg in self.s.segments(bk["pnr"]) for sp in self.s.segpax(sg["id"]))
            bk["totals"]["total"] = bk["totals"]["base_fare"] + bk["totals"]["taxes"] + bk["totals"]["ancillaries"]
            self.s.quotes.pop(quote_id, None)
            return {"status": "CHANGED", "pnr": bk["pnr"], "original_pnr": b["pnr"] if bk is not b else None, "new_flight": self.flight_view(nf, compact=True),
                    "fare_family": q["new_fare_family"], "amount_charged_inr": fees, "wallet_credit_inr": credit}
        return self.charge(b, q["total"], payment_method, payment_method_id, f"Change {b['pnr']}", apply,
                           replay={"kind": "tool", "name": "change_booking", "args": {"pnr": pnr, "segment_id": segment_id, "new_flight_id": new_flight_id, "quote_id": quote_id,
                                                                                   "new_fare_family": new_fare_family, "passenger_ids": passenger_ids}})

    def t_correct_name(self, pnr, passenger_id, new_first_name, new_last_name):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        p = next((x for x in self.s.passengers(b["pnr"]) if x["id"] == passenger_id), None)
        if not p:
            err("NOT_FOUND", "Passenger not on this booking.")
        segs = [s for s in self.s.segments(b["pnr"]) if s["status"] == "confirmed"]
        if segs:
            self.cutoff_check(segs[0]["scheduled_departure_local"], b["route_type"])
        chk = R.name_correction_allowed(p["first_name"], p["last_name"], new_first_name, new_last_name)
        if not chk["allowed"]:
            err("NOT_ALLOWED", f"{chk['chars_changed']} characters would change. Only up to 3, or a first/last swap, is allowed (CH-8). The ticket cannot be transferred; cancel and rebook.", chars_changed=chk["chars_changed"])
        old = f"{p['first_name']} {p['last_name']}"
        p["first_name"], p["last_name"] = new_first_name.strip(), new_last_name.strip()
        return {"status": "CORRECTED", "from": old, "to": f"{p['first_name']} {p['last_name']}", "fee_inr": 0}

    # ============================================================ cancellation & refunds
    def disruption_of(self, sg):
        f = self.s.flights.get(sg["flight_id"]) if sg["flight_id"] else None
        if not f:
            return None, None
        d = next((x for x in self.s.db["disruptions"] if x["flight_id"] == f["id"]), None)
        return f, d

    def is_disrupted(self, sg):
        f, d = self.disruption_of(sg)
        if not d:
            return False
        return R.disruption_options(d["type"], d["minutes"])["free_rebook_or_full_refund"]

    def t_quote_cancellation(self, pnr, reason, segment_ids=None, passenger_ids=None):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        now = self.s.now()
        if b["status"] not in ("confirmed", "no_show"):
            err("NOT_ALLOWED", f"Booking status is {b['status']}.")
        segs = [s for s in self.s.segments(b["pnr"]) if (not segment_ids or s["id"] in segment_ids)]
        segs = [s for s in segs if s["status"] in ("confirmed", "cancelled_by_airline")]
        if not segs:
            err("NOT_ALLOWED", "Nothing left to cancel on this booking.")
        li = R.look_in_eligible(b["created_at"], self.s.segments(b["pnr"])[0]["scheduled_departure_local"], b["route_type"], now)
        lines = []
        for sg in segs:
            rt = self.route_type(sg["origin"], sg["destination"])
            dep = R.dt(sg["scheduled_departure_local"])
            if reason == "airline_disruption" and not self.is_disrupted(sg):
                err("NOT_DISRUPTED", f"{sg['flight_number']} has no qualifying disruption (DS-3). Use reason voluntary.")
            if reason == "voluntary" and R.cutoff_passed(sg["scheduled_departure_local"], rt, now):
                err("CUTOFF_PASSED", "Inside the cancellation cutoff. Only no-show rules apply (CX-6, CX-10).", hint="quote with reason no_show to explain the refund")
            if reason == "no_show" and not R.cutoff_passed(sg["scheduled_departure_local"], rt, now) and dep > now:
                err("NOT_ALLOWED", "Not a no-show yet; the flight can still be cancelled normally.")
            for sp in self.s.segpax(sg["id"]):
                if passenger_ids and sp["passenger_id"] not in passenger_ids:
                    continue
                if sp.get("status") == "cancelled":
                    continue
                q = R.cancellation_line(self.seg_family(b, sg), rt, sp["base_fare"], sp["taxes"],
                                        {"seat": sp["seat_price"], "meal": sp["meal_price"], "bag": sp["bag_price"]}, sg["scheduled_departure_local"], now, reason, li)
                lines.append({"segment_id": sg["id"], "flight_number": sg["flight_number"], "passenger_id": sp["passenger_id"],
                              "paid_fare_inr": sp["base_fare"] + sp["taxes"], "paid_extras_inr": sp["seat_price"] + sp["meal_price"] + sp["bag_price"], **q})
        pay = next((p for p in self.s.db["payments"] if p["pnr"] == b["pnr"]), None)
        method = pay["method_type"] if pay else "card"
        days = RULES["refund"]["timeline_business_days"].get(method if method in ("card", "upi", "netbanking", "skywallet") else "card", 7)
        qid = self.s.new_id("QX")
        total = sum(l["refund"] for l in lines)
        self.s.quotes[qid] = {"kind": "cancel", "pnr": b["pnr"], "reason": reason, "lines": lines, "total": total, "created": now}
        comp = None
        if reason == "airline_disruption":
            comp = [self.t_quote_disruption_compensation(b["pnr"], sg["id"]) for sg in segs]
        return {"quote_id": qid, "reason": reason, "look_in_applied": li and reason == "voluntary", "lines": lines,
                "total_paid_inr": sum(l["paid_fare_inr"] + l["paid_extras_inr"] for l in lines), "total_fees_inr": sum(l["fee"] for l in lines),
                "total_refund_inr": total, "refund_options": {"original_payment": {"method": method, "business_days": days}, "skywallet": {"business_days": 0}},
                "compensation": comp, "valid_minutes": 15}

    def _wallet_credit(self, cid, amount, reason):
        c = self.s.customers[cid]
        c["skywallet_balance"] += amount
        self.s.db["wallet_transactions"].append({"id": self.s.new_id("WTX"), "customer_id": cid, "amount": amount, "type": "credit", "reason": reason, "date": self.s.now().isoformat()})

    def t_cancel_booking(self, pnr, quote_id, refund_to="original_payment"):
        b = self.booking(pnr)
        q = self._quote(quote_id, "cancel", b["pnr"])
        now = self.s.now()
        seg_ids = {l["segment_id"] for l in q["lines"]}
        for sg in self.s.segments(b["pnr"]):
            if sg["id"] not in seg_ids:
                continue
            sel = [l["passenger_id"] for l in q["lines"] if l["segment_id"] == sg["id"]]
            for sp in self.s.segpax(sg["id"]):
                if sp["passenger_id"] in sel:
                    sp["status"] = "cancelled"
            if all(sp.get("status") == "cancelled" for sp in self.s.segpax(sg["id"])):
                sg["status"] = "cancelled" if q["reason"] != "no_show" else "no_show"
            f = self.s.flights.get(sg["flight_id"]) if sg["flight_id"] else None
            if f and f["status"] != "cancelled" and self.seg_family(b, sg) in f["fares"]:
                f["fares"][self.seg_family(b, sg)]["seats_left"] += len(sel)
        if all(s["status"] in ("cancelled", "no_show", "flown") for s in self.s.segments(b["pnr"])):
            b["status"] = "cancelled"
        pay = next((p for p in self.s.db["payments"] if p["pnr"] == b["pnr"]), None)
        rid = self.s.new_id("RF")
        refund = {"id": rid, "pnr": b["pnr"], "amount": q["total"], "reason": q["reason"], "initiated_at": now.isoformat()}
        if refund_to == "skywallet" and b["customer_id"]:
            self._wallet_credit(b["customer_id"], q["total"], f"refund {b['pnr']}")
            refund.update(method="skywallet", payment_method_id=None, expected_by=now.isoformat(), completed_at=now.isoformat(), status="completed")
        else:
            m = pay["method_type"] if pay else "card"
            days = RULES["refund"]["timeline_business_days"].get(m, 7)
            refund.update(method=m, payment_method_id=pay["payment_method_id"] if pay else None, expected_by=R.add_business_days(now, days).isoformat(), completed_at=None, status="processing")
        if q["total"] > 0:
            self.s.db["refunds"].append(refund)
        comp_out = []
        if q["reason"] == "airline_disruption":
            for sid in seg_ids:
                c = self.t_quote_disruption_compensation(b["pnr"], sid)
                if c.get("due") is True:
                    rec = {"id": self.s.new_id("CMP"), "pnr": b["pnr"], "segment_id": sid, "amount_inr": c["total_inr"], "status": "processing"}
                    self.s.compensations.append(rec)
                    comp_out.append(rec)
        self.s.quotes.pop(quote_id, None)
        return {"status": "CANCELLED", "pnr": b["pnr"], "booking_status": b["status"], "refund": refund if q["total"] > 0 else None, "compensation": comp_out}

    def t_get_refund_status(self, pnr):
        b = self.booking(pnr)
        now = self.s.now()
        out = []
        for r in self.s.db["refunds"]:
            if r["pnr"] != b["pnr"]:
                continue
            bd = R.business_days_between(R.dt(r["initiated_at"]), now)
            out.append({**{k: r.get(k) for k in ("id", "amount", "method", "status", "initiated_at", "expected_by", "completed_at", "reason")},
                        "business_days_since_initiated": bd,
                        "overdue": r["status"] != "completed" and bd > RULES["refund"]["overdue_after_business_days"]})
        return {"pnr": b["pnr"], "refunds": out} if out else {"pnr": b["pnr"], "refunds": [], "message": "No refunds on this booking."}

    # ============================================================ disruptions
    def t_get_flight_status(self, flight_number, date):
        fn = flight_number.replace(" ", "").upper()
        f = self.s.flights.get(f"FL-{fn}-{date.replace('-', '')}")
        if not f:
            err("NOT_FOUND", f"No flight {fn} on {date}. Check the number and date (window {self.s.db['meta']['inventory_window']}).")
        v = self.flight_view(f)
        if "disruption" in v:
            ent = R.disruption_options(v["disruption"]["type"], v["disruption"]["minutes"])
            if ent["meal_voucher"]:
                ent["meal_voucher_inr"] = RULES["disruptions"]["meal_voucher"]["amount"][f["route_type"]]
            ent["compensation_note"] = "Compensation applies only to airline cancellations; use quote_disruption_compensation."
            v["passenger_entitlements"] = ent
        v["gate"] = f.get("gate")
        return v

    def t_rebook_disrupted(self, pnr, segment_id, new_flight_id):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        sg = self.seg(b["pnr"], segment_id)
        if not self.is_disrupted(sg):
            err("NOT_DISRUPTED", "No qualifying disruption on this segment (DS-3). Use quote_change.")
        old_f = self.s.flights[sg["flight_id"]]
        nf = self.s.flights.get(new_flight_id)
        if not nf or (nf["origin"], nf["destination"]) != (sg["origin"], sg["destination"]):
            err("NOT_ALLOWED", "Rebooking must be on the same route.")
        if abs((R.dt(nf["scheduled_departure_local"]).date() - R.dt(old_f["scheduled_departure_local"]).date()).days) > RULES["disruptions"]["rebook_window_days"]:
            err("NOT_ALLOWED", "Free rebooking only within 3 days either side of the original date (DS-3).")
        if nf["status"] in ("cancelled", "departed", "landed") or nf["id"] == old_f["id"]:
            err("NO_AVAILABILITY", "That flight cannot be used.")
        fam = self.seg_family(b, sg)
        n = sum(1 for p in self.s.passengers(b["pnr"]) if p["type"] != "infant")
        if nf["fares"].get(fam, {}).get("seats_left", 0) < n:
            err("NO_AVAILABILITY", "Not enough seats in the same cabin on that flight.")
        comp = self.t_quote_disruption_compensation(b["pnr"], segment_id, new_flight_id)
        nf["fares"][fam]["seats_left"] -= n
        sg.update(flight_id=nf["id"], flight_number=nf["flight_number"], scheduled_departure_local=nf["scheduled_departure_local"],
                  scheduled_arrival_local=nf["scheduled_arrival_local"], status="confirmed", rebooked_from=old_f["id"])
        for sp in self.s.segpax(sg["id"]):
            sp["seat"], sp["seat_type"] = None, None
        comp_rec = None
        if comp.get("due") is True:
            comp_rec = {"id": self.s.new_id("CMP"), "pnr": b["pnr"], "segment_id": segment_id, "amount_inr": comp["total_inr"], "status": "processing"}
            self.s.compensations.append(comp_rec)
        return {"status": "REBOOKED", "pnr": b["pnr"], "new_flight": self.flight_view(nf, compact=True), "charge_inr": 0, "compensation": comp_rec,
                "note": "Seat selection must be redone; paid seats from the old flight are refundable on request."}

    def t_quote_disruption_compensation(self, pnr, segment_id, accepted_rebooking_flight_id=None):
        b = self.booking(pnr)
        sg = self.seg(b["pnr"], segment_id)
        f = self.s.flights.get(sg.get("rebooked_from") or sg["flight_id"])
        d = next((x for x in self.s.db["disruptions"] if f and x["flight_id"] == f["id"]), None)
        if not d:
            return {"due": False, "amount_per_passenger_inr": 0, "total_inr": 0, "why": "no disruption on this flight"}
        rt = self.route_type(sg["origin"], sg["destination"])
        c = R.disruption_compensation(rt, d["type"], d["reason"], d["notified_at"], f["scheduled_departure_local"], f["block_minutes"])
        if c["due"] is True and accepted_rebooking_flight_id:
            nf = self.s.flights.get(accepted_rebooking_flight_id)
            limit = RULES["disruptions"]["compensation_domestic"]["not_payable_if_customer_accepts_rebooking_departing_within_hours"]
            if nf and abs(R.hours_between(R.dt(f["scheduled_departure_local"]), R.dt(nf["scheduled_departure_local"]))) <= limit:
                c = {"due": False, "amount": 0, "why": f"rebooked flight departs within {limit} h of the original"}
        n = sum(1 for p in self.s.passengers(b["pnr"]) if p["type"] != "infant")
        amt = c.get("amount") or 0
        return {"due": c["due"], "amount_per_passenger_inr": amt, "passengers": n, "total_inr": amt * n if c["due"] is True else 0, "why": c["why"],
                "paid_to": "original payment method within 7 business days" if c["due"] is True else None}

    # ============================================================ seats & extras
    def seat_map(self, b, sg):
        f = self.s.flights.get(sg["flight_id"])
        if not f:
            err("NOT_AVAILABLE", "Seat map not available for this flight.")
        ac = self.s.db["aircraft"][f["aircraft"]]
        cabin = sg["cabin"]
        r0, r1 = ac["rows"][cabin]
        letters = ac["layout"][cabin].replace("-", "")
        taken = {sp["seat"] for s2 in self.s.db["booking_segments"] if s2["flight_id"] == f["id"] for sp in self.s.segpax(s2["id"]) if sp["seat"] and sp.get("status") != "cancelled"}
        rnd = random.Random(f["id"] + cabin)
        cap = ac["business_seats"] if cabin == "business" else ac["economy_seats"]
        sold = sum(v["seats_left"] for k, v in f["fares"].items() if (k == "BUSINESS") == (cabin == "business"))
        fill = max(0.15, min(0.92, 1 - sold / max(cap, 1)))
        seats = {}
        for row in range(r0, r1 + 1):
            for L in letters:
                s = f"{row}{L}"
                occupied = s in taken or rnd.random() < fill
                st = "exit_row" if row in ac["exit_rows"] else ("preferred" if row <= 5 or (cabin == "economy" and row <= r0 + 2) else "standard")
                seats[s] = {"type": st, "available": not occupied, "window": L in (letters[0], letters[-1]),
                            "aisle": L in [ac["layout"][cabin].split("-")[i][j] for i, j in ((0, -1), (1, 0))] if "-" in ac["layout"][cabin] else False}
        return f, seats

    def seat_price(self, b, sg, seat_type):
        fam = self.seg_family(b, sg)
        rt = self.route_type(sg["origin"], sg["destination"])
        if fam == "BUSINESS":
            return 0
        free = set(RULES["seats"]["free_for"].get(fam, [])) | set(RULES["seats"]["free_for"].get(f"tier_{self.tier_of(b['customer_id'])}", []))
        return 0 if seat_type in free else RULES["seats"]["types"][seat_type]["price"][rt]

    def t_get_seat_map(self, pnr, segment_id):
        b = self.booking(pnr)
        sg = self.seg(b["pnr"], segment_id)
        f, seats = self.seat_map(b, sg)
        avail = {t: [s for s, v in seats.items() if v["available"] and v["type"] == t] for t in AIRCRAFT_SEAT_TYPES}
        return {"flight_number": sg["flight_number"], "aircraft": f["aircraft"], "cabin": sg["cabin"],
                "prices_for_this_customer_inr": {t: self.seat_price(b, sg, t) for t in AIRCRAFT_SEAT_TYPES},
                "available": {t: v for t, v in avail.items() if v}, "window_letters": self.s.db["aircraft"][f["aircraft"]]["layout"][sg["cabin"]][0] + "," + self.s.db["aircraft"][f["aircraft"]]["layout"][sg["cabin"]][-1],
                "exit_row_rules": RULES["seats"]["types"]["exit_row"]["restrictions"], "selection_closes_hours_before": 2}

    def t_select_seat(self, pnr, segment_id, passenger_id, seat, payment_method=None, payment_method_id=None):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        sg = self.seg(b["pnr"], segment_id)
        if sg["status"] != "confirmed":
            err("NOT_ALLOWED", f"Segment is {sg['status']}.")
        self.cutoff_check(sg["scheduled_departure_local"], self.route_type(sg["origin"], sg["destination"]), "seat_selection")
        p = next((x for x in self.s.passengers(b["pnr"]) if x["id"] == passenger_id), None)
        if not p:
            err("NOT_FOUND", "Passenger not on booking.")
        if p["type"] == "infant":
            err("NOT_ALLOWED", "Infants sit on an adult's lap.")
        f, seats = self.seat_map(b, sg)
        seat = seat.upper().strip()
        info = seats.get(seat)
        if not info:
            err("NOT_FOUND", f"Seat {seat} does not exist in {sg['cabin']} on this aircraft.")
        sp = next(x for x in self.s.segpax(sg["id"]) if x["passenger_id"] == passenger_id)
        if not info["available"] and sp["seat"] != seat:
            err("NO_AVAILABILITY", f"Seat {seat} is taken.")
        if info["type"] == "exit_row":
            a = age_on(p.get("date_of_birth"), R.dt(sg["scheduled_departure_local"]).date())
            if any(x["type"] == "infant" for x in self.s.passengers(b["pnr"])):
                err("NOT_ALLOWED", "Exit row not allowed on a booking travelling with an infant (ST-3).")
            if p["type"] == "child" or (a is not None and a < 15):
                err("NOT_ALLOWED", "Exit row passengers must be 15 or older (ST-3).")
            if sp.get("special_assistance"):
                err("NOT_ALLOWED", "Exit row not allowed with a special assistance request (ST-3).")
        price = self.seat_price(b, sg, info["type"])
        if price and not payment_method:
            err("PAYMENT_REQUIRED", f"Seat {seat} costs {price} INR. Confirm with the customer and pass payment_method.", price_inr=price)

        def apply():
            sp["seat"], sp["seat_type"], sp["seat_price"] = seat, info["type"], sp["seat_price"] + price
            b["totals"]["ancillaries"] += price
            b["totals"]["total"] += price
            return {"status": "SEAT_ASSIGNED", "seat": seat, "seat_type": info["type"], "price_inr": price}
        return self.charge(b, price, payment_method, payment_method_id, f"Seat {seat} {b['pnr']}", apply,
                           replay={"kind": "tool", "name": "select_seat", "args": {"pnr": pnr, "segment_id": segment_id, "passenger_id": passenger_id, "seat": seat}})

    def t_add_baggage(self, pnr, segment_id, passenger_id, kg, payment_method, payment_method_id=None):
        b = self.booking(pnr)
        self.fraud_check(b["customer_id"])
        sg = self.seg(b["pnr"], segment_id)
        rt = self.route_type(sg["origin"], sg["destination"])
        self.cutoff_check(sg["scheduled_departure_local"], rt, "prepaid_baggage")
        pack = next((x for x in RULES["baggage"]["prepaid_packs"][rt] if x["kg"] == int(kg)), None)
        if not pack:
            err("BAD_ARGUMENTS", "Packs are 5, 10 or 15 kg.")
        sp = next((x for x in self.s.segpax(sg["id"]) if x["passenger_id"] == passenger_id), None)
        if not sp:
            err("NOT_FOUND", "Passenger not on this segment.")

        def apply():
            sp["extra_bag_kg"] += pack["kg"]
            sp["bag_price"] += pack["price"]
            b["totals"]["ancillaries"] += pack["price"]
            b["totals"]["total"] += pack["price"]
            return {"status": "BAGGAGE_ADDED", "extra_kg": pack["kg"], "price_inr": pack["price"], "total_extra_kg_now": sp["extra_bag_kg"]}
        return self.charge(b, pack["price"], payment_method, payment_method_id, f"{pack['kg']} kg bag {b['pnr']}", apply,
                           replay={"kind": "tool", "name": "add_baggage", "args": {"pnr": pnr, "segment_id": segment_id, "passenger_id": passenger_id, "kg": kg}})

    def t_add_meal(self, pnr, segment_id, passenger_id, meal, payment_method=None):
        b = self.booking(pnr)
        sg = self.seg(b["pnr"], segment_id)
        rt = self.route_type(sg["origin"], sg["destination"])
        if R.hours_between(self.s.now(), R.dt(sg["scheduled_departure_local"])) < RULES["meals"]["preorder_until_hours_before_departure"]:
            err("CUTOFF_PASSED", "Meals can be pre-ordered until 24 hours before departure (ST-6).")
        sp = next((x for x in self.s.segpax(sg["id"]) if x["passenger_id"] == passenger_id), None)
        if not sp:
            err("NOT_FOUND", "Passenger not on this segment.")
        price = 0 if rt in RULES["meals"]["included"][self.seg_family(b, sg)] else RULES["meals"]["price"][rt]
        if price and not payment_method:
            err("PAYMENT_REQUIRED", f"Meal costs {price} INR on this fare.", price_inr=price)

        def apply():
            sp["meal"], sp["meal_price"] = meal, sp["meal_price"] + price
            b["totals"]["ancillaries"] += price
            b["totals"]["total"] += price
            return {"status": "MEAL_ADDED", "meal": meal, "price_inr": price}
        return self.charge(b, price, payment_method, None, f"Meal {b['pnr']}", apply,
                           replay={"kind": "tool", "name": "add_meal", "args": {"pnr": pnr, "segment_id": segment_id, "passenger_id": passenger_id, "meal": meal}})

    def t_add_special_assistance(self, pnr, passenger_id, type, notes=None):
        b = self.booking(pnr)
        segs = [s for s in self.s.segments(b["pnr"]) if s["status"] == "confirmed" and R.dt(s["scheduled_departure_local"]) > self.s.now()]
        if not segs:
            err("NOT_ALLOWED", "No upcoming flights on this booking.")
        hours = R.hours_between(self.s.now(), R.dt(segs[0]["scheduled_departure_local"]))
        for sg in segs:
            for sp in self.s.segpax(sg["id"]):
                if sp["passenger_id"] == passenger_id:
                    sp["special_assistance"] = type
                    if sp.get("seat_type") == "exit_row":
                        sp["seat"], sp["seat_type"] = None, None
        return {"status": "ASSISTANCE_ADDED", "type": type, "segments": len(segs), "fee_inr": 0,
                "guaranteed": hours >= 48, "note": None if hours >= 48 else "Requested less than 48 hours before departure: subject to availability (SA-1)."}

    # ============================================================ loyalty
    def t_loyalty_balance(self, customer_id):
        self.account(customer_id)
        l = self.s.loyalty(customer_id)
        t = RULES["loyalty"]["tier_thresholds_points_12_months"]
        nxt = {"BLUE": "SILVER", "SILVER": "GOLD", "GOLD": "PLATINUM"}.get(l["tier"])
        return {"skypoints_id": l["skypoints_id"], "tier": l["tier"], "tier_valid_until": l["tier_valid_until"], "points_balance": l["points_balance"],
                "value_inr": l["points_balance"] * RULES["loyalty"]["redeem_value_inr_per_point"], "tier_points_12_months": l["tier_points_12_months"],
                "next_tier": nxt, "points_to_next_tier": max(0, t[nxt] - l["tier_points_12_months"]) if nxt else 0,
                "expiring_next_90_days": l["expiring_next_90_days"],
                "nominees": [f"{c['first_name']} {c['last_name']}" for c in self.s.db["companions"] if c["id"] in l["nominee_companion_ids"]]}

    def t_quote_redemption(self, customer_id, pnr, points=None):
        self.account(customer_id)
        self.fraud_check(customer_id)
        b = self.booking(pnr)
        if b["status"] != "held":
            err("NOT_ALLOWED", "Points can be used while paying for a new booking (held status).")
        l = self.s.loyalty(customer_id)
        noms = set(l["nominee_companion_ids"])
        c = self.s.customers[customer_id]
        bad = [f"{p['first_name']} {p['last_name']}" for p in self.s.passengers(b["pnr"])
               if not ((p["first_name"].lower(), p["last_name"].lower()) == (c["first_name"].lower(), c["last_name"].lower()) or p.get("companion_id") in noms)]
        if bad:
            err("NOT_ELIGIBLE", f"Not the account holder or a registered nominee: {', '.join(bad)} (LY-4).")
        rate = RULES["loyalty"]["redeem_value_inr_per_point"]
        max_pts = min(l["points_balance"], int(b["totals"]["base_fare"] / rate))
        use = min(points or max_pts, max_pts)
        if use < RULES["loyalty"]["redeem_min_points"]:
            err("NOT_ENOUGH_POINTS", f"Minimum {RULES['loyalty']['redeem_min_points']} points; available {l['points_balance']}.")
        value = int(use * rate)
        qid = self.s.new_id("QR")
        self.s.quotes[qid] = {"kind": "redeem", "pnr": b["pnr"], "points": use, "value": value, "customer_id": customer_id, "created": self.s.now()}
        return {"quote_id": qid, "points_used": use, "inr_covered": value, "amount_still_due_inr": b["amount_due"] - value, "points_left_after": l["points_balance"] - use}

    def t_redeem_points(self, customer_id, pnr, quote_id):
        b = self.booking(pnr)
        q = self._quote(quote_id, "redeem", b["pnr"])
        l = self.s.loyalty(customer_id)
        l["points_balance"] -= q["points"]
        l["points_value_inr"] = l["points_balance"] * RULES["loyalty"]["redeem_value_inr_per_point"]
        self.s.db["loyalty_transactions"].append({"id": self.s.new_id("LTX"), "customer_id": customer_id, "type": "redeem", "points": -q["points"],
                                                  "date": self.s.now().isoformat(), "pnr": b["pnr"], "description": f"Redeemed on {b['pnr']}"})
        b["amount_due"] -= q["value"]
        self.s.db["payments"].append({"id": self.s.new_id("PAY"), "pnr": b["pnr"], "amount": q["value"], "method_type": "skypoints", "payment_method_id": None, "status": "captured", "paid_at": self.s.now().isoformat()})
        self.s.quotes.pop(quote_id, None)
        if b["amount_due"] <= 0:
            return {"status": "REDEEMED", **self._confirm_held(b, "skywallet", None, already_paid=True)}
        return {"status": "REDEEMED", "points_used": q["points"], "amount_still_due_inr": b["amount_due"], "next": "Collect the rest with confirm_payment or a payment link."}

    # ============================================================ support
    def t_search_knowledge_base(self, query, limit=3):
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        scored = []
        for a in self.s.kb:
            text = (a["title"] + " " + a["title"] + " " + a["body"]).lower()
            score = sum(text.count(w) for w in words)
            if score:
                scored.append((score, a))
        scored.sort(key=lambda x: -x[0])
        return {"results": [{"id": a["id"], "title": a["title"], "body": a["body"]} for _, a in scored[:limit]]}

    def t_create_support_ticket(self, category, summary, customer_id=None, pnr=None, bag_tag=None):
        if pnr:
            self.booking(pnr)
        tno = f"TKT-{next(_TICKETS)}"
        sla = {"refund_overdue": 48, "baggage": 24, "complaint": 72, "special_assistance": 24}.get(category, 72)
        self.s.db["support_tickets"].append({"ticket_no": tno, "customer_id": customer_id, "pnr": pnr, "category": category, "status": "open",
                                             "created_at": self.s.now().isoformat(), "summary": summary, "bag_tag": bag_tag, "resolution": None})
        return {"ticket_no": tno, "category": category, "expected_response_hours": sla}

    def t_issue_goodwill_credit(self, amount_inr, reason, customer_id=None, pnr=None):
        if pnr and not customer_id:
            b = self.booking(pnr)
            customer_id = b["customer_id"]
            if not customer_id:
                err("NO_ACCOUNT", "Guest booking with no SkyPoints account; goodwill cannot be credited. Create a ticket instead.")
        elif customer_id:
            self.account(customer_id)
        else:
            err("BAD_ARGUMENTS", "Pass customer_id or pnr.")
        g = RULES["goodwill"]
        if amount_inr > g["max_skywallet_credit_inr"]:
            err("ABOVE_LIMIT", "Above the 1,000 INR agent limit. Hand off (ES-6).")
        if reason not in g["only_for"]:
            err("NOT_ELIGIBLE", "Reason not eligible for goodwill (ES-6).")
        recent = [x for x in self.s.db["goodwill_credits"] if x["customer_id"] == customer_id and R.dt(x["date"]) > self.s.now() - timedelta(days=g["max_per_customer_per_days"])]
        if recent:
            err("GOODWILL_LIMIT", f"Goodwill already given on {recent[0]['date'][:10]}; only once per 30 days (ES-6).")
        rec = {"id": self.s.new_id("GW"), "customer_id": customer_id, "amount": amount_inr, "reason": reason, "date": self.s.now().isoformat(), "pnr": pnr}
        self.s.db["goodwill_credits"].append(rec)
        self._wallet_credit(customer_id, amount_inr, f"goodwill {rec['id']}")
        return {"status": "CREDITED", "goodwill_id": rec["id"], "amount_inr": amount_inr, "skywallet_balance_inr": self.s.customers[customer_id]["skywallet_balance"]}

    def t_handoff_to_human(self, reason, summary, customer_id=None, pnr=None, priority="normal"):
        tier = self.tier_of(customer_id) if customer_id else "BLUE"
        wait = 2 if tier == "PLATINUM" or priority == "urgent" else 6 if priority == "high" else 9
        case = {"case_no": f"CASE-{next(_CASES)}", "reason": reason, "summary": summary, "customer_id": customer_id, "pnr": pnr,
                "priority": priority, "created_at": self.s.now().isoformat(), "expected_wait_minutes": wait}
        self.s.handoffs.append(case)
        self.s.ended = True
        return {"status": "HANDED_OFF", **case, "message": "Tell the customer the case number and expected wait, then end."}

    def t_update_contact_details(self, customer_id, new_phone=None, new_email=None):
        c = self.account(customer_id, need_otp=True)
        self.fraud_check(customer_id)
        if not (new_phone or new_email):
            err("BAD_ARGUMENTS", "Give new_phone or new_email.")
        changed = {}
        if new_phone:
            if not re.fullmatch(r"\+\d{10,14}", new_phone.replace(" ", "")):
                err("BAD_ARGUMENTS", "Phone must be in +<country><number> format.")
            changed["phone"] = mask_phone(new_phone)
            self.s.outbox.append({"channel": "sms", "to": c["phone"], "text": "Your SkyPilot phone number was changed."})
            c["phone"] = new_phone.replace(" ", "")
        if new_email:
            if "@" not in new_email:
                err("BAD_ARGUMENTS", "Invalid email.")
            changed["email"] = mask_email(new_email)
            self.s.outbox.append({"channel": "email", "to": c["email"], "text": "Your SkyPilot email was changed."})
            c["email"] = new_email.strip()
        return {"status": "UPDATED", "changed": changed, "note": "A security notice was sent to the old contact."}

    def t_send_itinerary(self, pnr, channel="email", include_invoice=False):
        b = self.booking(pnr)
        dest = b["contact"]["email"] if channel == "email" else b["contact"]["phone"]
        if not dest:
            err("NO_CONTACT", f"No {channel} on this booking.")
        self.s.outbox.append({"channel": channel, "to": dest, "text": f"Itinerary {b['pnr']}" + (" + GST invoice" if include_invoice else "")})
        return {"sent": True, "channel": channel, "destination": mask_email(dest) if channel == "email" else mask_phone(dest), "invoice_attached": bool(include_invoice and channel == "email")}
