/* NAM A2-Lite inference, see a2lite.h.
 *
 * Layout: every layer owns a ring of 3-float columns holding its INPUT history
 * (ring = lookback + block columns). A block is cut where a tap's columns wrap
 * around the ring, so each pass reads contiguous runs (no mirror copy: that saved
 * 23 x block columns of OCRAM). A layer's residual output is written straight into
 * the next layer's ring.
 *
 * Taps are processed in pairs with their 18 weights in registers. Per output
 * channel the additions happen in exactly the order of a2_fast.cpp (tap 0 seeded
 * with the bias, taps in order, inputs 0..2 within a tap), so a host build without
 * FMA contraction is bit-exact with NeuralAmpModelerCore.
 */
#include "a2lite.h"

static const int a2_kernel[A2_LAYERS] = {6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 15, 15, 6, 6, 6, 6, 6, 6, 6};
static const int a2_dilation[A2_LAYERS] = {1, 3, 7, 17, 41, 101, 239, 1, 3, 7, 17, 41, 101, 239,
                                           1, 13, 1, 3, 7, 17, 41, 101, 239};

#define HEAD_LB (A2_HEAD_K - 1)

struct a2_layer {
    const float *conv;      /* [K][9], w[j*3+i] = in j -> out i */
    const float *conv_b;    /* [3] */
    const float *mixin;     /* [3] */
    const float *l1x1;      /* [9] */
    const float *l1x1_b;    /* [3] */
    float *hist;            /* ring columns */
    int ring;               /* lookback + A2_BLOCK */
    int cur;                /* column of the current block's first frame */
    int k, d;
};

static struct {
    const float *rechannel;
    const float *head;      /* [16][3] */
    float head_b, head_scale;
    struct a2_layer L[A2_LAYERS];
    float z[A2_BLOCK * A2_C];
    float cond[A2_BLOCK];
    float head_hist[(HEAD_LB + A2_BLOCK) * A2_C];   /* linear: 15 old columns, then this block */
} S;

static float pool[A2_POOL_COLS * A2_C];

static inline int wrap(int c, int ring)
{
    c %= ring;
    return c < 0 ? c + ring : c;
}



static inline float leaky(float x)
{
    /* == (x >= 0 ? x : 0.01x) for every finite x, incl. -0 and denormals; VMAXNM on the M7 */
    return __builtin_fmaxf(x, x * 0.01f);
}

#ifndef A2_HOST
/*
 * Cortex-M7 (Thumb-2, FPv5-SP) versions of the hot loops. Two frames per iteration give 6 independent
 * VFMA chains instead of 3, which hides the FPU latency; inputs, accumulators and outputs move with
 * VLDMIA/VSTMIA (64-bit load path); an odd last frame is done in the same routine. Per output the
 * operations are exactly those the compiler emits for the C loops below (same fused multiply-adds in
 * the same order), so the results are bit-identical to the C build on the device (nam/cmp_axf.py).
 *
 * a2m7_pass2(w, s0, s1, z, n, init):  w = 18 weights (tap k0 then k0+1, w[j*3+i] = in j -> out i)
 *   z[f] = (init ? init : z[f]) + tap k0 . s0[f] + tap k0+1 . s1[f], f < n
 * a2m7_post(&p): mixin, LeakyReLU, head sum and residual for p.n frames (struct a2m7_post_args)
 */
struct a2m7_post_args {
    const float *z, *cond, *s;
    float *hsum, *dst, *dend, *hist;
    int n;
    const float *w;             /* mixin[3], l1x1[9], l1x1_b[3] (contiguous in the packed weights) */
    float slope;                /* 0.01f */
};
void a2m7_pass2(const float *w, const float *s0, const float *s1, float *z, int n, const float *init);
void a2m7_post(struct a2m7_post_args *p);

