# steam-hdmi-cec

HDMI-CEC TV control for a SteamOS box connected to a TV through a UGREEN
DisplayPort→HDMI adapter (CEC-Tunneling-over-AUX).

The TV turns on and switches to this PC when the machine boots, resumes from
suspend, or **when you press Home/Guide on any connected controller**. It goes
to standby when the machine suspends or shuts down.

## Install

```sh
git clone https://github.com/ivanzgroot/steam-hdmi-cec.git
cd steam-hdmi-cec
sudo bash install.sh
```

That is the whole flow. The installer writes every file, creates the systemd
units, and enables and starts the services. It asks for nothing beyond the sudo
password, and it is idempotent — after a `git pull`, run it again to update in
place. Your `config.conf` is never overwritten.

Requires `cec-ctl` (package `v4l-utils`) and `python3`. Nothing is installed
with pip and there is no venv: the controller watcher uses only the Python
standard library, so it works on SteamOS's read-only root with no compiler.

## Before you trust the Home button: check Steam is not eating it

Steam owns controller input on SteamOS and may consume or remap the Guide/Home
button for its own overlay. **Verify on your hardware before relying on this**:

```sh
sudo /etc/cec-hdmi/cec-controller-watch.py --monitor
```

Press Home on your controller. If you see a line for it, raw evdev can see the
button and everything here works:

```
14:22:31  /dev/input/event3  Microsoft X-Box 360 pad  BTN_MODE  PRESS  code=316  <-- would trigger a wake
```

Run it in Desktop Mode **and** with Gaming Mode on screen. SSH in for the Gaming
Mode test — `/dev/input` does not care which UI is in front, so you do not need
to be sitting at the TV.

`--monitor` is a passive reader. It never calls `EVIOCGRAB`, so it does not
steal input from Steam, games, or the overlay while it runs.

