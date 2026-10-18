/*
 * NAM A2-Lite from the Clone slots: replaces the factory Clone module (menu 11, id 0x0F000000).
 *
 * The app passes the selected slot in ctx[0] (flash window address, FUN_000249ec). A slot written by
 * matribox/usb/nam_clone.py holds an a2_blob (nam/a2blob.h); anything else (a factory Clone capture, an empty
 * slot) is passed through dry.
 *
 * 48 kHz captures (all NAM trainings) run at 48 kHz through nam/rs48.c (+0.95 ms, ~+4% CPU) unless the
 * blob says A2B_NATIVE_RATE; other rates run at the pedal's 44.1 kHz.
 *
 * Knobs keep the factory Clone layout (0..100, 50 = neutral) so presets and the PC editor still match:
 *   0 Gain    input drive, (k - 50) * 0.4 dB
 *   1 Volume  output level, (k - 50) * 0.4 dB, 0 = mute
 *   2 Bass    low shelf 100 Hz,  (k - 50) * 0.24 dB
 *   3 Middle  peak 700 Hz Q 0.7, (k - 50) * 0.24 dB
 *   4 Treble  high shelf 3 kHz,  (k - 50) * 0.24 dB
 */
#include "matribox_module.h"
#include "a2lite.h"
#include "a2blob.h"
#include "rs48.h"

#if A2_BLOCK < RS_MAX48
#error "build with -DA2_BLOCK=72: the 48 kHz path hands the engine up to 70 samples per block"
#endif

#define CLONE_EFFECT_ID 0x0F000000u
#define ADC_TO_NAM 0.45659f         /* ADC full scale is +-2.19 in the app's float chain, NAM expects +-1 */

/* In ITCM, inside the code region: the loader copies every PROGBITS section of ER_IROM into the ITCM
   pool (by name, FUN_000405c8), so a writable array there costs 7.5 KB of the 32 KB ELF but is read
   with no wait states and outside the D-cache the NAM state keeps flushing. OCRAM (192 KB for all
   slots) stays for the NAM state and the resampler tables, so a Cab/IR (~70 KB) still fits. Measured
   on the pedal before: from SDRAM every block refetched them. */
__attribute__((section(".text.weights"), aligned(8))) static float weights[A2_NUM_WEIGHTS];
/* CPU meter (read on the PC with matribox/usb/nam_clone.py cpu). The app's global-setting read (0x16000002, flag
   0x11) returns the float at app+0x7c+4*idx for idx < 32; idx 31 = app+0xf8 sits after the 31 global-EQ
   settings and nothing in the app touches it, so it carries one tagged word: field << 28 | microseconds.
   Fields 0-3 hold the worst case over ~0.75 s: 0 this module's Process, 1 NAM only, 2 resampler only,
   3 the app's whole audio block (its own SysTick measurement, 0x20238198[0], limit 1250 us); 4 and 5 are
   the peaks of fields 0 and 3 since Init. The meter keeps running while the block is off (the app turns
   every slot off after an overload), so the peak of an overload stays readable. */
#define SYST_RVR (*(volatile uint32_t *)0xE000E014u)
#define SYST_CVR (*(volatile uint32_t *)0xE000E018u)
#define APP_DIAG (*(volatile uint32_t *)0x202382a0u)
#define APP_BLOCK_MS (*(volatile const float *)0x20238198u)
#define DIAG_WINDOW 512
#define DIAG_FIELDS 6
static uint32_t dmax[4], dpub[DIAG_FIELDS], dblocks, drot;

static inline uint32_t ticks(void) { return SYST_CVR; }
static inline uint32_t elapsed(uint32_t t0)
{
    const uint32_t t1 = SYST_CVR;   /* counts down at 600 MHz and reloads every ms */
    return t0 >= t1 ? t0 - t1 : t0 + SYST_RVR + 1u - t1;
}

static void diag_block(uint32_t total, uint32_t nam, uint32_t rs)
{
    const float app_ms = APP_BLOCK_MS;
    const uint32_t v[4] = {total / 600u, nam / 600u, rs / 600u,
                           app_ms > 0.0f && app_ms < 100.0f ? (uint32_t)(app_ms * 1000.0f) : 0u};
    for (int i = 0; i < 4; i++)
        if (v[i] > dmax[i]) dmax[i] = v[i];
    if (v[0] > dpub[4]) dpub[4] = v[0];
    if (v[3] > dpub[5]) dpub[5] = v[3];
    if (++dblocks >= DIAG_WINDOW) {
        for (int i = 0; i < 4; i++) { dpub[i] = dmax[i]; dmax[i] = 0; }
        dblocks = 0;
    }
    const uint32_t f = (drot++ >> 5) % DIAG_FIELDS; /* rotate the published field every 32 blocks */
    APP_DIAG = f << 28 | (dpub[f] & 0x0fffffffu);
}

static int active, at48;
static float g_in, g_out, norm;
static float buf[A2_BLOCK], buf48[RS_MAX48];

struct biquad { float b0, b1, b2, a1, a2, z1, z2; };
static struct biquad eq[3];
static float eq_knob[3];

static const float eq_cos[3] = {0.99989850f, 0.99503078f, 0.91003511f};
static const float eq_sin[3] = {0.01424710f, 0.09956785f, 0.41453118f};

static float knob_db(float k, float scale) { return (k - 50.0f) * scale; }

static inline float vsqrt(float x)
{
    float r;
    __asm__("vsqrt.f32 %0, %1" : "=t"(r) : "t"(x));
    return r;
}

