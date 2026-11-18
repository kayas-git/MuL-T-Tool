"""funcgen_window.py - the PC screen of the NUCLEO-G474RE function
generator (+ oscilloscope and multimeter, to watch what it makes).

The board makes two waves (generator CH1 on A2, CH2 on D12), measures and
sends packets (about 10 to 90 per second). This script only:
  1. finds the board's COM port,
  2. reads the packets (2 channels of samples + multimeter readings +
     the generator's settings),
  3. draws them: an oscilloscope screen, or big multimeter numbers, and
     the generator's settings with its real frequency,
  4. sends the chosen sample rate (one byte), a generator channel's
     settings ('G' + 12 bytes), a drawn wave shape ('W' + 102 bytes) and
     CH2's phase + coupling ('P' + 4 bytes) to the board.

Extras: K shows two time + two voltage cursors (drag them with the mouse);
press K again for "track" mode, where the voltage cursors follow the wave.
D lets you draw one period of a "custom" wave with the mouse.
Y chooses which generator channel the generator keys change.
Every key also has a clickable button in the right panel; a click simply
presses that key, so both ways do exactly the same thing.

INPUTS: 0 V to 3.3 V ONLY. Never connect mains, car or other batteries,
or anything negative. That can destroy the board and your PC's USB port.

Run:  py funcgen_window.py              (finds the board by itself)
      py funcgen_window.py --port COM4
      py funcgen_window.py --rref 4700   (known resistor for the Ω meter)
"""

import argparse
import array
import math
import struct

import pygame
import serial
from serial.tools import list_ports

BAUD = 4250000                # must match USART2 in the board's FuncGen.ioc
START = b"\xA5\x5A"           # every packet starts with these 2 bytes
HEADER = 54                   # bytes before the samples (see scope.c)
TIMER_HZ = 170_000_000        # TIM2 (frequency counter) and TIM7 count this fast

