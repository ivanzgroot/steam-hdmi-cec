#!/usr/bin/env bash
# install.sh - HDMI-CEC wake/standby hooks + controller Home-button wake for SteamOS
#
# Installs:
#   /etc/cec-hdmi/cec-hook.sh              the actual cec-ctl logic (on/off)
#   /etc/cec-hdmi/cec-controller-watch.py  controller Home/Guide button watcher
#   /etc/cec-hdmi/config.conf              tunables (never overwritten on re-install)
#   cec-hdmi-power.service       boot -> wake TV, shutdown/reboot -> standby
#   cec-hdmi-sleep.service       before suspend/hibernate -> standby
#   cec-hdmi-resume.service      after resuming from suspend/hibernate -> wake TV
#   cec-hdmi-controller.service  long-running: controller Home button -> wake TV
#
# Usage:
#   sudo bash install.sh                 install (safe to re-run / update)
#   sudo bash install.sh uninstall       remove everything this script installed
#   sudo bash install.sh uninstall --purge   ...and delete /etc/cec-hdmi too
#   sudo bash install.sh status          show service/DPCD/controller/log state
#   bash install.sh --help               usage
#   bash install.sh --version            version of this script + what's installed

set -euo pipefail

VERSION="2.0.0"

CEC_DIR="/etc/cec-hdmi"
HOOK_SCRIPT="$CEC_DIR/cec-hook.sh"
WATCH_SCRIPT="$CEC_DIR/cec-controller-watch.py"
CONFIG_FILE="$CEC_DIR/config.conf"
CONFIG_DEFAULTS="$CEC_DIR/config.conf.default"
VERSION_FILE="$CEC_DIR/VERSION"
LOGFILE="$CEC_DIR/cec-hook.log"
CTRL_LOGFILE="$CEC_DIR/cec-controller.log"
POWER_UNIT="/etc/systemd/system/cec-hdmi-power.service"
SLEEP_UNIT="/etc/systemd/system/cec-hdmi-sleep.service"
RESUME_UNIT="/etc/systemd/system/cec-hdmi-resume.service"
CTRL_UNIT="/etc/systemd/system/cec-hdmi-controller.service"

SERVICES="cec-hdmi-power cec-hdmi-sleep cec-hdmi-resume cec-hdmi-controller"

usage() {
    cat << USAGE_EOF
install.sh $VERSION - HDMI-CEC TV control for SteamOS

  sudo bash install.sh                    install or update (idempotent)
  sudo bash install.sh uninstall          remove services and unit files
  sudo bash install.sh uninstall --purge  also delete $CEC_DIR (scripts, config, logs)
  sudo bash install.sh status             services, DPCD state, controllers, recent logs
  bash install.sh --help                  this text
  bash install.sh --version               script version and installed version

What gets installed
  $HOOK_SCRIPT
      Wakes the TV and claims this PC as the active HDMI source ("on"), or sends
      standby ("off"). Includes the DPCD 0x3001 CEC-tunneling re-enable fix that
      post-suspend wakes depend on. Run it by hand to test:
          sudo bash $HOOK_SCRIPT on
          sudo bash $HOOK_SCRIPT off
          sudo bash $HOOK_SCRIPT dpcd-status

  $WATCH_SCRIPT
      Long-running watcher. Any connected controller's Home/Guide button runs
      "cec-hook.sh on". Hotplug-aware, multi-controller, debounced. Standard
      library only - no evdev/pyudev/pip needed.
          sudo $WATCH_SCRIPT --detect    list devices, show which are watched
          sudo $WATCH_SCRIPT --monitor   print key events live (find your button)

  $CONFIG_FILE
      Cooldown, trigger button codes, dry-run, notifications, log caps.
      Written with defaults on first install and NEVER overwritten afterwards;
      current defaults are always mirrored to $CONFIG_DEFAULTS for reference.
      Edit it, then: sudo systemctl restart cec-hdmi-controller.service

Services
  cec-hdmi-power.service       boot -> wake TV; shutdown/reboot -> standby
  cec-hdmi-sleep.service       before suspend -> standby
  cec-hdmi-resume.service      after resume -> wake TV
  cec-hdmi-controller.service  controller Home button -> wake TV (always running)

Logs
  $LOGFILE            (CEC wake/standby)
  $CTRL_LOGFILE      (controller watcher)
  Both are size-capped via LOG_MAX_BYTES/LOG_KEEP in config.conf, and also go to
  the systemd journal: journalctl -u cec-hdmi-controller.service -f
USAGE_EOF
}

ACTION="${1:-install}"
ARG2="${2:-}"

case "$ACTION" in
    -h|--help|help)
        usage
        exit 0
        ;;
    -V|--version|version)
        echo "install.sh $VERSION"
        if [ -r "$VERSION_FILE" ]; then
            echo "installed:  $(cat "$VERSION_FILE")"
        else
            echo "installed:  (nothing installed at $CEC_DIR)"
        fi
        exit 0
        ;;
    install|uninstall|status)
        ;;
    *)
        echo "Unknown action: $ACTION" >&2
        echo >&2
        usage >&2
        exit 2
        ;;
esac

