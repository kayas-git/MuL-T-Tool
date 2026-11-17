/* generator.c - the function generator: a table of values -> DMA -> DAC.
 *
 * How a wave is made (the CPU does nothing while it plays):
 *
 *   table[100]  --DMA-->  DAC  -->  pin A2 (PA4)
 *                ^
 *                TIM7: every time it overflows, the DMA copies the NEXT
 *                table value into the DAC, which outputs that voltage.
 *
 * The table holds ONE period of the wave (GEN_POINTS = 100 values). The
 * shape comes from a formula (sine, square, triangle, sawtooth) or, for the
 * "custom" wave, from 100 numbers the user drew on the PC. The DMA
 * is "circular": after the last value it starts again at the first one. So
 *
 *   frequency = 170 MHz / (PSC + 1) / (ARR + 1) / 100
 *
 *   170 MHz  = the timer's clock
 *   PSC + 1  = TIM7's prescaler: it divides the clock first
 *   ARR + 1  = TIM7 counts 0..ARR, then overflows: it divides again
 *   100      = table values per period
 *
 * Both PSC and ARR are whole numbers (at most 65535), so most frequencies
 * can only be made approximately. generator_set() picks the closest one,
 * and the PC shows the real result. Example, 440 Hz:
 *   170 000 000 / 100 / 440 = 3863.6 timer ticks per value -> round to 3864
 *   (PSC = 0, ARR = 3863) -> 170 000 000 / 1 / 3864 / 100 = 439.96 Hz.
 *
 * Why at most 10 kHz? Then the DAC gets 10 000 x 100 = 1 000 000 new values
 * a second, the most this DAC (with its output buffer) is made for.
 * (CubeMX note: on the G4 the DAC must be written 32 bits at a time, so the
 * DMA's "peripheral data width" is Word. With Half Word it stops at once.)
 */
#include <math.h>

#include "main.h"
#include "generator.h"

extern DAC_HandleTypeDef hdac1;
extern TIM_HandleTypeDef htim7;

#define TIMER_CLOCK 170000000UL   /* TIM7 counts at the full 170 MHz */

static uint16_t table[GEN_POINTS];   /* one period, DAC values 0..4095 */
static uint8_t custom[GEN_POINTS];   /* the drawn shape, 0 (bottom)..255 (top) */
static GeneratorSettings settings;
static uint32_t full_scale_mv = 3300; /* the DAC outputs this at value 4095 */

/* The wave's shape at point i of the table, from -1 (bottom) to +1 (top). */
static float shape(uint8_t wave, int i)
{
  float x = (float)i / GEN_POINTS;      /* 0 .. 0.99: where in the period */
  switch (wave) {
    case WAVE_SQUARE:                   /* top for half, bottom for half */
      return (x < 0.5f) ? 1.0f : -1.0f;
    case WAVE_TRIANGLE:                 /* up for half, down for half */
      return (x < 0.5f) ? -1.0f + 4.0f * x : 3.0f - 4.0f * x;
    case WAVE_SAWTOOTH:                 /* slowly up, then drop at once */
      return -1.0f + 2.0f * (float)i / (GEN_POINTS - 1);
    case WAVE_CUSTOM:                   /* drawn on the PC: 0..255 -> -1..+1 */
      return custom[i] / 127.5f - 1.0f;
    default:                            /* WAVE_SINE */
      return sinf(2.0f * 3.14159265f * x);
  }
}

/* Fill the table: volts = offset + (amplitude / 2) x shape, then turn the
 * volts into DAC values (0 V -> 0, full_scale_mv -> 4095). */
static void fill_table(void)
{
  for (int i = 0; i < GEN_POINTS; i++) {
    float mv = settings.offset_mv
             + settings.amplitude_mv / 2.0f * shape(settings.wave, i);
    float value = mv * 4095.0f / full_scale_mv + 0.5f;   /* +0.5: round */
    if (value < 0.0f) {
      value = 0.0f;                     /* can't go below 0 V: "clipped" */
    }
    if (value > 4095.0f) {
      value = 4095.0f;                  /* or above the supply */
    }
    table[i] = (uint16_t)value;
  }
}

/* Choose PSC and ARR so that 170 MHz / (PSC+1) / (ARR+1) / 100 is as close
 * as possible to freq_hz. */
static void choose_timer(uint32_t freq_hz)
{
  /* Timer ticks for each table value, rounded: (PSC+1) x (ARR+1). */
  uint32_t ticks = (TIMER_CLOCK / GEN_POINTS + freq_hz / 2) / freq_hz;

  /* ARR + 1 can be at most 65536. Use the smallest prescaler that fits,
   * so ARR stays big and the rounding stays small. */
  uint32_t divide1 = ticks / 65536 + 1;               /* PSC + 1 */
  uint32_t divide2 = (ticks + divide1 / 2) / divide1; /* ARR + 1, rounded */

  settings.prescaler = divide1 - 1;
  settings.period = divide2 - 1;
}

static uint32_t limit(uint32_t value, uint32_t low, uint32_t high)
{
  if (value < low) {
    return low;
  }
  return value > high ? high : value;
}

void generator_set(uint8_t wave, uint32_t freq_hz,
                   uint32_t amplitude_mv, uint32_t offset_mv)
{
  settings.wave = wave <= WAVE_CUSTOM ? wave : WAVE_SINE;
  settings.freq_hz = limit(freq_hz, GEN_MIN_HZ, GEN_MAX_HZ);
  settings.amplitude_mv = limit(amplitude_mv, 0, GEN_MAX_MV);
  settings.offset_mv = limit(offset_mv, 0, GEN_MAX_MV);

  /* 1. Stop the timer and the DMA, so we can safely change the table. */
  HAL_TIM_Base_Stop(&htim7);
  HAL_DAC_Stop_DMA(&hdac1, DAC_CHANNEL_1);

  /* 2. New table and new timer speed. HAL_TIM_Base_Init() writes PSC and
   *    ARR into TIM7 (it keeps the other settings CubeMX made). */
  fill_table();
  choose_timer(settings.freq_hz);
  htim7.Init.Prescaler = settings.prescaler;
  htim7.Init.Period = settings.period;
  HAL_TIM_Base_Init(&htim7);

  /* 3. Go: the DMA copies table[] to the DAC, one value per TIM7 tick. */
  HAL_DAC_Start_DMA(&hdac1, DAC_CHANNEL_1, (uint32_t *)table, GEN_POINTS,
                    DAC_ALIGN_12B_R);
  HAL_TIM_Base_Start(&htim7);
}

void generator_set_custom(const uint8_t *points)
{
  uint8_t check = 0;
  for (int i = 0; i < GEN_POINTS; i++) {
    custom[i] = points[i];
    check += points[i];
  }
  settings.custom_check = check;
  if (settings.wave == WAVE_CUSTOM) {   /* playing it now: new table */
    generator_set(settings.wave, settings.freq_hz,
                  settings.amplitude_mv, settings.offset_mv);
  }
}

void generator_start(uint32_t vdda_mv)
{
  full_scale_mv = vdda_mv;
  /* Until the user draws one, the custom wave is 4 steps, like stairs:
   * 0, 85, 170, 255 (25 points each). */
  uint8_t stairs[GEN_POINTS];
  for (int i = 0; i < GEN_POINTS; i++) {
    stairs[i] = (i / 25) * 85;
  }
  generator_set_custom(stairs);
  /* 1 kHz sine, 2 V peak to peak, around 1.65 V: from 0.65 V to 2.65 V. */
  generator_set(WAVE_SINE, 1000, 2000, 1650);
}

const GeneratorSettings *generator_settings(void)
{
  return &settings;
}
