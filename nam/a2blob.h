/* NAM A2-Lite capture as stored in a Clone slot (written by matribox/usb/nam_clone.py).
 *
 * The app keeps 0x2000 bytes per Clone slot (FUN_00025d30) and hands the module a pointer to
 * them in the flash window (ctx[0] = FUN_000249ec(slot)), so a blob must stay below 8 KB.
 * Little-endian:
 *   0x00 u32 magic "NAM2"
 *   0x04 u16 version (1)
 *   0x06 u16 number of weights (A2_NUM_WEIGHTS)
 *   0x08 f32 loudness of the model in dB (-18 when the .nam has none)
 *   0x0c f32 sample rate the model was trained at
 *   0x10 u32 CRC-32 (IEEE) of the weight bytes
 *   0x14 u32 flags: bit 0 = run at the pedal's 44.1 kHz even if the model is 48 kHz (no resampling)
 *   0x18 zeros up to 0x88: the factory Clone module (ToneCloneInit) reads its IR lengths at +0x7c and
 *        +0x84 and leaves the slot inactive when they are 0, instead of following them out of bounds
 *   0x88 f32 weights[A2_NUM_WEIGHTS], packed like matribox/nam/nam_a2.py
 */
#ifndef A2BLOB_H
#define A2BLOB_H

#include <stdint.h>

#define A2B_MAGIC   0x324D414Eu     /* "NAM2" */
#define A2B_VERSION 1
#define A2B_HEADER  0x88

struct a2_blob {
    uint32_t magic;
    uint16_t version, nweights;
    float loudness, sample_rate;
    uint32_t crc;
    uint32_t flags;
    uint32_t reserved[28];
};

#define A2B_NATIVE_RATE 1u

static inline uint32_t a2b_crc32(const uint8_t *p, uint32_t n)
{
    uint32_t c = 0xffffffffu;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++)
            c = (c >> 1) ^ (0xedb88320u & (0u - (c & 1u)));
    }
    return ~c;
}

#endif