if [ "$ACTION" != "status" ] && [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo bash $0 $ACTION" >&2
    exit 1
fi

write_config_defaults() {
    cat > "$1" << 'CONFIG_EOF'
# /etc/cec-hdmi/config.conf
#
# Plain KEY=VALUE. Sourced by cec-hook.sh (bash) and parsed by
# cec-controller-watch.py (no shell expansion there - keep values literal).
# Re-running install.sh never overwrites this file; the shipped defaults are
# always mirrored to config.conf.default so you can diff after an upgrade.
#
# After editing:  sudo systemctl restart cec-hdmi-controller.service

# Ignore further Home-button presses for this many seconds after one fires.
# Also covers the duplicate events you get when Steam mirrors a physical pad
# onto a virtual uinput device. Fractional values are fine.
COOLDOWN_SECONDS=2.5

# Which button counts as "Home". Space- or comma-separated evdev key names, or
# raw numeric codes. A device is watched if it advertises at least one of these.
#   BTN_MODE      (316) Guide/Home/PS/Xbox button on almost every gamepad
#   BTN_HOME            not defined by mainline Linux input-event-codes.h; kept
#                       here for kernels/controllers that do define it, and
#                       harmlessly ignored (with a one-line note) when they do not
#   KEY_HOMEPAGE  (172) used by some Bluetooth pads that expose Home on a
#                       separate keyboard-ish input node
# Run "sudo /etc/cec-hdmi/cec-controller-watch.py --monitor" and press your
# button to find out what your controller actually sends.
BUTTON_CODES="BTN_MODE BTN_HOME KEY_HOMEPAGE"

# 1 = only watch devices that look like gamepads (advertise BTN_GAMEPAD/0x130).
# Default 0, because some controllers report Home on a separate non-gamepad node
# that would otherwise be skipped.
GAMEPAD_ONLY=0

# 1 = log "would trigger" instead of actually running cec-hook.sh. Lets you test
# button detection with no TV/CEC hardware involved.
DRY_RUN=0

# Safety-net rescan interval (seconds) for hotplugged controllers. Hotplug is
# normally instant via the kernel netlink uevent socket; this only backstops it.
RESCAN_SECONDS=5

# Desktop notifications (best effort; they generally show in Desktop Mode and
# not inside gamescope). 1 = on, 0 = off.
NOTIFY_ON_TRIGGER=0
NOTIFY_ON_FAILURE=1

# Log size cap. Each log is rotated to .1 .. .N once it reaches LOG_MAX_BYTES.
# Set LOG_MAX_BYTES=0 to disable rotation entirely.
LOG_MAX_BYTES=1048576
LOG_KEEP=2
CONFIG_EOF
}

install_all() {
    if ! command -v cec-ctl >/dev/null 2>&1; then
        echo "ERROR: cec-ctl not found (package: v4l-utils). Install it first." >&2
        exit 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: python3 not found. It ships with SteamOS - something is wrong." >&2
        exit 1
    fi

    # The watcher deliberately uses only the Python standard library (raw
    # /dev/input/event* reads + sysfs + a netlink uevent socket) instead of
    # python-evdev/pyudev. SteamOS has a read-only root with no compiler and an
    # unpopulated pacman keyring, so "pip install evdev" (a C extension) is the
    # single most likely thing to break a one-command install. Nothing to
    # install here, and no venv to keep in sync.
    echo "==> Using system python3: $(python3 --version 2>&1) (no extra packages needed)"

    echo "==> Creating $CEC_DIR"
    mkdir -p "$CEC_DIR"

    echo "==> Writing $HOOK_SCRIPT"
    cat > "$HOOK_SCRIPT" << 'HOOK_EOF'
#!/usr/bin/env bash
# Wakes the TV and claims this PC as the active HDMI-CEC source ("on"),
# or puts the TV into standby ("off"). Physical address is re-read from
# cec-ctl every run, so it stays correct if the HDMI port/cable changes.

set -uo pipefail

CEC_DEV="/dev/cec0"
CEC_DIR="/etc/cec-hdmi"
CONFIG_FILE="$CEC_DIR/config.conf"
LOGFILE="$CEC_DIR/cec-hook.log"
ACTION="${1:-}"

# Log rotation defaults; config.conf may override them.
LOG_MAX_BYTES=1048576
LOG_KEEP=2
# shellcheck source=/dev/null
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE" 2>/dev/null || true

rotate_log() {
    local size i
    case "${LOG_MAX_BYTES:-0}" in
        ''|*[!0-9]*) return 0 ;;
        0) return 0 ;;
    esac
    [ -f "$LOGFILE" ] || return 0
    size=$(stat -c %s "$LOGFILE" 2>/dev/null || echo 0)
    [ "$size" -lt "$LOG_MAX_BYTES" ] && return 0
    i="${LOG_KEEP:-2}"
    while [ "$i" -gt 1 ]; do
        [ -f "$LOGFILE.$((i - 1))" ] && mv -f "$LOGFILE.$((i - 1))" "$LOGFILE.$i"
        i=$((i - 1))
    done
    if [ "${LOG_KEEP:-2}" -ge 1 ]; then
        mv -f "$LOGFILE" "$LOGFILE.1"
    else
        rm -f "$LOGFILE"
    fi
}
rotate_log

log() {
    echo "$(date '+%F %T') [cec-hook:$ACTION] $*" | tee -a "$LOGFILE"
}

wait_for_device() {
    local i=0
    while [ ! -e "$CEC_DEV" ] && [ "$i" -lt 20 ]; do
        sleep 1
        i=$((i + 1))
    done
    [ -e "$CEC_DEV" ]
}