If **no** event appears in Gaming Mode, Steam is consuming the button before
evdev and this trigger cannot work as built — see
[Troubleshooting](#the-home-button-does-nothing).

## Commands

```sh
sudo bash install.sh                    # install or update
sudo bash install.sh status             # services, DPCD state, controllers, recent logs
sudo bash install.sh uninstall          # remove services and units
sudo bash install.sh uninstall --purge  # ...and delete /etc/cec-hdmi entirely
bash install.sh --help                  # full usage
bash install.sh --version               # script version + installed version
```

Test the CEC path by hand at any time:

```sh
sudo bash /etc/cec-hdmi/cec-hook.sh on           # wake TV, claim the HDMI input
sudo bash /etc/cec-hdmi/cec-hook.sh off          # standby
sudo bash /etc/cec-hdmi/cec-hook.sh dpcd-status  # is CEC tunneling enabled?
```

Inspect controller detection:

```sh
sudo /etc/cec-hdmi/cec-controller-watch.py --detect    # what is watched, and why
sudo /etc/cec-hdmi/cec-controller-watch.py --monitor   # live key events
sudo /etc/cec-hdmi/cec-controller-watch.py --dry-run   # run without touching the TV
```

## What gets installed

Everything lives under `/etc`, because SteamOS's root filesystem is read-only
(`steamos-readonly`) and `/etc` is the writable, update-persistent exception.

| Path | Purpose |
| --- | --- |
| `/etc/cec-hdmi/cec-hook.sh` | All CEC logic. `on` wakes the TV and makes this PC the active source; `off` sends standby. Both idempotent. |
| `/etc/cec-hdmi/cec-controller-watch.py` | Long-running controller watcher. Shells out to `cec-hook.sh on`. |
| `/etc/cec-hdmi/config.conf` | Tunables. Written once, never overwritten. |
| `/etc/cec-hdmi/config.conf.default` | Current shipped defaults, rewritten every install so you can diff after an upgrade. |
| `/etc/cec-hdmi/cec-hook.log` | CEC wake/standby log. |
| `/etc/cec-hdmi/cec-controller.log` | Controller watcher log. |

Services:

| Unit | Fires |
| --- | --- |
| `cec-hdmi-power.service` | boot → wake TV; shutdown/reboot → standby |
| `cec-hdmi-sleep.service` | before suspend → standby |
| `cec-hdmi-resume.service` | after resume → wake TV |
| `cec-hdmi-controller.service` | always running; controller Home button → wake TV |

## Configuration

Edit `/etc/cec-hdmi/config.conf`, then:

```sh
sudo systemctl restart cec-hdmi-controller.service
```

Plain `KEY=VALUE`, sourceable by bash and parsed by the daemon without any
dependencies.

| Key | Default | Meaning |
| --- | --- | --- |
| `COOLDOWN_SECONDS` | `2.5` | Ignore further Home presses for this long after one fires. Fractions allowed. |
| `BUTTON_CODES` | `"BTN_MODE BTN_HOME KEY_HOMEPAGE"` | Which codes count as Home. Names or numbers, space- or comma-separated. |
| `GAMEPAD_ONLY` | `0` | `1` = only watch devices that advertise `BTN_GAMEPAD`. |
| `DRY_RUN` | `0` | `1` = log "would trigger" instead of calling `cec-hook.sh`. |
| `RESCAN_SECONDS` | `5` | Safety-net rescan for hotplug; the netlink socket normally makes this instant. |
| `NOTIFY_ON_TRIGGER` | `0` | Desktop notification after a successful wake. |
| `NOTIFY_ON_FAILURE` | `1` | Desktop notification when a wake fails. |
| `LOG_MAX_BYTES` | `1048576` | Rotate each log at this size. `0` disables rotation. |
| `LOG_KEEP` | `2` | How many rotated logs to keep. |

### Changing the trigger button

Run `--monitor`, press your button, note the `code=` number, and put either the
name or the number in `BUTTON_CODES`:

```sh
BUTTON_CODES="BTN_MODE 316 KEY_HOMEPAGE"
```

A device is watched if it advertises at least one of these codes, so listing
extra codes costs nothing.

> **On `BTN_HOME`:** mainline Linux `input-event-codes.h` does not define it —
> `BTN_MODE` (316) is what essentially every gamepad reports for Guide/Home, and
> `KEY_HOMEPAGE` (172) is what some Bluetooth pads emit on a separate
> keyboard-like node. `BTN_HOME` is kept in the default list for kernels or
> controllers that do define it; when they do not, it is ignored with a one-line
> note in the log and in `--detect`, and nothing else changes.

## How it behaves

- **All controllers.** Any connected device advertising a trigger code works,
  not one hardcoded device.
- **Hotplug.** Controllers plugged in or removed while the service runs are
  picked up and dropped live via the kernel netlink uevent socket, backed up by
  a periodic rescan. No restart needed.
- **Debounced.** After a trigger, further presses are ignored for
  `COOLDOWN_SECONDS`. This also absorbs the duplicate events you get when Steam
  mirrors a physical pad onto a virtual uinput device.
- **Non-blocking.** `cec-hook.sh` runs as a subprocess while the input loop keeps
  reading. A wake can take tens of seconds when the retry escalation kicks in.
- **One wake at a time.** While a wake is in flight, further presses are logged
  and ignored rather than starting a second concurrent `cec-ctl` sequence.

## Logs

```sh
tail -f /etc/cec-hdmi/cec-controller.log
tail -f /etc/cec-hdmi/cec-hook.log
journalctl -u cec-hdmi-controller.service -f
```

Both logs are size-capped (see `LOG_MAX_BYTES` / `LOG_KEEP`) and also go to the
journal.

## The suspend/resume fix

This is the non-obvious part of the project, and the reason nothing else should
ever reimplement CEC sending.

The CEC controller lives inside the DP→HDMI dongle and is driven over the
DisplayPort AUX channel. On suspend the dongle loses power and its CEC block
resets, clearing DPCD register `0x3001` (`DP_CEC_TUNNELING_CONTROL`). The kernel
never rewrites it, because the EDID has not changed and it sees no reason to
reconfigure the adapter.

The result is a failure that looks like working hardware: the physical address
still reads back fine from `cec-ctl -s -x`, but every directed CEC message is
NACKed. `cec-hook.sh` reads `0x3001` over `/dev/drm_dp_auxN` before each wake
attempt and rewrites it to `0x01` if it is clear — the software equivalent of
reseating the HDMI cable.

Check it any time with `sudo bash /etc/cec-hdmi/cec-hook.sh dpcd-status`.

**Any new code path that needs a wake must run `cec-hook.sh on`.** Do not
duplicate the CEC sequence elsewhere; it exists in exactly one place so this fix
cannot be lost.

## Troubleshooting

### The Home button does nothing

1. `sudo bash install.sh status` — is `cec-hdmi-controller.service` active?
2. `sudo /etc/cec-hdmi/cec-controller-watch.py --detect` — is your controller
   listed as `WATCHED`? If it says `WOULD FAIL - cannot open`, the service is
   not running as root.
3. `sudo /etc/cec-hdmi/cec-controller-watch.py --monitor` — press Home. No line
   at all means Steam is consuming the button before evdev sees it; a line with
   an unexpected code means you need to add that code to `BUTTON_CODES`.
4. `tail -f /etc/cec-hdmi/cec-controller.log` while pressing — a `cooldown` or
   `already in progress` line means detection works and the debounce is doing
   its job.

### The button is detected but the TV does not wake

Detection and CEC are separate. Test CEC on its own:

```sh
sudo bash /etc/cec-hdmi/cec-hook.sh on
sudo bash /etc/cec-hdmi/cec-hook.sh dpcd-status
tail -50 /etc/cec-hdmi/cec-hook.log
```

`Not Acknowledged` in the log with tunneling showing as enabled usually means
the TV's CEC receiver is still waking; the hook retries five times with
escalating re-initialisation.

### Testing without the TV

```sh
sudo systemctl stop cec-hdmi-controller.service
sudo /etc/cec-hdmi/cec-controller-watch.py --dry-run
```

Button presses are logged as `DRY RUN - would run` and the TV is never touched.

## Uninstall

```sh
sudo bash install.sh uninstall           # keeps /etc/cec-hdmi (config + logs)
sudo bash install.sh uninstall --purge   # removes it too
```
