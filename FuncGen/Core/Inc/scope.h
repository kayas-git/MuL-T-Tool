/* scope.h - the oscilloscope and multimeter measurements.
 *
 * Inputs (0 to 3.3 V only!):
 *   A0 (PA0) : scope channel 1, also the DC voltmeter
 *   A1 (PA1) : scope channel 2
 *   D3 (PB3) : frequency / duty cycle counter
 *   A3 (PB0) : continuity probe (the other probe goes to GND)
 */
#ifndef SCOPE_H
#define SCOPE_H

#include <stdint.h>

void scope_init(void);  /* call once after the CubeMX init functions */
void scope_run(void);   /* call forever: measure once, send it to the PC */
uint32_t scope_measure_vdda_mv(void);  /* the real supply voltage, in mV */

#endif