get_phys_addr() {
    local pa i=0
    local max_tries=20
    while [ "$i" -lt "$max_tries" ]; do
        pa=$(cec-ctl -s -x 2>/dev/null)
        echo "$(date '+%F %T') [cec-hook:get-phys-addr] attempt $((i + 1))/$max_tries: '$pa'" >> "$LOGFILE"
        if [[ "$pa" =~ ^[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]$ ]] && [ "$pa" != "f.f.f.f" ]; then
            printf '%s' "$pa"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

OSD_NAME="steamdeck"

# The CEC controller lives inside the DP->HDMI dongle and is driven over the
# DP AUX channel (CEC-Tunneling-over-AUX). On suspend the dongle loses power
# and its CEC block resets, but because the EDID is unchanged on resume the
# kernel never reconfigures it - so it looks alive (address still readable)
# while silently NACKing everything. These two helpers re-init it.

get_connector_path() {
    local adap p
    adap=$(cec-ctl 2>/dev/null | awk -F': *' '/Adapter Name/ {print $2; exit}' | tr -d '[:space:]')
    [ -z "$adap" ] && return 1
    for p in /sys/class/drm/card*-"$adap"; do
        if [ -e "$p/status" ]; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

# DPCD register offsets (decimal, for dd seek/skip)
DPCD_CEC_TUNNELING_CONTROL=12289   # 0x3001

# Find the /dev/drm_dp_auxN node belonging to our connector.
find_aux_dev() {
    local conn cname a tgt
    conn=$(get_connector_path) || return 1
    cname=$(basename "$conn")

    for a in "$conn"/drm_dp_aux*; do
        [ -d "$a" ] && { printf '/dev/%s' "$(basename "$a")"; return 0; }
    done

    for a in /sys/class/drm_dp_aux_dev/drm_dp_aux*; do
        [ -e "$a" ] || continue
        tgt=$(readlink -f "$a/device" 2>/dev/null)
        case "$tgt" in
            *"$cname"*) printf '/dev/%s' "$(basename "$a")"; return 0 ;;
        esac
    done

    [ -e /dev/drm_dp_aux0 ] && { printf '/dev/drm_dp_aux0'; return 0; }
    return 1
}

# THE key fix. After a suspend/resume the dongle's CEC engine comes back with
# DP_CEC_TUNNELING_CONTROL cleared, and the kernel never rewrites it because
# the EDID is unchanged - so CEC looks alive but NACKs every directed message.
# Writing the enable bit back restores it without a physical cable reseat.
enable_cec_tunneling() {
    local aux cur
    if ! aux=$(find_aux_dev); then
        log "WARNING: could not find a drm_dp_aux device, skipping tunneling enable"
        return 1
    fi

    cur=$(dd if="$aux" bs=1 skip="$DPCD_CEC_TUNNELING_CONTROL" count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
    if [ "$cur" = "01" ]; then
        log "CEC tunneling already enabled on $aux (0x3001=0x$cur)"
        return 0
    fi

    log "CEC tunneling disabled on $aux (0x3001=0x${cur:-??}), re-enabling"
    printf '\001' | dd of="$aux" bs=1 seek="$DPCD_CEC_TUNNELING_CONTROL" conv=notrunc 2>/dev/null
    sleep 1

    cur=$(dd if="$aux" bs=1 skip="$DPCD_CEC_TUNNELING_CONTROL" count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
    if [ "$cur" = "01" ]; then
        log "CEC tunneling re-enabled OK (0x3001=0x$cur)"
        return 0
    fi
    log "WARNING: tunneling enable did not stick (0x3001=0x${cur:-??})"
    return 1
}

# Soft: disable then re-enable CEC tunneling and re-claim the logical address.
# This rewrites the dongle's CEC registers over AUX. Cheap, no display impact.
reinit_cec_soft() {
    log "Re-initialising CEC tunneling (clear + re-claim logical address)"
    cec-ctl -s -C 2>&1 | sed 's/^/    [clear] /' | tee -a "$LOGFILE"
    sleep 1
    cec-ctl -s --playback --osd-name "$OSD_NAME" 2>&1 | sed 's/^/    [playback] /' | tee -a "$LOGFILE"
    sleep 2
}

# Hard: force the DRM connector to re-probe, which re-reads the EDID and makes
# the kernel fully re-register the CEC adapter - the software equivalent of
# physically reseating the HDMI cable. May briefly blank the display.
reinit_cec_hard() {
    local conn
    if ! conn=$(get_connector_path); then
        log "WARNING: could not locate DRM connector sysfs path, skipping hard reset"
        return 1
    fi
    log "Forcing DRM re-probe on $(basename "$conn") (software cable reseat)"
    log "  status before: $(cat "$conn/status" 2>/dev/null)"
    if echo off > "$conn/status" 2>/dev/null; then
        log "  wrote 'off' OK"
    else
        log "  ERROR: failed to write 'off' to $conn/status"
    fi
    sleep 2
    log "  status while forced off: $(cat "$conn/status" 2>/dev/null)"
    if echo detect > "$conn/status" 2>/dev/null; then
        log "  wrote 'detect' OK"
    else
        log "  ERROR: failed to write 'detect' to $conn/status"
    fi
    sleep 3
    log "  status after: $(cat "$conn/status" 2>/dev/null)"
    reinit_cec_soft
}

case "$ACTION" in
  on)
    if ! wait_for_device; then
        log "ERROR: $CEC_DEV never appeared, aborting"
        exit 1
    fi

    if ! PHYS_ADDR=$(get_phys_addr); then
        log "ERROR: never got a valid physical address from 'cec-ctl -s -x'"
        exit 1
    fi
    log "Detected physical address: $PHYS_ADDR"

    # cec-ctl exits 0 even when the TV NACKs a directed message (it only
    # reports "Tx, Not Acknowledged" in its output), so check the actual
    # text and retry - right after resume the TV's CEC receiver can take
    # a few seconds to start acknowledging even though its physical
    # address is already readable.
    # Escalation: the DPCD tunneling-enable write is applied before every
    # attempt (it is what actually fixes the post-resume NACKs, and note that
    # reinit_cec_soft's 'cec-ctl -C' clears the bit again). Attempts 2-3 add a
    # soft CEC re-init, attempts 4+ a full DRM re-probe.
    ACKED=1
    tries=0
    max_wake_tries=5
    while [ "$tries" -lt "$max_wake_tries" ]; do
        tries=$((tries + 1))

        if [ "$tries" -eq 2 ] || [ "$tries" -eq 3 ]; then
            reinit_cec_soft
        elif [ "$tries" -ge 4 ]; then
            reinit_cec_hard
        fi

        enable_cec_tunneling

        if [ "$tries" -gt 1 ]; then
            if NEW_PA=$(get_phys_addr); then
                PHYS_ADDR="$NEW_PA"
                log "Physical address after re-init: $PHYS_ADDR"
            fi
        fi

        WAKE_OUT=$( { cec-ctl -v -s -t0 --cec-version-1.4 --user-control-pressed=ui-cmd=power-on-function && \
          sleep 1 && \
          cec-ctl -v -s -t0 --cec-version-1.4 --image-view-on && \
          sleep 1 && \
          cec-ctl -v -s -t0 --cec-version-1.4 --set-stream-path=phys-addr="$PHYS_ADDR" && \
          sleep 1 && \
          cec-ctl -v -s --cec-version-1.4 --active-source=phys-addr="$PHYS_ADDR"; } 2>&1 )
        WAKE_STATUS=$?
        echo "$WAKE_OUT" | tee -a "$LOGFILE"

        if [ "$WAKE_STATUS" -eq 0 ] && ! printf '%s' "$WAKE_OUT" | grep -q "Not Acknowledged"; then
            ACKED=0
            log "TV acknowledged on attempt $tries/$max_wake_tries"
            break
        fi
        log "WARNING: attempt $tries/$max_wake_tries not acknowledged by TV"
        sleep 2
    done

    if [ "$ACKED" -eq 0 ]; then
        log "Wake sequence sent OK (phys-addr=$PHYS_ADDR)"
    else
        log "ERROR: TV never acknowledged the wake commands after $max_wake_tries attempts"
        exit 1
    fi
    ;;
  off)
    if [ ! -e "$CEC_DEV" ]; then
        log "WARNING: $CEC_DEV not present, skipping standby"
        exit 0
    fi

    cec-ctl --to 0 --standby 2>&1 | tee -a "$LOGFILE"

    if [ "$?" -eq 0 ]; then
        log "Standby sent OK"
    else
        log "ERROR: standby command failed"
        exit 1
    fi
    ;;
  dpcd-status)
    # Read-only diagnostic: is the dongle's CEC-tunneling enable bit set?
    # Prints to stdout only - deliberately does not touch the log.
    if aux=$(find_aux_dev); then
        cur=$(dd if="$aux" bs=1 skip="$DPCD_CEC_TUNNELING_CONTROL" count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
        case "$cur" in
            01) echo "$aux  DPCD 0x3001 = 0x01  (CEC tunneling ENABLED)" ;;
            "")  echo "$aux  DPCD 0x3001 = <unreadable>  (run as root?)" ;;
            *)  echo "$aux  DPCD 0x3001 = 0x$cur  (CEC tunneling DISABLED - next 'on' will fix it)" ;;
        esac
    else
        echo "no drm_dp_aux device found for the CEC adapter"
        exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {on|off|dpcd-status}" >&2
    exit 2
    ;;
esac
HOOK_EOF
    chmod +x "$HOOK_SCRIPT"

    echo "==> Writing $WATCH_SCRIPT"
    cat > "$WATCH_SCRIPT" << 'PYEOF'
