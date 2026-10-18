/*
 * NAM A2-Lite inference (3-channel A2 WaveNet) in plain C for the Matribox II Pro.
 * Port of NeuralAmpModelerCore/NAM/wavenet/a2_fast.cpp (Channels == 3 path), same
 * operation order, so a host build without FMA contraction matches the official core.
 *
 * Weights come packed by matribox/nam/nam_a2.py (see the order described there).
 * Single instance per module copy (all state is static).
 */
#ifndef A2LITE_H
#define A2LITE_H

#define A2_C        3
#define A2_LAYERS   23
#define A2_HEAD_K   16
#ifndef A2_BLOCK
#define A2_BLOCK    64      /* largest n per call; 72 when a resampler feeds 69/70-sample blocks */
#endif
#define A2_NUM_WEIGHTS 1871

/* sum over layers of (kernel-1)*dilation */
#define A2_LOOKBACK_SUM 6331
/* ring columns: per layer lookback + block */
#define A2_POOL_COLS (A2_LOOKBACK_SUM + A2_LAYERS * A2_BLOCK)

/* reset all state to the steady state of a silent input (what the core's prewarm reaches) */
void a2_init(const float *weights);
/* n <= A2_BLOCK; in and out may alias */
void a2_process(const float *in, float *out, int n);

#endif
