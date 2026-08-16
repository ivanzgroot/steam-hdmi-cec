#!/usr/bin/env bash
# install.sh - HDMI-CEC TV control for SteamOS, sent as CEC frames.
#
# Installs, from the files next to this script:
#   src/*.py                    -> /etc/cec-hdmi/
#   config/config.conf.default  -> /etc/cec-hdmi/config.conf (first install only)
#   systemd/*.service           -> /etc/systemd/system/
#
# Usage:
#   sudo bash install.sh                     install (safe to re-run / update)
#   sudo bash install.sh uninstall           remove everything this script installed
#   sudo bash install.sh uninstall --purge   ...and delete /etc/cec-hdmi too
#   sudo bash install.sh status              adapter, tunneling, bus, controllers, logs
#   bash install.sh selftest                 run the test suite (no root, no install)
#   bash install.sh --help                   usage
#   bash install.sh --version                version of this repo + what is installed

set -euo pipefail

# Resolve the repo root from this script's own location, so the installer works
# regardless of the caller's working directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

SRC_DIR="$SCRIPT_DIR/src"
UNIT_SRC_DIR="$SCRIPT_DIR/systemd"
CONFIG_SRC="$SCRIPT_DIR/config/config.conf.default"
VERSION_SRC="$SCRIPT_DIR/VERSION"
TESTS_DIR="$SCRIPT_DIR/tests"

CEC_DIR="/etc/cec-hdmi"
HOOK_SCRIPT="$CEC_DIR/cec-hook.py"
WATCH_SCRIPT="$CEC_DIR/cec-watch.py"
CONFIG_FILE="$CEC_DIR/config.conf"
CONFIG_DEFAULTS="$CEC_DIR/config.conf.default"
VERSION_FILE="$CEC_DIR/VERSION"
LOGFILE="$CEC_DIR/cec-hook.log"
CTRL_LOGFILE="$CEC_DIR/cec-controller.log"
UNIT_DIR="/etc/systemd/system"

SERVICES="cec-hdmi-power cec-hdmi-sleep cec-hdmi-resume cec-hdmi-controller"

# Entry points get the executable bit; the modules beside them are imported and
# do not.
ENTRY_POINTS="cec-hook.py cec-watch.py"

VERSION="$(cat "$VERSION_SRC" 2>/dev/null || echo "unknown")"

usage() {
    cat << USAGE_EOF
install.sh $VERSION - HDMI-CEC TV control for SteamOS

  sudo bash install.sh                    install or update (idempotent)
  sudo bash install.sh uninstall          remove services and unit files
  sudo bash install.sh uninstall --purge  also delete $CEC_DIR (scripts, config, logs)
  sudo bash install.sh status             adapter, tunneling, bus, controllers, logs
  bash install.sh selftest                run the test suite (no root, changes nothing)
  bash install.sh --help                  this text
  bash install.sh --version               repo version and installed version

What gets installed
  $HOOK_SCRIPT
      Surveys the CEC bus, works out the smallest set of frames that will get
      the TV on and showing this PC, and sends those. Includes the DisplayPort
      DPCD 0x3001 tunneling fix that post-suspend wakes depend on.
          sudo $HOOK_SCRIPT on
          sudo $HOOK_SCRIPT on --dry-run    decide, print, send nothing
          sudo $HOOK_SCRIPT off
          sudo $HOOK_SCRIPT status
          sudo $HOOK_SCRIPT scan            what else is on the bus

  $WATCH_SCRIPT
      Long-running watcher. Any connected controller's Home/Guide button runs
      "cec-hook.py on". Hotplug-aware, multi-controller, debounced. Standard
      library only - no evdev/pyudev/pip needed.
          sudo $WATCH_SCRIPT --detect    list devices, show which are watched
          sudo $WATCH_SCRIPT --monitor   print key events live (find your button)

  $CONFIG_FILE
      Which frames a wake and a standby may send, the adapter identity, timing,
      trigger button codes, dry-run, notifications, log caps.
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

# Everything this installer copies out. Checked up front so a partial or
# stripped-down copy of the repo fails with a clear message instead of a
# half-finished install. tests/test_packaging.py asserts this list against the
# contents of src/ and systemd/, so a new file that is never copied is caught
# before it ships rather than after.
PAYLOAD="
$SRC_DIR/cec_frames.py
$SRC_DIR/cec_device.py
$SRC_DIR/cec_dpcd.py
$SRC_DIR/cec_config.py
$SRC_DIR/cec_control.py
$SRC_DIR/cec_log.py
$SRC_DIR/cec-hook.py
$SRC_DIR/cec-watch.py
$CONFIG_SRC
$VERSION_SRC
$UNIT_SRC_DIR/cec-hdmi-power.service
$UNIT_SRC_DIR/cec-hdmi-sleep.service
$UNIT_SRC_DIR/cec-hdmi-resume.service
$UNIT_SRC_DIR/cec-hdmi-controller.service
"

check_payload() {
    missing=""
    for f in $PAYLOAD; do
        [ -f "$f" ] || missing="$missing  $f"$'\n'
    done
    if [ -n "$missing" ]; then
        echo "ERROR: this installer is missing files it needs to install:" >&2
        printf '%s' "$missing" >&2
        echo >&2
        echo "install.sh is not self-contained - it copies the files that live" >&2
        echo "alongside it. Run it from a full clone of the repository:" >&2
        echo >&2
        echo "  git clone https://github.com/ivanzgroot/steam-hdmi-cec.git" >&2
        echo "  cd steam-hdmi-cec" >&2
        echo "  sudo bash install.sh" >&2
        exit 1
    fi
}

ACTION="${1:-install}"
ARG2="${2:-}"

case "$ACTION" in
    -h|--help|help)
        usage
        exit 0
        ;;
    -V|--version|version)
        echo "repo:       $VERSION"
        if [ -r "$VERSION_FILE" ]; then
            echo "installed:  $(cat "$VERSION_FILE")"
        else
            echo "installed:  (nothing installed at $CEC_DIR)"
        fi
        exit 0
        ;;
    install|uninstall|status|selftest)
        ;;
    *)
        echo "Unknown action: $ACTION" >&2
        echo >&2
        usage >&2
        exit 2
        ;;
