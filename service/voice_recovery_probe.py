"""Isolated launchd diagnostic. Never opens audio or registers input observers."""
import json
import os
from pathlib import Path
import threading

from voice_recovery import RecoveryDeadline


def run(directory):
    root = Path(directory).resolve(strict=True)
    if (not root.is_relative_to(Path('/private/tmp')) or
            not root.name.startswith('voicefly-recovery-probe-') or
            root.stat().st_uid != os.getuid()):
        raise ValueError('probe requires an owned temporary directory')
    attempt = root / 'attempt.json'
    try:
        fd = os.open(attempt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        previous = json.loads(attempt.read_text())
        (root / 'recovered.json').write_text(json.dumps(
            {'ready': True, 'pid': os.getpid(), 'previous_pid': previous['pid'],
             'microphone_opened': False, 'live_service_started': False}))
        return 0
    with os.fdopen(fd, 'w') as output:
        json.dump({'pid': os.getpid()}, output)
    guard = RecoveryDeadline(0.3, lambda: os._exit(75))
    # Model the observed stop call that never returns. launchd must start a
    # different process after exit 75; its second invocation records readiness.
    threading.Event().wait()
    return 1