__asm__(
    ".syntax unified\n"
    ".thumb\n"
    /* frames A (s18-20) and B (s21-23): + w0..w8 (s0-8) . s0 column, + v0..v8 (s9-17) . s1 column */
    ".macro A2M7_PAIR\n"
    "    pld      [r1, #96]\n"                 /* the D-cache has no prefetcher: fetch 4 iterations ahead */
    "    pld      [r2, #96]\n"
    "    vldmia   r1!, {s24-s29}\n"
    "    vfma.f32 s18, s0, s24\n"
    "    vfma.f32 s19, s1, s24\n"
    "    vfma.f32 s20, s2, s24\n"
    "    vfma.f32 s21, s0, s27\n"
    "    vfma.f32 s22, s1, s27\n"
    "    vfma.f32 s23, s2, s27\n"
    "    vfma.f32 s18, s3, s25\n"
    "    vfma.f32 s19, s4, s25\n"
    "    vfma.f32 s20, s5, s25\n"
    "    vfma.f32 s21, s3, s28\n"
    "    vfma.f32 s22, s4, s28\n"
    "    vfma.f32 s23, s5, s28\n"
    "    vfma.f32 s18, s6, s26\n"
    "    vfma.f32 s19, s7, s26\n"
    "    vfma.f32 s20, s8, s26\n"
    "    vfma.f32 s21, s6, s29\n"
    "    vfma.f32 s22, s7, s29\n"
    "    vfma.f32 s23, s8, s29\n"
    "    vldmia   r2!, {s24-s29}\n"
    "    vfma.f32 s18, s9, s24\n"
    "    vfma.f32 s19, s10, s24\n"
    "    vfma.f32 s20, s11, s24\n"
    "    vfma.f32 s21, s9, s27\n"
    "    vfma.f32 s22, s10, s27\n"
    "    vfma.f32 s23, s11, s27\n"
    "    vfma.f32 s18, s12, s25\n"
    "    vfma.f32 s19, s13, s25\n"
    "    vfma.f32 s20, s14, s25\n"
    "    vfma.f32 s21, s12, s28\n"
    "    vfma.f32 s22, s13, s28\n"
    "    vfma.f32 s23, s14, s28\n"
    "    vfma.f32 s18, s15, s26\n"
    "    vfma.f32 s19, s16, s26\n"
    "    vfma.f32 s20, s17, s26\n"
    "    vfma.f32 s21, s15, s29\n"
    "    vfma.f32 s22, s16, s29\n"
    "    vfma.f32 s23, s17, s29\n"
    ".endm\n"

    ".section .text.a2m7_pass2,\"ax\",%progbits\n"
    ".global a2m7_pass2\n"
    ".type a2m7_pass2,%function\n"
    ".thumb_func\n"
    ".p2align 2\n"
    "a2m7_pass2:\n"
    "    pld      [r1]\n"                      /* start the first lines of both streams together */
    "    pld      [r2]\n"
    "    pld      [r1, #32]\n"
    "    pld      [r2, #32]\n"
    "    pld      [r1, #64]\n"
    "    pld      [r2, #64]\n"
    "    push     {r4, r5, r6, lr}\n"
    "    ldrd     r4, r5, [sp, #16]\n"          /* n, init (0: accumulate onto z) */
    "    vpush    {s16-s31}\n"
    "    vldmia   r0, {s0-s17}\n"
    "    lsrs     r6, r4, #1\n"
    "    beq      5f\n"
    "    cmp      r5, #0\n"
    "    beq      2f\n"
    "1:\n"                                      /* first pass: accumulators start at the bias */
    "    vldmia   r5, {s18-s20}\n"
    "    vldmia   r5, {s21-s23}\n"
    "    A2M7_PAIR\n"
    "    vstmia   r3!, {s18-s23}\n"
    "    subs     r6, r6, #1\n"
    "    bne      1b\n"
    "    b        5f\n"
    "2:\n"                                      /* later passes: accumulate onto z */
    "    vldmia   r3, {s18-s23}\n"
    "    A2M7_PAIR\n"
    "    vstmia   r3!, {s18-s23}\n"
    "    subs     r6, r6, #1\n"
    "    bne      2b\n"
    "5:\n"
    "    tst      r4, #1\n"
    "    beq      9f\n"
    "    cmp      r5, #0\n"
    "    ite      ne\n"
    "    movne    r6, r5\n"
    "    moveq    r6, r3\n"
    "    vldmia   r6, {s18-s20}\n"              /* odd frame: frame A only */
    "    vldmia   r1!, {s24-s26}\n"
    "    vfma.f32 s18, s0, s24\n"
    "    vfma.f32 s19, s1, s24\n"
    "    vfma.f32 s20, s2, s24\n"
    "    vfma.f32 s18, s3, s25\n"
    "    vfma.f32 s19, s4, s25\n"
    "    vfma.f32 s20, s5, s25\n"
    "    vfma.f32 s18, s6, s26\n"
    "    vfma.f32 s19, s7, s26\n"
    "    vfma.f32 s20, s8, s26\n"
    "    vldmia   r2!, {s24-s26}\n"
    "    vfma.f32 s18, s9, s24\n"
    "    vfma.f32 s19, s10, s24\n"
    "    vfma.f32 s20, s11, s24\n"
    "    vfma.f32 s18, s12, s25\n"
    "    vfma.f32 s19, s13, s25\n"
    "    vfma.f32 s20, s14, s25\n"
    "    vfma.f32 s18, s15, s26\n"
    "    vfma.f32 s19, s16, s26\n"
    "    vfma.f32 s20, s17, s26\n"
    "    vstmia   r3!, {s18-s20}\n"
    "9:\n"
    "    vpop     {s16-s31}\n"
    "    pop      {r4, r5, r6, pc}\n"
    ".size a2m7_pass2, .-a2m7_pass2\n"

    ".section .text.a2m7_post,\"ax\",%progbits\n"
    ".global a2m7_post\n"
    ".type a2m7_post,%function\n"
    ".thumb_func\n"
    ".p2align 2\n"
    "a2m7_post:\n"
    "    push     {r4, r5, r6, r7, r8, r9, r10, lr}\n"
    "    vpush    {s16-s31}\n"
    "    mov      r10, r0\n"
    "    ldmia    r0, {r1, r2, r3, r4, r5, r6, r7, r8, r9}\n"   /* z cond s hsum dst dend hist n w */
    "    pld      [r3]\n"
    "    pld      [r5]\n"
    "    pld      [r3, #32]\n"
    "    pld      [r5, #32]\n"
    "    pld      [r3, #64]\n"
    "    pld      [r5, #64]\n"
    "    vldmia   r9, {s0-s14}\n"               /* m0-2 = s0-2, l1x1[0..8] = s3-11, lb0-2 = s12-14 */
    "    vldr     s15, [r10, #36]\n"            /* slope 0.01 */
    "    lsrs     r9, r8, #1\n"
    "    beq      5f\n"
    "1:\n"
    "    pld      [r3, #96]\n"                 /* residual input and destination, 4 iterations ahead */
    "    pld      [r5, #96]\n"
    "    vldmia   r1!, {s16-s21}\n"             /* z: frame A s16-18, frame B s19-21 */
    "    vldmia   r2!, {s28-s29}\n"             /* cond A, B */
    "    vfma.f32 s16, s0, s28\n"               /* a = z + m * cond */
    "    vfma.f32 s17, s1, s28\n"
    "    vfma.f32 s18, s2, s28\n"
    "    vfma.f32 s19, s0, s29\n"
    "    vfma.f32 s20, s1, s29\n"
    "    vfma.f32 s21, s2, s29\n"
    "    vmul.f32 s22, s16, s15\n"              /* LeakyReLU = maxnm(a, 0.01 a) */
    "    vmul.f32 s23, s17, s15\n"
    "    vmul.f32 s24, s18, s15\n"
    "    vmul.f32 s25, s19, s15\n"
    "    vmul.f32 s26, s20, s15\n"
    "    vmul.f32 s27, s21, s15\n"
    "    vmaxnm.f32 s16, s16, s22\n"
    "    vmaxnm.f32 s17, s17, s23\n"
    "    vmaxnm.f32 s18, s18, s24\n"
    "    vmaxnm.f32 s19, s19, s25\n"
    "    vmaxnm.f32 s20, s20, s26\n"
    "    vmaxnm.f32 s21, s21, s27\n"
    "    vldmia   r4, {s22-s27}\n"              /* head sums += a */
    "    vadd.f32 s22, s22, s16\n"
    "    vadd.f32 s23, s23, s17\n"
    "    vadd.f32 s24, s24, s18\n"
    "    vadd.f32 s25, s25, s19\n"
    "    vadd.f32 s26, s26, s20\n"
    "    vadd.f32 s27, s27, s21\n"
    "    vstmia   r4!, {s22-s27}\n"
    "    vmov.f32 s22, s12\n"                   /* residual: lb + l1x1 . a */
    "    vmov.f32 s23, s13\n"
    "    vmov.f32 s24, s14\n"
    "    vmov.f32 s25, s12\n"
    "    vmov.f32 s26, s13\n"
    "    vmov.f32 s27, s14\n"
    "    vfma.f32 s22, s3, s16\n"
    "    vfma.f32 s23, s4, s16\n"
    "    vfma.f32 s24, s5, s16\n"
    "    vfma.f32 s25, s3, s19\n"
    "    vfma.f32 s26, s4, s19\n"
    "    vfma.f32 s27, s5, s19\n"
    "    vfma.f32 s22, s6, s17\n"
    "    vfma.f32 s23, s7, s17\n"
    "    vfma.f32 s24, s8, s17\n"
    "    vfma.f32 s25, s6, s20\n"
    "    vfma.f32 s26, s7, s20\n"
    "    vfma.f32 s27, s8, s20\n"
    "    vfma.f32 s22, s9, s18\n"
    "    vfma.f32 s23, s10, s18\n"
    "    vfma.f32 s24, s11, s18\n"
    "    vfma.f32 s25, s9, s21\n"
    "    vfma.f32 s26, s10, s21\n"
    "    vfma.f32 s27, s11, s21\n"
    "    vldmia   r3!, {s28-s30}\n"             /* frame A: dst = s + residual */
    "    vadd.f32 s22, s28, s22\n"
    "    vadd.f32 s23, s29, s23\n"
    "    vadd.f32 s24, s30, s24\n"
    "    vstmia   r5!, {s22-s24}\n"
    "    cmp      r5, r6\n"
    "    it       eq\n"
    "    moveq    r5, r7\n"
    "    vldmia   r3!, {s28-s30}\n"             /* frame B */
    "    vadd.f32 s25, s28, s25\n"
    "    vadd.f32 s26, s29, s26\n"
    "    vadd.f32 s27, s30, s27\n"
    "    vstmia   r5!, {s25-s27}\n"
    "    cmp      r5, r6\n"
    "    it       eq\n"
    "    moveq    r5, r7\n"
    "    subs     r9, r9, #1\n"
    "    bne      1b\n"
    "5:\n"
    "    tst      r8, #1\n"
    "    beq      9f\n"
    "    vldmia   r1!, {s16-s18}\n"             /* odd frame: frame A only */
    "    vldmia   r2!, {s28}\n"
    "    vfma.f32 s16, s0, s28\n"
    "    vfma.f32 s17, s1, s28\n"
    "    vfma.f32 s18, s2, s28\n"
    "    vmul.f32 s22, s16, s15\n"
    "    vmul.f32 s23, s17, s15\n"
    "    vmul.f32 s24, s18, s15\n"
    "    vmaxnm.f32 s16, s16, s22\n"
    "    vmaxnm.f32 s17, s17, s23\n"
    "    vmaxnm.f32 s18, s18, s24\n"
    "    vldmia   r4, {s22-s24}\n"
    "    vadd.f32 s22, s22, s16\n"
    "    vadd.f32 s23, s23, s17\n"
    "    vadd.f32 s24, s24, s18\n"
    "    vstmia   r4!, {s22-s24}\n"
    "    vmov.f32 s22, s12\n"
    "    vmov.f32 s23, s13\n"
    "    vmov.f32 s24, s14\n"
    "    vfma.f32 s22, s3, s16\n"
    "    vfma.f32 s23, s4, s16\n"
    "    vfma.f32 s24, s5, s16\n"
    "    vfma.f32 s22, s6, s17\n"
    "    vfma.f32 s23, s7, s17\n"
    "    vfma.f32 s24, s8, s17\n"
    "    vfma.f32 s22, s9, s18\n"
    "    vfma.f32 s23, s10, s18\n"
    "    vfma.f32 s24, s11, s18\n"
    "    vldmia   r3!, {s28-s30}\n"
    "    vadd.f32 s22, s28, s22\n"
    "    vadd.f32 s23, s29, s23\n"
    "    vadd.f32 s24, s30, s24\n"
    "    vstmia   r5!, {s22-s24}\n"
    "    cmp      r5, r6\n"
    "    it       eq\n"
    "    moveq    r5, r7\n"
    "9:\n"
    "    str      r5, [r10, #16]\n"
    "    vpop     {s16-s31}\n"
    "    pop      {r4, r5, r6, r7, r8, r9, r10, pc}\n"
    ".size a2m7_post, .-a2m7_post\n"
    ".text\n");
