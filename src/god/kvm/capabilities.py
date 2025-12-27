"""
KVM capability checking.

This module queries KVM for its supported capabilities and provides
human-readable descriptions of each.
"""

from dataclasses import dataclass

from .system import KVMSystem


@dataclass
class Capability:
    """
    Describes a KVM capability.

    Attributes:
        name: The constant name (e.g., "KVM_CAP_MAX_VCPUS")
        number: The capability number used with KVM_CHECK_EXTENSION
        description: Human-readable description of what this capability does
        value: The value returned by KVM (None if not queried yet)
    """

    name: str
    number: int
    description: str
    value: int | None = None


# All capabilities we care about, with descriptions
CAPABILITIES = [
    Capability(
        name="KVM_CAP_NR_MEMSLOTS",
        number=10,
        description="Maximum number of memory slots per VM",
    ),
    Capability(
        name="KVM_CAP_MAX_VCPUS",
        number=66,
        description="Maximum number of vCPUs per VM",
    ),
    Capability(
        name="KVM_CAP_MAX_VCPU_ID",
        number=128,
        description="Maximum vCPU ID allowed",
    ),
    Capability(
        name="KVM_CAP_ONE_REG",
        number=70,
        description="Supports getting/setting individual registers",
    ),
    Capability(
        name="KVM_CAP_ARM_VM_IPA_SIZE",
        number=165,
        description="Maximum Intermediate Physical Address (IPA) size in bits (ARM64)",
    ),
    Capability(
        name="KVM_CAP_ARM_PSCI_0_2",
        number=102,
        description="Supports PSCI 0.2 (Power State Coordination Interface) for CPU on/off",
    ),
    Capability(
        name="KVM_CAP_ARM_PMU_V3",
        number=126,
        description="Supports ARM Performance Monitor Unit v3",
    ),
    Capability(
        name="KVM_CAP_IRQCHIP",
        number=0,
        description="Supports in-kernel interrupt controller (GIC)",
    ),
    Capability(
        name="KVM_CAP_IOEVENTFD",
        number=36,
        description="Supports IOEVENTFD (efficient doorbell mechanism)",
    ),
    Capability(
        name="KVM_CAP_IRQFD",
        number=32,
        description="Supports IRQFD (efficient interrupt injection)",
    ),
    Capability(
        name="KVM_CAP_ARM_EL1_32BIT",
        number=105,
        description="Supports 32-bit guests at EL1 (AArch32 mode)",
    ),
]


def query_capabilities(kvm: KVMSystem) -> list[Capability]:
    """
    Query all known capabilities from KVM.

    Args:
        kvm: An open KVMSystem instance.

    Returns:
        List of Capability objects with values filled in.
    """
    results = []
    for cap in CAPABILITIES:
        value = kvm.check_extension(cap.number)
        results.append(
            Capability(
                name=cap.name,
                number=cap.number,
                description=cap.description,
                value=value,
            )
        )
    return results


def format_capabilities(capabilities: list[Capability]) -> str:
    """
    Format capabilities for display.

    Args:
        capabilities: List of queried capabilities.

    Returns:
        Formatted string suitable for printing.
    """
    lines = []

    # Find the longest name for alignment
    max_name_len = max(len(cap.name) for cap in capabilities)

    for cap in capabilities:
        # Format the value
        if cap.value is None:
            value_str = "not queried"
        elif cap.value == 0:
            value_str = "not supported"
        else:
            value_str = str(cap.value)

        # Build the line
        name_padded = cap.name.ljust(max_name_len)
        lines.append(f"  {name_padded}  = {value_str}")
        lines.append(f"    └─ {cap.description}")

    return "\n".join(lines)
