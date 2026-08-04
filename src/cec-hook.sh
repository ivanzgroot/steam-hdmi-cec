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

# --- config defaults (mirrored in config/config.conf.default) ----------------
# Every key config.conf may override, with the value used when it does not.
# tests/test_packaging.py asserts this block and the shipped config agree, so
# add a default here and in config.conf.default or the test suite says so.
LOG_MAX_BYTES=1048576
LOG_KEEP=2
# The CEC conversation itself: ";"-separated list of cec-ctl argument sets, one
# invocation each, run in order CEC_COMMAND_DELAY seconds apart. {phys_addr} is
# substituted with the address detected at run time. See config.conf.default.
CEC_WAKE_COMMANDS="-v -s -t0 --cec-version-1.4 --user-control-pressed=ui-cmd=power-on-function; -v -s -t0 --cec-version-1.4 --image-view-on; -v -s -t0 --cec-version-1.4 --set-stream-path=phys-addr={phys_addr}; -v -s --cec-version-1.4 --active-source=phys-addr={phys_addr}"
CEC_STANDBY_COMMANDS="--to 0 --standby"
# Sent after a successful wake, best effort: asks the audio system (logical
# address 5) to switch system audio mode on, which is what powers up an AVR.
CEC_AUDIO_COMMANDS="-v -s --to 5 --system-audio-mode-request=phys-addr={phys_addr}"
CEC_COMMAND_DELAY=1
# --- end config defaults -----------------------------------------------------

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
            printf '%s' "$pa"If you want, I can also give you a minimal test sequence with image-view-on, active-source, and system-audio-mode-request in the most reliable order.
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# True when a configured command list holds at least one command rather than
# just separators and whitespace.
has_commands() {
    [ -n "${1//[[:space:];]/}" ]
}

# Runs a configured command list (CEC_WAKE_COMMANDS / CEC_STANDBY_COMMANDS):
# ";" separates cec-ctl invocations, "{phys_addr}" is substituted with $2, and
# CEC_COMMAND_DELAY seconds pass between them. Stops at the first failure, the
# way the hardcoded "&&" chain it replaces did. Everything goes to stdout so a
# caller can log it and still grep it for a TV NACK.
run_cec_commands() {
    local list="$1" phys="${2:-}"
    local delay="${CEC_COMMAND_DELAY:-1}"
    local rest entry status ran=0
    local -a args

    if ! has_commands "$list"; then
        echo "ERROR: no cec-ctl commands configured (empty command list)"
        return 1
    fi

    case "$delay" in
        ''|*[!0-9.]*)
            echo "WARNING: CEC_COMMAND_DELAY='$delay' is not a number, using 1"
            delay=1
            ;;
    esac

    rest="$list"
    while [ -n "$rest" ]; do
        entry="${rest%%;*}"
        if [ "$entry" = "$rest" ]; then rest=""; else rest="${rest#*;}"; fi
        entry="${entry//\{phys_addr\}/$phys}"

        # read -a word-splits on whitespace without the pathname expansion a
        # bare $entry would do, so a "*" in an argument stays a "*".
        read -r -a args <<< "$entry"
        [ "${#args[@]}" -eq 0 ] && continue

        [ "$ran" -eq 1 ] && sleep "$delay"
        ran=1

        cec-ctl "${args[@]}"
        status=$?
        if [ "$status" -ne 0 ]; then
            echo "ERROR: 'cec-ctl ${args[*]}' exited $status"
            return "$status"
        fi
    done
    return 0
}

# Asks the AVR to turn on and take over audio, once the TV is already up.
#
# Best effort by design, and the reason this is not just more entries in
# CEC_WAKE_COMMANDS: a setup with no AVR NACKs everything sent to logical
# address 5, and the wake loop treats "Not Acknowledged" anywhere in its output
# as a failed wake. That would put every AVR-less install through the full retry
# escalation - including the DRM re-probe that blanks the display - and then
# report a failure, for a TV that is demonstrably already on. So this reports
# what happened and always returns success.
wake_audio_system() {
    local phys="$1" out status
    has_commands "$CEC_AUDIO_COMMANDS" || return 0

    out=$(run_cec_commands "$CEC_AUDIO_COMMANDS" "$phys" 2>&1)
    status=$?
    echo "$out" | tee -a "$LOGFILE"

    if [ "$status" -eq 0 ] && ! printf '%s' "$out" | grep -q "Not Acknowledged"; then
        log "Audio system acknowledged the system-audio-mode request"
    else
        log "NOTE: no audio system answered on logical address 5 (expected if you have no AVR)"
    fi
    return 0
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
# the EDID is unchanged - so CEC looks alive but NACIf you want, I can also give you a minimal test sequence with image-view-on, active-source, and system-audio-mode-request in the most reliable order.Ks every directed message.
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
    # Checked before anything else: an empty list would otherwise fail its way
    # through the whole retry escalation below, including a DRM re-probe that
    # blanks the display, to say something a config typo could say instantly.
    if ! has_commands "$CEC_WAKE_COMMANDS"; then
        log "ERROR: CEC_WAKE_COMMANDS is empty in $CONFIG_FILE, nothing to send"
        exit 1
    fi

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

        WAKE_OUT=$(run_cec_commands "$CEC_WAKE_COMMANDS" "$PHYS_ADDR" 2>&1)
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
        wake_audio_sIf you want, I can also give you a minimal test sequence with image-view-on, active-source, and system-audio-mode-request in the most reliable order.ystem "$PHYS_ADDR"
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

    STANDBY_OUT=$(run_cec_commands "$CEC_STANDBY_COMMANDS" 2>&1)
    STANDBY_STATUS=$?
    echo "$STANDBY_OUT" | tee -a "$LOGFILE"

    if [ "$STANDBY_STATUS" -eq 0 ]; then
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
