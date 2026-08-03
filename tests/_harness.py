"""Shared scaffolding for the test suites.

Loads src/cec-controller-watch.py as a module. The daemon targets Linux, so a
couple of Unix-only attributes are stubbed to let these run anywhere python3
does - including a Windows dev box - which is the point: you can validate a
change before pushing it to the SteamOS machine.
"""

import importlib.util
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_SRC = os.path.join(REPO_ROOT, "src", "cec-controller-watch.py")
HOOK_SRC = os.path.join(REPO_ROOT, "src", "cec-hook.sh")
CONFIG_SRC = os.path.join(REPO_ROOT, "config", "config.conf.default")
UNIT_DIR = os.path.join(REPO_ROOT, "systemd")
INSTALLER = os.path.join(REPO_ROOT, "install.sh")
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")


# Loading the daemon by path would otherwise drop a __pycache__ into src/.
sys.dont_write_bytecode = True


def _stub_unix_only():
    if "pwd" not in sys.modules:
        stub = types.ModuleType("pwd")
        stub.getpwuid = lambda uid: None
        sys.modules["pwd"] = stub
    if not hasattr(os, "O_NONBLOCK"):
        os.O_NONBLOCK = 0
    if not hasattr(os, "geteuid"):
        os.geteuid = lambda: 0


def load_daemon():
    """Import src/cec-controller-watch.py (its name is not a valid identifier,
    so it has to go through importlib rather than a plain import)."""
    _stub_unix_only()
    spec = importlib.util.spec_from_file_location("cec_controller_watch", DAEMON_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_version():
    with open(VERSION_FILE) as fh:
        return fh.read().strip()


class Checks:
    """Tiny assertion recorder - keeps output readable without pulling in a
    test framework the SteamOS box would have to have installed."""

    def __init__(self):
        self.failures = []
        self.passed = 0

    def section(self, title):
        print("== %s ==" % title)

    def __call__(self, label, got, want):
        if got == want:
            self.passed += 1
            print("  ok   %s" % label)
        else:
            self.failures.append(label)
            print("  FAIL %s\n         got:  %r\n         want: %r" % (label, got, want))

    def finish(self):
        print()
        if self.failures:
            print("%d of %d checks FAILED: %s"
                  % (len(self.failures), len(self.failures) + self.passed,
                     ", ".join(self.failures)))
            return 1
        print("%d checks passed" % self.passed)
        return 0


def mask_for(bits, module):
    """Render bit numbers the way the kernel renders a capabilities bitmap:
    one unsigned long per word, most significant first, "%lx" so nothing is
    zero padded. Word size follows the module so the fixture stays honest on
    both LP64 and 32-bit hosts."""
    word_bits = module.LONG_BITS
    value = 0
    for bit in bits:
        value |= 1 << bit
    words = (max(bits) // word_bits + 1) if bits else 1
    return " ".join(
        "%x" % ((value >> (index * word_bits)) & ((1 << word_bits) - 1))
        for index in range(words - 1, -1, -1)
    )
