"""Asynchronous client for the existing length-prefixed TransIME worker.

No text, key, or translation is logged. The PIME event thread only enqueues
owned strings; slow model startup and inference stay on a daemon thread.
"""

import json
import queue
import struct
import subprocess
import threading

MAX_FRAME = 65536


def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("worker closed")
        data.extend(chunk)
    return bytes(data)


class WorkerBridge:
    def __init__(self, command):
        if not isinstance(command, list) or not command or not all(
                isinstance(item, str) and item for item in command):
            raise ValueError("worker command must be a nonempty string list")
        self.command = command
        self.jobs = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.version = 0
        self.ready = None
        self.process = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def invalidate(self):
        with self.lock:
            self.version += 1
            self.ready = None

    def request(self, source):
        with self.lock:
            self.version += 1
            version = self.version
            self.ready = None
        try:
            while True:
                self.jobs.get_nowait()
        except queue.Empty:
            pass
        self.jobs.put_nowait((version, source))

    def get_ready(self, source):
        with self.lock:
            if self.ready and self.ready[0] == self.version and self.ready[1] == source:
                return self.ready[2]
        return None

    def close(self):
        self.invalidate()
        try:
            if self.process is not None:
                self.process.kill()
        except OSError:
            pass

    def _exchange(self, version, source):
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        request = {"id": version, "source": source, "history": [], "context": False}
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not 0 < len(payload) <= MAX_FRAME:
            return None
        self.process.stdin.write(struct.pack(">I", len(payload)) + payload)
        self.process.stdin.flush()
        size = struct.unpack(">I", _read_exact(self.process.stdout, 4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError("invalid worker frame")
        response = json.loads(_read_exact(self.process.stdout, size).decode("utf-8"))
        text = response.get("translation")
        if response.get("id") != version or response.get("error") is not None:
            return None
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 4096:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            return None
        return text

    def _run(self):
        while True:
            version, source = self.jobs.get()
            try:
                result = self._exchange(version, source)
            except (OSError, ValueError, EOFError, BrokenPipeError, UnicodeError):
                result = None
                if self.process is not None:
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                    self.process = None
            with self.lock:
                if version == self.version and result is not None:
                    self.ready = (version, source, result)
