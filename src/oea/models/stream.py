"""Attempt-scoped transient projections; durable receipts remain authoritative."""

from collections import OrderedDict

from oea.contracts.errors import require


class ResponseStreams:
    def __init__(self):
        self.active = {}
        self.snapshots = OrderedDict()
        self.snapshot_bytes = 0

    def start(self, run_id, action_id, attempt_id, position):
        value = {"run_id": run_id, "action_id": action_id, "attempt_id": attempt_id,
                 "attempt_position": position, "block_id": "content", "sequence": 0, "fragments": []}
        self.active[run_id] = value
        return value

    def append(self, value, text):
        if self.active.get(value["run_id"]) is not value:
            return None
        value["fragments"].append(text)
        value["sequence"] += 1
        return {key: item for key, item in value.items() if key != "fragments"} | {"kind": "model.delta", "text": text}

    def finish(self, value):
        if self.active.get(value["run_id"]) is value:
            self.active.pop(value["run_id"], None)

    def read(self, run_id, action_id, attempt_id, version):
        if version and version.startswith("live:"):
            value = self.snapshots.get((run_id, action_id, attempt_id, version))
            if value:
                self.snapshots.move_to_end((run_id, action_id, attempt_id, version))
            require(value is not None, "SOURCE_VERSION_CHANGED", "Transient response version is no longer available; read the original attempt's retained facts")
            return value
        active = self.active.get(run_id)
        if not active or action_id not in {None, active["action_id"]} or attempt_id not in {None, active["attempt_id"]}:
            return None
        version = "live:" + active["attempt_id"] + ":" + str(active["sequence"])
        key = (run_id, active["action_id"], active["attempt_id"], version)
        if key not in self.snapshots:
            value = {field: item for field, item in active.items() if field != "fragments"} | {
                "text": "".join(active["fragments"]), "version": version, "status": "streaming", "unsaved_gap": False}
            size = len(value["text"].encode("utf-8"))
            require(size <= 16 * 1024 * 1024, "SOURCE_VERSION_UNAVAILABLE", "Live response exceeds the snapshot cache bound; read its retained receipt after settlement")
            self.snapshots[key] = value
            self.snapshot_bytes += size
            while len(self.snapshots) > 16 or self.snapshot_bytes > 16 * 1024 * 1024:
                _, old = self.snapshots.popitem(last=False)
                self.snapshot_bytes -= len(old["text"].encode("utf-8"))
        return self.snapshots[key]
