import json
import os
import sqlite3

os.makedirs("/workspace/data", exist_ok=True)
with sqlite3.connect("/workspace/data/jobs.sqlite") as db:
    db.execute(
        "CREATE TABLE jobs(id TEXT PRIMARY KEY,input TEXT NOT NULL,status TEXT NOT NULL,output TEXT)"
    )
    for jid, values, status in [
        ("submitted-001", [2, 3], "done"),
        ("submitted-002", [-4, 1], "pending"),
        ("submitted-003", [], "pending"),
    ]:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?)",
            (
                jid,
                json.dumps(values),
                status,
                json.dumps(sum(x * x for x in values)) if status == "done" else None,
            ),
        )
