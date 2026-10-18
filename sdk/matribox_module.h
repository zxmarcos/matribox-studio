/*
 * Matribox II Pro (QME-200) effect module ABI — reverse engineered from firmware V1.2.2.
 *
 * A module is an ELF loaded by the app's in-house loader (ITCM 0x40xxx). Four regions,
 * selected by the high nibble of the link address:
 *   0x0xxxxxxx  ER_IROM  code + module header   -> ITCM pool   (128 KB shared by all slots)
 *   0x1xxxxxxx  ER_SROM  read-only data         -> SDRAM pool  (128 KB shared)
 *   0x2xxxxxxx  ER_IRAM  fast state             -> OCRAM pool  (192 KB shared)
 *   0x3xxxxxxx  ER_SRAM  big buffers            -> SDRAM pool  (~6.8 MB shared)
 * The packed ELF must be <= 32 KB (per-slot staging buffer). RAM is NOT zeroed by the
 * loader: Init must initialise every piece of state it uses.
 *
 * Audio: 44.1 kHz, blocks of 64 frames, interleaved stereo float (L,R,L,R...), processed in place.
 * Bypass is the module's job: when *enabled == 0 the module must output the dry signal
 * (and may keep tails ringing).
 */
#ifndef MATRIBOX_MODULE_H
#define MATRIBOX_MODULE_H

#include <stdint.h>

#define MB_SAMPLE_RATE 44100.0f
#define MB_MAGIC       0xFFAA55AAu

typedef void (*mb_init_fn)(const float *knobs, void *ctx);
typedef void (*mb_process_fn)(float *io, uint16_t nframes, const float *knobs, float *aux,
                              const uint8_t *enabled, const uint32_t *beat_ms, float **extra);

/* Lives at offset 0 of ER_IROM. Word 0 is overwritten by the host (0xFFAA0000 | instance). */
struct mb_module_header {
    uint32_t host_word;
    uint32_t reserved[4];
    uint32_t effect_id;      /* must match the library entry id, e.g. 0x0B0000xx for delays */
    const void *srom_base;
    mb_init_fn init;
    mb_process_fn process;
    uint32_t magic;          /* MB_MAGIC */
};

extern const char __srom_base[];

#define MB_MODULE(id, init_fn, process_fn)                                              \
    __attribute__((section(".module_header"), used))                                   \
    struct mb_module_header mb_module_header = {                                        \
        0, {0, 0, 0, 0}, (id), __srom_base, (init_fn), (process_fn), MB_MAGIC }

/* place a (zero-initialised, but NOT zeroed by the loader) object in the big SDRAM pool */
#define MB_SRAM __attribute__((section(".bss.sram")))
/* read-only tables go to ER_SROM automatically (const data) */

static inline float mb_clampf(float x, float lo, float hi) { return x < lo ? lo : (x > hi ? hi : x); }

/*
 * Tempo sync. A parameter flagged f2 = 0x1N is syncable; parameter #N (flag 0x2M) is its Sync switch.
 * With Sync on, the UI turns the value into a note-division index 0..10 (shown as below; switching
 * Sync on sets it to 4 = 1/4). Same table as the factory delays (.Lswitch.table in Pure/Warm/...).
 *   0 1/1, 1 1/2, 2 1/2D, 3 1/2T, 4 1/4, 5 1/4D, 6 1/4T, 7 1/8, 8 1/8D, 9 1/8T, 10 1/16
 */
#define MB_NOTE_DIVISIONS 11
static const float mb_note_beats[MB_NOTE_DIVISIONS] = {
    4.0f, 2.0f, 3.0f, 4.0f / 3.0f, 1.0f, 1.5f, 2.0f / 3.0f, 0.5f, 0.75f, 1.0f / 3.0f, 0.25f };

/* period in ms for a division index, given the host's ms-per-beat (only the low 16 bits are valid) */
static inline float mb_sync_ms(float division, const uint32_t *beat_ms)
{
    int d = (int)(division + 0.5f);
    float beats = (d >= 0 && d < MB_NOTE_DIVISIONS) ? mb_note_beats[d] : 1.0f;
    return (float)(*beat_ms & 0xffffu) * beats;
}

/* 2^x without libm (relative error ~1e-7), mainly for dB -> gain */
static inline float mb_exp2f(float x)
{
    if (x < -126.0f) return 0.0f;
    if (x > 127.0f) x = 127.0f;
    int i = (int)x;
    if ((float)i > x) i--;
    float f = x - (float)i;
    float p = 1.0f + f * (0.69314718f + f * (0.24022651f + f * (0.05550411f + f * (0.00961813f + f * 0.00133336f))));
    union { float f; uint32_t u; } v;
    v.u = (uint32_t)(i + 127) << 23;
    return p * v.f;
}

static inline float mb_db_to_gain(float db) { return mb_exp2f(db * 0.16609640474f); }

/* display format 0x74 shows values below -60 as "Mute" */
static inline float mb_level_gain(float db) { return db < -60.5f ? 0.0f : mb_db_to_gain(db); }

#endif
