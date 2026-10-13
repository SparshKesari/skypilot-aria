"""Integrity checks for db.json and ground_truth.json. Exit code 1 on any failure.

    python data/validate.py
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import rules as R  # noqa: E402

db = json.loads((ROOT / "data" / "db.json").read_text())
gt = json.loads((ROOT / "data" / "ground_truth.json").read_text())
NOW = R.dt(db["meta"]["sim_now"])
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


ids = lambda k, f="id": {x[f] for x in db[k]}  # noqa: E731
cust, flights, pnrs = ids("customers"), ids("flights"), ids("bookings", "pnr")
pax, segs, pms = ids("booking_passengers"), ids("booking_segments"), ids("payment_methods")

check(len(cust) == 50, f"expected 50 customers, got {len(cust)}")
for b in db["bookings"]:
    check(b["customer_id"] in cust, f"booking {b['pnr']} unknown customer")
for s in db["booking_segments"]:
    check(s["pnr"] in pnrs, f"segment {s['id']} unknown pnr")
    if s["flight_id"]:
        check(s["flight_id"] in flights, f"segment {s['id']} unknown flight")
    if R.dt(s["scheduled_departure_local"]) > NOW:
        check(s["flight_id"] is not None, f"future segment {s['id']} has no inventory flight")
for p in db["booking_passengers"]:
    check(p["pnr"] in pnrs, f"pax {p['id']} unknown pnr")
for sp in db["segment_passengers"]:
    check(sp["segment_id"] in segs and sp["passenger_id"] in pax, f"segment_passenger bad ref {sp}")
for pay in db["payments"]:
    check(pay["payment_method_id"] is None or pay["payment_method_id"] in pms, f"payment {pay['id']} unknown method")

# totals add up
by_pnr = {}
seg_pnr = {s["id"]: s["pnr"] for s in db["booking_segments"]}
for sp in db["segment_passengers"]:
    t = by_pnr.setdefault(seg_pnr[sp["segment_id"]], 0)
    by_pnr[seg_pnr[sp["segment_id"]]] = t + sp["base_fare"] + sp["taxes"] + sp["seat_price"] + sp["bag_price"] + sp["meal_price"]
for b in db["bookings"]:
    check(by_pnr[b["pnr"]] == b["totals"]["total"], f"booking {b['pnr']} total mismatch")

# seats unique per segment, children not on exit rows handled by tools
seat_seen = Counter((sp["segment_id"], sp["seat"]) for sp in db["segment_passengers"] if sp["seat"])
dups = [k for k, v in seat_seen.items() if v > 1]
check(not dups, f"{len(dups)} duplicate seat assignments")

# future segments on cancelled flights are flagged
fl = {f["id"]: f for f in db["flights"]}
for s in db["booking_segments"]:
    if s["flight_id"] and fl[s["flight_id"]]["status"] == "cancelled":
        check(s["status"] in ("cancelled_by_airline", "cancelled"), f"segment {s['id']} on cancelled flight still {s['status']}")

# no PNR collisions, pnr format
check(len(pnrs) == len(db["bookings"]), "duplicate PNRs")
check(all(len(p) == 6 for p in pnrs), "bad PNR length")

# ground truth references exist
for g in gt["scenarios"]:
    check(g["customer_id"] in cust, f"{g['id']} unknown customer")
    if g.get("pnr"):
        check(g["pnr"] in pnrs, f"{g['id']} unknown pnr")

# unique emails
emails = Counter(c["email"] for c in db["customers"])
check(all(v == 1 for v in emails.values()), "duplicate customer emails")

print("tiers:", Counter(l["tier"] for l in db["loyalty_accounts"]))
print("booking status:", Counter(b["status"] for b in db["bookings"]))
print("fare families:", Counter(b["fare_family"] for b in db["bookings"]))
print("upcoming bookings:", sum(1 for b in db["bookings"] if b["status"] == "confirmed"))
print("disruptions:", Counter(d["type"] for d in db["disruptions"]))
print("bookings per customer min/avg/max:",
      min(Counter(b["customer_id"] for b in db["bookings"]).values()),
      round(len(db["bookings"]) / 50, 1), max(Counter(b["customer_id"] for b in db["bookings"]).values()))
if errors:
    print(f"\n{len(errors)} ERRORS")
    for e in errors[:30]:
        print(" -", e)
    sys.exit(1)
print("\nAll checks passed.")