# The generator (generator.h). The PC uses the same limits as the board.
WAVES = ["sine", "square", "triangle", "sawtooth", "custom"]
CUSTOM = 4                    # the wave number of the drawn ("custom") wave
POINTS = 100                  # table points per period (GEN_POINTS)
# The board's custom shape until you draw one: 4 stairs (generator.c).
STAIRS = [(i // 25) * 85 for i in range(POINTS)]
GEN_MIN_HZ, GEN_MAX_HZ, GEN_MAX_V = 1, 10000, 3.3
GEN_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
GEN_COLOR = (120, 255, 140)
GEN_PINS = ["A2", "D12"]      # generator CH1 and CH2 outputs
GEN_KEYS = ("wave", "freq", "amp", "offset", "on")   # one channel's settings
CURSOR_COLOR = (230, 120, 255)

# The board's sample rates (RATES[] in scope.c). We send the index.
RATES = [1000, 2500, 5000, 10000, 25000, 50000,
         100000, 250000, 500000, 1000000, 2000000]
METER_RATE = 3                # 10 000 samples/s in multimeter mode
VOLTS_PER_DIV = [0.1, 0.2, 0.5, 1.0]

# Screen layout: 10 x 8 divisions of 80 x 60 pixels.
DIV_X, DIV_Y, DIVS_X, DIVS_Y = 80, 60, 10, 8
GX, GY = 20, 20                           # top-left corner of the grid
GW, GH = DIV_X * DIVS_X, DIV_Y * DIVS_Y
SHOWN = 500                               # samples across the screen
PRE_TRIGGER = 100                         # samples shown before the trigger (20 %)
AFTER_TRIGGER = SHOWN - PRE_TRIGGER       # samples shown from the trigger on
WIDTH, HEIGHT = GX + GW + 340, GY + GH + 210
PANEL_X = GX + GW + 25                    # the right-hand panel (buttons)

# On-screen buttons: 2 columns in the right-hand panel.
BUTTON_W, BUTTON_H, BUTTON_GAP = 150, 21, 4
BUTTON_COLOR, BUTTON_HOVER = (35, 40, 52), (60, 68, 88)

# Trigger modes, like a real oscilloscope (Tab switches between them):
#   Auto   - draw every capture; use the trigger when there is one.
#   Normal - draw only triggered captures; otherwise keep the last one.
#   Single - wait for ONE trigger, then freeze ("STOPPED"). S arms again.
TRIGGER_MODES = ["Auto", "Normal", "Single"]

BLACK, GRID, WHITE = (10, 12, 16), (45, 50, 60), (230, 230, 230)
GREY, RED = (140, 140, 150), (255, 80, 80)
CH_COLORS = [(255, 220, 0), (0, 220, 255)]   # CH1 yellow, CH2 cyan
WARNING = "INPUTS 0 - 3.3 V ONLY! No mains, no batteries above 3.3 V, nothing negative."


def find_port():
    """Return the COM port of the ST-LINK (USB vendor id 0x0483), or None."""
    for port in list_ports.comports():
        if port.vid == 0x0483:
            return port.device
    return None


def read_channel(buffer, offset, points):
    """One generator channel in the packet: 15 bytes (put_channel() in
    scope.c)."""
    wave, on, freq, psc, arr, amp_mv, offset_mv, shape = \
        struct.unpack_from("<BBIHHHHB", buffer, offset)
    return {"wave": wave, "on": on, "freq": freq, "amp": amp_mv / 1000,
            "offset": offset_mv / 1000, "psc": psc, "arr": arr,
            "points": points, "shape": shape}


def read_packets(buffer):
    """Take all complete, good packets out of `buffer` (removing the bytes).
    Each packet becomes a dict."""
    packets = []
    while True:
        start = buffer.find(START)
        if start < 0:
            del buffer[:-1]                 # keep a possible half start marker
            return packets
        if len(buffer) - start < HEADER:
            del buffer[:start]
            return packets                  # wait for the rest
        rate, n, vdda_mv, period, high, continuity, counter, \
            points, phase, coupled = \
            struct.unpack_from("<IHHIIBBBHB", buffer, start + 2)
        size = HEADER + 4 * n + 2
        if n > 5000:                        # nonsense: not a real packet
            del buffer[:start + 1]
            continue
        if len(buffer) - start < size:
            del buffer[:start]
            return packets                  # wait for the rest
        body = buffer[start + 2:start + size - 2]
        checksum = buffer[start + size - 2] | (buffer[start + size - 1] << 8)
        if sum(body) & 0xFFFF != checksum:
            del buffer[:start + 1]          # broken: look for the next start
            continue
        samples = array.array("H", buffer[start + HEADER:start + size - 2])
        volts = vdda_mv / 1000 / 4095       # one ADC step in volts
        packets.append({
            "rate": rate,
            "vdda": vdda_mv / 1000,
            "ch": [[s * volts for s in samples[:n]],
                   [s * volts for s in samples[n:]]],
            "freq": TIMER_HZ / period if period else 0.0,
            "duty": 100 * high / period if period else 0.0,
            "continuity": bool(continuity),
            "counter": counter,             # +1 for every packet the board sends
            # The generator: both channels, then CH2's phase and coupling.
            "gen": [read_channel(buffer, start + 24 + 15 * ch, points)
                    for ch in (0, 1)],
            "phase": phase, "coupled": coupled,
        })
        del buffer[:start + size]


def find_edge(signal, level, rising, first, last):
    """First index i (first <= i < last) where the signal crosses `level`
    between sample i-1 and sample i. None if there is no such crossing."""
    for i in range(max(first, 1), min(last, len(signal))):
        a, b = signal[i - 1], signal[i]
        if (rising and a < level <= b) or (not rising and a > level >= b):
            return i
    return None


def triggered_window(previous, packet, source, level, rising):
    """Find a trigger edge and cut SHOWN samples of both channels around it
    (PRE_TRIGGER before it, the rest after). Returns [ch1, ch2] or None.

    An edge near the END of a packet has too few samples after it, so it is
    skipped now. Next time it is near the START of the previous packet, and
    we glue the two packets together to show it (step 2). That is how a
    one-off edge, like touching a battery to A0, is never missed. (The board
    starts the next capture right after sending, so the gap at the joint is
    small: about 10 ms.)"""
    n = len(packet["ch"][0])
    # 1. An edge with enough samples on both sides inside this packet.
    i = find_edge(packet["ch"][source], level, rising, PRE_TRIGGER, n - AFTER_TRIGGER + 1)
    if i is not None:
        return [ch[i - PRE_TRIGGER:i + AFTER_TRIGGER] for ch in packet["ch"]]
    # 2. An edge near the joint: end of the previous packet, or the very
    #    start of this one (not enough samples before it on its own).
    if previous is not None:
        glued = [old + new for old, new in zip(previous["ch"], packet["ch"])]
        i = find_edge(glued[source], level, rising, n - AFTER_TRIGGER + 1, n + PRE_TRIGGER)
        if i is not None:
            return [ch[i - PRE_TRIGGER:i + AFTER_TRIGGER] for ch in glued]
    return None


def signal_frequency(signal, rate):
    """Frequency from the samples: count upward crossings of the middle."""
    low, high = min(signal), max(signal)
    if high - low < 0.1:
        return None                         # flat: no wave to measure
    middle, hysteresis = (low + high) / 2, (high - low) / 10
    ups, armed = [], False
    for i, v in enumerate(signal):
        if v < middle - hysteresis:
            armed = True
        elif armed and v > middle:
            ups.append(i)
            armed = False
    if len(ups) < 2:
        return None
    return rate * (len(ups) - 1) / (ups[-1] - ups[0])


def nice(value, unit):
    """1500 -> '1.50 k' + unit, 0.0002 -> '200 u' + unit."""
    for scale, prefix in ((1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "u")):
        if abs(value) >= scale:
            return f"{value / scale:.3g} {prefix}{unit}"
    return f"0 {unit}"


def resistance(volts, vdda, r_known):
    """Unknown resistor from a voltage divider (see README):
         3V3 ── r_known ──┬── unknown ── GND
                          A0 (= volts)
    The same current flows through both resistors, so
         unknown = r_known * volts / (vdda - volts).
    Returns (text, warning)."""
    if volts > 0.98 * vdda:
        return "open", "nothing between A0 and GND"
    if volts < 0.01 * vdda:
        return f"short (<{nice(r_known / 99, 'Ω')})", ""
    r = r_known * volts / (vdda - volts)
    # Best when both resistors are similar; far from that, 1 ADC step is a
    # big part of the answer.
    if not r_known / 10 <= r <= r_known * 10:
        return nice(r, "Ω"), f"less accurate (best {nice(r_known / 10, 'Ω')}"\
                              f" - {nice(r_known * 10, 'Ω')})"
    return nice(r, "Ω"), ""


def real_frequency(gen):
    """The frequency the board really makes (generator.c):
         170 MHz / (PSC + 1) / (ARR + 1) / table points
    Also returns the DAC's speed: values per second."""
    values_per_s = TIMER_HZ / ((gen["psc"] + 1) * (gen["arr"] + 1))
    return values_per_s / gen["points"], values_per_s


def generator_command(ch, g):
    """The 13 bytes that change one generator channel (see scope.c):
    'G', channel (0 = CH1, 1 = CH2), wave, frequency Hz (4 bytes),
    amplitude mV (2), offset mV (2), on (1),
    check = the 11 bytes after 'G' added up (low 8 bits)."""
    body = struct.pack("<BBIHHB", ch, g["wave"], g["freq"], round(g["amp"] * 1000),
                       round(g["offset"] * 1000), g["on"])
    return b"G" + body + bytes([sum(body) & 0xFF])


def shape_command(ch, shape):
    """The 103 bytes that send a drawn wave (see scope.c): 'W', channel,
    the 100 shape points (0 = bottom .. 255 = top), check = the 101 added up."""
    body = bytes([ch]) + bytes(shape)
    return b"W" + body + bytes([sum(body) & 0xFF])


def phase_command(phase, coupled):
    """The 5 bytes that set CH2's phase (degrees) and coupling: 'P',
    phase (2 bytes), coupled (1), check. The board restarts both channels
    together, so sending it again "aligns" them."""
    body = struct.pack("<HB", phase, coupled)
    return b"P" + body + bytes([sum(body) & 0xFF])


def real_phase(phase):
    """The phase the board really makes: CH2's table is turned by a whole
    number of points, phase x 100 / 360 rounded, and 1 point = 3.6 degrees."""
    shift = int(phase * POINTS / 360 + 0.5) % POINTS   # half up, like generator.c
    return shift * 360 / POINTS


def measurements(signal, rate):
    """Automatic measurements of the samples on screen, like a real scope:
    min, max, peak-to-peak, average, RMS and frequency."""
    low, high = min(signal), max(signal)
    return {"min": low, "max": high, "vpp": high - low,
            "avg": sum(signal) / len(signal),
            # RMS = root of the mean of the squares. It includes the DC part:
            # for a sine around 1.65 V it is NOT amplitude / 2.83.
            "rms": math.sqrt(sum(v * v for v in signal) / len(signal)),
            "freq": signal_frequency(signal, rate)}


def gated_peaks(signal, t1, t2):
    """Peak search between the two time cursors ("gated": only that part).
    Returns (first, index of the highest sample, index of the lowest), or
    None if there are no samples (screen cleared with C)."""
    if not signal:
        return None
    last = len(signal) - 1
    first, end = sorted(min(round(t), last) for t in (t1, t2))   # either order
    part = range(first, end + 1)        # t1 == t2: just that one sample
    return (first, max(part, key=lambda i: signal[i]),
            min(part, key=lambda i: signal[i]))


def make_beep():
    """A 1 kHz tone to loop while the continuity probe touches GND."""
    pygame.mixer.init(44100, -16, 1)
    tone = array.array("h", (int(8000 * math.sin(2 * math.pi * 1000 * i / 44100))
                             for i in range(4410)))          # 0.1 s, loops cleanly
    return pygame.mixer.Sound(buffer=tone.tobytes())


class Window:
    def __init__(self, link, r_known=10000):
        self.link = link
        self.r_known = r_known              # known resistor for the Ω meter
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption("NUCLEO function generator + scope + multimeter")
        self.font = pygame.font.SysFont("consolas", 16)
        self.big = pygame.font.SysFont("consolas", 56, bold=True)
        self.small = pygame.font.SysFont("consolas", 14)
        self.button_rects = []              # [(rectangle, key)] drawn last frame
        self.beep = make_beep()
        self.beeping = False
        self.meter_mode = False
        self.rate_index = 5                 # 50 000 samples/s = 1 ms/div
        self.vdiv_index = 2                 # 0.5 V/div
        self.trigger_level = 1.6
        self.trigger_rising = True
        self.trigger_source = 0             # CH1
        self.trigger_mode = "Auto"          # one of TRIGGER_MODES
        self.single_armed = True            # Single mode: still waiting?
        self.visible = [True, True]
        self.paused = False
        self.packet = None                  # newest packet (for the meter)
        self.previous = None                # the packet before it (for gluing)
        self.view = None                    # what the scope screen shows
        self.found_trigger = False          # did the newest packet trigger?
        self.packet_times = []
        # Generator settings we want: a list of 2 dicts (CH1, CH2). None
        # until the first packet tells us what the board is doing now.
        self.gen = None
        self.pair = None                    # {"phase": degrees, "coupled": 0/1}
        self.edit = 0                       # the channel the generator keys change
        self.gen_sent_ms = 0                # when we last sent them
        self.entry = None                   # typing a number: ("F", "440")
        # Each channel's custom shape (100 numbers, 0..255), and the shape
        # being drawn (a list of 100 numbers while in drawing mode, else None).
        self.custom = [list(STAIRS), list(STAIRS)]
        self.drawing = None
        self.last_drawn = None              # last point the mouse drew
        # Cursors (K): 2 time cursors, as a sample position on the screen
        # (0 .. SHOWN), and 2 voltage cursors, in volts.
        # K cycles the mode: "off" -> "manual" (drag all 4 lines) ->
        # "track" (drag t1/t2; V1/V2 = the trigger channel's voltage there).
        self.cursor_mode = "off"
        self.cursor_t = [100.0, 300.0]
        self.cursor_v = [0.5, 2.5]
        self.dragging = None                # ("t" or "v", 0 or 1) while dragging
        self.rate_sent_ms = 0
        self.send_rate()

    # ---- talking to the board ----
    def send_rate(self):
        index = METER_RATE if self.meter_mode else self.rate_index
        self.link.write(bytes([index]))
        self.rate_sent_ms = pygame.time.get_ticks()

    def send_generator(self, ch, with_shape=False):
        """Send one channel's settings ('G'). with_shape: first send its
        custom shape ('W'), so a following 'G' with the custom wave plays it."""
        g = self.gen[ch]
        if with_shape:
            self.link.write(shape_command(ch, self.custom[ch]))
            g["shape"] = sum(self.custom[ch]) & 0xFF
        self.link.write(generator_command(ch, g))
        self.gen_sent_ms = pygame.time.get_ticks()

    def send_phase(self):
        """Send CH2's phase and coupling ('P'); also restarts both together."""
        self.link.write(phase_command(self.pair["phase"], self.pair["coupled"]))
        self.gen_sent_ms = pygame.time.get_ticks()

    def check_generator(self, packet):
        """Take over the board's settings at the start. Later, if the board
        does not show ours (a lost command, or the board was reset), send
        them again, at most once a second. The shape only matters while the
        custom wave plays.
        Only ONE 'G' per check: the board has one buffer for 'G', so a
        second 'G' sent at once could overwrite the first before it is used.
        The other channel is fixed a second later."""
        if self.gen is None:
            self.gen = [{k: b[k] for k in GEN_KEYS + ("shape",)} for b in packet["gen"]]
            self.pair = {"phase": packet["phase"], "coupled": packet["coupled"]}
            return
        if pygame.time.get_ticks() - self.gen_sent_ms <= 1000:
            return
        for ch, board in enumerate(packet["gen"]):
            keys = list(GEN_KEYS)
            if self.gen[ch]["wave"] == CUSTOM:
                keys.append("shape")
            wrong = [k for k in keys if abs(self.gen[ch][k] - board[k]) > 0.0005]
            if wrong:
                self.send_generator(ch, with_shape="shape" in wrong)
                break
        if (packet["phase"], packet["coupled"]) != (self.pair["phase"], self.pair["coupled"]):
            self.send_phase()

    def change_generator(self, **changes):
        """Change the channel being edited (Y chooses it), and send it."""
        if self.gen is None:
            return                          # no packet yet: we don't know it
        g = self.gen[self.edit]
        g.update(changes)
        # With the custom wave, send its shape too (103 more bytes: cheap).
        self.send_generator(self.edit, with_shape=g["wave"] == CUSTOM)

    # ---- typing a number (F, A, O) ----
    ENTRY_TEXT = {"F": f"frequency in Hz ({GEN_MIN_HZ} .. {GEN_MAX_HZ})",
                  "A": f"amplitude in volts peak-to-peak (0 .. {GEN_MAX_V})",
                  "O": f"offset (middle level) in volts (0 .. {GEN_MAX_V})",
                  "P": "CH2 phase in degrees (0 .. 359)"}

    def entry_key(self, event):
        what, text = self.entry
        if event.key == pygame.K_ESCAPE:
            self.entry = None               # cancel
        elif event.key == pygame.K_BACKSPACE:
            self.entry = (what, text[:-1])
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.entry = None
            try:
                value = float(text)
            except ValueError:
                return                      # nothing (or nonsense) typed
            if what == "F":
                freq = round(min(max(value, GEN_MIN_HZ), GEN_MAX_HZ))
                self.change_generator(freq=freq)
            elif what == "P":                 # whole degrees, 0 .. 359
                self.pair["phase"] = round(value) % 360
                self.send_phase()
            elif what == "A":                 # (round to mV, like the board)
                self.change_generator(amp=round(min(max(value, 0.0), GEN_MAX_V), 3))
            else:
                self.change_generator(offset=round(min(max(value, 0.0), GEN_MAX_V), 3))
        elif event.unicode and event.unicode in "0123456789." and len(text) < 8:
            self.entry = (what, text + event.unicode)

    # ---- drawing a custom wave (D) ----
    def draw_key(self, event):
        """Keys while drawing: Enter = send it, Esc = cancel, C = flat."""
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.custom[self.edit] = [round(v) for v in self.drawing]
            self.drawing = None
            if self.gen is not None:        # play it: 'W' (shape) + 'G' (custom)
                self.gen[self.edit]["wave"] = CUSTOM
                self.send_generator(self.edit, with_shape=True)
        elif event.key == pygame.K_ESCAPE:
            self.drawing = None             # cancel: keep the old shape
        elif event.key == pygame.K_c:
            self.drawing = [127.5] * POINTS  # a flat line in the middle

    def draw_at(self, x, y):
        """The mouse is at (x, y) with the button down: set the shape there.
        Between the last point and this one we draw a straight line, so a
        fast mouse leaves no gaps. (This turns the mouse path into exactly
        100 points: the "resampling".)"""
        i = round((x - GX) / GW * (POINTS - 1))
        i = min(max(i, 0), POINTS - 1)
        value = (GY + GH - y) / GH * 255     # bottom of the grid 0, top 255
        value = min(max(value, 0.0), 255.0)
        j, old = self.last_drawn if self.last_drawn else (i, value)
        steps = abs(i - j)
        for s in range(steps + 1):
            k = j + (s if i > j else -s)
            t = s / steps if steps else 1.0
            self.drawing[k] = old + (value - old) * t
        self.last_drawn = (i, value)

    # ---- the mouse: drawing, or dragging a cursor ----
    def y_of(self, volts):                  # 0 V is the bottom line
        return GY + GH - volts / VOLTS_PER_DIV[self.vdiv_index] * DIV_Y

    def mouse(self, event):
        if event.type == pygame.MOUSEBUTTONUP:
            self.dragging = self.last_drawn = None
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button != 1:
            return                          # left button only
        if event.type == pygame.MOUSEMOTION and not event.buttons[0]:
            return                          # moving without the button
        x, y = event.pos
        # A click on a button: press its key, and nothing else (no drawing,
        # no cursor grab). Only a new click counts, so dragging a cursor
        # over the buttons does not press them.
        if event.type == pygame.MOUSEBUTTONDOWN and self.click_button(x, y):
            return
        if self.drawing is not None:
            self.draw_at(x, y)
            return
        if self.cursor_mode == "off" or self.meter_mode:
            return
        if event.type == pygame.MOUSEBUTTONDOWN:
            # Grab the cursor line nearest to the mouse (within 10 pixels).
            near = [(abs(x - (GX + t * GW / SHOWN)), ("t", n))
                    for n, t in enumerate(self.cursor_t)]
            if self.cursor_mode == "manual":    # in track mode V1/V2 move by themselves
                near += [(abs(y - self.y_of(v)), ("v", n))
                         for n, v in enumerate(self.cursor_v)]
            distance, which = min(near)
            self.dragging = which if distance <= 10 else None
        if self.dragging:
            kind, n = self.dragging
            if kind == "t":                 # pixels -> sample position
                self.cursor_t[n] = min(max((x - GX) * SHOWN / GW, 0), SHOWN)
            else:                           # pixels -> volts
                top = VOLTS_PER_DIV[self.vdiv_index] * DIVS_Y
                volts = (GY + GH - y) / DIV_Y * VOLTS_PER_DIV[self.vdiv_index]
                self.cursor_v[n] = min(max(volts, 0.0), top)

    # ---- on-screen buttons ----
    # Every button is just a key: a click "presses" that key (press_key),
    # so a button always does exactly what its key does.
    def buttons(self):
        """The buttons to show now: a list of (title, [(label, key name,
        pygame key), ...]). Some labels show the current state."""
        if self.drawing is not None:        # drawing mode: only its own keys
            return [("Drawing a wave", [("Send it", "Enter", pygame.K_RETURN),
                                        ("Flat line", "C", pygame.K_c),
                                        ("Cancel", "Esc", pygame.K_ESCAPE)])]
        if self.entry is not None:          # typing a number (digits: keyboard)
            return [("Typing a number", [("OK", "Enter", pygame.K_RETURN),
                                         ("Cancel", "Esc", pygame.K_ESCAPE),
                                         ("Delete digit", "Bksp", pygame.K_BACKSPACE)])]
        g = self.gen[self.edit] if self.gen else None
        wave = WAVES[g["wave"]] if g else "?"
        on = ("ON" if g["on"] else "OFF") if g else "?"
        coupled = self.pair and self.pair["coupled"]
        return [
            ("Mode", [
                ("Now: meter" if self.meter_mode else "Now: scope", "M", pygame.K_m),
                ("Paused" if self.paused else "Pause", "Space", pygame.K_SPACE),
                ("Screenshot", "F12", pygame.K_F12)]),
            ("Scope", [
                ("Time/div -", "Left", pygame.K_LEFT),
                ("Time/div +", "Right", pygame.K_RIGHT),
                ("Volts/div -", "Up", pygame.K_UP),
                ("Volts/div +", "Down", pygame.K_DOWN)]),
            ("Trigger", [
                ("Level +", "PgUp", pygame.K_PAGEUP),
                ("Level -", "PgDn", pygame.K_PAGEDOWN),
                ("Edge: rising" if self.trigger_rising else "Edge: falling", "E", pygame.K_e),
                (f"Source CH{self.trigger_source + 1}", "T", pygame.K_t),
                (f"Mode: {self.trigger_mode}", "Tab", pygame.K_TAB),
                ("Arm single", "S", pygame.K_s),
                ("Clear screen", "C", pygame.K_c)]),
            ("View", [
                (f"CH1 {'shown' if self.visible[0] else 'hidden'}", "1", pygame.K_1),
                (f"CH2 {'shown' if self.visible[1] else 'hidden'}", "2", pygame.K_2),
                (f"Cursors {self.cursor_mode}", "K", pygame.K_k)]),
            (f"Generator (editing CH{self.edit + 1})", [
                (f"Edit CH{2 - self.edit}", "Y", pygame.K_y),
                (f"Output {on}", "X", pygame.K_x),
                (f"Wave {wave}", "G", pygame.K_g),
                ("Draw wave", "D", pygame.K_d),
                ("Freq step -", "[", pygame.K_LEFTBRACKET),
                ("Freq step +", "]", pygame.K_RIGHTBRACKET),
                ("Type freq", "F", pygame.K_f),
                ("Type amplitude", "A", pygame.K_a),
                ("Type offset", "O", pygame.K_o),
                ("Type CH2 phase", "P", pygame.K_p),
                ("CH2 coupled" if coupled else "CH2 free", "L", pygame.K_l),
                ("Align (restart)", "R", pygame.K_r)]),
        ]

    def press_key(self, k):
        """Do what key k does, as if it had been pressed on the keyboard."""
        self.key(pygame.event.Event(pygame.KEYDOWN, key=k, unicode="", mod=0))

    def click_button(self, x, y):
        """Was (x, y) on a button? Then press its key and say True."""
        for rect, k in self.button_rects:
            if rect.collidepoint(x, y):
                self.press_key(k)
                return True
        return False

    def draw_buttons(self, x, y):
        """Draw the buttons from buttons() at (x, y), 2 per row, each with its
        label on the left and its key on the right. Remember where they are
        (button_rects) for click_button(). Returns the y below the last one."""
        self.button_rects = []
        mouse = pygame.mouse.get_pos()
        for title, row in self.buttons():
            self.text(title, x, y, WHITE, self.small)
            y += 18
            for i, (label, key_name, k) in enumerate(row):
                column, line = i % 2, i // 2
                rect = pygame.Rect(x + column * (BUTTON_W + BUTTON_GAP),
                                   y + line * (BUTTON_H + BUTTON_GAP), BUTTON_W, BUTTON_H)
                hover = rect.collidepoint(mouse)
                pygame.draw.rect(self.screen, BUTTON_HOVER if hover else BUTTON_COLOR,
                                 rect, border_radius=4)
                pygame.draw.rect(self.screen, GRID, rect, 1, border_radius=4)
                self.text(label, rect.x + 6, rect.y + 3, WHITE, self.small)
                key_text = self.small.render(key_name, True, GREY)
                self.screen.blit(key_text, (rect.right - key_text.get_width() - 6, rect.y + 3))
                self.button_rects.append((rect, k))
            y += (len(row) + 1) // 2 * (BUTTON_H + BUTTON_GAP) + 4
        return y

    # ---- keyboard ----
    def key(self, event):
        if self.entry is not None:          # typing a number: keys go there
            self.entry_key(event)
            return
        if self.drawing is not None:        # drawing: keys go there
            self.draw_key(event)
            return
        k = event.key
        g = self.gen[self.edit] if self.gen else None   # the edited channel
        if k == pygame.K_d:                 # draw a wave, starting from the last one
            self.drawing = [float(v) for v in self.custom[self.edit]]
        elif k == pygame.K_y:               # edit the other generator channel
            self.edit = 1 - self.edit
        elif k == pygame.K_x and g:         # edited channel's output on/off
            self.change_generator(on=1 - g["on"])
        elif k == pygame.K_l and g:         # CH2 coupled to CH1 or not
            self.pair["coupled"] = 1 - self.pair["coupled"]
            self.send_phase()
        elif k == pygame.K_r and g:         # align: restart both together
            self.send_phase()
        elif k == pygame.K_p and g:         # type CH2's phase
            self.entry = ("P", "")
        elif k == pygame.K_k:               # cursors: off -> manual -> track -> off
            modes = ["off", "manual", "track"]
            self.cursor_mode = modes[(modes.index(self.cursor_mode) + 1) % 3]
        elif k == pygame.K_g and g:         # next wave shape
            self.change_generator(wave=(g["wave"] + 1) % len(WAVES))
        elif k == pygame.K_RIGHTBRACKET and g:          # next step up
            higher = [f for f in GEN_STEPS if f > g["freq"]]
            self.change_generator(freq=higher[0] if higher else GEN_MAX_HZ)
        elif k == pygame.K_LEFTBRACKET and g:           # next step down
            lower = [f for f in GEN_STEPS if f < g["freq"]]
            self.change_generator(freq=lower[-1] if lower else GEN_MIN_HZ)
        elif k in (pygame.K_f, pygame.K_a, pygame.K_o) and g:
            self.entry = (pygame.key.name(k).upper(), "")
        elif k == pygame.K_m:
            self.meter_mode = not self.meter_mode
            self.send_rate()
        elif k == pygame.K_LEFT and self.rate_index < len(RATES) - 1:
            self.rate_index += 1            # faster sampling = less time/div
            self.send_rate()
        elif k == pygame.K_RIGHT and self.rate_index > 0:
            self.rate_index -= 1
            self.send_rate()
        elif k == pygame.K_UP and self.vdiv_index > 0:
            self.vdiv_index -= 1
        elif k == pygame.K_DOWN and self.vdiv_index < len(VOLTS_PER_DIV) - 1:
            self.vdiv_index += 1
        elif k == pygame.K_PAGEUP:
            self.trigger_level = min(3.3, self.trigger_level + 0.1)
        elif k == pygame.K_PAGEDOWN:
            self.trigger_level = max(0.0, self.trigger_level - 0.1)
        elif k == pygame.K_e:
            self.trigger_rising = not self.trigger_rising
        elif k == pygame.K_t:
            self.trigger_source = 1 - self.trigger_source
        elif k == pygame.K_TAB:             # Auto -> Normal -> Single -> Auto
            i = TRIGGER_MODES.index(self.trigger_mode)
            self.trigger_mode = TRIGGER_MODES[(i + 1) % len(TRIGGER_MODES)]
            self.single_armed = True
        elif k == pygame.K_c:               # clear: an empty picture (no lines)
            # It stays empty until the trigger mode draws a new capture:
            # Auto at once, Normal at the next trigger, Single only if armed.
            self.view = {"rate": RATES[self.rate_index], "ch": [[], []]}
        elif k == pygame.K_s:               # (re-)arm a single capture
            self.trigger_mode = "Single"
            self.single_armed = True
        elif k in (pygame.K_1, pygame.K_2):
            self.visible[k - pygame.K_1] = not self.visible[k - pygame.K_1]
        elif k == pygame.K_SPACE:
            self.paused = not self.paused
        elif k == pygame.K_F12:
            pygame.image.save(self.screen, "screenshot.png")

    # ---- a new packet arrived ----
    def new_packet(self, packet):
        """Keep the packet for the meter, and decide (trigger mode) whether
        the scope screen shows something new."""
        previous = self.previous
        # Glue only to the packet just before (same rate, counter + 1).
        if previous is None or previous["rate"] != packet["rate"] \
                or (previous["counter"] + 1) % 256 != packet["counter"]:
            previous = None
        self.previous = self.packet = packet

        if self.trigger_mode == "Single" and not self.single_armed:
            return                          # STOPPED: keep the frozen picture
        window = triggered_window(previous, packet, self.trigger_source,
                                  self.trigger_level, self.trigger_rising)
        self.found_trigger = window is not None
        if window is not None:              # triggered: every mode shows it
            self.view = {"rate": packet["rate"], "ch": window}
            if self.trigger_mode == "Single":
                self.single_armed = False   # caught it: freeze
        elif self.trigger_mode == "Auto" or self.view is None:
            # Auto (or nothing on screen yet): show the start of the packet.
            self.view = {"rate": packet["rate"],
                         "ch": [ch[:SHOWN] for ch in packet["ch"]]}
        # Normal / Single without a trigger: keep the old picture.

    def trigger_status(self):
        """The trigger part of the info line, e.g. 'Normal: waiting...'."""
        if self.trigger_mode == "Single":
            if self.single_armed:
                return "Single: ARMED, waiting for trigger"
            return "Single: STOPPED (S = arm again)"
        if self.trigger_mode == "Normal":
            return "Normal: triggered" if self.found_trigger else "Normal: waiting for trigger"
        return "Auto" if self.found_trigger else "Auto (no trigger!)"

    # ---- drawing ----
    def text(self, string, x, y, color=WHITE, font=None):
        self.screen.blit((font or self.font).render(string, True, color), (x, y))

    def draw_scope(self, view):
        for i in range(DIVS_X + 1):
            pygame.draw.line(self.screen, GRID, (GX + i * DIV_X, GY), (GX + i * DIV_X, GY + GH))
        for i in range(DIVS_Y + 1):
            pygame.draw.line(self.screen, GRID, (GX, GY + i * DIV_Y), (GX + GW, GY + i * DIV_Y))
        if view is None:
            self.text("Waiting for the board...", GX + 300, GY + GH // 2)
            return
        vdiv = VOLTS_PER_DIV[self.vdiv_index]
        y_of = self.y_of

        color = CH_COLORS[self.trigger_source]
        level_y = y_of(self.trigger_level)
        if GY <= level_y <= GY + GH:        # trigger level: small arrow
            pygame.draw.polygon(self.screen, color, [(GX - 12, level_y - 6),
                                                     (GX - 12, level_y + 6), (GX - 2, level_y)])
        trigger_x = GX + PRE_TRIGGER * GW / SHOWN   # trigger point: mark at the top
        pygame.draw.line(self.screen, GREY, (trigger_x, GY), (trigger_x, GY + 8), 2)

        self.screen.set_clip(pygame.Rect(GX, GY, GW + 1, GH + 1))
        for ch in (0, 1):
            if self.visible[ch] and len(view["ch"][ch]) > 1:   # empty after C
                points = [(GX + i * GW / SHOWN, y_of(v)) for i, v in enumerate(view["ch"][ch])]
                pygame.draw.lines(self.screen, CH_COLORS[ch], False, points, 2)
        self.screen.set_clip(None)
        if self.cursor_mode != "off":
            self.draw_cursors(view)

        time_div = SHOWN / DIVS_X / view["rate"]
        y = GY + GH + 10
        self.text(f"{nice(time_div, 's')}/div  {vdiv:g} V/div  "
                  f"{nice(view['rate'], 'S/s')}  trig CH{self.trigger_source + 1} "
                  f"{'rising' if self.trigger_rising else 'falling'} "
                  f"{self.trigger_level:.1f} V  {self.trigger_status()}"
                  f"{'  PAUSED' if self.paused else ''}", GX, y)
        for ch in (0, 1):
            signal = view["ch"][ch]         # numbers for what is on screen
            y_text = y + 22 + 20 * ch
            if not signal:                  # cleared with C
                self.text(f"CH{ch + 1}: -", GX, y_text, CH_COLORS[ch])
                continue
            m = measurements(signal, view["rate"])
            freq = (f"{nice(m['freq'], 'Hz')} (T {nice(1 / m['freq'], 's')})"
                    if m["freq"] else "-")
            self.text(f"CH{ch + 1}: Vpp {m['vpp']:.2f}  min {m['min']:.2f}  "
                      f"max {m['max']:.2f}  avg {m['avg']:.3f}  rms {m['rms']:.3f} V  "
                      f"f {freq}", GX, y_text, CH_COLORS[ch])

    def draw_cursors(self, view):
        """Two time cursors (vertical) and two voltage cursors (horizontal),
        with what they measure in a box at the top left of the screen."""
        track = self.cursor_mode == "track"
        signal = view["ch"][self.trigger_source]
        if track and signal:
            # Track: V1/V2 = the sample under t1/t2 on the trigger channel.
            # Done every frame, so they follow the wave as it changes.
            self.cursor_v = [signal[min(round(t), len(signal) - 1)] for t in self.cursor_t]
        xs = [GX + t * GW / SHOWN for t in self.cursor_t]
        ys = [self.y_of(v) for v in self.cursor_v]
        for n in (0, 1):
            pygame.draw.line(self.screen, CURSOR_COLOR, (xs[n], GY), (xs[n], GY + GH))
            pygame.draw.line(self.screen, CURSOR_COLOR, (GX, ys[n]), (GX + GW, ys[n]))
            self.text(f"t{n + 1}", xs[n] + 3, GY + GH - 18, CURSOR_COLOR)
            self.text(f"V{n + 1}", GX + GW - 26, ys[n] - 18, CURSOR_COLOR)
            if track and signal:            # a ring where the cursor meets the wave
                pygame.draw.circle(self.screen, CURSOR_COLOR, (xs[n], ys[n]), 6, 2)
        dt = abs(self.cursor_t[1] - self.cursor_t[0]) / view["rate"]  # samples -> seconds
        dv = self.cursor_v[1] - self.cursor_v[0]
        mode = (f"TRACK on CH{self.trigger_source + 1} (T changes)" if track
                else "MANUAL (K = track)")
        lines = [mode,
                 f"dt = {nice(dt, 's')}   1/dt = {nice(1 / dt, 'Hz') if dt else '-'}",
                 f"V1 = {self.cursor_v[0]:.3f} V   V2 = {self.cursor_v[1]:.3f} V",
                 f"dV = V2 - V1 = {dv:+.3f} V"]
        peaks = gated_peaks(signal, *self.cursor_t)
        if peaks:
            # Peak search between t1 and t2. Times are counted from the
            # left-hand cursor.
            first, i_max, i_min = peaks
            for name, i in (("max", i_max), ("min", i_min)):
                after = (i - first) / view["rate"]
                lines.append(f"{name} {signal[i]:.3f} V @ t +{nice(after, 's')}")
            lines.append(f"max - min = {signal[i_max] - signal[i_min]:.3f} V")
        height = 4 + 20 * len(lines)
        pygame.draw.rect(self.screen, BLACK, (GX + 4, GY + 14, 330, height))
        pygame.draw.rect(self.screen, CURSOR_COLOR, (GX + 4, GY + 14, 330, height), 1)
        for i, line in enumerate(lines):
            self.text(line, GX + 10, GY + 18 + 20 * i, CURSOR_COLOR)
        if peaks:
            # Drawn last, so the box can't hide them: a triangle above the
            # highest sample (tip down) and one below the lowest (tip up).
            for i, tip, base in ((i_max, -4, -14), (i_min, 4, 14)):
                x, y = GX + i * GW / SHOWN, self.y_of(signal[i])
                pygame.draw.polygon(self.screen, CURSOR_COLOR,
                                    [(x, y + tip), (x - 6, y + base), (x + 6, y + base)])

    def draw_canvas(self):
        """Drawing mode (D): the screen is one period of the edited channel's
        custom wave.
        Left = start, right = end, top = offset + amplitude/2, bottom =
        offset - amplitude/2."""
        for i in range(DIVS_X + 1):
            pygame.draw.line(self.screen, GRID, (GX + i * DIV_X, GY), (GX + i * DIV_X, GY + GH))
        for i in range(DIVS_Y + 1):
            pygame.draw.line(self.screen, GRID, (GX, GY + i * DIV_Y), (GX + GW, GY + i * DIV_Y))
        points = [(GX + i * GW / (POINTS - 1), GY + GH - v / 255 * GH)
                  for i, v in enumerate(self.drawing)]
        pygame.draw.lines(self.screen, GEN_COLOR, False, points, 3)
        g = self.gen[self.edit] if self.gen else None
        top = f"{g['offset'] + g['amp'] / 2:.2f} V" if g else "top"
        bottom = f"{g['offset'] - g['amp'] / 2:.2f} V" if g else "bottom"
        self.text(f"top = {top}", GX + 6, GY + 4, GEN_COLOR)
        self.text(f"bottom = {bottom}", GX + 6, GY + GH - 20, GEN_COLOR)
        y = GY + GH + 10
        self.text(f"DRAWING one period of the CH{self.edit + 1} custom wave (100 points):",
                  GX, y, GEN_COLOR)
        self.text("hold the left mouse button and draw from left to right.", GX, y + 22, GEN_COLOR)
        self.text("Enter = send it to the board   C = flat line   Esc = cancel",
                  GX, y + 42, (255, 255, 0))

    def draw_meter(self, p):
        if p is None:
            self.text("Waiting for the board...", GX + 300, GY + GH // 2)
            return
        ch1 = p["ch"][0]
        volts = sum(ch1) / len(ch1)         # average of 1000 samples (0.1 s)
        ohms, note = resistance(volts, p["vdda"], self.r_known)
        rows = [("DC voltage  (A0)", f"{volts:.3f} V", CH_COLORS[0]),
                (f"Resistance  (A0, divider with {nice(self.r_known, 'Ω')} to 3V3) {note}",
                 ohms, CH_COLORS[0]),
                ("Frequency   (D3)", nice(p["freq"], "Hz") if p["freq"] else "no signal", WHITE),
                ("Duty cycle  (D3)", f"{p['duty']:.1f} %" if p["freq"] else "-", WHITE),
                ("Continuity  (A3 to GND)",
                 "CONNECTED (beep)" if p["continuity"] else "open",
                 (80, 255, 120) if p["continuity"] else GREY)]
        for i, (label, value, color) in enumerate(rows):
            self.text(label, GX + 20, GY + 5 + i * 95)
            self.text(value, GX + 20, GY + 25 + i * 95, color, self.big)

    def draw_generator(self, p):
        """Under the screen: two lines per generator channel (settings, then
        the real frequency and how it is made), and the typing prompt.
        "<" marks the channel the generator keys change (Y)."""
        x, y = GX, GY + GH + 72
        if p is None:
            return
        for ch, g in enumerate(p["gen"]):
            yc = y + 40 * ch
            color = GEN_COLOR if g["on"] else GREY
            low, high = g["offset"] - g["amp"] / 2, g["offset"] + g["amp"] / 2
            clipped = ""
            if g["on"] and (low < 0 or high > p["vdda"]):
                clipped = f"  CLIPPED (DAC: 0 .. {p['vdda']:.2f} V)"
            edit = "  < Y" if ch == self.edit else ""
            self.text(f"GEN CH{ch + 1} ({GEN_PINS[ch]}) {'ON ' if g['on'] else 'OFF'} "
                      f"{WAVES[g['wave']].upper()}  {g['amp']:.2f} Vpp  offset "
                      f"{g['offset']:.2f} V{clipped}{edit}", x, yc, color)
            if ch == 1 and p["coupled"]:
                # Same timer as CH1, so same frequency; the phase is real.
                self.text(f"  coupled to CH1 (L)   phase {p['phase']} deg -> real "
                          f"{real_phase(p['phase']):.1f} deg (steps of 3.6)",
                          x, yc + 20, color)
                continue
            real, values_per_s = real_frequency(g)
            error = 100 * (real - g["freq"]) / g["freq"]
            phase = "   phase n/a (not coupled)" if ch == 1 else ""
            self.text(f"  asked {g['freq']} Hz, real {real:.6g} Hz ({error:+.3f} %) "
                      f"= 170 MHz/{g['psc'] + 1}/{g['arr'] + 1}/{g['points']}{phase}",
                      x, yc + 20, color)
        if self.entry is not None:
            what, text = self.entry
            self.text(f"Type the {self.ENTRY_TEXT[what]}, Enter = OK, Esc = cancel:  "
                      f"{text}_", x, y + 82, (255, 255, 0))

    def draw_panel(self, p):
        """The right-hand panel: mode, packets/s, supply, the buttons (each
        shows its keyboard key on the right), and the pins."""
        x, y = PANEL_X, GY
        self.text("MULTIMETER" if self.meter_mode else "OSCILLOSCOPE", x, y, CH_COLORS[0])
        rate = len(self.packet_times) / 2
        vdda = f"   VDDA {p['vdda']:.3f} V" if p else ""
        self.text(f"packets/s: {rate:.0f}{vdda}", x, y + 22, GREY, self.small)
        y = self.draw_buttons(x, y + 46)
        pins = ["Pins: A0 = CH1, volts, ohms  A1 = CH2",
                "D3 = freq. counter  A3 = continuity",
                "A2 = gen. CH1      D12 = gen. CH2",
                "Esc = quit"]
        for i, line in enumerate(pins):
            self.text(line, x, y + 4 + i * 17, GREY, self.small)
        self.text(WARNING, GX, HEIGHT - 26, RED)

    def update_beep(self):
        want = bool(self.meter_mode and self.packet and self.packet["continuity"])
        if want and not self.beeping:
            self.beep.play(loops=-1)
        elif not want and self.beeping:
            self.beep.stop()
        self.beeping = want

    def run(self):
        buffer = bytearray()
        clock = pygame.time.Clock()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE and self.entry is None \
                            and self.drawing is None:
                        return
                    self.key(event)
                if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP,
                                  pygame.MOUSEMOTION):
                    self.mouse(event)

            buffer += self.link.read(self.link.in_waiting or 1)
            now = pygame.time.get_ticks()
            for packet in read_packets(buffer):
                self.packet_times.append(now)
                self.check_generator(packet)
                expected = RATES[METER_RATE if self.meter_mode else self.rate_index]
                if packet["rate"] != expected and now - self.rate_sent_ms > 1000:
                    self.send_rate()        # our rate byte got lost: again
                if not self.paused and packet["rate"] == expected:
                    self.new_packet(packet)
            self.packet_times = [t for t in self.packet_times if now - t < 2000]

            self.screen.fill(BLACK)
            if self.drawing is not None:
                self.draw_canvas()
            elif self.meter_mode:
                self.draw_meter(self.packet)
            else:
                self.draw_scope(self.view)
            self.draw_panel(self.packet)
            self.draw_generator(self.packet)
            pygame.display.flip()
            self.update_beep()
            clock.tick(60)


def main():
    parser = argparse.ArgumentParser(description="NUCLEO scope + multimeter window")
    parser.add_argument("--port", help="COM port of the board, e.g. COM4")
    parser.add_argument("--rref", type=float, default=10000,
                        help="known resistor for the ohm meter, in ohms (default 10000)")
    args = parser.parse_args()
    port = args.port or find_port()
    if port is None:
        raise SystemExit("No ST-LINK board found. Plug it in, or use --port COM4")
    print(f"Using {port}. {WARNING}")
    link = serial.Serial(port, BAUD, timeout=0.01)
    pygame.init()
    try:
        Window(link, args.rref).run()
    finally:
        link.close()
        pygame.quit()


if __name__ == "__main__":
    main()
