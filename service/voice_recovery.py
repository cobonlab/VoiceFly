"""Bounded, session-local deadlines. No audio, recognized text or OS imports."""
import threading

FINALIZATION_TIMEOUT_SECONDS = 6.0
MICROPHONE_CLOSE_TIMEOUT_SECONDS = 3.0


class RecoveryDeadline:
    def __init__(self, seconds, on_timeout):
        if seconds <= 0:
            raise ValueError("deadline must be positive")
        self._lock = threading.Lock()
        self._finished = False
        self.timed_out = False
        self._on_timeout = on_timeout
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self.timed_out = True
        # Never hold the deadline lock while notifying/restarting the service.
        self._on_timeout()

    def finish(self):
        with self._lock:
            self._finished = True
            success = not self.timed_out
        self._timer.cancel()
        return success