#endif

/* first pair of taps: z = bias + tap k0 + tap k0+1 */
static void pass2_first(const float *w, const float *cb, const float *s0, const float *s1, float *z, int n)
{
#ifndef A2_HOST
    a2m7_pass2(w, s0, s1, z, n, cb);
#else
    const float w0 = w[0], w1 = w[1], w2 = w[2], w3 = w[3], w4 = w[4], w5 = w[5], w6 = w[6], w7 = w[7], w8 = w[8];
    const float v0 = w[9], v1 = w[10], v2 = w[11], v3 = w[12], v4 = w[13], v5 = w[14], v6 = w[15], v7 = w[16],
                v8 = w[17];
    const float b0 = cb[0], b1 = cb[1], b2 = cb[2];
    for (int f = 0; f < n; f++, s0 += 3, s1 += 3, z += 3) {
        float a0 = b0 + w0 * s0[0], a1 = b1 + w1 * s0[0], a2 = b2 + w2 * s0[0];
        a0 += w3 * s0[1]; a1 += w4 * s0[1]; a2 += w5 * s0[1];
        a0 += w6 * s0[2]; a1 += w7 * s0[2]; a2 += w8 * s0[2];
        a0 += v0 * s1[0]; a1 += v1 * s1[0]; a2 += v2 * s1[0];
        a0 += v3 * s1[1]; a1 += v4 * s1[1]; a2 += v5 * s1[1];
        a0 += v6 * s1[2]; a1 += v7 * s1[2]; a2 += v8 * s1[2];
        z[0] = a0; z[1] = a1; z[2] = a2;
    }
#endif
}

