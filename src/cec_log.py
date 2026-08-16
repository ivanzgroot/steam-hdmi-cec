"""Timestamped, size-capped logging shared by the hook and the watcher.

Every line goes to both the log file and stdout. stdout matters because systemd
captures it into the journal, so `journalctl -u cec-hdmi-controller` and the log
file always agree without the daemon writing anything twice.
"""

import os
import time


class Logger:
    def __init__(self, path, tag="cec", max_bytes=1048576, keep=2, echo=True):
        self.path = path
        self.tag = tag
        self.max_bytes = max_bytes
        self.keep = keep
        self.echo = echo

    def _rotate_if_needed(self):
        if self.max_bytes <= 0:
            return
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        try:
            for index in range(self.keep, 1, -1):
                older = "%s.%d" % (self.path, index - 1)
                if os.path.exists(older):
                    os.replace(older, "%s.%d" % (self.path, index))
            if self.keep >= 1:
                os.replace(self.path, self.path + ".1")
            else:
                os.unlink(self.path)
        except OSError:
            pass

    def __call__(self, message):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.tag, message)
        if self.echo:
            print(line, flush=True)
        try:
            self._rotate_if_needed()
            with open(self.path, "a", errors="replace") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # Never let an unwritable log stop a wake; say so once and continue.
            if self.echo:
                print("%s [%s] WARNING: cannot write %s: %s"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), self.tag, self.path, exc),
                      flush=True)
