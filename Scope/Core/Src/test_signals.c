/* test_signals.c - the sine wave (DAC) and square wave (timer PWM).
 *
 * Sine: a table of 100 DAC values (one full sine period) is copied to the
 * DAC by DMA, one value each time TIM7 ticks. TIM7 ticks 100 000 times a
 * second (170 MHz / 1700), so the sine repeats 100 000 / 100 = 1000 times a
 * second = 1 kHz. The CPU does nothing after the start: DMA does the work.
 * (CubeMX note: on the G4 the DAC must be written 32 bits at a time, so the
 * DMA's "peripheral data width" is Word. With Half Word it stops at once.)
 *
 * Square: TIM3 counts 0..4999 at 10 MHz (170 MHz / 17) = 2 kHz. Its output
 * pin is high while the count is below 1250, so it is high 25 % of the time.
 * All of these numbers are set in CubeMX (Scope.ioc).
 */
#include <math.h>

#include "main.h"
#include "test_signals.h"

extern DAC_HandleTypeDef hdac1;
extern TIM_HandleTypeDef htim3;
extern TIM_HandleTypeDef htim7;

#define SINE_POINTS 100

static uint16_t sine_table[SINE_POINTS];

void test_signals_start(void)
{
  /* The DAC turns 0..4095 into 0..3.3 V. Middle = 2048, swing +-1500
   * (about +-1.2 V), so the wave stays away from 0 V and 3.3 V. */
  for (int i = 0; i < SINE_POINTS; i++) {
    float angle = 2.0f * 3.14159265f * (float)i / SINE_POINTS;
    sine_table[i] = (uint16_t)(2048.0f + 1500.0f * sinf(angle));
  }

  HAL_DAC_Start_DMA(&hdac1, DAC_CHANNEL_1, (uint32_t *)sine_table,
                    SINE_POINTS, DAC_ALIGN_12B_R);
  HAL_TIM_Base_Start(&htim7);              /* TIM7 paces the DAC */

  HAL_TIM_PWM_Start(&htim3, TIM_CHANNEL_1); /* square wave on D12 */
}