/* middle pair of taps: z += tap k0 + tap k0+1 */
static void pass2_mid(const float *w, const float *s0, const float *s1, float *z, int n)
{
#ifndef A2_HOST
    a2m7_pass2(w, s0, s1, z, n, 0);
#else
    const float w0 = w[0], w1 = w[1], w2 = w[2], w3 = w[3], w4 = w[4], w5 = w[5], w6 = w[6], w7 = w[7], w8 = w[8];
    const float v0 = w[9], v1 = w[10], v2 = w[11], v3 = w[12], v4 = w[13], v5 = w[14], v6 = w[15], v7 = w[16],
                v8 = w[17];
    for (int f = 0; f < n; f++, s0 += 3, s1 += 3, z += 3) {
        float a0 = z[0] + w0 * s0[0], a1 = z[1] + w1 * s0[0], a2 = z[2] + w2 * s0[0];
        a0 += w3 * s0[1]; a1 += w4 * s0[1]; a2 += w5 * s0[1];
        a0 += w6 * s0[2]; a1 += w7 * s0[2]; a2 += w8 * s0[2];
        a0 += v0 * s1[0]; a1 += v1 * s1[0]; a2 += v2 * s1[0];
        a0 += v3 * s1[1]; a1 += v4 * s1[1]; a2 += v5 * s1[1];
        a0 += v6 * s1[2]; a1 += v7 * s1[2]; a2 += v8 * s1[2];
        z[0] = a0; z[1] = a1; z[2] = a2;
    }
#endif
}