#!/usr/bin/env python3
"""cec-controller-watch - wake the TV when a controller's Home/Guide button is pressed.

Watches every connected input device that advertises one of the configured
trigger button codes, picks hotplugged controllers up and dropped ones down
live, debounces, and shells out to /etc/cec-hdmi/cec-hook.sh on.

It never speaks CEC itself. cec-hook.sh owns all of that, including the DPCD
0x3001 CEC-tunneling re-enable that post-suspend wakes depend on.

Standard library only - no evdev, no pyudev, no pip, no venv. Devices are
enumerated through sysfs (/sys/class/input/event*/device/...), read as raw
struct input_event from /dev/input/event*, and hotplug arrives on a netlink
uevent socket. That keeps a read-only SteamOS root with no compiler viable.

Modes:
    (no args)   run as a daemon; what cec-hdmi-controller.service does
    --detect    list input devices and which ones would be watched, then exit
    --monitor   print key events live, to find out what your Home button sends
"""

import argparse
import asyncio
import os
import pwd
import re
import signal
import socket
import struct
import sys
import time

VERSION = "2.0.0"

CEC_DIR = "/etc/cec-hdmi"
DEFAULT_CONFIG = os.path.join(CEC_DIR, "config.conf")
HOOK_SCRIPT = os.path.join(CEC_DIR, "cec-hook.sh")
DEFAULT_LOG = os.path.join(CEC_DIR, "cec-controller.log")

SYS_INPUT = "/sys/class/input"
DEV_INPUT = "/dev/input"
KERNEL_CODE_HEADER = "/usr/include/linux/input-event-codes.h"

EV_KEY = 0x01
BTN_GAMEPAD = 0x130

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# 24 bytes on x86_64 (LP64): two 8-byte longs, two u16, one s32.
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# BITS_PER_LONG as seen by this process; the unit sysfs capability bitmaps use.
LONG_BITS = struct.calcsize("l") * 8

NETLINK_KOBJECT_UEVENT = 15
UEVENT_KERNEL_GROUP = 1

# Delays after a kernel "add" uevent before trying to open the node: the kernel
# announces the device before udev has applied its root:input 0660 permissions.
HOTPLUG_RETRY_DELAYS = (0.3, 1.0, 3.0)

DEFAULTS = {
    "COOLDOWN_SECONDS": "2.5",
    "BUTTON_CODES": "BTN_MODE BTN_HOME KEY_HOMEPAGE",
    "GAMEPAD_ONLY": "0",
    "DRY_RUN": "0",
    "RESCAN_SECONDS": "5",
    "NOTIFY_ON_TRIGGER": "0",
    "NOTIFY_ON_FAILURE": "1",
    "LOG_MAX_BYTES": "1048576",
    "LOG_KEEP": "2",
}

# Enough of linux/input-event-codes.h to resolve the names anyone would
# reasonably put in BUTTON_CODES. Augmented at runtime from the real header
# when it is present. Order matters: the first name listed for a code is the
# one used when printing that code back out.
BASE_KEY_NAMES = {
    "BTN_SOUTH": 0x130, "BTN_A": 0x130, "BTN_GAMEPAD": 0x130,
    "BTN_EAST": 0x131, "BTN_B": 0x131,
    "BTN_C": 0x132,
    "BTN_NORTH": 0x133, "BTN_X": 0x133,
    "BTN_WEST": 0x134, "BTN_Y": 0x134,
    "BTN_Z": 0x135,
    "BTN_TL": 0x136, "BTN_TR": 0x137, "BTN_TL2": 0x138, "BTN_TR2": 0x139,
    "BTN_SELECT": 0x13A, "BTN_START": 0x13B, "BTN_MODE": 0x13C,
    "BTN_THUMBL": 0x13D, "BTN_THUMBR": 0x13E,
    "BTN_DPAD_UP": 0x220, "BTN_DPAD_DOWN": 0x221,
    "BTN_DPAD_LEFT": 0x222, "BTN_DPAD_RIGHT": 0x223,
    "BTN_TRIGGER_HAPPY": 0x2C0,
    "KEY_HOME": 102, "KEY_POWER": 116, "KEY_MENU": 139, "KEY_SLEEP": 142,
    "KEY_WAKEUP": 143, "KEY_BACK": 158, "KEY_HOMEPAGE": 172,
}

_DEFINE_RE = re.compile(
    r"^#define\s+((?:KEY|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+|(?:KEY|BTN)_\w+)\s*(?:/\*|$)"
)


def load_key_names():
    """BASE_KEY_NAMES, plus every KEY_*/BTN_* the running system's headers define."""
    names = dict(BASE_KEY_NAMES)
    aliases = []
    try:
        with open(KERNEL_CODE_HEADER, "r", errors="replace") as fh:
            for line in fh:
                match = _DEFINE_RE.match(line.strip())
                if not match:
                    continue
                name, value = match.groups()
                if name.endswith(("_MAX", "_CNT")):
                    continue
                if value.startswith(("KEY_", "BTN_")):
                    aliases.append((name, value))
                else:
                    names.setdefault(name, int(value, 0))
    except OSError:
        pass
    for name, target in aliases:
        if target in names:
            names.setdefault(name, names[target])
    return names


KEY_NAMES = load_key_names()

CODE_NAMES = {}
for _name, _code in KEY_NAMES.items():
    CODE_NAMES.setdefault(_code, _name)


def code_name(code):
    return CODE_NAMES.get(code, "code_%d" % code)


def fmt_codes(codes):
    return ", ".join("%s(%d)" % (code_name(c), c) for c in codes) or "none"


# --------------------------------------------------------------------------- config


