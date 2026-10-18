/* 44.1 kHz <-> 48 kHz resampler, see rs48.h.
 *
 * Each stage is a rational polyphase resampler on one prototype h[N] (sum 1), with time counted in
 * prototype samples: an input sample every Pi, an output every Po.
 *   y[n] = Pi * sum_j x[b - j] * h[p + Pi j],  b = floor(n Po / Pi), p = n Po - b Pi
 * Phase p's taps are stored reversed and contiguous (tab[p][i] = Pi * h[p + Pi (K-1-i)], zero past N, K
 * padded to a multiple of 4), so an output is one forward dot product over x[b-K+1 .. b]. The tables
 * (11.6 KB) live in OCRAM; the prototypes are only needed in Init and stay in SDRAM. On the pedal a
 * single 160/147 stage with 53 KB of tables in SDRAM cost 225 us per block: bandwidth, not arithmetic.
 * A stage makes every output whose newest input has arrived; the last one feeds a FIFO that hands out
 * 64 samples per block. With all stages starting in phase the FIFO never runs dry (design script, 30000
 * blocks: its level stays within 0..66 after each pop).
 */
#include "rs48.h"

#ifdef A2_HOST
#define RS_SDRAM
#else
#define RS_SDRAM __attribute__((section(".bss.sram")))
#endif

#define N1U 120
#define N1D 240
#define N2 1280
#define K4(n, pi) (((((n) + (pi) - 1) / (pi)) + 3) & ~3)

/* stage geometry: Pi, Po, prototype length, padded taps per phase, most inputs per call */
#define U1_PI 5
#define U1_K K4(N1U, 5)         /* 24 */
#define U2_PI 32
#define U2_K K4(N2, 32)         /* 40 */
#define D1_PI 49
#define D1_K K4(N2, 49)         /* 28 */
#define D2_PI 3
#define D2_K K4(N1D, 3)         /* 80 */

static RS_SDRAM float p1u[N1U], p1d[N1D], p2[N2];

static float t_u1[U1_PI][U1_K], t_u2[U2_PI][U2_K], t_d1[D1_PI][D1_K], t_d2[D2_PI][D2_K];
static float x_u1[U1_K - 1 + 64], x_u2[U2_K - 1 + 108], x_d1[D1_K - 1 + RS_MAX48], x_d2[D2_K - 1 + 108];

struct stage {
    const float *tab;           /* [Pi][K] */
    float *x;                   /* K-1 inputs of history, then this call's */
    int Pi, Po, K;
    int b, p;                   /* next output: newest input index (relative to this call's), phase */
};

static struct stage s_u1, s_u2, s_d1, s_d2;
static float mid_up[108], mid_dn[108];
static float fifo[64 + 72];
static int fifo_n;

static double sqrt_d(double x)
{
#ifdef A2_HOST
    return __builtin_sqrt(x);
#else
    double r;
    __asm__("vsqrt.f64 %P0, %P1" : "=w"(r) : "w"(x));
    return r;
#endif
}

static double bessel_i0(double x)
{
    double s = 1.0, t = 1.0, y = x * x * 0.25;
    for (int k = 1; k < 64; k++) {
        t *= y / ((double)k * (double)k);
        s += t;
        if (t < 1e-17 * s)
            break;
    }
    return s;
}

/* Kaiser-windowed sinc (N even), normalised to sum 1. Built outward from the centre with a rotation
   recurrence, so only sin/cos of small angles are needed (Taylor is exact there). */
static void design(float *h, int N, double fs, double fc, double atten)
{
    const double w = 2.0 * 3.14159265358979323846 * fc / fs, beta = 0.1102 * (atten - 8.7);
    const double c = (N - 1) * 0.5, i0b = bessel_i0(beta);
    double s2 = w * w * 0.25;
    double sn = w * 0.5 * (1.0 - s2 / 6.0 * (1.0 - s2 / 20.0 * (1.0 - s2 / 42.0)));
    double cs = 1.0 - s2 / 2.0 * (1.0 - s2 / 12.0 * (1.0 - s2 / 30.0 * (1.0 - s2 / 56.0)));
    s2 = w * w;
    const double sw = w * (1.0 - s2 / 6.0 * (1.0 - s2 / 20.0 * (1.0 - s2 / 42.0)));
    const double cw = 1.0 - s2 / 2.0 * (1.0 - s2 / 12.0 * (1.0 - s2 / 30.0 * (1.0 - s2 / 56.0)));
    double sum = 0.0;
    for (int k = 0; k < N / 2; k++) {
        const double d = k + 0.5, r = d / c;
        const double v = sn / (w * d) * bessel_i0(beta * sqrt_d(1.0 - r * r)) / i0b;
        h[N / 2 + k] = (float)v;
        sum += 2.0 * v;
        const double t = sn * cw + cs * sw;
        cs = cs * cw - sn * sw;
        sn = t;
    }
    for (int k = 0; k < N / 2; k++)
        h[N / 2 + k] = h[N / 2 - 1 - k] = (float)(h[N / 2 + k] / sum);
}

