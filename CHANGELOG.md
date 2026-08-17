# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-08-17

First stable release.

### Added

- **Wake on Home button.** Any connected controller's Home/Guide button wakes
  the TV and claims its input. Multi-controller, hotplug-aware via the kernel
  netlink uevent socket, and debounced across devices so a pad mirrored onto a
  virtual node triggers once.
- **Wake on boot and resume**, and **standby on suspend, reboot and shutdown**,
  through four systemd units.
- **Receiver support.** An AV receiver answering on the bus is woken and switched
  to this input with a single `<System Audio Mode Request>`. A bus with no
  receiver has nothing addressed to it.
- **State-aware messaging.** Every action begins by polling for the TV and the
  receiver, asking the TV's power status and asking which physical address is on
  screen. Only the messages those answers show to be missing are sent — an awake
  TV already displaying this PC receives none.
- **The DisplayPort AUX fix.** DPCD register `0x3001`
  (`DP_CEC_TUNNELING_CONTROL`) is repaired before every wake attempt, and always
  after any logical-address change. This is what makes resume work on
  adapter-based setups, where the CEC controller lives in the DisplayPort→HDMI
  dongle and loses its state when the machine suspends.
- **Escalating recovery.** Attempts two and three re-claim the logical address;
  attempt four and beyond force the display connector to re-probe.
- **Direct CEC.** Messages are built as raw frames and sent to `/dev/cec0`
  through the kernel's ioctl interface, with delivery reported per message as
  acknowledged, refused, arbitration-lost, low-drive or line error.
- **`cec-hook.py`** with `on`, `off`, `status` and `scan`, plus `--dry-run` to
  see which messages a wake would send without sending them.
- **`cec-watch.py`** with `--detect` and `--monitor` for confirming that
  controller input reaches the daemon.
- **Configuration** in `/etc/cec-hdmi/config.conf`: adapter identity, which
  messages a wake and a standby may send, timing, retry limits, trigger button
  codes, notifications and log caps. Parsed, never executed.
- **Extra messages** by symbolic name or raw bytes, for hardware wanting
  something outside the standard set — vendor-specific commands included.
- **Readable logs**, recording each message with its bytes, its meaning and the
  reason it was sent, size-capped and mirrored to the journal.
- **Test suite** of nine suites and 449 checks, covering message bytes, the
  kernel ABI, the decision logic, the tunneling ordering rule, configuration
  parsing and the input layer. Runs without root, without an install and without
  CEC hardware, on any platform with python3.

### Requirements

- System `python3` and nothing else. No pip, no venv, no compiler, no
  `v4l-utils`.
