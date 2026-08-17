"""Look at the bus, work out the smallest set of frames that changes anything,
send those, and check they landed.

This is the part that replaces the old fixed list of cec-ctl invocations. That
design sent the same four messages every time, in the same order, whatever state
the room was in - so turning on a games console in front of an already-on TV
still fired a power-on keypress, a wake, a routing request and a source claim,
and then decided whether it had worked by searching a subprocess's output for
the words "Not Acknowledged".

Here, a wake asks three questions first:

    is the TV on?              <Give Device Power Status> -> <Report Power Status>
    who is on screen?          <Request Active Source>    -> <Active Source>
    is there a receiver?       a poll of logical address 5

and then sends only what the answers say is missing. If the TV is already awake
and already showing us, that is no frames at all. If it is awake but showing
something else, it is one frame. Every frame carries a plain-language reason,
which is what the log prints.
"""

import time

import cec_frames as frames
from cec_device import CecDevice, CecError, TX_NACK
from cec_dpcd import DpcdTunneling


class BusState:
    """What the bus looked like when we last asked."""

    __slots__ = ("our_phys", "our_addr", "tv_present", "tv_power",
                 "audio_present", "audio_mode", "active_source", "notes")

    def __init__(self):
        self.our_phys = None
        self.our_addr = None
        self.tv_present = False
        self.tv_power = None
        self.audio_present = False
        self.audio_mode = None
        self.active_source = None
        self.notes = []

    @property
    def tv_awake(self):
        """Both "on" and "waking up" count. A TV mid-transition does not need a
        second wake sent at it; it needs a moment."""
        return self.tv_power in (frames.POWER_ON, frames.POWER_TO_ON)

    @property
    def we_are_on_screen(self):
        return (self.active_source is not None
                and self.our_phys is not None
                and self.active_source == self.our_phys)

    def describe(self):
        lines = [
            "  us            %s  (logical address %s)"
            % (frames.format_phys_addr(self.our_phys), self.our_addr),
            "  TV            %s%s" % (
                "present" if self.tv_present else "no answer",
                "" if self.tv_power is None
                else ", %s" % frames.POWER_NAMES.get(self.tv_power,
                                                     "0x%02X" % self.tv_power)),
            "  on screen     %s" % (
                frames.format_phys_addr(self.active_source)
                + (" (us)" if self.we_are_on_screen else "")
                if self.active_source is not None else "nobody answered"),
            "  audio system  %s%s" % (
                "present" if self.audio_present else "none on this bus",
                "" if self.audio_mode is None
                else ", system audio %s" % ("on" if self.audio_mode else "off")),
        ]
        return "\n".join(lines)


class PlannedFrame:
    """A frame plus why the survey decided it was needed."""

    __slots__ = ("frame", "reason", "required")

    def __init__(self, frame, reason, required=True):
        self.frame = frame
        self.reason = reason
        # Broadcasts and best-effort extras must not fail a wake on their own.
        self.required = required


