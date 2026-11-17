/* test_signals.h - two built-in test signals, so you can try the scope with
 * only jumper wires:
 *   A2  (PA4) : 1 kHz sine wave, about 0.4 V to 2.9 V (made by the DAC)
 *   D12 (PA6) : 2 kHz square wave, 0 V / 3.3 V, 25 % high (made by TIM3)
 */
#ifndef TEST_SIGNALS_H
#define TEST_SIGNALS_H

void test_signals_start(void);

#endif