/* one tap: z += tap . s */
static void pass1_mid(const float *w, const float *s, float *z, int n)
{
    const float w0 = w[0], w1 = w[1], w2 = w[2], w3 = w[3], w4 = w[4], w5 = w[5], w6 = w[6], w7 = w[7], w8 = w[8];
    for (int f = 0; f < n; f++, s += 3, z += 3) {
        float a0 = z[0] + w0 * s[0], a1 = z[1] + w1 * s[0], a2 = z[2] + w2 * s[0];
        a0 += w3 * s[1]; a1 += w4 * s[1]; a2 += w5 * s[1];
        a0 += w6 * s[2]; a1 += w7 * s[2]; a2 += w8 * s[2];
        z[0] = a0; z[1] = a1; z[2] = a2;
    }
}

/* everything after the conv, z already holds all taps: mixin, LeakyReLU, head sum, residual */
static void post(const struct a2_layer *L, const float *s, float *hsum, struct a2_layer *next, int f0, int n)
{
    const float *z = S.z + f0 * 3;
    const float *cond = S.cond + f0;
    float *dst = next->hist + wrap(next->cur + f0, next->ring) * 3;
    float *const dend = next->hist + next->ring * 3;
#ifndef A2_HOST
    struct a2m7_post_args p = {z, cond, s, hsum, dst, dend, next->hist, n, L->mixin, 0.01f};
    a2m7_post(&p);
#else
    const float m0 = L->mixin[0], m1 = L->mixin[1], m2 = L->mixin[2];
    const float *lw = L->l1x1;
    const float l00 = lw[0], l01 = lw[1], l02 = lw[2], l10 = lw[3], l11 = lw[4], l12 = lw[5];
    const float l20 = lw[6], l21 = lw[7], l22 = lw[8];
    const float lb0 = L->l1x1_b[0], lb1 = L->l1x1_b[1], lb2 = L->l1x1_b[2];
    for (int f = 0; f < n; f++, s += 3, z += 3, hsum += 3) {
        const float cf = cond[f];
        float a0 = z[0] + m0 * cf, a1 = z[1] + m1 * cf, a2 = z[2] + m2 * cf;
        a0 = leaky(a0); a1 = leaky(a1); a2 = leaky(a2);
        hsum[0] += a0; hsum[1] += a1; hsum[2] += a2;
        dst[0] = s[0] + (lb0 + l00 * a0 + l10 * a1 + l20 * a2);
        dst[1] = s[1] + (lb1 + l01 * a0 + l11 * a1 + l21 * a2);
        dst[2] = s[2] + (lb2 + l02 * a0 + l12 * a1 + l22 * a2);
        dst += 3;
        if (dst == dend) dst = next->hist;
    }
#endif
}

