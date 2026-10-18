/* Patch for the original app (V1.2.2): draw a Split / Path B / Merge section of the chain as two branches,
 * on the main screen and on the chain edit screen.
 *
 * Block placement: every chain block is placed with WM_SetWindowPos (0xfec6), whose first instruction is
 * replaced by a b.w to setpos_hook. Three call sites are recognised by their return address:
 *   main screen  FUN_8000afc4  WM_SetWindowPos(block, 243 - 19*n + 38*i, 70, 32, 44)   r6 = i, r7 = n
 *   edit screen  FUN_8000d79c  WM_SetWindowPos(block, table[n][i], w, h)               r5 = i
 *   edit cursor  FUN_80004554  WM_SetWindowPos(0x82f, table[n][sel], w, h + 20)        r6 = sel
 * (edit screen: n = byte at 0x20234868; n <= 6 big blocks 54x76 in one row, else 36x50 in two rows)
 *
 * Connectors: the two dialogs' callbacks (literals passed to GUI_CreateDialogBox) are replaced by main_cb
 * and edit_cb, which forward to the originals and draw the fork/join lines on WM_PAINT. The main screen's
 * straight line is a widget (0x83a) and is hidden while a parallel section is shown; the edit screen draws
 * its lines in its own WM_PAINT, which is skipped then.
 */
#include <stdint.h>
#include "route_icons.h"

/* ---- app state */
#define APP_STATE    0x202381a8u     /* global settings + current preset (FUN_000399f0's DAT_00039cc4) */
#define CHAIN_ORDER  (APP_STATE + 0x238 + 0x22)          /* slot shown at each position, 0xff = none */
#define SLOT_ID(s)   (*(const volatile uint32_t *)(APP_STATE + 0x268 + 0x44 * (s) + 4))
#define EDIT_COUNT   (*(const volatile uint8_t *)0x20234868u)   /* blocks laid out on the edit screen */

#define SPLIT 0x01000070u
#define PATHB 0x01000071u
#define MERGE 0x01000072u

/* ---- app / emWin functions (ITCM, Thumb) */
typedef struct { int MsgId; int hWin; int hWinSrc; void *Data; } WM_MESSAGE;
#define WM_PAINT 0x0f
typedef void cb_t(WM_MESSAGE *);
#define MAIN_CB          ((cb_t *)0x80018361u)
#define EDIT_CB          ((cb_t *)0x800159b5u)
#define chain_count      ((int (*)(void))0x20855u)              /* blocks in the current preset */
#define app_color        ((uint32_t (*)(int))0x3585du)         /* theme colour k */
#define GUI_SetColor     ((uint32_t (*)(uint32_t))0x88f5u)
#define GUI_SetPenSize   ((int (*)(int))0x89d5u)
#define GUI_DrawHLine    ((void (*)(int, int, int))0x5be5u)    /* y, x0, x1 */
#define GUI_DrawVLine    ((void (*)(int, int, int))0x5fa5u)    /* x, y0, y1 */
#define WM_GetDialogItem ((int (*)(int, int))0xed65u)
#define WM_ShowWindow    ((void (*)(int))0xfee5u)
#define WM_HideWindow    ((void (*)(int))0xf419u)
#define WM_Invalidate    ((void (*)(int))0xf5c7u)
#define block_bitmap     ((void (*)(int, int, const GUI_BITMAP *))0x800120c5u) /* (block, 0 on / 1 bypassed, bmp) */
#define LINE_COLOR 7                                            /* the edit screen's line colour */

/* ---- layout */
struct geo {
    int w, pitch;               /* block width, preferred horizontal pitch */
    int y_main, y_a, y_b;       /* block tops: serial part, path A, path B */
    int line;                   /* connector height inside a block */
    int x0, x1;                 /* extent of the main line */
};
static const struct geo MAIN_GEO  = { 32, 46, 79, 58, 100, 22, 10, 470 };
static const struct geo EDIT_SMALL = { 36, 50, 100, 64, 135, 25, 6, 475 };
static const struct geo EDIT_BIG   = { 54, 78, 99, 58, 140, 34, 6, 475 };

struct par { int s, b, m, k, cols, pitch, left; };

static uint32_t pos_id(int i)
{
    uint8_t s = ((const volatile uint8_t *)CHAIN_ORDER)[i];
    return s < 12 ? SLOT_ID(s) : 0;
}

static int find(uint32_t id, int n)
{
    for (int i = 0; i < n; i++)
        if (pos_id(i) == id)
            return i;
    return -1;
}

/* is there a parallel section in the first n positions? fills p */
static int parallel(int n, const struct geo *g, struct par *p)
{
    if (n < 2 || n > 12)
        return 0;
    p->s = find(SPLIT, n);
    p->m = find(MERGE, n);
    if (p->s < 0 || p->m < p->s)
        return 0;
    p->b = find(PATHB, n);
    if (p->b < p->s || p->b > p->m)
        p->b = p->m;                                    /* no Path B: path B is the dry signal */
    int a_len = p->b - p->s - 1, b_len = p->m - p->b;   /* row B starts with the Path B block */
    p->k = a_len > b_len ? a_len : b_len;
    if (p->k < 1)
        p->k = 1;
    p->cols = p->s + 1 + p->k + (n - p->m);
    p->pitch = g->pitch;
    if (p->cols > 1 && (p->cols - 1) * p->pitch + g->w > g->x1 - g->x0)
        p->pitch = (g->x1 - g->x0 - g->w) / (p->cols - 1);
    p->left = 240 - ((p->cols - 1) * p->pitch + g->w) / 2;
    return 1;
}

