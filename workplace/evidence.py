import hashlib
import json
import os
import time
from pathlib import Path


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


class Evidence:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.events = []

    def emit(self, kind, **data):
        event = {"id": len(self.events) + 1, "time": time.time(), "kind": kind, **data}
        self.events.append(event)
        with (self.root / "events.jsonl").open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event

    def seal(self):
        files = {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file()
            and p.name not in ("manifest.json", "grades.json", "SUMMARY.md")
        }
        save(self.root / "manifest.json", {"sha256": files})


def verify(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    for name, digest in manifest["sha256"].items():
        p = root / name
        if not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("unsafe manifest path")
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError("evidence changed: " + name)
