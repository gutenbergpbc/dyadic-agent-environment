"""Job service. POST v1 accepts {values: [integers]}; results sum their squares."""

import json
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/workspace/data/jobs.sqlite"
ENABLE_V2 = False
ENABLE_V1 = True


def connect():
    return sqlite3.connect(DB, timeout=10)


def worker():
    while True:
        try:
            with connect() as db:
                row = db.execute(
                    "SELECT id, input FROM jobs WHERE status='pending' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row:
                    values = json.loads(row[1])
                    db.execute(
                        "UPDATE jobs SET status='done', output=? WHERE id=?",
                        (json.dumps(sum(x * x for x in values)), row[0]),
                    )
        except Exception as exc:
            print(repr(exc), flush=True)
        time.sleep(0.1)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/v1/jobs" and ENABLE_V1:
            version = 1
        elif self.path == "/v2/jobs" and ENABLE_V2:
            version = 2
        else:
            return self.reply(404, {"error": "endpoint unavailable"})
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            values = body["values"] if version == 1 else body["input"]["values"]
            if not isinstance(values, list) or not all(type(x) is int for x in values):
                raise ValueError("integer list required")
            jid = uuid.uuid4().hex
            with connect() as db:
                db.execute(
                    "INSERT INTO jobs VALUES (?, ?, 'pending', NULL)",
                    (jid, json.dumps(values)),
                )
            self.reply(202, {"id": jid})
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        if self.path.startswith("/jobs/"):
            with connect() as db:
                row = db.execute(
                    "SELECT id,input,status,output FROM jobs WHERE id=?",
                    (self.path.split("/")[-1],),
                ).fetchone()
            if row:
                return self.reply(
                    200,
                    {
                        "id": row[0],
                        "values": json.loads(row[1]),
                        "status": row[2],
                        "output": json.loads(row[3]) if row[3] else None,
                    },
                )
        self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
