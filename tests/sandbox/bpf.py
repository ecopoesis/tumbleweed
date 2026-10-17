"""A classic-BPF interpreter for seccomp programs: what a filter decides, on any architecture.

The kernel only ever shows a program the architecture it runs on. This runs the exported program
on a hand-made `struct seccomp_data` instead, so the architecture check can be proved for the
ABIs a test host cannot execute (32-bit ARM on aarch64), and every rule can be probed without
making the syscall. It supports the instructions libseccomp emits and refuses any other, so a
libseccomp that starts using one fails loudly instead of being half understood.
"""

import struct
from collections.abc import Sequence
from typing import Final

ALLOW: Final = 0x7FFF0000
KILL_PROCESS: Final = 0x80000000
ERRNO: Final = 0x00050000
ACTION_MASK: Final = 0xFFFF0000
X32_SYSCALL_BIT: Final = 0x40000000

# AUDIT_ARCH_* (linux/audit.h): what `seccomp_data.arch` holds.
AUDIT_X86_64: Final = 0xC000003E
AUDIT_I386: Final = 0x40000003
AUDIT_AARCH64: Final = 0xC00000B7
AUDIT_ARM: Final = 0x40000028
AUDIT_ARCHES: Final = {"x86_64": AUDIT_X86_64, "aarch64": AUDIT_AARCH64}

_LD_W_ABS: Final = 0x20
_LD_W_IMM: Final = 0x00
_LD_W_MEM: Final = 0x60
_ST: Final = 0x02
_ALU_AND_K: Final = 0x54
_ALU_OR_K: Final = 0x44
_ALU_RSH_K: Final = 0x74
_JA: Final = 0x05
_JEQ_K: Final = 0x15
_JGT_K: Final = 0x25
_JGE_K: Final = 0x35
_JSET_K: Final = 0x45
_RET_K: Final = 0x06
_RET_A: Final = 0x16
_TAX: Final = 0x07
_TXA: Final = 0x87


def errno_action(code: int) -> int:
    return ERRNO | code


def decide(program: bytes, *, nr: int, arch: int, args: Sequence[int] = ()) -> int:
    """The action (`SECCOMP_RET_*`, data included) the program returns for this syscall."""
    padded = [*args, *([0] * (6 - len(args)))]
    data = struct.pack("<IIQ6Q", nr & 0xFFFFFFFF, arch, 0, *padded)
    instructions: list[tuple[int, int, int, int]] = []
    for offset in range(0, len(program), 8):
        code, jump_true, jump_false, k = struct.unpack_from("<HBBI", program, offset)
        instructions.append((int(code), int(jump_true), int(jump_false), int(k)))
    accumulator: int = 0
    index_register: int = 0
    memory = [0] * 16
    pc = 0
    for _ in range(100_000):
        code, jt, jf, k = instructions[pc]
        pc += 1
        if code == _LD_W_ABS:
            accumulator = int(struct.unpack_from("<I", data, k)[0])
        elif code == _LD_W_IMM:
            accumulator = k
        elif code == _LD_W_MEM:
            accumulator = memory[k]
        elif code == _ST:
            memory[k] = accumulator
        elif code == _ALU_AND_K:
            accumulator &= k
        elif code == _ALU_OR_K:
            accumulator |= k
        elif code == _ALU_RSH_K:
            accumulator >>= k
        elif code == _TAX:
            index_register = accumulator
        elif code == _TXA:
            accumulator = index_register
        elif code == _JA:
            pc += k
        elif code in {_JEQ_K, _JGT_K, _JGE_K, _JSET_K}:
            taken = {
                _JEQ_K: accumulator == k,
                _JGT_K: accumulator > k,
                _JGE_K: accumulator >= k,
                _JSET_K: bool(accumulator & k),
            }[code]
            pc += jt if taken else jf
        elif code == _RET_K:
            return k
        elif code == _RET_A:
            return accumulator
        else:
            raise AssertionError(f"unsupported BPF instruction {code:#x}")
    raise AssertionError("the BPF program did not end")