static void build(float *tab, const float *h, int N, int Pi, int K)
{
    for (int p = 0; p < Pi; p++)
        for (int i = 0; i < K; i++) {
            const int t = p + Pi * (K - 1 - i);
            tab[p * K + i] = t < N ? (float)Pi * h[t] : 0.0f;
        }
}

static void stage_init(struct stage *s, const float *tab, float *x, int nx, int Pi, int Po, int K)
{
    s->tab = tab; s->x = x; s->Pi = Pi; s->Po = Po; s->K = K;
    s->b = s->p = 0;
    for (int i = 0; i < nx; i++)
        x[i] = 0.0f;
}

/* n is a multiple of 4; four accumulators summed as (a0 + a1) + (a2 + a3) */
static inline float dot(const float *x, const float *h, int n)
{
#ifdef A2_HOST
    float a0 = 0.0f, a1 = 0.0f, a2 = 0.0f, a3 = 0.0f;
    for (int i = 0; i < n; i += 4) {
        a0 += x[i] * h[i];
        a1 += x[i + 1] * h[i + 1];
        a2 += x[i + 2] * h[i + 2];
        a3 += x[i + 3] * h[i + 3];
    }
    return (a0 + a1) + (a2 + a3);
#else
    /* the compiler loads one float per VLDR; VLDMIA takes four per instruction on the M7's 64-bit path */
    float r;
    __asm__ volatile(
        "mov      r12, #0\n\t"
        "vmov     s8, r12\n\t"
        "vmov     s9, r12\n\t"
        "vmov     s10, r12\n\t"
        "vmov     s11, r12\n"
        "1:\n\t"
        "pld      [%[h], #64]\n\t"
        "vldmia   %[x]!, {s0-s3}\n\t"
        "vldmia   %[h]!, {s4-s7}\n\t"
        "vfma.f32 s8, s0, s4\n\t"
        "vfma.f32 s9, s1, s5\n\t"
        "vfma.f32 s10, s2, s6\n\t"
        "vfma.f32 s11, s3, s7\n\t"
        "subs     %[n], %[n], #4\n\t"
        "bne      1b\n\t"
        "vadd.f32 s8, s8, s9\n\t"
        "vadd.f32 s10, s10, s11\n\t"
        "vadd.f32 %[r], s8, s10\n\t"
        : [r] "=t"(r), [x] "+r"(x), [h] "+r"(h), [n] "+r"(n)
        :
        : "r12", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "cc", "memory");
    return r;
#endif
}

static int stage_run(struct stage *s, const float *in, int nin, float *out)
{
    const int K = s->K, Pi = s->Pi, Po = s->Po;
    float *x = s->x + (K - 1);              /* x[0] = first new input, x[-1..-(K-1)] = history */
    for (int i = 0; i < nin; i++)
        x[i] = in[i];
    int c = 0, b = s->b, p = s->p;
    while (b < nin) {
        out[c++] = dot(x + b - (K - 1), s->tab + p * K, K);
        p += Po;
        while (p >= Pi) {
            p -= Pi;
            b++;
        }
    }
    s->b = b - nin;
    s->p = p;
    for (int i = 0; i < K - 1; i++)
        s->x[i] = x[nin - (K - 1) + i];
    return c;
}

void rs48_design(void)
{
    design(p1u, N1U, 220500.0, 24000.0, 70.0);  /* pass 20k, stop 28k */
    design(p1d, N1D, 220500.0, 22000.0, 70.0);  /* pass 20k, stop 24.1k */
    design(p2, N2, 2352000.0, 24000.0, 70.0);   /* pass 20k, stop 28k */
    build(&t_u1[0][0], p1u, N1U, U1_PI, U1_K);
    build(&t_u2[0][0], p2, N2, U2_PI, U2_K);
    build(&t_d1[0][0], p2, N2, D1_PI, D1_K);
    build(&t_d2[0][0], p1d, N1D, D2_PI, D2_K);
}

void rs48_reset(void)
{
    stage_init(&s_u1, &t_u1[0][0], x_u1, sizeof x_u1 / 4, U1_PI, 3, U1_K);     /* 44.1k -> 73.5k */
    stage_init(&s_u2, &t_u2[0][0], x_u2, sizeof x_u2 / 4, U2_PI, 49, U2_K);    /* 73.5k -> 48k */
    stage_init(&s_d1, &t_d1[0][0], x_d1, sizeof x_d1 / 4, D1_PI, 32, D1_K);    /* 48k -> 73.5k */
    stage_init(&s_d2, &t_d2[0][0], x_d2, sizeof x_d2 / 4, D2_PI, 5, D2_K);     /* 73.5k -> 44.1k */
    fifo_n = 0;
}

int rs48_up(const float *in44, float *out48)
{
    const int m = stage_run(&s_u1, in44, 64, mid_up);
    return stage_run(&s_u2, mid_up, m, out48);
}

void rs48_down(const float *in48, int c, float *out44)
{
    const int m = stage_run(&s_d1, in48, c, mid_dn);
    fifo_n += stage_run(&s_d2, mid_dn, m, fifo + fifo_n);
    for (int i = 0; i < 64; i++)
        out44[i] = fifo[i];
    fifo_n -= 64;
    for (int i = 0; i < fifo_n; i++)
        fifo[i] = fifo[64 + i];
}
