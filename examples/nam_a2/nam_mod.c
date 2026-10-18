/*
 * NAM A2-Lite amp model as a Matribox II Pro module.
 *
 * knobs[0] Gain  (dB, input drive into the model)
 * knobs[1] Level (dB, after the model; the model's loudness is normalised to -18 dB like the NAM plugin)
 *
 * Mono: (L+R)/2 in, the model output on both channels. The model runs at the pedal's 44.1 kHz
 * (captures are trained at 48 kHz, so the response is ~8% lower in frequency).
 */
#include "matribox_module.h"
#include "a2lite.h"

#ifndef NAM_EFFECT_ID
#define NAM_EFFECT_ID 0x07000100u
#endif

extern const float a2_model_weights[A2_NUM_WEIGHTS];
extern const float a2_model_loudness;

/* The app scales the ADC so that its full scale is +-2.19 in the float chain
   (1.0198e-9 * 2^31, audio loop FUN_00031e18); NAM expects +-1.0 = interface full scale. */
#define ADC_TO_NAM 0.45659f

static float g_in, g_out, norm;
static float buf[A2_BLOCK];

static void nam_init(const float *knobs, void *ctx)
{
    (void)ctx;
    a2_init(a2_model_weights);
    norm = mb_db_to_gain(-18.0f - a2_model_loudness);
    g_in = mb_db_to_gain(knobs[0]) * ADC_TO_NAM;
    g_out = mb_level_gain(knobs[1]) * norm;
}

static void nam_process(float *io, uint16_t nframes, const float *knobs, float *aux,
                        const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    if (!*enabled || nframes > A2_BLOCK)
        return;
    /* per-block linear ramps towards the knob gains */
    const float ti = mb_db_to_gain(knobs[0]) * ADC_TO_NAM, to = mb_level_gain(knobs[1]) * norm;
    const float inv = 1.0f / (float)nframes;
    const float di = (ti - g_in) * inv, dout = (to - g_out) * inv;
    float gi = g_in, go = g_out;
    for (int f = 0; f < nframes; f++) {
        gi += di;
        buf[f] = (io[2 * f] + io[2 * f + 1]) * 0.5f * gi;
    }
    a2_process(buf, buf, nframes);
    for (int f = 0; f < nframes; f++) {
        go += dout;
        const float y = buf[f] * go;
        io[2 * f] = y;
        io[2 * f + 1] = y;
    }
    g_in = ti;
    g_out = to;
}

MB_MODULE(NAM_EFFECT_ID, nam_init, nam_process);
