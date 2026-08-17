#!/usr/bin/env python3
"""cec-hook - put the TV on this PC, or send it to sleep.

    cec-hook.py on        wake the TV, claim its input, wake a receiver if present
    cec-hook.py off       send the TV (and the receiver) to standby
    cec-hook.py status    adapter, tunneling bit, and what is on the bus
    cec-hook.py scan      every device answering on the bus

Every code path that needs a wake goes through here. Nothing else in this
project speaks CEC, so the DisplayPort tunneling fix cannot be bypassed by
accident - see cec_dpcd for why that matters.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cec_config import CEC_DIR, Config, DEFAULT_CONFIG          # noqa: E402
from cec_control import Controller                              # noqa: E402
from cec_device import CecDevice, CecError                      # noqa: E402
from cec_dpcd import DpcdTunneling                              # noqa: E402
from cec_log import Logger                                      # noqa: E402

VERSION = "3.0.0"
DEFAULT_LOG = os.path.join(CEC_DIR, "cec-hook.log")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cec-hook",
        description="HDMI-CEC wake and standby, sent as frames.",
    )
    parser.add_argument("action", choices=("on", "off", "status", "scan"),
                        help="on: wake and claim the input. off: standby. "
                             "status: adapter and tunneling state. "
                             "scan: list devices on the bus.")
    parser.add_argument("--dry-run", action="store_true",
                        help="work out which frames are needed and print them "
                             "without sending anything")
    parser.add_argument("--config", default=DEFAULT_CONFIG, metavar="PATH",
                        help="config file (default: %(default)s)")
    parser.add_argument("--log", default=DEFAULT_LOG, metavar="PATH",
                        help="log file to append to (default: %(default)s)")
    parser.add_argument("--quiet", action="store_true",
                        help="log to the file only, not to stdout")
    parser.add_argument("--version", action="version", version="cec-hook " + VERSION)
    return parser


def run_status(config, log):
    """Read-only diagnostics, reported in layers.

    Each layer is printed before the next is attempted, so a failure half way
    down still leaves everything above it on screen. "The adapter exists, holds
    address 4, and tunneling is off" is a diagnosis; a single line saying the
    wake failed is not.
    """
    print("cec-hook %s" % VERSION)
    print("config:  %s" % config.path)
    for problem in config.problems:
        print("  note:  %s" % problem)
    print()

    if not os.path.exists(config.device):
        print("adapter: %s does not exist" % config.device)
        print("         the CEC adapter has not been created - check that the")
        print("         display is connected and that the kernel probed it")
        return 1

    device = None
    try:
        device = CecDevice(config.device).open()
        print("adapter: %s" % device.describe())

        info = device.log_addrs_info()
        if info["addr"] is None and info["count"]:
            print("logical: claiming in progress")
        elif info["addr"] is None:
            print("logical: unconfigured (no address claimed yet)")
        else:
            print("logical: %d, advertised as %r, CEC version 0x%02X"
                  % (info["addr"], info["osd_name"], info["version"]))

        tunneling = DpcdTunneling(device.name)
        print("DPCD:    %s" % tunneling.status()[1])
        if tunneling.connector:
            print("display: %s is %s" % (os.path.basename(tunneling.connector),
                                         tunneling.connector_status()))
    except CecError as exc:
        print("adapter: ERROR - %s" % exc)
        if device is not None:
            device.close()
        return 1
    finally:
        if device is not None:
            device.close()

    print()
    try:
        with Controller(config, log) as controller:
            state = controller.survey()
            print("bus:")
            print(state.describe())
            for note in state.notes:
                print("  note: %s" % note)
            print()
            plan = controller.plan_wake(state)
            if plan:
                print("a wake right now would send %d frame(s):" % len(plan))
                for planned in plan:
                    print("  %s" % planned.frame.describe())
                    print("      %s" % planned.reason)
            else:
                print("a wake right now would send nothing: "
                      "the TV is awake and already showing us.")
    except CecError as exc:
        print("bus:     ERROR - %s" % exc)
        return 1
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = Config(args.config)

    log = Logger(args.log, tag="cec-hook:%s" % args.action,
                 max_bytes=config.log_max_bytes, keep=config.log_keep,
                 echo=not args.quiet)

    if args.action == "status":
        return run_status(config, log)

    for problem in config.problems:
        log("config: %s" % problem)

    try:
        with Controller(config, log) as controller:
            if args.action == "on":
                return controller.wake(dry_run=args.dry_run)
            if args.action == "off":
                return controller.standby(dry_run=args.dry_run)
            if args.action == "scan":
                print(controller.scan())
                return 0
    except CecError as exc:
        log("ERROR: %s" % exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