static void place(const struct geo *g, const struct par *p, int i, int *x, int *y)
{
    int col, top;
    if (i <= p->s)     { col = i;                          top = g->y_main; }
    else if (i < p->b) { col = i;                          top = g->y_a; }
    else if (i < p->m) { col = p->s + 1 + i - p->b;        top = g->y_b; }
    else               { col = p->s + 1 + p->k + i - p->m; top = g->y_main; }
    *x = p->left + p->pitch * col;
    *y = top;
}

/* fork after Split, two rows, join before Merge; the blocks are drawn over the lines */
static void connectors(const struct geo *g, const struct par *p)
{
    int xs, xm, y;
    place(g, p, p->s, &xs, &y);
    place(g, p, p->m, &xm, &y);
    int gap = (p->pitch - g->w) / 2;
    int fork = xs + g->w + gap, join = xm - gap;
    int ym = g->y_main + g->line, ya = g->y_a + g->line, yb = g->y_b + g->line;
    GUI_SetPenSize(1);
    GUI_SetColor(app_color(LINE_COLOR));
    GUI_DrawHLine(ym, g->x0, fork);
    GUI_DrawHLine(ym, join, g->x1);
    GUI_DrawVLine(fork, ya, yb);
    GUI_DrawVLine(join, ya, yb);
    GUI_DrawHLine(ya, fork, join);
    GUI_DrawHLine(yb, fork, join);
}

/* the app picks a block's two bitmaps by menu (Split / Path B / Merge are in the EQ menu): replace them */
static void route_icon(int block, int i, int size)
{
    uint32_t id = pos_id(i);
    if (id < SPLIT || id > MERGE)
        return;
    block_bitmap(block, 0, route_icons_on[id - SPLIT][size]);
    block_bitmap(block, 1, route_icons_off[id - SPLIT][size]);
}

/* ---- hooks called from setpos_hook; saved = r0..r3 of the WM_SetWindowPos call (r1 = x, r2 = y) */
void main_pos(int i, int n, int *saved, const uint8_t *screen)
{
    struct par p;
    int on = parallel(n, &MAIN_GEO, &p);
    int dlg = *(const int *)(screen + 0x74);
    if (i == 0) {
        int line = WM_GetDialogItem(dlg, 0x83a);
        if (line)
            (on ? WM_HideWindow : WM_ShowWindow)(line);
    }
    if (i < n)
        route_icon(saved[0], i, 0);
    if (!on || i >= n)
        return;
    place(&MAIN_GEO, &p, i, &saved[1], &saved[2]);
    if (i == n - 1)
        WM_Invalidate(dlg);                             /* repaint the connectors */
}

void edit_pos(int i, int *saved, int block)
{
    int n = EDIT_COUNT;
    const struct geo *g = n < 7 ? &EDIT_BIG : &EDIT_SMALL;
    struct par p;
    if (i >= n)
        return;
    if (block)                                      /* not the cursor */
        route_icon(saved[0], i, n < 7 ? 2 : 1);
    if (parallel(n, g, &p))
        place(g, &p, i, &saved[1], &saved[2]);
}

/* ---- dialog callbacks */
__attribute__((section(".hook.main_cb"), used))
void main_cb(WM_MESSAGE *msg)
{
    MAIN_CB(msg);
    struct par p;
    if (msg->MsgId == WM_PAINT && parallel(chain_count(), &MAIN_GEO, &p))
        connectors(&MAIN_GEO, &p);
}

__attribute__((section(".hook.edit_cb"), used))
void edit_cb(WM_MESSAGE *msg)
{
    int n = EDIT_COUNT;
    const struct geo *g = n < 7 ? &EDIT_BIG : &EDIT_SMALL;
    struct par p;
    if (msg->MsgId == WM_PAINT && parallel(n, g, &p)) {
        connectors(g, &p);                              /* instead of the serial / zig-zag lines */
        return;
    }
    EDIT_CB(msg);
}

/* replaces the first 4 bytes of WM_SetWindowPos (push {r4, r5, r6, lr}; mov r4, r2) */
__attribute__((naked, section(".hook"), used))
void setpos_hook(void)
{
    __asm volatile(
        "ldr ip, =0x8000b37b\n"         /* main screen block */
        "cmp lr, ip\n"
        "beq 2f\n"
        "ldr ip, =0x8000d83b\n"         /* edit screen block */
        "cmp lr, ip\n"
        "beq 3f\n"
        "ldr ip, =0x8000463d\n"         /* edit screen cursor */
        "cmp lr, ip\n"
        "beq 4f\n"
        "1:\n"
        "push {r4, r5, r6, lr}\n"
        "mov r4, r2\n"
        "ldr ip, =0x0000fecb\n"         /* rest of WM_SetWindowPos */
        "bx ip\n"
        "2:\n"
        "push {r0-r3, ip, lr}\n"
        "mov r0, r6\n"                  /* position */
        "mov r1, r7\n"                  /* block count */
        "mov r2, sp\n"                  /* saved r0..r3 */
        "mov r3, r4\n"                  /* main screen state (dialog handle at +0x74) */
        "bl main_pos\n"
        "pop {r0-r3, ip, lr}\n"
        "b 1b\n"
        "3:\n"
        "push {r0-r3, ip, lr}\n"
        "mov r0, r5\n"
        "mov r1, sp\n"
        "movs r2, #1\n"
        "bl edit_pos\n"
        "pop {r0-r3, ip, lr}\n"
        "b 1b\n"
        "4:\n"
        "push {r0-r3, ip, lr}\n"
        "mov r0, r6\n"
        "mov r1, sp\n"
        "movs r2, #0\n"
        "bl edit_pos\n"
        "pop {r0-r3, ip, lr}\n"
        "b 1b\n"
        ".ltorg\n");
}