esac

# Needs no privileges, so check it before asking for a password - being told the
# files are missing is more useful than being told it after authenticating.
if [ "$ACTION" = "install" ]; then
    check_payload
fi

if [ "$ACTION" != "status" ] && [ "$ACTION" != "selftest" ] && [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo bash $0 $ACTION" >&2
    exit 1
fi

install_all() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: python3 not found. It ships with SteamOS - something is wrong." >&2
        exit 1
    fi

    # Nothing else is required. CEC is spoken directly to /dev/cec0 through the
    # kernel's ioctl interface, so there is no cec-ctl and no v4l-utils to
    # install; and the watcher reads /dev/input and a netlink socket with the
    # standard library, so there is no evdev, no pyudev, no pip and no venv.
    # That matters on SteamOS, where the root filesystem is read-only, there is
    # no compiler, and "pip install" is the single most likely thing to break a
    # one-command install.
    echo "==> Using system python3: $(python3 --version 2>&1) (no other dependencies)"

    echo "==> Installing into $CEC_DIR"
    install -d -m 0755 "$CEC_DIR"
    for f in "$SRC_DIR"/*.py; do
        name="$(basename "$f")"
        case " $ENTRY_POINTS " in
            *" $name "*) install -m 0755 "$f" "$CEC_DIR/$name" ;;
            *)           install -m 0644 "$f" "$CEC_DIR/$name" ;;
        esac
    done

    echo "==> Installing $CONFIG_DEFAULTS (reference copy of shipped defaults)"
    install -m 0644 "$CONFIG_SRC" "$CONFIG_DEFAULTS"

    if [ -f "$CONFIG_FILE" ]; then
        echo "==> Keeping existing $CONFIG_FILE (your edits are safe)"
    else
        echo "==> Installing $CONFIG_FILE with defaults"
        install -m 0644 "$CONFIG_SRC" "$CONFIG_FILE"
    fi

    echo "==> Installing systemd units into $UNIT_DIR"
    for unit in $SERVICES; do
        install -m 0644 "$UNIT_SRC_DIR/$unit.service" "$UNIT_DIR/$unit.service"
    done

    install -m 0644 "$VERSION_SRC" "$VERSION_FILE"

    echo "==> Reloading systemd"
    systemctl daemon-reload

    echo "==> Enabling cec-hdmi-power.service and running a wake now"
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
    for unit in $SERVICES; do
        echo "  $UNIT_DIR/$unit.service"
    done
    echo
    echo "Devices on the CEC bus:"
    "$HOOK_SCRIPT" scan 2>&1 | sed 's/^/  /' || true
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
    for unit in $SERVICES; do
        rm -f "$UNIT_DIR/$unit.service"
    done

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
    echo "cec-hdmi - repo $VERSION, installed $installed"
    echo

    echo "==> Services"
    for unit in $SERVICES; do
        enabled="$(systemctl is-enabled "$unit.service" 2>/dev/null || echo '-')"
        active="$(systemctl is-active "$unit.service" 2>/dev/null || echo '-')"
        printf '  %-30s %-10s %s\n' "$unit.service" "$enabled" "$active"
    done
    echo

    echo "==> Adapter, tunneling and bus"
    if [ -x "$HOOK_SCRIPT" ]; then
        "$HOOK_SCRIPT" status 2>&1 | sed 's/^/  /' || true
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

selftest() {
    if [ ! -f "$TESTS_DIR/run.sh" ]; then
        echo "ERROR: $TESTS_DIR/run.sh not found (run from a full clone)" >&2
        exit 1
    fi
    bash "$TESTS_DIR/run.sh"
}

case "$ACTION" in
    install) install_all ;;
    uninstall) uninstall_all ;;
    status) status_all ;;
    selftest) selftest ;;
esac