def read_config(path):
    """Parse the bash-sourceable KEY=VALUE config. Unknown keys are kept, bad
    lines are skipped; a broken config must never stop the watcher from running."""
    values = dict(DEFAULTS)
    problems = []
    try:
        with open(path, "r", errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, value = line.partition("=")
                key = key.strip()
                if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    problems.append("line %d: not a KEY=VALUE assignment" % lineno)
                    continue
                value = value.strip()
                if value[:1] in ("'", '"'):
                    quote = value[0]
                    end = value.find(quote, 1)
                    value = value[1:end] if end > 0 else value[1:]
                else:
                    value = value.split("#", 1)[0].strip()
                values[key] = value
    except FileNotFoundError:
        problems.append("%s not found, using built-in defaults" % path)
    except OSError as exc:
        problems.append("could not read %s: %s" % (path, exc))
    return values, problems


class Config:
    def __init__(self, path=DEFAULT_CONFIG):
        self.path = path
        self.values, self.problems = read_config(path)
        self.cooldown = self._number("COOLDOWN_SECONDS", float, minimum=0.0)
        self.rescan = self._number("RESCAN_SECONDS", float, minimum=1.0)
        self.log_max_bytes = self._number("LOG_MAX_BYTES", int, minimum=0)
        self.log_keep = self._number("LOG_KEEP", int, minimum=0)
        self.gamepad_only = self._bool("GAMEPAD_ONLY")
        self.dry_run = self._bool("DRY_RUN")
        self.notify_on_trigger = self._bool("NOTIFY_ON_TRIGGER")
        self.notify_on_failure = self._bool("NOTIFY_ON_FAILURE")
        self.trigger_codes, self.unknown_codes = self._codes()

    def _number(self, key, cast, minimum=None):
        raw = self.values.get(key, DEFAULTS[key])
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            self.problems.append("%s=%r is not a number, using %s" % (key, raw, DEFAULTS[key]))
            return cast(DEFAULTS[key])
        if minimum is not None and value < minimum:
            self.problems.append("%s=%r below minimum %s, clamping" % (key, raw, minimum))
            return cast(minimum)
        return value

    def _bool(self, key):
        raw = str(self.values.get(key, DEFAULTS[key])).strip().lower()
        if raw in ("1", "yes", "true", "on"):
            return True
        if raw in ("0", "no", "false", "off", ""):
            return False
        self.problems.append("%s=%r is not a boolean, using %s" % (key, raw, DEFAULTS[key]))
        return DEFAULTS[key] == "1"

    def _codes(self):
        raw = self.values.get("BUTTON_CODES", DEFAULTS["BUTTON_CODES"])
        codes, unknown = set(), []
        for token in re.split(r"[\s,]+", raw.strip()):
            if not token:
                continue
            upper = token.upper()
            if upper in KEY_NAMES:
                codes.add(KEY_NAMES[upper])
                continue
            try:
                codes.add(int(token, 0))
            except ValueError:
                unknown.append(token)
        if not codes:
            self.problems.append(
                "BUTTON_CODES=%r resolved to nothing, falling back to BTN_MODE" % raw)
            codes.add(KEY_NAMES["BTN_MODE"])
        return codes, unknown


# --------------------------------------------------------------------------- logging


class Logger:
    """Timestamped lines to both the log file and stdout (which systemd captures
    into the journal), matching cec-hook.sh's format. Size-capped in place."""

    def __init__(self, path=DEFAULT_LOG, max_bytes=0, keep=2, tag="cec-controller", echo=True):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.tag = tag
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
            with open(self.path, "a", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print("%s [%s] WARNING: cannot write %s: %s"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), self.tag, self.path, exc), flush=True)


# --------------------------------------------------------------------------- devices