/* last layer: no residual, only mixin, LeakyReLU and the head sum */
static void post_last(const struct a2_layer *L, float *hsum, int f0, int n)
{
    const float m0 = L->mixin[0], m1 = L->mixin[1], m2 = L->mixin[2];
    const float *z = S.z + f0 * 3;
    const float *cond = S.cond + f0;
    for (int f = 0; f < n; f++, z += 3, hsum += 3) {
        const float cf = cond[f];
        float a0 = z[0] + m0 * cf, a1 = z[1] + m1 * cf, a2 = z[2] + m2 * cf;
        a0 = leaky(a0); a1 = leaky(a1); a2 = leaky(a2);
        hsum[0] += a0; hsum[1] += a1; hsum[2] += a2;
    }
}

/* the last two taps (K-2, K-1) as a pair pass, then the post loop */
static void tail2(const struct a2_layer *L, const float *sp, const float *s, float *h, struct a2_layer *nx,
                  int f0, int n)
{
    pass2_mid(L->conv + (L->k - 2) * 9, sp, s, S.z + f0 * 3, n);
    if (nx) post(L, s, h, nx, f0, n);
    else post_last(L, h, f0, n);
}

/* the newest tap alone (odd K), then the post loop */
static void tail1(const struct a2_layer *L, const float *s, float *h, struct a2_layer *nx, int f0, int n)
{
    pass1_mid(L->conv + (L->k - 1) * 9, s, S.z + f0 * 3, n);
    if (nx) post(L, s, h, nx, f0, n);
    else post_last(L, h, f0, n);
}

