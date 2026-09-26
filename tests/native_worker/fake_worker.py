"""Synthetic transport peer. No model, personal data, or filesystem logging."""
import argparse
import json
import struct
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--model-dir", required=True)
parser.add_argument("--threads", type=int, required=True)
parser.parse_args()


def exact(count):
    result = bytearray()
    while len(result) < count:
        chunk = sys.stdin.buffer.read(count - len(result))
        if not chunk:
            return None
        result.extend(chunk)
    return result


while True:
    header = exact(4)
    if header is None:
        break
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= 65536:
        break
    payload = exact(size)
    if payload is None:
        break
    request = json.loads(payload)
    source = request["source"]
    if source in {"slow", "cancel", "first"}:
        time.sleep(0.08)
    if source == "timeout":
        time.sleep(30)
    if source == "exit":
        sys.exit(7)
    if source == "oversized":
        sys.stdout.buffer.write(struct.pack(">I", 65537))
        sys.stdout.buffer.flush()
        continue
    if source == "truncated":
        sys.stdout.buffer.write(struct.pack(">I", 50) + b"{}")
        sys.stdout.buffer.flush()
        sys.exit(0)
    response = {"id": request["id"], "translation": "translated " + source, "error": None}
    if source == "wrong_id":
        response["id"] += 1
    if source == "backend_error":
        response["error"] = "synthetic_error"
    if source == "large_translation":
        response["translation"] = "x" * 4097
    if source == "wrong_type":
        response["translation"] = []
    if source == "history":
        assert request["context"] is True
        assert request["history"] == [{"source": None, "committed": "prior", "language": "und"}]
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    if source == "bad_json":
        encoded = b"{"
    if source == "bad_utf8":
        encoded = b'{"id":1,"translation":"\xff","error":null}'
    frame = struct.pack(">I", len(encoded)) + encoded
    if source == "fragmented":
        for offset in range(0, len(frame), 3):
            sys.stdout.buffer.write(frame[offset:offset + 3])
            sys.stdout.buffer.flush()
            time.sleep(0.001)
    else:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
