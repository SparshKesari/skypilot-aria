"""Deterministic fare rule engine.

Every tool that returns money calls these functions, and the seed script uses the
same functions to write ground_truth.json. The LLM never does this arithmetic.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

RULES = json.loads((Path(__file__).parent / "fare_rules.json").read_text())
FF = RULES["fare_families"]


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


def business_days_between(a: datetime, b: datetime) -> int:
    d, n = a.date(), 0
    while d < b.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def add_business_days(a: datetime, days: int) -> datetime:
    d = a
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def cutoff_passed(dep_local: str, route_type: str, now: datetime, kind: str = "change_or_cancel") -> bool:
    limit = RULES["service_cutoffs_hours_before_departure"][kind][route_type]
    return hours_between(now, dt(dep_local)) < limit


def look_in_eligible(booking_created_at: str, first_dep_local: str, route_type: str, now: datetime) -> bool:
    li = RULES["look_in_option"]
    within_window = hours_between(dt(booking_created_at), now) <= li["window_hours_after_booking"]
    far_enough = (dt(first_dep_local) - dt(booking_created_at)).total_seconds() / 86400 >= li["min_days_before_departure"][route_type]
    return within_window and far_enough


def cancellation_line(fare_family: str, route_type: str, base: int, taxes: int, ancillaries: dict,
                      dep_local: str, now: datetime, reason: str, look_in: bool) -> dict:
    """Refund for one passenger on one segment.

    ancillaries: {"seat": int, "meal": int, "bag": int}
    reason: voluntary | no_show | airline_disruption
    """
    paid_fare = base + taxes
    anc_total = sum(ancillaries.values())
    rule = FF[fare_family]
    if reason == "airline_disruption":
        return {"fee": 0, "fare_refund": paid_fare, "ancillary_refund": anc_total, "refund": paid_fare + anc_total, "basis": "DS-3"}

    hours_left = hours_between(now, dt(dep_local))
    bag_refund = ancillaries.get("bag", 0) if (reason == "voluntary" and hours_left > 24) else 0

    if reason == "voluntary" and look_in:
        return {"fee": 0, "fare_refund": paid_fare, "ancillary_refund": bag_refund, "refund": paid_fare + bag_refund, "basis": "CH-4/CX-3"}

    if reason == "no_show":
        ns = rule["no_show"]
        if ns["refund"] == "taxes_only":
            fee, fare_refund = base, taxes
        else:
            fee = ns["fee"][route_type]
            fare_refund = max(paid_fare - fee, taxes)
        return {"fee": fee, "fare_refund": fare_refund, "ancillary_refund": 0, "refund": fare_refund, "basis": "CX-6"}

    c = rule["cancel"]
    if not c["refundable"]:
        fee, fare_refund = base, taxes
    else:
        fee = c["fee"][route_type]
        if "late_fee_within_hours" in c and hours_left < c["late_fee_within_hours"]:
            fee = c["late_fee"][route_type]
        fare_refund = max(paid_fare - fee, taxes)
    return {"fee": fee, "fare_refund": fare_refund, "ancillary_refund": bag_refund, "refund": fare_refund + bag_refund, "basis": "CX-2"}


def change_line(fare_family: str, route_type: str, old_base: int, old_taxes: int, new_base: int, new_taxes: int,
                look_in: bool) -> dict:
    rule = FF[fare_family]
    if not rule["change"]["allowed"] and not look_in:
        return {"allowed": False, "basis": "CH-1"}
    fee = 0 if look_in else rule["change"]["fee"][route_type]
    diff = (new_base + new_taxes) - (old_base + old_taxes)
    if diff >= 0:
        return {"allowed": True, "change_fee": fee, "fare_difference": diff, "wallet_credit": 0, "total_due": fee + diff, "basis": "CH-2"}
    credit = -diff if fare_family in ("FLEX", "BUSINESS") else 0
    return {"allowed": True, "change_fee": fee, "fare_difference": 0, "wallet_credit": credit, "total_due": fee, "basis": "CH-3"}


def disruption_compensation(route_type: str, kind: str, reason: str, notified_at: str, scheduled_dep: str,
                            block_minutes: int) -> dict:
    comp = RULES["disruptions"]["compensation_domestic"]
    if kind != "cancellation":
        return {"due": False, "amount": 0, "why": "only airline cancellations qualify"}
    if route_type == "international":
        return {"due": "handoff", "amount": None, "why": "DS-5 international compensation handled by a specialist"}
    if reason not in comp["only_for_reasons"]:
        return {"due": False, "amount": 0, "why": f"reason '{reason}' is excluded"}
    notice_h = hours_between(dt(notified_at), dt(scheduled_dep))
    if notice_h >= comp["applies_if_notified_hours_before_departure_less_than"]:
        return {"due": False, "amount": 0, "why": f"notified {notice_h:.1f} h before departure (24 h or more)"}
    for band in comp["by_block_time_minutes"]:
        if block_minutes <= band["max_minutes"]:
            return {"due": True, "amount": band["amount"], "why": f"notified {notice_h:.1f} h before, block time {block_minutes} min"}


def disruption_options(kind: str, minutes: int) -> dict:
    d = RULES["disruptions"]
    meal = kind == "delay" and minutes >= d["meal_voucher"]["min_delay_minutes"]
    free = (kind == "cancellation"
            or (kind == "delay" and minutes >= d["free_rebook_or_refund"]["delay_minutes_at_least"])
            or (kind == "schedule_change" and minutes >= d["free_rebook_or_refund"]["schedule_change_minutes_at_least"]))
    return {"meal_voucher": meal, "free_rebook_or_full_refund": free}


def name_correction_allowed(old_first: str, old_last: str, new_first: str, new_last: str) -> dict:
    def lev(a, b):
        a, b = a.lower(), b.lower()
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]
    if old_first.lower() == new_last.lower() and old_last.lower() == new_first.lower():
        return {"allowed": True, "chars_changed": 0, "basis": "CH-8 swap"}
    n = lev(old_first, new_first) + lev(old_last, new_last)
    return {"allowed": n <= RULES["name_correction"]["free_max_characters_changed"], "chars_changed": n, "basis": "CH-8"}


def tier_for_points(points_12m: int) -> str:
    t = RULES["loyalty"]["tier_thresholds_points_12_months"]
    if points_12m >= t["PLATINUM"]:
        return "PLATINUM"
    if points_12m >= t["GOLD"]:
        return "GOLD"
    if points_12m >= t["SILVER"]:
        return "SILVER"
    return "BLUE"


def points_earned(base_fare: int, tier: str, fare_family: str) -> int:
    per100 = RULES["loyalty"]["earn_per_100_inr_base_fare"][tier]
    return int(base_fare / 100 * per100 * FF[fare_family]["points_multiplier"])