/* frames where the runs starting at ring columns a and b wrap: cut = {0, ..., n}, returns its length */
static inline int split2(int a, int b, int n, int R, int *cut)
{
    int ca = a + n > R ? R - a : n, cb = b + n > R ? R - b : n, nc = 0;
    if (ca > cb) { const int t = ca; ca = cb; cb = t; }
    cut[nc++] = 0;
    if (ca < n) cut[nc++] = ca;
    if (cb < n && cb != ca) cut[nc++] = cb;
    cut[nc++] = n;
    return nc;
}

/* each pass is cut only where its own taps wrap around the ring (at most twice per pass) */
static void layer_forward(struct a2_layer *L, float *hsum, struct a2_layer *next, int n)
{
    const int K = L->k, D = L->d, R = L->ring;
    const float *h = L->hist;
    int cut[4], nc;
    #define ST(tb) wrap(L->cur - (tb) * D, R)
    #define COL(c, f) (h + ((c) + (f) >= R ? (c) + (f) - R : (c) + (f)) * 3)
    #define SPANS for (int i = 0, f0, m; i + 1 < nc && (f0 = cut[i], m = cut[i + 1] - f0, 1); i++)
    /* K = 6: pair, pair, tail with 2 taps; K = 15: 7 pairs, tail with 1 tap */
    int a = ST(K - 1), b = ST(K - 2);
    nc = split2(a, b, n, R, cut);
    SPANS pass2_first(L->conv, L->conv_b, COL(a, f0), COL(b, f0), S.z + f0 * 3, m);
    int k = 2;
    for (; k + 2 <= K - ((K & 1) ? 1 : 2); k += 2) {
        a = ST(K - 1 - k); b = ST(K - 2 - k);
        nc = split2(a, b, n, R, cut);
        SPANS pass2_mid(L->conv + k * 9, COL(a, f0), COL(b, f0), S.z + f0 * 3, m);
    }
    const int s = ST(0);
    if (k == K - 2) {
        a = ST(1);
        nc = split2(a, s, n, R, cut);
        SPANS tail2(L, COL(a, f0), COL(s, f0), hsum + f0 * 3, next, f0, m);
    } else {
        nc = split2(s, s, n, R, cut);
        SPANS tail1(L, COL(s, f0), hsum + f0 * 3, next, f0, m);
    }
    #undef ST
    #undef COL
    #undef SPANS
}

