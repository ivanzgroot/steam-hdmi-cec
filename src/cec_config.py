"""config.conf: plain KEY=VALUE, parsed in exactly one place.

In the previous design this file had to be read by two languages at once - bash
sourced it and Python parsed it - which cost more than it looked like. Every
default had to be written twice and kept in sync by a test; the Python side had
to guess at shell quoting; and because bash `source` executes what it reads, a
value containing $(...) was arbitrary code running as root.

Nothing here is executed. Values are data, defaults live in one dictionary, and
DEFAULTS below is the single source of truth that tests/test_packaging.py checks
the shipped config against.

A broken config must never stop a wake. Anything unparseable is recorded in
.problems, logged once at startup, and replaced with its default.
"""

import os
import re

CEC_DIR = "/etc/cec-hdmi"
DEFAULT_CONFIG = os.path.join(CEC_DIR, "config.conf")

DEFAULTS = {
    # -- adapter and identity
    "CEC_DEVICE": "/dev/cec0",
    "OSD_NAME": "SteamOS",
    "DEVICE_TYPE": "playback",
    "CEC_VERSION": "1.4",
    "VENDOR_ID": "",

    # -- what a wake is allowed to do
    "WAKE_TV": "1",
    "CLAIM_SOURCE": "1",
    "WAKE_AUDIO": "1",
    "SEND_STREAM_PATH": "0",
    "SEND_REMOTE_POWER_KEY": "0",

    # -- what a standby is allowed to do
    "STANDBY_TV": "1",
    "STANDBY_AUDIO": "1",
    "STANDBY_BROADCAST": "0",

    # -- decide-then-send, or just send
    "FORCE_ALL_FRAMES": "0",

    # -- timing
    "FRAME_GAP_MS": "100",
    "REPLY_TIMEOUT_MS": "1200",
    "WAKE_ATTEMPTS": "5",
    "WAKE_SETTLE_MS": "1500",

    # -- escape hatches
    "EXTRA_WAKE_FRAMES": "",
    "EXTRA_STANDBY_FRAMES": "",

    # -- controller watcher
    "COOLDOWN_SECONDS": "2.5",
    "BUTTON_CODES": "BTN_MODE BTN_HOME KEY_HOMEPAGE",
    "GAMEPAD_ONLY": "0",
    "DRY_RUN": "0",
    "RESCAN_SECONDS": "5",
    "NOTIFY_ON_TRIGGER": "0",
    "NOTIFY_ON_FAILURE": "1",

    # -- logs
    "LOG_MAX_BYTES": "1048576",
    "LOG_KEEP": "2",
}

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def read_config(path):
    """Parse KEY=VALUE into a dict. Returns (values, problems)."""
    values = dict(DEFAULTS)
    problems = []
    try:
        with open(path, "r", errors="replace") as handle:
            for lineno, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, value = line.partition("=")
                key = key.strip()
                if not sep or not _KEY_RE.fullmatch(key):
                    problems.append("line %d: not a KEY=VALUE assignment" % lineno)
                    continue
                values[key] = _unquote(value.strip())
    except FileNotFoundError:
        problems.append("%s not found, using built-in defaults" % path)
    except OSError as exc:
        problems.append("cannot read %s: %s" % (path, exc))
    return values, problems