class Controller:
    """Owns the adapter for the lifetime of one hook invocation."""

    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.device = None
        self.dpcd = None

    # ----------------------------------------------------------------- setup

    def open(self):
        if not CecDevice.wait_for_device(self.config.device):
            raise CecError("%s never appeared" % self.config.device)

        self.device = CecDevice(self.config.device).open()
        self.dpcd = DpcdTunneling(self.device.name, log=self.log)

        if self.dpcd.available:
            self.log("DisplayPort adapter: %s (connector %s)"
                     % (self.dpcd.aux_path, self.device.name))
        else:
            self.log("no DisplayPort AUX device; treating this as a direct "
                     "HDMI adapter (no tunneling bit to manage)")

        self._claim_address()
        return self

    def close(self):
        if self.device is not None:
            self.device.close()
            self.device = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
        return False

    def _claim_address(self):
        """Claim a logical address, then enable tunneling - in that order.

        Claiming resets the adapter, which on a DisplayPort dongle clears DPCD
        0x3001. Enabling tunneling first and claiming second gives you a wake
        that works from cold boot and NACKs after resume. See cec_dpcd.
        """
        self.device.configure(
            osd_name=self.config.osd_name,
            device_type=self.config.device_type,
            cec_version=self.config.cec_version,
            vendor_id=self.config.vendor_id or None,
        )
        self.dpcd.ensure_enabled()
        self.device.read_phys_addr()
        self.log("adapter ready: %s%s"
                 % (self.device.describe(),
                    " (kept the address it already held)"
                    if self.device.adopted_existing else " (claimed)"))

        if self.device.physical_addr is None:
            raise CecError("the adapter has no physical address - the display "
                           "is not connected or has not been probed")

    # ----------------------------------------------------------------- survey

    def survey(self, full=True):
        """Ask the bus what state it is in.

        With full=False only the cheap presence polls run. That is what the
        standby path uses: it is holding up a suspend, and it does not need to
        know whether the TV is on in order to tell it to go to sleep.
        """
        state = BusState()
        state.our_phys = self.device.physical_addr
        state.our_addr = self.device.logical_addr

        state.tv_present = self.device.poll(frames.LA_TV)
        if self.config.wake_audio or self.config.standby_audio:
            state.audio_present = self.device.poll(frames.LA_AUDIOSYSTEM)

        if not full:
            return state

        if state.tv_present:
            state.tv_power = self._ask_power(frames.LA_TV)
        else:
            # Not all TVs answer a poll while in standby. Absence here is a
            # hint, never a reason to skip the wake.
            state.notes.append("the TV did not answer a poll; it may be asleep")

        state.active_source = self._ask_active_source()

        if state.audio_present:
            state.audio_mode = self._ask_audio_mode()

        return state

    def _ask_power(self, address):
        result = self.device.transmit(
            frames.give_device_power_status(self.device.logical_addr, address),
            reply_timeout_ms=self.config.reply_timeout_ms)
        if result.reply is not None and result.reply.operands:
            return result.reply.operands[0]
        return None

    def _ask_active_source(self):
        """"Who is on screen?" is broadcast, and so is its answer, so the reply
        arrives as an ordinary incoming frame rather than on the transmit."""
        request = frames.request_active_source(self.device.logical_addr)
        self.device.transmit(request, reply_timeout_ms=0)
        answer = self.device.wait_for(frames.OP_ACTIVE_SOURCE,
                                      timeout_ms=self.config.reply_timeout_ms)
        if answer is not None and len(answer.operands) >= 2:
            return (answer.operands[0] << 8) | answer.operands[1]
        return None

    def _ask_audio_mode(self):
        result = self.device.transmit(
            frames.give_system_audio_mode_status(self.device.logical_addr),
            reply_timeout_ms=self.config.reply_timeout_ms)
        if result.reply is not None and result.reply.operands:
            return bool(result.reply.operands[0])
        return None

    # ----------------------------------------------------------------- planning

    def plan_wake(self, state):
        """The smallest set of frames that gets us on screen, with sound."""
        config = self.config
        addr, phys = state.our_addr, state.our_phys
        force = config.force_all_frames
        plan = []

        need_tv = config.wake_tv and (force or not state.tv_awake)
        if need_tv:
            if config.send_remote_power_key:
                plan.append(PlannedFrame(
                    frames.user_control_pressed(addr, ui=frames.UI_POWER_ON),
                    "the TV is asleep and this one wants a remote keypress"))
                plan.append(PlannedFrame(
                    frames.user_control_released(addr),
                    "release the key, as a real remote would"))
            if state.tv_awake:
                reason = "FORCE_ALL_FRAMES is on, so the wake goes out regardless"
            elif state.tv_power is None:
                reason = "the TV is not reporting its power state"
            else:
                reason = "the TV is %s" % frames.POWER_NAMES.get(
                    state.tv_power, "0x%02X" % state.tv_power)
            plan.append(PlannedFrame(frames.image_view_on(addr), reason))
        elif config.wake_tv:
            state.notes.append("the TV is already awake; no wake frame needed")

        # If we just woke the TV, whatever it thought was on screen is stale, so
        # the claim is needed regardless of what the survey found.
        need_source = config.claim_source and (
            force or need_tv or not state.we_are_on_screen)
        if need_source:
            if config.send_stream_path:
                plan.append(PlannedFrame(
                    frames.set_stream_path(addr, phys),
                    "spell out the routing for TVs that want it",
                    required=False))
            if state.we_are_on_screen:
                reason = ("the TV was just woken, so what it last reported as "
                          "on screen is stale")
            elif state.active_source is None:
                reason = "nothing on the bus claimed to be on screen"
            else:
                reason = ("the TV is showing %s"
                          % frames.format_phys_addr(state.active_source))
            plan.append(PlannedFrame(frames.active_source(addr, phys), reason,
                                     required=False))
        elif config.claim_source:
            state.notes.append("we are already the active source; no claim needed")

        # A receiver only gets asked if one actually answered a poll. This is
        # the whole reason discovery exists: on a bus with no receiver, address
        # 5 never answers, and the old design's unconditional request to it was
        # a guaranteed NACK on every single wake.
        if config.wake_audio and state.audio_present:
            need_audio = force or need_source or state.audio_mode is not True
            if need_audio:
                plan.append(PlannedFrame(
                    frames.system_audio_mode_request(addr, phys),
                    "wake the receiver and point it at this input"))
            else:
                state.notes.append("the receiver already has system audio on us")
        elif config.wake_audio:
            state.notes.append("no receiver on this bus; skipping audio entirely")

        plan.extend(self._extra(config.extra_wake_frames, addr, phys,
                                "extra frame from EXTRA_WAKE_FRAMES"))
        return plan

    def plan_standby(self, state):
        """Standby is deliberately dumber than wake.

        It runs Before=sleep.target with a fifteen second budget, so it asks
        nothing it does not have to. Telling an already-sleeping TV to sleep
        costs one frame and harms nothing; finding out whether it needed telling
        would cost a query and a reply timeout on the suspend path.
        """
        config = self.config
        addr, phys = state.our_addr, state.our_phys
        plan = []

        if config.standby_broadcast:
            plan.append(PlannedFrame(
                frames.standby(addr, frames.LA_BROADCAST),
                "STANDBY_BROADCAST=1: send everything on the bus to sleep",
                required=False))
        else:
            if config.standby_tv:
                plan.append(PlannedFrame(
                    frames.standby(addr, frames.LA_TV), "put the TV to sleep"))
            if config.standby_audio and state.audio_present:
                plan.append(PlannedFrame(
                    frames.standby(addr, frames.LA_AUDIOSYSTEM),
                    "put the receiver to sleep too"))

        plan.extend(self._extra(config.extra_standby_frames, addr, phys,
                                "extra frame from EXTRA_STANDBY_FRAMES"))
        return plan

    def _extra(self, spec, addr, phys, reason):
        if not spec.strip():
            return []
        try:
            return [PlannedFrame(frame, reason, required=False)
                    for frame in frames.parse_frame_list(spec, addr, phys)]
        except ValueError as exc:
            self.log("WARNING: ignoring bad extra frames (%s)" % exc)
            return []

    # ----------------------------------------------------------------- sending

    def send(self, plan):
        """Send a plan in order. Returns True when everything required landed."""
        ok = True
        for index, planned in enumerate(plan):
            if index:
                time.sleep(self.config.frame_gap_ms / 1000.0)

            frame = planned.frame
            result = self.device.transmit(
                frame, reply_timeout_ms=self.config.reply_timeout_ms)

            self.log("  -> %s" % frame.describe())
            self.log("     why: %s" % planned.reason)

            if result.ok:
                continue

            # A broadcast is not acknowledged by anyone in the normal case, so
            # the kernel reporting a NACK for one means a device actively
            # rejected it - worth a line, never worth failing a wake over.
            if frame.is_broadcast and result.status & TX_NACK:
                self.log("     (broadcast rejected by a device; continuing)")
                continue

            self.log("     FAILED: %s" % result.why())
            if planned.required:
                ok = False
                break
        return ok

    def show_plan(self, plan, header):
        self.log(header)
        if not plan:
            self.log("  (nothing to send)")
            return
        for planned in plan:
            self.log("  %s" % planned.frame.describe())
            self.log("      %s" % planned.reason)

    # ----------------------------------------------------------------- actions

    def wake(self, dry_run=False):
        """Bring the TV up and put us on screen, doing as little as possible."""
        attempts = self.config.wake_attempts
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._recover(attempt)

            # Re-applied on every attempt, and always after _recover, because
            # re-claiming an address clears the tunneling bit again.
            self.dpcd.ensure_enabled()

            state = self.survey()
            self.log("bus state:\n%s" % state.describe())
            for note in state.notes:
                self.log("  note: %s" % note)

            plan = self.plan_wake(state)

            if not plan:
                self.log("nothing to do: the TV is awake and already showing us")
                return 0

            if dry_run:
                self.show_plan(plan, "DRY RUN - would send %d frame(s):" % len(plan))
                return 0

            self.log("sending %d frame(s), attempt %d/%d"
                     % (len(plan), attempt, attempts))
            if self.send(plan):
                self._confirm(state)
                return 0

            self.log("WARNING: attempt %d/%d did not get through" % (attempt, attempts))

        self.log("ERROR: the TV never acknowledged after %d attempts" % attempts)
        return 1

    def _confirm(self, state):
        """Report what actually happened. Never downgrades a success.

        Frames that the TV acknowledged are the real evidence a wake worked; a
        TV that declines to report its power status afterwards is being terse,
        not broken, and must not turn an acknowledged wake into a failure.
        """
        if self.config.wake_settle_ms:
            time.sleep(self.config.wake_settle_ms / 1000.0)
        power = self._ask_power(frames.LA_TV)
        if power is None:
            self.log("wake sent and acknowledged (the TV did not report a power state)")
        else:
            self.log("wake sent and acknowledged; the TV reports %s"
                     % frames.POWER_NAMES.get(power, "0x%02X" % power))

    def standby(self, dry_run=False):
        state = self.survey(full=False)
        plan = self.plan_standby(state)

        if not plan:
            self.log("nothing to do: standby is disabled for every device")
            return 0

        if dry_run:
            self.show_plan(plan, "DRY RUN - would send %d frame(s):" % len(plan))
            return 0

        if self.send(plan):
            self.log("standby sent")
            return 0
        self.log("ERROR: standby was not acknowledged")
        return 1

    def _recover(self, attempt):
        """Escalating repair between wake attempts.

        Attempts 2 and 3 re-claim the logical address, which rebuilds the
        adapter's CEC state cheaply. From attempt 4 the connector is forced to
        re-probe - the software cable reseat - which is heavy enough to blank
        the display briefly, so it is last.
        """
        if attempt <= 3:
            self.log("re-claiming the logical address and retrying")
            try:
                self.device.clear_log_addrs()
                time.sleep(0.5)
                self._claim_address()
            except CecError as exc:
                self.log("WARNING: re-claim failed: %s" % exc)
            return

        self.log("escalating: forcing the display connector to re-probe")
        self.dpcd.reprobe_connector()
        time.sleep(3.0)
        try:
            self._claim_address()
        except CecError as exc:
            self.log("WARNING: could not re-claim after re-probe: %s" % exc)

    # ----------------------------------------------------------------- reporting

    def scan(self):
        """Everything on the bus, for `cec-hook scan`."""
        lines = ["devices answering on this bus:"]
        for address in range(16):
            if address == frames.LA_BROADCAST:
                continue
            if address == self.device.logical_addr:
                lines.append("  %2d  %-14s us" % (address, frames.la_name(address)))
                continue
            if not self.device.poll(address):
                continue

            detail = []
            power = self._ask_power(address)
            if power is not None:
                detail.append(frames.POWER_NAMES.get(power, "0x%02X" % power))
            name = self._ask_osd_name(address)
            if name:
                detail.append('"%s"' % name)
            lines.append("  %2d  %-14s %s"
                         % (address, frames.la_name(address), ", ".join(detail)))
        return "\n".join(lines)

    def _ask_osd_name(self, address):
        result = self.device.transmit(
            frames.give_osd_name(self.device.logical_addr, address),
            reply_timeout_ms=self.config.reply_timeout_ms)
        if result.reply is None:
            return ""
        return bytes(result.reply.operands).decode("utf-8", "replace").strip("\0")