void a2_process(const float *in, float *out, int n)
{
    struct a2_layer *L0 = &S.L[0];
    const float r0 = S.rechannel[0], r1 = S.rechannel[1], r2 = S.rechannel[2];
    float *dst = L0->hist + L0->cur * 3;
    float *const dend = L0->hist + L0->ring * 3;
    float *hsum = S.head_hist + HEAD_LB * 3;
    for (int f = 0; f < n; f++) {
        const float x = in[f];
        S.cond[f] = x;
        dst[0] = r0 * x; dst[1] = r1 * x; dst[2] = r2 * x;
        dst += 3;
        if (dst == dend) dst = L0->hist;
        hsum[f * 3] = 0.0f; hsum[f * 3 + 1] = 0.0f; hsum[f * 3 + 2] = 0.0f;
    }

    for (int i = 0; i < A2_LAYERS; i++)
        layer_forward(&S.L[i], hsum, i + 1 < A2_LAYERS ? &S.L[i + 1] : 0, n);
    for (int i = 0; i < A2_LAYERS; i++) {
        struct a2_layer *L = &S.L[i];
        L->cur += n;
        if (L->cur >= L->ring) L->cur -= L->ring;
    }

    /* head: K=16 conv over the head sums; head_hist holds 15 older columns then this block */
    /* four frames at a time: four independent chains of 48 multiply-adds (each in the core's order) */
    const float *hw = S.head;
    int f = 0;
    for (; f + 4 <= n; f += 4) {
        const float *src = S.head_hist + f * 3;
        float y0 = S.head_b, y1 = S.head_b, y2 = S.head_b, y3 = S.head_b;
        for (int k = 0; k < A2_HEAD_K; k++, src += 3) {
            const float h0 = hw[k * 3], h1 = hw[k * 3 + 1], h2 = hw[k * 3 + 2];
            y0 += h0 * src[0]; y1 += h0 * src[3]; y2 += h0 * src[6]; y3 += h0 * src[9];
            y0 += h1 * src[1]; y1 += h1 * src[4]; y2 += h1 * src[7]; y3 += h1 * src[10];
            y0 += h2 * src[2]; y1 += h2 * src[5]; y2 += h2 * src[8]; y3 += h2 * src[11];
        }
        out[f] = y0 * S.head_scale;
        out[f + 1] = y1 * S.head_scale;
        out[f + 2] = y2 * S.head_scale;
        out[f + 3] = y3 * S.head_scale;
    }
    for (; f < n; f++) {
        const float *src = S.head_hist + f * 3;
        float y = S.head_b;
        for (int k = 0; k < A2_HEAD_K; k++, src += 3) {
            y += hw[k * 3] * src[0];
            y += hw[k * 3 + 1] * src[1];
            y += hw[k * 3 + 2] * src[2];
        }
        out[f] = y * S.head_scale;
    }
    for (int i = 0; i < HEAD_LB * 3; i++)
        S.head_hist[i] = S.head_hist[n * 3 + i];
}

static void ring_fill(float *hist, int cols, const float *col)
{
    for (int c = 0; c < cols; c++) {
        hist[c * 3] = col[0]; hist[c * 3 + 1] = col[1]; hist[c * 3 + 2] = col[2];
    }
}

void a2_init(const float *w)
{
    float *p = pool;
    S.rechannel = w; w += 3;
    for (int i = 0; i < A2_LAYERS; i++) {
        struct a2_layer *L = &S.L[i];
        L->k = a2_kernel[i];
        L->d = a2_dilation[i];
        L->conv = w; w += L->k * 9;
        L->conv_b = w; w += 3;
        L->mixin = w; w += 3;
        L->l1x1 = w; w += 9;
        L->l1x1_b = w; w += 3;
        L->ring = (L->k - 1) * L->d + A2_BLOCK;
        L->hist = p; p += L->ring * 3;
        L->cur = 0;
    }
    S.head = w; w += A2_HEAD_K * 3;
    S.head_b = *w++;
    S.head_scale = *w++;

    /* steady state of silence (what the core's prewarm converges to): every history holds one
       constant column, computed with the same per-channel operation order as the block code */
    float col[3] = {S.rechannel[0] * 0.0f, S.rechannel[1] * 0.0f, S.rechannel[2] * 0.0f};
    float hs[3] = {0.0f, 0.0f, 0.0f};
    for (int i = 0; i < A2_LAYERS; i++) {
        struct a2_layer *L = &S.L[i];
        ring_fill(L->hist, L->ring, col);
        float a[3];
        for (int c = 0; c < 3; c++) {
            const float *wk = L->conv;
            float v = L->conv_b[c] + wk[c] * col[0];
            v += wk[3 + c] * col[1];
            v += wk[6 + c] * col[2];
            for (int k = 1; k < L->k; k++) {
                wk = L->conv + k * 9;
                v = v + wk[c] * col[0];
                v += wk[3 + c] * col[1];
                v += wk[6 + c] * col[2];
            }
            v += L->mixin[c] * 0.0f;
            a[c] = leaky(v);
            hs[c] += a[c];
        }
        const float *lw = L->l1x1;
        float nx[3];
        for (int c = 0; c < 3; c++)
            nx[c] = col[c] + (L->l1x1_b[c] + lw[c] * a[0] + lw[3 + c] * a[1] + lw[6 + c] * a[2]);
        col[0] = nx[0]; col[1] = nx[1]; col[2] = nx[2];
    }
    ring_fill(S.head_hist, HEAD_LB + A2_BLOCK, hs);
}