def _read_sysfs(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def parse_bitmap(text, word_bits=LONG_BITS):
    """Decode a sysfs capabilities bitmask into the set of bit numbers set in it.

    The kernel writes these as one unsigned long per word, most significant word
    first, space separated, formatted with plain "%lx" - so words are NOT zero
    padded ("7cdb000000000000 0 0 0 0") and their text length says nothing about
    the word size. It is always BITS_PER_LONG, which for a native process is
    sizeof(long) * 8.
    """
    words = text.split()
    if not words:
        return set()
    bits = set()
    for index, word in enumerate(reversed(words)):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = index * word_bits
        offset = 0
        while value:
            if value & 1:
                bits.add(base + offset)
            value >>= 1
            offset += 1
    return bits


class InputDevice:
    __slots__ = ("event", "path", "name", "phys", "keys", "has_abs", "is_gamepad", "fd")

    def __init__(self, event):
        base = os.path.join(SYS_INPUT, event, "device")
        self.event = event
        self.path = os.path.join(DEV_INPUT, event)
        self.name = _read_sysfs(os.path.join(base, "name")) or event
        self.phys = _read_sysfs(os.path.join(base, "phys"))
        self.keys = parse_bitmap(_read_sysfs(os.path.join(base, "capabilities", "key")))
        self.has_abs = bool(parse_bitmap(_read_sysfs(os.path.join(base, "capabilities", "abs"))))
        self.is_gamepad = BTN_GAMEPAD in self.keys and self.has_abs
        self.fd = -1


def list_input_devices():
    try:
        entries = os.listdir(SYS_INPUT)
    except OSError:
        return []

    def sort_key(entry):
        try:
            return int(entry[len("event"):])
        except ValueError:
            return 1 << 30

    devices = []
    for entry in sorted((e for e in entries if e.startswith("event")), key=sort_key):
        device = InputDevice(entry)
        if os.path.exists(device.path):
            devices.append(device)
    return devices


# --------------------------------------------------------------------------- notify


def _session_targets():
    """(uid, passwd entry) for every logged-in user with a session bus."""
    targets = []
    try:
        entries = os.listdir("/run/user")
    except OSError:
        return targets
    for entry in entries:
        if not entry.isdigit():
            continue
        uid = int(entry)
        if uid < 1000 or not os.path.exists("/run/user/%d/bus" % uid):
            continue
        try:
            targets.append((uid, pwd.getpwuid(uid)))
        except KeyError:
            continue
    return targets


async def notify(log, summary, body, urgency="normal"):
    """Best-effort desktop notification. Shows up in Desktop Mode; gamescope
    generally swallows it in Gaming Mode, so nothing here is load-bearing."""
    for uid, entry in _session_targets():
        env = {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % uid,
            "XDG_RUNTIME_DIR": "/run/user/%d" % uid,
            "DISPLAY": ":0",
            "HOME": entry.pw_dir,
            "USER": entry.pw_name,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "cec-hdmi", "-u", urgency, summary, body,
                env=env, user=uid, group=entry.pw_gid,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            log("notify: could not notify %s: %s" % (entry.pw_name, exc))


# --------------------------------------------------------------------------- watcher


class ControllerWatcher:
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.trigger_codes = config.trigger_codes
        self.open_devices = {}
        self.open_failures = {}
        self.last_trigger = float("-inf")
        self.hook_running = False
        self.loop = None
        self.netlink = None
        self.stopping = None

    # -- device bookkeeping

    def _watchable(self, device):
        matched = sorted(self.trigger_codes & device.keys)
        if not matched:
            return None
        if self.config.gamepad_only and not device.is_gamepad:
            return None
        return matched

    def _add(self, device):
        if device.path in self.open_devices:
            return
        matched = self._watchable(device)
        if matched is None:
            return
        try:
            fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            if self.open_failures.get(device.path) != exc.errno:
                self.open_failures[device.path] = exc.errno
                self.log("WARNING: cannot open %s [%s]: %s"
                         % (device.path, device.name, exc.strerror))
            return
        self.open_failures.pop(device.path, None)
        device.fd = fd
        self.open_devices[device.path] = device
        self.loop.add_reader(fd, self._readable, device.path)
        self.log("watching %s [%s] trigger codes: %s"
                 % (device.path, device.name, fmt_codes(matched)))

    def _drop(self, path, reason):
        device = self.open_devices.pop(path, None)
        if device is None:
            return
        try:
            self.loop.remove_reader(device.fd)
        except (OSError, ValueError):
            pass
        try:
            os.close(device.fd)
        except OSError:
            pass
        self.log("stopped watching %s [%s] (%s)" % (path, device.name, reason))

    def _scan(self):
        present = set()
        for device in list_input_devices():
            present.add(device.path)
            self._add(device)
        for path in list(self.open_devices):
            if path not in present:
                self._drop(path, "device gone")
        for path in list(self.open_failures):
            if path not in present:
                self.open_failures.pop(path, None)

    def _try_add_path(self, path):
        if path in self.open_devices or not os.path.exists(path):
            return
        event = os.path.basename(path)
        if not os.path.isdir(os.path.join(SYS_INPUT, event)):
            return
        self._add(InputDevice(event))

    # -- event handling

    def _readable(self, path):
        device = self.open_devices.get(path)
        if device is None:
            return
        try:
            data = os.read(device.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError as exc:
            self._drop(path, "read error: %s" % exc.strerror)
            return
        if not data:
            self._drop(path, "end of file")
            return
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(EVENT_FMT, data, offset)
            # value 1 is key-down; 2 is autorepeat, 0 is key-up - ignore both.
            if etype == EV_KEY and value == 1 and code in self.trigger_codes:
                self._on_press(device, code)

    def _on_press(self, device, code):
        where = "%s [%s] %s" % (device.path, device.name, code_name(code))
        if self.hook_running:
            self.log("%s: wake already in progress, ignoring" % where)
            return
        now = time.monotonic()
        elapsed = now - self.last_trigger
        if elapsed < self.config.cooldown:
            self.log("%s: within %.1fs cooldown (%.1fs left), ignoring"
                     % (where, self.config.cooldown, self.config.cooldown - elapsed))
            return
        self.last_trigger = now
        if self.config.dry_run:
            self.log("%s: DRY RUN - would run '%s on'" % (where, HOOK_SCRIPT))
            return
        self.log("%s: triggering wake ('%s on')" % (where, HOOK_SCRIPT))
        self.hook_running = True
        self.loop.create_task(self._run_hook())

    async def _run_hook(self):
        """Run cec-hook.sh out of band. The wake sequence can take tens of
        seconds when the DPCD fix and retry escalation kick in, and the input
        loop has to keep reading throughout."""
        started = time.monotonic()
        status = -1
        try:
            proc = await asyncio.create_subprocess_exec(
                HOOK_SCRIPT, "on",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            status = await proc.wait()
        except OSError as exc:
            self.log("ERROR: could not run '%s on': %s" % (HOOK_SCRIPT, exc))
        finally:
            self.hook_running = False

        took = time.monotonic() - started
        if status == 0:
            self.log("wake sequence finished OK in %.1fs" % took)
            if self.config.notify_on_trigger:
                await notify(self.log, "TV woken", "HDMI-CEC wake sent from the controller.")
        else:
            self.log("ERROR: wake sequence failed (exit %d) after %.1fs - see %s"
                     % (status, took, os.path.join(CEC_DIR, "cec-hook.log")))
            if self.config.notify_on_failure:
                await notify(self.log, "HDMI-CEC wake failed",
                             "The TV did not acknowledge. See cec-hook.log.", "critical")

    # -- hotplug

    def _start_netlink(self):
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind((0, UEVENT_KERNEL_GROUP))
            sock.setblocking(False)
        except OSError as exc:
            self.log("hotplug: netlink uevent socket unavailable (%s); relying on the "
                     "%.0fs rescan instead" % (exc, self.config.rescan))
            return
        self.netlink = sock
        self.loop.add_reader(sock.fileno(), self._netlink_readable)
        self.log("hotplug: listening on the kernel netlink uevent socket")

    def _netlink_readable(self):
        while True:
            try:
                data = self.netlink.recv(1 << 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self.log("hotplug: netlink read failed (%s), falling back to rescans" % exc)
                try:
                    self.loop.remove_reader(self.netlink.fileno())
                    self.netlink.close()
                except (OSError, ValueError):
                    pass
                self.netlink = None
                return
            action = devname = subsystem = None
            for field in data.split(b"\x00"):
                if field.startswith(b"ACTION="):
                    action = field[7:].decode("utf-8", "replace")
                elif field.startswith(b"DEVNAME="):
                    devname = field[8:].decode("utf-8", "replace")
                elif field.startswith(b"SUBSYSTEM="):
                    subsystem = field[10:].decode("utf-8", "replace")
            if subsystem != "input" or not devname:
                continue
            # DEVNAME is a POSIX path relative to /dev, e.g. "input/event7".
            path = devname if devname.startswith("/") else "/dev/" + devname
            if not path.startswith(DEV_INPUT + "/event"):
                continue
            if action == "add":
                # The kernel announces the node before udev chmods it, so retry.
                for delay in HOTPLUG_RETRY_DELAYS:
                    self.loop.call_later(delay, self._try_add_path, path)
            elif action == "remove":
                self._drop(path, "unplugged")

    # -- lifecycle

    async def _rescan_loop(self):
        while True:
            await asyncio.sleep(self.config.rescan)
            # Never let one bad scan kill the task: it is the fallback that
            # keeps hotplug working if the netlink socket is gone.
            try:
                self._scan()
            except OSError as exc:
                self.log("WARNING: device rescan failed: %s" % exc)

    def stop(self, reason):
        self.log("stopping (%s)" % reason)
        if not self.stopping.done():
            self.stopping.set_result(None)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.stopping = self.loop.create_future()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self.stop, signal.Signals(sig).name)
            except (NotImplementedError, RuntimeError):
                pass

        self.log("cec-controller-watch %s starting (config: %s)" % (VERSION, self.config.path))
        for problem in self.config.problems:
            self.log("config: %s" % problem)
        self.log("trigger codes: %s" % fmt_codes(sorted(self.trigger_codes)))
        if self.config.unknown_codes:
            self.log("config: BUTTON_CODES entries this kernel does not define, ignored: %s"
                     % ", ".join(self.config.unknown_codes))
        self.log("cooldown: %.1fs | gamepad-only: %s | dry-run: %s"
                 % (self.config.cooldown,
                    "yes" if self.config.gamepad_only else "no",
                    "yes" if self.config.dry_run else "no"))

        self._start_netlink()
        self._scan()
        if not self.open_devices:
            self.log("no controllers with a trigger button are connected yet - waiting "
                     "(run with --detect to see what is present)")

        rescan = self.loop.create_task(self._rescan_loop())
        try:
            await self.stopping
        finally:
            rescan.cancel()
            for path in list(self.open_devices):
                self._drop(path, "shutting down")
            if self.netlink is not None:
                try:
                    self.loop.remove_reader(self.netlink.fileno())
                    self.netlink.close()
                except (OSError, ValueError):
                    pass
        return 0


# --------------------------------------------------------------------------- detect


def run_detect(config):
    """List every input device and say exactly which ones the daemon watches."""
    print("cec-controller-watch %s" % VERSION)
    print("config:         %s" % config.path)
    for problem in config.problems:
        print("  config note:  %s" % problem)
    print("trigger codes:  %s" % fmt_codes(sorted(config.trigger_codes)))
    if config.unknown_codes:
        print("  not defined by this kernel, ignored: %s" % ", ".join(config.unknown_codes))
    print("gamepad-only:   %s" % ("yes" if config.gamepad_only else "no"))
    print("cooldown:       %.1fs%s"
          % (config.cooldown, "  (DRY RUN - hook will not be called)" if config.dry_run else ""))
    if os.geteuid() != 0:
        print("\nNOTE: not running as root; /dev/input/event* is usually root:input 0660,")
        print("      so 'readable' below will say no. Re-run with sudo for a true picture.")
    print()

    devices = list_input_devices()
    if not devices:
        print("No input devices found under %s." % SYS_INPUT)
        return 1

    watched = 0
    for device in devices:
        matched = sorted(config.trigger_codes & device.keys)
        try:
            fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            readable, why = True, ""
        except OSError as exc:
            readable, why = False, " (%s)" % exc.strerror

        if not matched:
            verdict = "ignored - no trigger button"
        elif config.gamepad_only and not device.is_gamepad:
            verdict = "ignored - GAMEPAD_ONLY=1 and this is not a gamepad"
        elif not readable:
            verdict = "WOULD FAIL - cannot open%s" % why
        else:
            verdict = "WATCHED"
            watched += 1

        print("%-22s %s" % (device.path, device.name))
        if device.phys:
            print("    phys:      %s" % device.phys)
        print("    gamepad:   %-4s readable: %s%s"
              % ("yes" if device.is_gamepad else "no", "yes" if readable else "no", why))
        print("    triggers:  %s" % fmt_codes(matched))
        print("    -> %s" % verdict)
        print()

    print("%d input device(s), %d watched." % (len(devices), watched))
    if watched == 0:
        print()
        print("Nothing would trigger a wake. Things to try:")
        print("  * connect a controller and re-run")
        print("  * sudo %s --monitor   then press Home and see what code appears"
              % os.path.abspath(sys.argv[0]))
        print("  * add that code to BUTTON_CODES in %s" % config.path)
        return 1
    return 0


# --------------------------------------------------------------------------- monitor


def run_monitor(config):
    """Print every key-down event from every readable input device.

    This is the tool for the open question Steam raises on SteamOS: whether the
    Guide/Home button is visible on the raw evdev node while Steam is running,
    or whether Steam consumes it. Run it in Desktop Mode and again with Gaming
    Mode on screen (over SSH - /dev/input does not care which UI is in front),
    press Home, and see whether an event shows up.

    Nothing here grabs a device (no EVIOCGRAB), so Steam's own input handling,
    the overlay and games are unaffected while this runs.
    """
    import selectors

    if os.geteuid() != 0:
        print("NOTE: not running as root; most /dev/input/event* nodes will be "
              "unreadable. Re-run with sudo.\n")

    print("cec-controller-watch %s - live key monitor" % VERSION)
    print("Passive read only, no EVIOCGRAB: Steam keeps full control of input.")
    print("Press buttons on your controller. Ctrl-C to stop.\n")

    selector = selectors.DefaultSelector()
    watching = {}

    def refresh():
        present = set()
        for device in list_input_devices():
            present.add(device.path)
            if device.path in watching or not device.keys:
                continue
            try:
                fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            device.fd = fd
            watching[device.path] = device
            selector.register(fd, selectors.EVENT_READ, device)
            triggers = sorted(config.trigger_codes & device.keys)
            print("+ %-22s %s%s"
                  % (device.path, device.name,
                     "   [has trigger: %s]" % fmt_codes(triggers) if triggers else ""))
        for path in list(watching):
            if path not in present:
                device = watching.pop(path)
                try:
                    selector.unregister(device.fd)
                    os.close(device.fd)
                except (OSError, KeyError, ValueError):
                    pass
                print("- %-22s %s" % (path, device.name))

    refresh()
    if not watching:
        print("No readable input devices with key capabilities found.")
        return 1
    print()

    last_refresh = time.monotonic()
    try:
        while True:
            for key, _mask in selector.select(timeout=2.0):
                device = key.data
                try:
                    data = os.read(device.fd, EVENT_SIZE * 64)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    try:
                        selector.unregister(device.fd)
                        os.close(device.fd)
                    except (OSError, KeyError, ValueError):
                        pass
                    watching.pop(device.path, None)
                    continue
                for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _u, etype, code, value = struct.unpack_from(EVENT_FMT, data, offset)
                    if etype != EV_KEY:
                        continue
                    action = {0: "release", 1: "PRESS", 2: "repeat"}.get(value, str(value))
                    mark = "  <-- would trigger a wake" if (
                        value == 1 and code in config.trigger_codes) else ""
                    print("%s  %-22s %-28s %-16s %-7s code=%d%s"
                          % (time.strftime("%H:%M:%S"), device.path, device.name[:28],
                             code_name(code), action, code, mark))
            now = time.monotonic()
            if now - last_refresh >= 2.0:
                last_refresh = now
                refresh()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        for device in watching.values():
            try:
                os.close(device.fd)
            except OSError:
                pass
        selector.close()
    return 0


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cec-controller-watch",
        description="Wake the TV over HDMI-CEC when a controller's Home/Guide "
                    "button is pressed.",
        epilog="With no mode flag it runs as a daemon, which is what "
               "cec-hdmi-controller.service does.",
    )
    parser.add_argument("--detect", action="store_true",
                        help="list input devices and which would be watched, then exit")
    parser.add_argument("--monitor", action="store_true",
                        help="print key events live to find your Home button's code")
    parser.add_argument("--dry-run", action="store_true",
                        help="log 'would trigger' instead of running cec-hook.sh "
                             "(overrides DRY_RUN in the config)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, metavar="PATH",
                        help="config file to read (default: %(default)s)")
    parser.add_argument("--log", default=DEFAULT_LOG, metavar="PATH",
                        help="log file to append to (default: %(default)s)")
    parser.add_argument("--version", action="version", version="cec-controller-watch " + VERSION)
    args = parser.parse_args(argv)

    if args.detect and args.monitor:
        parser.error("--detect and --monitor are mutually exclusive")

    config = Config(args.config)
    if args.dry_run:
        config.dry_run = True

    if args.detect:
        return run_detect(config)
    if args.monitor:
        return run_monitor(config)

    log = Logger(args.log, max_bytes=config.log_max_bytes, keep=config.log_keep)
    try:
        return asyncio.run(ControllerWatcher(config, log).run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
    chmod +x "$WATCH_SCRIPT"

    echo "==> Writing $CONFIG_DEFAULTS (reference copy of shipped defaults)"
    write_config_defaults "$CONFIG_DEFAULTS"

    if [ -f "$CONFIG_FILE" ]; then
        echo "==> Keeping existing $CONFIG_FILE (your edits are safe)"
    else
        echo "==> Writing $CONFIG_FILE with defaults"
        write_config_defaults "$CONFIG_FILE"
    fi

    echo "==> Writing $POWER_UNIT"
    cat > "$POWER_UNIT" << 'POWER_EOF'
[Unit]
Description=HDMI-CEC: wake TV and switch to this PC on boot; standby on shutdown

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/cec-hdmi/cec-hook.sh on
ExecStop=/etc/cec-hdmi/cec-hook.sh off
TimeoutStartSec=150
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
POWER_EOF

    echo "==> Writing $SLEEP_UNIT"
    cat > "$SLEEP_UNIT" << 'SLEEP_EOF'
[Unit]
Description=HDMI-CEC: standby TV before system sleep/suspend
Before=sleep.target

[Service]
Type=oneshot
ExecStart=/etc/cec-hdmi/cec-hook.sh off
TimeoutStartSec=15

[Install]
WantedBy=sleep.target
SLEEP_EOF

    echo "==> Writing $RESUME_UNIT"
    cat > "$RESUME_UNIT" << 'RESUME_EOF'
[Unit]
Description=HDMI-CEC: wake TV after resuming from suspend/hibernate
After=systemd-suspend.service
After=systemd-hibernate.service
After=systemd-hybrid-sleep.service
After=systemd-suspend-then-hibernate.service

[Service]
Type=oneshot
ExecStart=/etc/cec-hdmi/cec-hook.sh on
TimeoutStartSec=150

[Install]
WantedBy=systemd-suspend.service
WantedBy=systemd-hibernate.service
WantedBy=systemd-hybrid-sleep.service
WantedBy=systemd-suspend-then-hibernate.service
RESUME_EOF

    echo "==> Writing $CTRL_UNIT"
    cat > "$CTRL_UNIT" << 'CTRL_EOF'
[Unit]
Description=HDMI-CEC: wake TV when a controller Home/Guide button is pressed
Documentation=file:/etc/cec-hdmi/config.conf
After=multi-user.target

[Service]
Type=simple
ExecStart=/etc/cec-hdmi/cec-controller-watch.py
Restart=on-failure
RestartSec=3
# Root, like every other unit here: /dev/input/event* is root:input 0660 and the
# wake path writes DPCD registers over /dev/drm_dp_auxN.
User=root

[Install]
WantedBy=multi-user.target
CTRL_EOF

    echo "$VERSION" > "$VERSION_FILE"

    echo "==> Reloading systemd"
    systemctl daemon-reload

    echo "==> Enabling cec-hdmi-power.service and running the wake sequence now"
    systemctl enable cec-hdmi-power.service
    systemctl restart cec-hdmi-power.service

    echo "==> Enabling cec-hdmi-sleep.service (fires before suspend, not run now)"
    systemctl enable cec-hdmi-sleep.service

    echo "==> Enabling cec-hdmi-resume.service (fires after resume, not run now)"
    systemctl enable cec-hdmi-resume.service

    echo "==> Enabling + starting cec-hdmi-controller.service (stays running)"
    systemctl enable cec-hdmi-controller.service
    systemctl restart cec-hdmi-controller.service

    echo
    echo "Installed v$VERSION:"
    echo "  $HOOK_SCRIPT"
    echo "  $WATCH_SCRIPT"
    echo "  $CONFIG_FILE"
    echo "  $POWER_UNIT"
    echo "  $SLEEP_UNIT"
    echo "  $RESUME_UNIT"
    echo "  $CTRL_UNIT"
    echo
    echo "Controllers seen right now:"
    "$WATCH_SCRIPT" --detect 2>&1 | sed -n '/^\/dev\/input/,$p' | sed 's/^/  /' || true
    echo
    echo "Logs:   $LOGFILE"
    echo "        $CTRL_LOGFILE"
    echo "Status: sudo bash $0 status"
    echo "Config: $CONFIG_FILE  (restart cec-hdmi-controller.service after editing)"
    echo "Undo:   sudo bash $0 uninstall"
}

uninstall_all() {
    echo "==> Stopping + disabling services"
    systemctl disable --now cec-hdmi-controller.service 2>/dev/null || true
    systemctl disable --now cec-hdmi-power.service 2>/dev/null || true
    systemctl disable cec-hdmi-sleep.service 2>/dev/null || true
    systemctl disable cec-hdmi-resume.service 2>/dev/null || true

    echo "==> Removing unit files"
    rm -f "$POWER_UNIT" "$SLEEP_UNIT" "$RESUME_UNIT" "$CTRL_UNIT"

    echo "==> Reloading systemd"
    systemctl daemon-reload
    systemctl reset-failed 2>/dev/null || true

    purge=""
    if [ "$ARG2" = "--purge" ]; then
        purge="y"
    elif [ -t 0 ]; then
        reply=""
        read -rp "Also remove $CEC_DIR (scripts, config, logs)? [y/N] " reply || true
        [[ "$reply" =~ ^[Yy]$ ]] && purge="y"
    fi

    if [ "$purge" = "y" ]; then
        rm -rf "$CEC_DIR"
        echo "Removed $CEC_DIR"
    else
        echo "Left $CEC_DIR in place (scripts, config and logs kept)"
        echo "  remove it with: sudo rm -rf $CEC_DIR"
    fi

    echo "Uninstalled."
}

status_all() {
    installed="not installed"
    [ -r "$VERSION_FILE" ] && installed="$(cat "$VERSION_FILE")"
    echo "cec-hdmi - install.sh $VERSION, installed $installed"
    echo

    echo "==> Services"
    for unit in $SERVICES; do
        enabled="$(systemctl is-enabled "$unit.service" 2>/dev/null || echo '-')"
        active="$(systemctl is-active "$unit.service" 2>/dev/null || echo '-')"
        printf '  %-30s %-10s %s\n' "$unit.service" "$enabled" "$active"
    done
    echo

    echo "==> DP->HDMI dongle CEC tunneling (DPCD 0x3001)"
    if [ -x "$HOOK_SCRIPT" ]; then
        "$HOOK_SCRIPT" dpcd-status 2>&1 | sed 's/^/  /' || true
    else
        echo "  $HOOK_SCRIPT not installed"
    fi
    echo

    echo "==> Controllers"
    if [ -x "$WATCH_SCRIPT" ]; then
        "$WATCH_SCRIPT" --detect 2>&1 | sed 's/^/  /' || true
    else
        echo "  $WATCH_SCRIPT not installed"
    fi
    echo

    for logpath in "$LOGFILE" "$CTRL_LOGFILE"; do
        echo "==> $logpath (last 10 lines)"
        if [ -r "$logpath" ]; then
            tail -n 10 "$logpath" 2>/dev/null | sed 's/^/  /' || true
        else
            echo "  (no log yet)"
        fi
        echo
    done
}

case "$ACTION" in
    install) install_all ;;
    uninstall) uninstall_all ;;
    status) status_all ;;
esac
