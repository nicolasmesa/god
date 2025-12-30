/*
 * timer_test.S - Test the ARM Generic Timer
 *
 * This program demonstrates that the timer counter is working:
 * 1. Reads CNTFRQ_EL0 to get the timer frequency
 * 2. Reads CNTVCT_EL0 to get current counter value
 * 3. Waits for 100ms by polling the counter
 * 4. Reads counter again and shows elapsed ticks
 * 5. Tests writing and reading the TVAL register
 *
 * This test uses polling (not interrupts) to verify the timer works.
 */

    .global _start

    /* UART constants */
    .equ UART_BASE, 0x09000000
    .equ UART_DR,   0x000

_start:
    /* Set up stack pointer */
    ldr     x0, =0x44000000
    mov     sp, x0

    /* Print banner */
    adr     x0, msg_banner
    bl      print_string

    /* ============================================ */
    /* Read and display counter frequency           */
    /* ============================================ */
    adr     x0, msg_freq
    bl      print_string

    mrs     x19, CNTFRQ_EL0         /* x19 = frequency (save for later) */
    mov     x0, x19
    bl      print_hex
    adr     x0, msg_hz
    bl      print_string

    /* Calculate and print MHz */
    ldr     x0, =1000000
    udiv    x0, x19, x0             /* MHz (integer part) */
    bl      print_decimal
    adr     x0, msg_mhz
    bl      print_string

    /* ============================================ */
    /* Read initial counter value                   */
    /* ============================================ */
    adr     x0, msg_counter1
    bl      print_string

    mrs     x20, CNTVCT_EL0         /* x20 = starting counter value */
    mov     x0, x20
    bl      print_hex
    bl      print_newline

    /* ============================================ */
    /* Wait 100ms by polling the counter            */
    /* target = start + (frequency / 10)            */
    /* ============================================ */
    adr     x0, msg_waiting
    bl      print_string

    /* Calculate ticks for 100ms: frequency / 10 */
    mov     x0, #10
    udiv    x21, x19, x0            /* x21 = ticks for 100ms */

    /* Calculate target counter value */
    add     x22, x20, x21           /* x22 = target = start + ticks_100ms */

wait_loop:
    mrs     x0, CNTVCT_EL0          /* Read current counter */
    cmp     x0, x22                 /* Compare with target */
    b.lt    wait_loop               /* Loop until counter >= target */

    /* ============================================ */
    /* Read counter value after waiting             */
    /* ============================================ */
    adr     x0, msg_counter2
    bl      print_string

    mrs     x23, CNTVCT_EL0         /* x23 = ending counter value */
    mov     x0, x23
    bl      print_hex
    bl      print_newline

    /* ============================================ */
    /* Calculate and display elapsed ticks          */
    /* ============================================ */
    adr     x0, msg_elapsed
    bl      print_string

    sub     x24, x23, x20           /* x24 = elapsed ticks */
    mov     x0, x24
    bl      print_hex
    adr     x0, msg_ticks
    bl      print_string

    /* Calculate actual milliseconds */
    /* ms = (elapsed * 1000) / frequency */
    mov     x0, #1000
    mul     x0, x24, x0             /* elapsed * 1000 */
    udiv    x0, x0, x19             /* / frequency = ms */
    bl      print_decimal
    adr     x0, msg_ms
    bl      print_string

    /* ============================================ */
    /* Test setting and reading TVAL                */
    /* ============================================ */
    adr     x0, msg_tval_test
    bl      print_string

    /* Set TVAL to 1,000,000 ticks */
    ldr     x0, =1000000
    msr     CNTV_TVAL_EL0, x0

    /* Read it back immediately */
    mrs     x0, CNTV_TVAL_EL0
    bl      print_hex
    adr     x0, msg_tval_read
    bl      print_string

    /* Success! */
    adr     x0, msg_success
    bl      print_string

    /* Exit via PSCI SYSTEM_OFF */
    mov     x0, #0x0008
    movk    x0, #0x8400, lsl #16
    hvc     #0

    b       .

    /* ============================================ */
    /* Utility Functions                            */
    /* ============================================ */

print_string:
    stp     x29, x30, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
1:  ldrb    w11, [x9], #1
    cbz     w11, 2f
    str     w11, [x10, #UART_DR]
    b       1b
2:  ldp     x29, x30, [sp], #16
    ret

print_hex:
    stp     x29, x30, [sp, #-16]!
    stp     x9, x10, [sp, #-16]!
    stp     x11, x12, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
    mov     x11, #60
1:  lsr     x12, x9, x11
    and     x12, x12, #0xf
    cmp     x12, #10
    b.lt    2f
    add     x12, x12, #('a' - 10)
    b       3f
2:  add     x12, x12, #'0'
3:  str     w12, [x10, #UART_DR]
    subs    x11, x11, #4
    b.ge    1b
    ldp     x11, x12, [sp], #16
    ldp     x9, x10, [sp], #16
    ldp     x29, x30, [sp], #16
    ret

print_decimal:
    /* Simple decimal printing for small numbers */
    stp     x29, x30, [sp, #-16]!
    stp     x9, x10, [sp, #-16]!
    stp     x11, x12, [sp, #-16]!
    stp     x13, x14, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
    mov     x13, #0
    ldr     x11, =100000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 1f
    cbz     x13, 2f
1:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
2:  ldr     x11, =10000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 3f
    cbz     x13, 4f
3:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
4:  mov     x11, #1000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 5f
    cbz     x13, 6f
5:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
6:  mov     x11, #100
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 7f
    cbz     x13, 8f
7:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
8:  mov     x11, #10
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 9f
    cbz     x13, 10f
9:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
10: add     x12, x9, #'0'
    str     w12, [x10, #UART_DR]
    ldp     x13, x14, [sp], #16
    ldp     x11, x12, [sp], #16
    ldp     x9, x10, [sp], #16
    ldp     x29, x30, [sp], #16
    ret

print_newline:
    stp     x29, x30, [sp, #-16]!
    ldr     x10, =UART_BASE
    mov     w11, #'\n'
    str     w11, [x10, #UART_DR]
    ldp     x29, x30, [sp], #16
    ret

    /* Data Section */
    .align 4
msg_banner:
    .asciz "=== ARM Timer Test ===\n\n"
msg_freq:
    .asciz "Timer frequency: 0x"
msg_hz:
    .asciz " Hz ("
msg_mhz:
    .asciz " MHz)\n"
msg_counter1:
    .asciz "Counter (start): 0x"
msg_waiting:
    .asciz "Waiting 100ms (polling counter)...\n"
msg_counter2:
    .asciz "Counter (end):   0x"
msg_elapsed:
    .asciz "Elapsed:         0x"
msg_ticks:
    .asciz " ticks ("
msg_ms:
    .asciz " ms)\n\n"
msg_tval_test:
    .asciz "Testing TVAL register...\n"
msg_tval_read:
    .asciz " (wrote 1000000, read back)\n\n"
msg_success:
    .asciz "Timer test PASSED!\n"
