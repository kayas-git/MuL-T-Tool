/* generator.h - the function generator on A2 (PA4, the DAC output).
 *
 * It makes a sine, square, triangle or sawtooth wave, 0 to 3.3 V only.
 * The PC chooses the wave, the frequency (1 Hz to 10 kHz), the amplitude
 * (peak to peak) and the offset (the middle level). See generator.c.
 */
#ifndef GENERATOR_H
#define GENERATOR_H

#include <stdint.h>

/* The 4 wave shapes. The numbers are what the PC sends. */
enum { WAVE_SINE = 0, WAVE_SQUARE = 1, WAVE_TRIANGLE = 2, WAVE_SAWTOOTH = 3 };

#define GEN_POINTS   100      /* values in the table = points per period */
#define GEN_MIN_HZ   1
#define GEN_MAX_HZ   10000    /* 10 kHz x 100 points = 1 000 000 values/s */
#define GEN_MAX_MV   3300     /* amplitude and offset: 0 .. 3300 mV */

/* The current settings (the PC shows them). */
typedef struct {
  uint8_t  wave;           /* WAVE_SINE ... WAVE_SAWTOOTH */
  uint32_t freq_hz;        /* the frequency the PC asked for */
  uint16_t amplitude_mv;   /* peak to peak, in millivolts */
  uint16_t offset_mv;      /* middle level, in millivolts */
  uint16_t prescaler;      /* TIM7 PSC register: it divides by PSC + 1 */
  uint16_t period;         /* TIM7 ARR register: it divides by ARR + 1 */
} GeneratorSettings;

/* Start with a 1 kHz sine. vdda_mv = the real supply voltage (the DAC's
 * full scale), so that the millivolts come out right. */
void generator_start(uint32_t vdda_mv);

/* Change the wave. Values out of range are limited to the allowed range. */
void generator_set(uint8_t wave, uint32_t freq_hz,
                   uint32_t amplitude_mv, uint32_t offset_mv);

const GeneratorSettings *generator_settings(void);

#endif
