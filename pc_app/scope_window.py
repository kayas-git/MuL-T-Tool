"""scope_window.py - the PC screen of the NUCLEO-G474RE oscilloscope and
multimeter.

The board measures and sends packets (about 10 to 90 per second). This
script only:
  1. finds the board's COM port,
  2. reads the packets (2 channels of samples + multimeter readings),
  3. draws them: an oscilloscope screen, or big multimeter numbers,
  4. sends the chosen sample rate to the board (one byte).

INPUTS: 0 V to 3.3 V ONLY. Never connect mains, car or other batteries,
or anything negative. That can destroy the board and your PC's USB port.

Run:  py scope_window.py              (finds the board by itself)
      py scope_window.py --port COM4
      py scope_window.py --rref 4700   (known resistor for the Ω meter)
"""

import argparse
import array
import math
import struct

import pygame
import serial
from serial.tools import list_ports

BAUD = 4250000                # must match USART2 in the board's Scope.ioc
START = b"\xA5\x5A"           # every packet starts with these 2 bytes
HEADER = 20                   # bytes before the samples (see scope.c)
TIMER_HZ = 170_000_000        # TIM2 (frequency counter) counts this fast

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
WIDTH, HEIGHT = GX + GW + 280, GY + GH + 110

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
        rate, n, vdda_mv, period, high, continuity, counter = \
            struct.unpack_from("<IHHIIBB", buffer, start + 2)
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
        pygame.display.set_caption("NUCLEO scope + multimeter")
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
        self.send_rate()

    # ---- talking to the board ----
    def send_rate(self):
        index = METER_RATE if self.meter_mode else self.rate_index
        self.link.write(bytes([index]))

    # ---- keyboard ----
    def key(self, k):
        if k == pygame.K_m:
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

        def y_of(volts):                    # 0 V is the bottom line
            return GY + GH - volts / vdiv * DIV_Y

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

        time_div = SHOWN / DIVS_X / view["rate"]
        y = GY + GH + 10
        self.text(f"{nice(time_div, 's')}/div  {vdiv:g} V/div  "
                  f"{nice(view['rate'], 'S/s')}  trig CH{self.trigger_source + 1} "
                  f"{'rising' if self.trigger_rising else 'falling'} "
                  f"{self.trigger_level:.1f} V  {self.trigger_status()}"
                  f"{'  PAUSED' if self.paused else ''}", GX, y)
        for ch in (0, 1):
            signal = view["ch"][ch]         # numbers for what is on screen
            if not signal:                  # cleared with C
                self.text(f"CH{ch + 1}: min -  max -  avg -  freq -",
                          GX, y + 22 + 20 * ch, CH_COLORS[ch])
                continue
            freq = signal_frequency(signal, view["rate"])
            self.text(f"CH{ch + 1}: min {min(signal):.2f} V  max {max(signal):.2f} V  "
                      f"avg {sum(signal) / len(signal):.3f} V  "
                      f"freq {nice(freq, 'Hz') if freq else '-'}",
                      GX, y + 22 + 20 * ch, CH_COLORS[ch])

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
                  "1 / 2       show/hide CH",
                  "Space       pause", "F12         screenshot", "",
                  "Pins:", "A0 = CH1 / volts / ohms", "A1 = CH2",
                  "D3 = frequency counter", "A3 = continuity probe",
                  "", "Test signals out:", "A2  = 1 kHz sine", "D12 = 2 kHz square"]
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
                    if event.key == pygame.K_ESCAPE:
                        return
                    self.key(event.key)

            buffer += self.link.read(self.link.in_waiting or 1)
            now = pygame.time.get_ticks()
            for packet in read_packets(buffer):
                self.packet_times.append(now)
                expected = RATES[METER_RATE if self.meter_mode else self.rate_index]
                if not self.paused and packet["rate"] == expected:
                    self.new_packet(packet)
            self.packet_times = [t for t in self.packet_times if now - t < 2000]

            self.screen.fill(BLACK)
            if self.meter_mode:
                self.draw_meter(self.packet)
            else:
                self.draw_scope(self.view)
            self.draw_panel(self.packet)
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
