"""funcgen_window.py - the PC screen of the NUCLEO-G474RE function
generator (+ oscilloscope and multimeter, to watch what it makes).

The board makes a wave on A2, measures and sends packets (about 10 to 90
per second). This script only:
  1. finds the board's COM port,
  2. reads the packets (2 channels of samples + multimeter readings +
     the generator's settings),
  3. draws them: an oscilloscope screen, or big multimeter numbers, and
     the generator's settings with its real frequency,
  4. sends the chosen sample rate (one byte), generator settings
     ('G' + 10 bytes) and a drawn wave shape ('W' + 101 bytes) to the board.

Extras: K shows two time + two voltage cursors (drag them with the mouse);
press K again for "track" mode, where the voltage cursors follow the wave.
D lets you draw one period of a "custom" wave with the mouse.

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
HEADER = 35                   # bytes before the samples (see scope.c)
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
WIDTH, HEIGHT = GX + GW + 280, GY + GH + 190

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
            wave, points, freq, psc, arr, amp_mv, offset_mv, shape = \
            struct.unpack_from("<IHHIIBBBBIHHHHB", buffer, start + 2)
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
            "gen": {"wave": wave, "freq": freq, "amp": amp_mv / 1000,
                    "offset": offset_mv / 1000, "psc": psc, "arr": arr,
                    "points": points, "shape": shape},
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


def generator_command(wave, freq, amp, offset):
    """The 11 bytes that change the generator (see scope.c):
    'G', wave, frequency Hz (4 bytes), amplitude mV (2), offset mV (2),
    check = the 9 bytes after 'G' added up (low 8 bits)."""
    body = struct.pack("<BIHH", wave, freq, round(amp * 1000), round(offset * 1000))
    return b"G" + body + bytes([sum(body) & 0xFF])


def shape_command(shape):
    """The 102 bytes that send a drawn wave (see scope.c): 'W', the 100
    shape points (0 = bottom .. 255 = top), check = the 100 added up."""
    return b"W" + bytes(shape) + bytes([sum(shape) & 0xFF])


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
        # Generator settings we want. None until the first packet tells us
        # what the board is doing now.
        self.gen = None
        self.gen_sent_ms = 0                # when we last sent them
        self.entry = None                   # typing a number: ("F", "440")
        # The custom wave's shape (100 numbers, 0..255), and the shape being
        # drawn (a list of 100 numbers while in drawing mode, else None).
        self.custom = list(STAIRS)
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

    def send_generator(self, with_shape=False):
        """Send the settings ('G'). with_shape: first send our custom shape
        ('W'), so a following 'G' with the custom wave plays it."""
        g = self.gen
        if with_shape:
            self.link.write(shape_command(self.custom))
            g["shape"] = sum(self.custom) & 0xFF
        self.link.write(generator_command(g["wave"], g["freq"], g["amp"], g["offset"]))
        self.gen_sent_ms = pygame.time.get_ticks()

    def check_generator(self, packet):
        """Take over the board's settings at the start. Later, if the board
        does not show ours (a lost command, or the board was reset), send
        them again, at most once a second. The shape only matters while the
        custom wave plays."""
        board = packet["gen"]
        if self.gen is None:
            self.gen = {k: board[k] for k in ("wave", "freq", "amp", "offset", "shape")}
            return
        keys = ["wave", "freq", "amp", "offset"]
        if self.gen["wave"] == CUSTOM:
            keys.append("shape")
        wrong = [k for k in keys if abs(self.gen[k] - board[k]) > 0.0005]
        if wrong and pygame.time.get_ticks() - self.gen_sent_ms > 1000:
            self.send_generator(with_shape="shape" in wrong)

    def change_generator(self, **changes):
        if self.gen is None:
            return                          # no packet yet: we don't know it
        self.gen.update(changes)
        # With the custom wave, send its shape too (102 more bytes: cheap).
        self.send_generator(with_shape=self.gen["wave"] == CUSTOM)

    # ---- typing a number (F, A, O) ----
    ENTRY_TEXT = {"F": f"frequency in Hz ({GEN_MIN_HZ} .. {GEN_MAX_HZ})",
                  "A": f"amplitude in volts peak-to-peak (0 .. {GEN_MAX_V})",
                  "O": f"offset (middle level) in volts (0 .. {GEN_MAX_V})"}

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
            self.custom = [round(v) for v in self.drawing]
            self.drawing = None
            if self.gen is not None:        # play it: 'W' (shape) + 'G' (custom)
                self.gen["wave"] = CUSTOM
                self.send_generator(with_shape=True)
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

    # ---- keyboard ----
    def key(self, event):
        if self.entry is not None:          # typing a number: keys go there
            self.entry_key(event)
            return
        if self.drawing is not None:        # drawing: keys go there
            self.draw_key(event)
            return
        k = event.key
        if k == pygame.K_d:                 # draw a wave, starting from the last one
            self.drawing = [float(v) for v in self.custom]
        elif k == pygame.K_k:               # cursors: off -> manual -> track -> off
            modes = ["off", "manual", "track"]
            self.cursor_mode = modes[(modes.index(self.cursor_mode) + 1) % 3]
        elif k == pygame.K_g and self.gen:  # next wave shape
            self.change_generator(wave=(self.gen["wave"] + 1) % len(WAVES))
        elif k == pygame.K_RIGHTBRACKET and self.gen:   # next step up
            higher = [f for f in GEN_STEPS if f > self.gen["freq"]]
            self.change_generator(freq=higher[0] if higher else GEN_MAX_HZ)
        elif k == pygame.K_LEFTBRACKET and self.gen:    # next step down
            lower = [f for f in GEN_STEPS if f < self.gen["freq"]]
            self.change_generator(freq=lower[-1] if lower else GEN_MIN_HZ)
        elif k in (pygame.K_f, pygame.K_a, pygame.K_o) and self.gen:
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
        """Drawing mode (D): the screen is one period of the custom wave.
        Left = start, right = end, top = offset + amplitude/2, bottom =
        offset - amplitude/2."""
        for i in range(DIVS_X + 1):
            pygame.draw.line(self.screen, GRID, (GX + i * DIV_X, GY), (GX + i * DIV_X, GY + GH))
        for i in range(DIVS_Y + 1):
            pygame.draw.line(self.screen, GRID, (GX, GY + i * DIV_Y), (GX + GW, GY + i * DIV_Y))
        points = [(GX + i * GW / (POINTS - 1), GY + GH - v / 255 * GH)
                  for i, v in enumerate(self.drawing)]
        pygame.draw.lines(self.screen, GEN_COLOR, False, points, 3)
        g = self.gen
        top = f"{g['offset'] + g['amp'] / 2:.2f} V" if g else "top"
        bottom = f"{g['offset'] - g['amp'] / 2:.2f} V" if g else "bottom"
        self.text(f"top = {top}", GX + 6, GY + 4, GEN_COLOR)
        self.text(f"bottom = {bottom}", GX + 6, GY + GH - 20, GEN_COLOR)
        y = GY + GH + 10
        self.text("DRAWING one period of the custom wave (100 points):", GX, y, GEN_COLOR)
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
        """Three lines under the screen: the generator's settings, its real
        frequency (and how it is made), and the typing prompt."""
        x, y = GX, GY + GH + 72
        if p is None:
            return
        g = p["gen"]
        real, values_per_s = real_frequency(g)
        error = 100 * (real - g["freq"]) / g["freq"]
        low, high = g["offset"] - g["amp"] / 2, g["offset"] + g["amp"] / 2
        clipped = ""
        if low < 0 or high > p["vdda"]:
            clipped = f"  CLIPPED (the DAC can only do 0 .. {p['vdda']:.2f} V)"
        self.text(f"GENERATOR (A2): {WAVES[g['wave']].upper()}  {g['amp']:.2f} Vpp"
                  f"  offset {g['offset']:.2f} V{clipped}", x, y, GEN_COLOR)
        self.text(f"asked {g['freq']} Hz, real {real:.6g} Hz ({error:+.3f} %)"
                  f"   DAC: {nice(values_per_s, 'values/s')}", x, y + 20, GEN_COLOR)
        self.text(f"real = 170 MHz / (PSC+1) / (ARR+1) / points = 170 MHz / "
                  f"{g['psc'] + 1} / {g['arr'] + 1} / {g['points']}", x, y + 40, GEN_COLOR)
        if self.entry is not None:
            what, text = self.entry
            self.text(f"Type the {self.ENTRY_TEXT[what]}, Enter = OK, Esc = cancel:  "
                      f"{text}_", x, y + 62, (255, 255, 0))

    def draw_panel(self, p):
        x, y = GX + GW + 25, GY
        self.text("MULTIMETER" if self.meter_mode else "OSCILLOSCOPE", x, y, CH_COLORS[0])
        rate = len(self.packet_times) / 2
        lines = [f"packets/s: {rate:.0f}"]
        if p:
            lines.append(f"supply VDDA: {p['vdda']:.3f} V")
        lines += ["", "M       scope / meter", "", "Scope keys:",
                  "Left/Right  time/div", "Up/Down     volts/div",
                  "PgUp/PgDn   trigger level", "E           trigger edge",
                  "T           trigger CH1/CH2", "Tab         trigger mode",
                  "S           arm Single", "C           clear screen",
                  "1 / 2       show/hide CH", "K           cursors/track",
                  "Space       pause", "F12         screenshot", "",
                  "Generator keys (A2):", "G           wave shape",
                  "[ / ]       frequency -/+", "F           type frequency",
                  "A           type amplitude", "O           type offset",
                  "D           draw a wave", "",
                  "Pins:", "A0 = CH1 / volts / ohms", "A1 = CH2",
                  "D3 = frequency counter", "A3 = continuity probe",
                  "A2 = GENERATOR output", "D12 = 2 kHz square"]
        for i, line in enumerate(lines):
            self.text(line, x, y + 26 + i * 19, GREY)
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
