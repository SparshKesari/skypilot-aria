"""Add deterministic long delays to db.json so the 5 h / 6 h / 7 h disruption edge cases (DS-3 threshold) have real data.
Run after seed.py: python data/seed.py && python data/patch_disruptions.py && python data/validate.py. Idempotent."""
import json
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).parent / "db.json"
EXTRA = [  # flight id, delay minutes, reason
    ("FL-SP253-20261012", 300, "crew availability"),             # 5 h: just under the free-rebooking threshold
    ("FL-SP281-20261012", 360, "technical inspection of aircraft"),  # exactly 6 h: threshold met
    ("FL-SP143-20261015", 420, "late arrival of incoming aircraft"),  # 7 h: clearly above
]
db = json.loads(DB.read_text())
flights = {f["id"]: f for f in db["flights"]}; have = {d["flight_id"] for d in db["disruptions"]}
added = 0
for fid, mins, reason in EXTRA:
    if fid in have:
        continue
    f = flights[fid]
    dep = datetime.fromisoformat(f["scheduled_departure_local"]); arr = datetime.fromisoformat(f["scheduled_arrival_local"])
    nd, na = dep + timedelta(minutes=mins), arr + timedelta(minutes=mins)
    db["disruptions"].append({"id": f"DS-{len(db['disruptions']) + 1:04d}", "flight_id": fid, "type": "delay", "minutes": mins, "reason": reason,
                              "notified_at": (datetime.fromisoformat(db["meta"]["sim_now"]) - timedelta(hours=1)).isoformat(), "new_departure_local": nd.isoformat()})
    f["status"] = "delayed"; f["estimated_departure_local"] = nd.isoformat(); f["estimated_arrival_local"] = na.isoformat()
    added += 1
db["meta"]["counts"]["disruptions"] = len(db["disruptions"])
DB.write_text(json.dumps(db, ensure_ascii=False, indent=1))
print(f"added {added} delays; disruptions now {len(db['disruptions'])}")