def _unquote(value):
    """Strip one layer of matching quotes; otherwise drop a trailing comment.

    Only a quote at both ends counts, so a value that merely contains a quote is
    left alone instead of being truncated at the first one it happens to hold.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value.split("#", 1)[0].strip()


class Config:
    """Typed, validated access to the config file."""

    def __init__(self, path=DEFAULT_CONFIG):
        self.path = path
        self.values, self.problems = read_config(path)

        self.device = self.text("CEC_DEVICE")
        self.osd_name = self.text("OSD_NAME")
        self.device_type = self.text("DEVICE_TYPE")
        self.cec_version = self.text("CEC_VERSION")
        self.vendor_id = self.text("VENDOR_ID")

        self.wake_tv = self.flag("WAKE_TV")
        self.claim_source = self.flag("CLAIM_SOURCE")
        self.wake_audio = self.flag("WAKE_AUDIO")
        self.send_stream_path = self.flag("SEND_STREAM_PATH")
        self.send_remote_power_key = self.flag("SEND_REMOTE_POWER_KEY")

        self.standby_tv = self.flag("STANDBY_TV")
        self.standby_audio = self.flag("STANDBY_AUDIO")
        self.standby_broadcast = self.flag("STANDBY_BROADCAST")

        self.force_all_frames = self.flag("FORCE_ALL_FRAMES")

        self.frame_gap_ms = self.number("FRAME_GAP_MS", int, 0, 5000)
        self.reply_timeout_ms = self.number("REPLY_TIMEOUT_MS", int, 100, 10000)
        self.wake_attempts = self.number("WAKE_ATTEMPTS", int, 1, 20)
        self.wake_settle_ms = self.number("WAKE_SETTLE_MS", int, 0, 20000)

        self.extra_wake_frames = self.text("EXTRA_WAKE_FRAMES")
        self.extra_standby_frames = self.text("EXTRA_STANDBY_FRAMES")

        self.cooldown = self.number("COOLDOWN_SECONDS", float, 0.0, 3600.0)
        self.rescan = self.number("RESCAN_SECONDS", float, 1.0, 3600.0)
        self.gamepad_only = self.flag("GAMEPAD_ONLY")
        self.dry_run = self.flag("DRY_RUN")
        self.notify_on_trigger = self.flag("NOTIFY_ON_TRIGGER")
        self.notify_on_failure = self.flag("NOTIFY_ON_FAILURE")

        self.log_max_bytes = self.number("LOG_MAX_BYTES", int, 0, 1 << 30)
        self.log_keep = self.number("LOG_KEEP", int, 0, 50)

    # -- accessors

    def text(self, key):
        return str(self.values.get(key, DEFAULTS.get(key, ""))).strip()

    def flag(self, key):
        raw = self.text(key).lower()
        if raw in ("1", "yes", "true", "on"):
            return True
        if raw in ("0", "no", "false", "off", ""):
            return False
        self.problems.append("%s=%r is not a yes/no value, using %s"
                             % (key, raw, DEFAULTS[key]))
        return DEFAULTS[key] == "1"

    def number(self, key, cast, minimum=None, maximum=None):
        raw = self.text(key)
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            self.problems.append("%s=%r is not a number, using %s"
                                 % (key, raw, DEFAULTS[key]))
            return cast(DEFAULTS[key])
        if minimum is not None and value < minimum:
            self.problems.append("%s=%r is below %s, clamping" % (key, raw, minimum))
            return cast(minimum)
        if maximum is not None and value > maximum:
            self.problems.append("%s=%r is above %s, clamping" % (key, raw, maximum))
            return cast(maximum)
        return value

    def button_codes(self, key_names):
        """BUTTON_CODES resolved against the running kernel's key names.

        Returns (codes, unknown). Names this kernel does not define are reported
        rather than silently dropped - BTN_HOME is not in mainline
        input-event-codes.h and its absence should be a note, not a mystery.
        """
        raw = self.text("BUTTON_CODES")
        codes, unknown = set(), []
        for token in re.split(r"[\s,]+", raw.strip()):
            if not token:
                continue
            upper = token.upper()
            if upper in key_names:
                codes.add(key_names[upper])
                continue
            try:
                codes.add(int(token, 0))
            except ValueError:
                unknown.append(token)
        if not codes:
            self.problems.append(
                "BUTTON_CODES=%r resolved to nothing, falling back to BTN_MODE" % raw)
            codes.add(key_names.get("BTN_MODE", 0x13C))
        return codes, unknown
