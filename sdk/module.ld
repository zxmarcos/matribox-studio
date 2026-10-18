/* Linker script for Matribox II Pro effect modules (see matribox_module.h). */
ENTRY(mb_module_header)

SECTIONS
{
  ER_IROM 0x00000000 : ALIGN(8)
  {
    KEEP(*(.module_header))
    *(.text .text.*)
  }

  ER_SROM 0x10000000 : ALIGN(8)
  {
    __srom_base = .;
    *(.rodata .rodata.*)
    LONG(0)            /* never empty: the module header points at __srom_base */
  }

  /* listed before ER_IRAM so .bss.sram* is claimed here first */
  ER_SRAM 0x30000000 (NOLOAD) : ALIGN(8)
  {
    *(.bss.sram .bss.sram.*)
  }

  ER_IRAM 0x20000000 : ALIGN(8)
  {
    *(.data .data.*)
    *(.bss .bss.* COMMON)
  }

  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.note*) *(.init_array*) *(.fini_array*) }
}
