; Device ABI regression: COM1 LSR, unknown IN, heartbeat, REP OUTSB, then HLT.
bits 32
org 0x1000
    test edi, edi
    jnz halt
    mov dx, 0x3fd
    in al, dx
    cmp al, 0x20
    jne fail
    mov dx, 0x1234
    in al, dx
    cmp al, 0xff
    jne fail
    out 0x80, al
    mov esi, ok
    mov ecx, ok_end-ok
    mov dx, 0x3f8
    rep outsb
halt:
    hlt
    jmp halt
fail:
    mov dx, 0x3f8
    mov al, '!'
    out dx, al
    jmp halt
ok: db 'I/O ABI OK', 10
ok_end:
