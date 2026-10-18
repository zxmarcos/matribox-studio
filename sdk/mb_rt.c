/* Minimal runtime for modules (compiled with -fno-builtin so these never call themselves). */
#include <stddef.h>
#include <stdint.h>

void *memset(void *d, int c, size_t n)
{
    uint8_t *p = d;
    while (n && ((uintptr_t)p & 3)) { *p++ = (uint8_t)c; n--; }
    uint32_t w = (uint8_t)c * 0x01010101u;
    for (; n >= 4; n -= 4, p += 4) *(uint32_t *)p = w;
    while (n--) *p++ = (uint8_t)c;
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    uint8_t *a = d;
    const uint8_t *b = s;
    while (n--) *a++ = *b++;
    return d;
}

void __aeabi_memclr(void *d, size_t n) { memset(d, 0, n); }
void __aeabi_memclr4(void *d, size_t n) { memset(d, 0, n); }
void __aeabi_memclr8(void *d, size_t n) { memset(d, 0, n); }
void __aeabi_memset(void *d, size_t n, int c) { memset(d, c, n); }
void __aeabi_memset4(void *d, size_t n, int c) { memset(d, c, n); }
void __aeabi_memcpy(void *d, const void *s, size_t n) { memcpy(d, s, n); }
void __aeabi_memcpy4(void *d, const void *s, size_t n) { memcpy(d, s, n); }