static void eq_design(int i, float db)
{
    struct biquad *q = &eq[i];
    const float A = mb_db_to_gain(db * 0.5f), c = eq_cos[i], s = eq_sin[i];
    float b0, b1, b2, a0, a1, a2;
    if (i == 1) {                                   /* RBJ peaking, Q = 0.7 */
        const float al = s / (2.0f * 0.7f);
        b0 = 1.0f + al * A; b1 = -2.0f * c; b2 = 1.0f - al * A;
        a0 = 1.0f + al / A; a1 = -2.0f * c; a2 = 1.0f - al / A;
    } else {                                        /* RBJ shelves, S = 1 */
        const float t = 2.0f * vsqrt(A) * s * 0.70710678f;
        const float p = A + 1.0f, m = A - 1.0f, sg = i == 0 ? 1.0f : -1.0f;
        b0 = A * (p - sg * m * c + t);
        b1 = sg * 2.0f * A * (m - sg * p * c);
        b2 = A * (p - sg * m * c - t);
        a0 = p + sg * m * c + t;
        a1 = -sg * 2.0f * (m + sg * p * c);
        a2 = p + sg * m * c - t;
    }
    const float inv = 1.0f / a0;
    q->b0 = b0 * inv; q->b1 = b1 * inv; q->b2 = b2 * inv; q->a1 = a1 * inv; q->a2 = a2 * inv;
}

static void eq_update(const float *knobs, int force)
{
    for (int i = 0; i < 3; i++) {
        if (force || knobs[2 + i] != eq_knob[i]) {
            eq_knob[i] = knobs[2 + i];
            eq_design(i, knob_db(eq_knob[i], 0.24f));
        }
    }
}

static float in_gain(const float *knobs) { return mb_db_to_gain(knob_db(knobs[0], 0.4f)) * ADC_TO_NAM; }
static float out_gain(const float *knobs)
{
    return knobs[1] < 0.5f ? 0.0f : mb_db_to_gain(knob_db(knobs[1], 0.4f)) * norm;
}

static int load_blob(const uint8_t *p)
{
    /* an empty slot points at zeros inside the app (0x46040); never trust anything but a full match */
    if (!p)
        return 0;
    const struct a2_blob *h = (const struct a2_blob *)p;
    if (h->magic != A2B_MAGIC || h->version != A2B_VERSION || h->nweights != A2_NUM_WEIGHTS)
        return 0;
    const uint8_t *w = p + A2B_HEADER;
    if (a2b_crc32(w, A2_NUM_WEIGHTS * 4) != h->crc)
        return 0;
    /* copy out of the XIP flash window: the slot may be rewritten while this preset is loaded */
    const uint32_t *src = (const uint32_t *)w;
    uint32_t *dst = (uint32_t *)weights;
    for (int i = 0; i < A2_NUM_WEIGHTS; i++)
        dst[i] = src[i];
    float loud = h->loudness;
    if (!(loud > -60.0f && loud < 20.0f))
        loud = -18.0f;
    norm = mb_db_to_gain(-18.0f - loud);
    at48 = h->sample_rate > 47000.0f && h->sample_rate < 49000.0f && !(h->flags & A2B_NATIVE_RATE);
    return 1;
}

static void clone_init(const float *knobs, void *ctx)
{
    at48 = 0;
    active = load_blob(ctx ? *(const uint8_t *const *)ctx : 0);
    if (active)
        a2_init(weights);
    if (at48) {
        rs48_design();
        rs48_reset();
    }
    for (int i = 0; i < 3; i++)
        eq[i].z1 = eq[i].z2 = 0.0f;
    eq_update(knobs, 1);
    for (int i = 0; i < 4; i++)
        dmax[i] = 0;
    for (int i = 0; i < DIAG_FIELDS; i++)
        dpub[i] = 0;
    dblocks = drot = 0;
    g_in = in_gain(knobs);
    g_out = out_gain(knobs);
}

static void clone_process(float *io, uint16_t nframes, const float *knobs, float *aux,
                          const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    if (!active || !*enabled || nframes > 64 || (at48 && nframes != 64)) {
        diag_block(0, 0, 0);
        return;
    }
    const uint32_t t0 = ticks();
    uint32_t t_nam = 0, t_rs = 0;
    eq_update(knobs, 0);
    const float ti = in_gain(knobs), to = out_gain(knobs);
    const float inv = 1.0f / (float)nframes;
    const float di = (ti - g_in) * inv, dout = (to - g_out) * inv;
    float gi = g_in, go = g_out;
    for (int f = 0; f < nframes; f++) {
        gi += di;
        buf[f] = (io[2 * f] + io[2 * f + 1]) * 0.5f * gi;
    }
    if (at48) {
        uint32_t t = ticks();
        const int c = rs48_up(buf, buf48);
        t_rs = elapsed(t);
        t = ticks();
        a2_process(buf48, buf48, c);
        t_nam = elapsed(t);
        t = ticks();
        rs48_down(buf48, c, buf);
        t_rs += elapsed(t);
    } else {
        const uint32_t t = ticks();
        a2_process(buf, buf, nframes);
        t_nam = elapsed(t);
    }
    for (int i = 0; i < 3; i++) {                   /* transposed direct form II */
        struct biquad *q = &eq[i];
        float z1 = q->z1, z2 = q->z2;
        for (int f = 0; f < nframes; f++) {
            const float x = buf[f], y = q->b0 * x + z1;
            z1 = q->b1 * x - q->a1 * y + z2;
            z2 = q->b2 * x - q->a2 * y;
            buf[f] = y;
        }
        q->z1 = z1; q->z2 = z2;
    }
    for (int f = 0; f < nframes; f++) {
        go += dout;
        const float y = buf[f] * go;
        io[2 * f] = y;
        io[2 * f + 1] = y;
    }
    g_in = ti;
    g_out = to;
    diag_block(elapsed(t0), t_nam, t_rs);
}

MB_MODULE(CLONE_EFFECT_ID, clone_init, clone_process);
