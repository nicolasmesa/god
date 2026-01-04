"""
ARM64 register definitions for KVM.

This module defines register IDs used with KVM_GET_ONE_REG and KVM_SET_ONE_REG.
The IDs encode information about the register type, size, and location.

ARM64 has:
- 31 general-purpose registers (x0-x30)
- Stack pointer (SP)
- Program counter (PC)
- Processor state register (PSTATE)
- Many system registers (SCTLR_EL1, etc.)

For our simple VMM, we focus on the core registers needed to run code.
"""

# ============================================================================
# Register ID Encoding
# ============================================================================
# KVM encodes register IDs as a 64-bit value with several fields:
#
# Bits 63-56: Architecture (0x60 = ARM64)
# Bits 55-52: Size (0x2 = 32-bit, 0x3 = 64-bit)
# Bits 51-32: Type (e.g., 0x10 = core registers)
# Bits 31-0:  Register-specific offset
#
# This encoding allows KVM to handle registers of different types and sizes.

# Architecture identifier for ARM64
KVM_REG_ARM64 = 0x6000000000000000

# Register type: core registers (x0-x30, PC, SP, PSTATE)
KVM_REG_ARM_CORE = 0x0010 << 16

# Size flags
KVM_REG_SIZE_U32 = 0x0020000000000000  # 32-bit register
KVM_REG_SIZE_U64 = 0x0030000000000000  # 64-bit register

# Base for core registers (ARM64 + 64-bit size + core type)
_CORE_REG_BASE = KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE


def _core_reg(offset: int) -> int:
    """
    Create a register ID for a core register.

    The offset is the index into the kernel's kvm_regs structure.
    We multiply by 2 because the structure uses 32-bit slots, but
    we're accessing 64-bit registers.

    Args:
        offset: Index into the register array (0 = x0, 1 = x1, etc.)

    Returns:
        The full 64-bit register ID for KVM.
    """
    return _CORE_REG_BASE | (offset * 2)


# ============================================================================
# General-Purpose Registers (x0-x30)
# ============================================================================
# ARM64 has 31 general-purpose 64-bit registers.
# By convention:
# - x0-x7: Arguments and return values
# - x8: Indirect result location / syscall number
# - x9-x15: Temporary (caller-saved)
# - x16-x17: Intra-procedure call scratch registers
# - x18: Platform register (reserved on some OSes)
# - x19-x28: Callee-saved
# - x29: Frame pointer (FP)
# - x30: Link register (LR) - holds return address

X0 = _core_reg(0)
X1 = _core_reg(1)
X2 = _core_reg(2)
X3 = _core_reg(3)
X4 = _core_reg(4)
X5 = _core_reg(5)
X6 = _core_reg(6)
X7 = _core_reg(7)
X8 = _core_reg(8)
X9 = _core_reg(9)
X10 = _core_reg(10)
X11 = _core_reg(11)
X12 = _core_reg(12)
X13 = _core_reg(13)
X14 = _core_reg(14)
X15 = _core_reg(15)
X16 = _core_reg(16)
X17 = _core_reg(17)
X18 = _core_reg(18)
X19 = _core_reg(19)
X20 = _core_reg(20)
X21 = _core_reg(21)
X22 = _core_reg(22)
X23 = _core_reg(23)
X24 = _core_reg(24)
X25 = _core_reg(25)
X26 = _core_reg(26)
X27 = _core_reg(27)
X28 = _core_reg(28)
X29 = _core_reg(29)  # Frame pointer
X30 = _core_reg(30)  # Link register

# List of all X registers for iteration
X_REGISTERS = [
    X0, X1, X2, X3, X4, X5, X6, X7, X8, X9,
    X10, X11, X12, X13, X14, X15, X16, X17, X18, X19,
    X20, X21, X22, X23, X24, X25, X26, X27, X28, X29, X30,
]


# ============================================================================
# Special Registers
# ============================================================================

# Stack pointer
# There are actually multiple stack pointers (SP_EL0, SP_EL1, etc.)
# but KVM abstracts this for us
SP = _core_reg(31)

# Program counter - address of the next instruction to execute
PC = _core_reg(32)

# Processor state register (PSTATE)
# Contains exception level, condition flags, interrupt masks, etc.
PSTATE = _core_reg(33)


# ============================================================================
# PSTATE Bits
# ============================================================================
# PSTATE contains several fields:
# - Bits 3-0: Exception level and stack pointer selection
# - Bits 9-6: Interrupt and abort masks (D, A, I, F)
# - Bits 31-28: Condition flags (N, Z, C, V)

# Exception level modes (bits 3-0)
# EL0 = user mode, EL1 = kernel mode, EL2 = hypervisor, EL3 = secure monitor
PSTATE_MODE_EL0T = 0x00  # EL0 with SP_EL0 (user mode, user stack)
PSTATE_MODE_EL1T = 0x04  # EL1 with SP_EL0 (kernel mode, user stack)
PSTATE_MODE_EL1H = 0x05  # EL1 with SP_EL1 (kernel mode, kernel stack)

# Interrupt and exception masks (set = masked/disabled)
PSTATE_F = 1 << 6   # FIQ mask (Fast Interrupt Request)
PSTATE_I = 1 << 7   # IRQ mask (Interrupt Request)
PSTATE_A = 1 << 8   # SError/Async abort mask
PSTATE_D = 1 << 9   # Debug mask


# ============================================================================
# System Registers
# ============================================================================
# System registers are accessed via a different encoding than core registers.
# Format: KVM_REG_ARM64 | size | KVM_REG_ARM64_SYSREG | (op0 << 14) | (op1 << 11) | (crn << 7) | (crm << 3) | op2
#
# For ESR_EL1 (Exception Syndrome Register):
#   op0=3, op1=0, crn=5, crm=2, op2=0
# For SCTLR_EL1 (System Control Register):
#   op0=3, op1=0, crn=1, crm=0, op2=0

KVM_REG_ARM64_SYSREG = 0x0013 << 16

def _sysreg(op0: int, op1: int, crn: int, crm: int, op2: int) -> int:
    """Create a system register ID."""
    return (
        KVM_REG_ARM64
        | KVM_REG_SIZE_U64
        | KVM_REG_ARM64_SYSREG
        | (op0 << 14)
        | (op1 << 11)
        | (crn << 7)
        | (crm << 3)
        | op2
    )

# Exception Syndrome Register - tells us what caused an exception
ESR_EL1 = _sysreg(3, 0, 5, 2, 0)

# System Control Register - controls MMU, caches, etc.
SCTLR_EL1 = _sysreg(3, 0, 1, 0, 0)

# Fault Address Register - address that caused a data/instruction abort
FAR_EL1 = _sysreg(3, 0, 6, 0, 0)

# Exception Link Register - return address after exception
ELR_EL1 = _sysreg(3, 0, 4, 0, 1)

# Vector Base Address Register - where exception vectors are
VBAR_EL1 = _sysreg(3, 0, 12, 0, 0)


# ============================================================================
# Register Names (for debugging)
# ============================================================================

REGISTER_NAMES = {
    **{_core_reg(i): f"x{i}" for i in range(31)},
    SP: "sp",
    PC: "pc",
    PSTATE: "pstate",
}


def get_register_name(reg_id: int) -> str:
    """
    Get a human-readable name for a register ID.

    Args:
        reg_id: The KVM register ID.

    Returns:
        A string like "x0", "pc", or "unknown(0x...)" if not recognized.
    """
    return REGISTER_NAMES.get(reg_id, f"unknown(0x{reg_id:x})")
