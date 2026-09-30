#!/usr/bin/env python3
"""Exercise the installed VoiceFlypy service over distributed notifications."""

import argparse
import sys
import time
import uuid

import Foundation
import objc


START_NAME = "com.simonli.voiceflypy.start"
STOP_NAME = "com.simonli.voiceflypy.stop"
EVENT_NAMES = (
    "com.simonli.voiceflypy.started",
    "com.simonli.voiceflypy.mic-ready",
    "com.simonli.voiceflypy.partial",
    "com.simonli.voiceflypy.final",
    "com.simonli.voiceflypy.error",
)


class Observer(Foundation.NSObject):
    def initWithSession_(self, session_id):
        self = objc.super(Observer, self).init()
        if self is None:
            return None
        self.session_id = session_id
        self.started = False
        self.mic_ready = False
        self.accepted_elapsed_ms = None
        self.finished = False
        self.failed = False
        self.posted_at = 0.0
        return self

    def handle_(self, notification):
        info = notification.userInfo() or {}
        if str(info.get("session", "")) != self.session_id:
            return
        name = str(notification.name())
        text = str(info.get("text", ""))
        message = str(info.get("message", ""))
        if name.endswith(".started"):
            self.started = True
            elapsed = (time.monotonic() - self.posted_at) * 1000
            self.accepted_elapsed_ms = elapsed
            print(
                f"[smoke] service accepted session={self.session_id} "
                f"elapsed_ms={elapsed:.3f}",
                flush=True,
            )
        elif name.endswith(".mic-ready"):
            self.mic_ready = True
            elapsed = (time.monotonic() - self.posted_at) * 1000
            print(f"[smoke] microphone ready elapsed_ms={elapsed:.3f}", flush=True)
        elif name.endswith(".partial"):
            print(f"[smoke] partial: {text}", flush=True)
        elif name.endswith(".final"):
            self.finished = True
            print(f"[smoke] final: {text or '<empty>'}", flush=True)
        elif name.endswith(".error"):
            self.failed = True
            self.finished = True
            print(f"[smoke] error: {message or '<unknown>'}", flush=True)


def pump_run_loop(seconds, predicate=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate is not None and predicate():
            return True
        Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05)
        )
    return predicate() if predicate is not None else True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--ready-timeout", type=float, default=3.0)
    parser.add_argument("--max-accept-ms", type=float)
    args = parser.parse_args()

    session_id = str(uuid.uuid4())
    center = Foundation.NSDistributedNotificationCenter.defaultCenter()
    observer = Observer.alloc().initWithSession_(session_id)
    for name in EVENT_NAMES:
        center.addObserver_selector_name_object_(observer, "handle:", name, None)

    try:
        observer.posted_at = time.monotonic()
        center.postNotificationName_object_userInfo_deliverImmediately_(
            START_NAME, None, {"session": session_id}, True
        )
        if not pump_run_loop(3.0, lambda: observer.started or observer.failed):
            print("[smoke] no start acknowledgement", file=sys.stderr)
            return 2
        if observer.failed:
            return 2
        if (
            args.max_accept_ms is not None
            and observer.accepted_elapsed_ms is not None
            and observer.accepted_elapsed_ms > args.max_accept_ms
        ):
            center.postNotificationName_object_userInfo_deliverImmediately_(
                STOP_NAME, None, {"session": session_id}, True
            )
            print(
                f"[smoke] service acceptance exceeded {args.max_accept_ms:.1f} ms",
                file=sys.stderr,
            )
            return 2
        if not pump_run_loop(
            args.ready_timeout,
            lambda: observer.mic_ready or observer.failed,
        ):
            center.postNotificationName_object_userInfo_deliverImmediately_(
                STOP_NAME, None, {"session": session_id}, True
            )
            print("[smoke] microphone did not become ready", file=sys.stderr)
            return 2
        if observer.failed:
            return 2

        print(f"[smoke] speak now for {args.seconds:.1f} seconds", flush=True)
        pump_run_loop(args.seconds)
        center.postNotificationName_object_userInfo_deliverImmediately_(
            STOP_NAME, None, {"session": session_id}, True
        )
        if not pump_run_loop(args.timeout, lambda: observer.finished):
            print("[smoke] no final response", file=sys.stderr)
            return 2
        return 2 if observer.failed else 0
    finally:
        center.removeObserver_(observer)


if __name__ == "__main__":
    raise SystemExit(main())
