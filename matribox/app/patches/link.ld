/* patch code in the ITCM left free by the original app (image ends at 0x53cb9; effect modules start at 0x58000) */
MEMORY
{
    FREE (rx) : ORIGIN = 0x53cc0, LENGTH = 0x58000 - 0x53cc0
}

ENTRY(__patch_start)
PHDRS { patch PT_LOAD; }

SECTIONS
{
    .patch : {
        __patch_start = .;
        KEEP(*(.hook))
        KEEP(*(.hook.*))
        *(.text*)
        *(.rodata*)
    } > FREE :patch

    /* the patch has no initialised or zeroed data of its own */
    .data : { *(.data*) *(.bss*) *(COMMON) } > FREE :patch
    ASSERT(SIZEOF(.data) == 0, "patch code must not use static data")

    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
