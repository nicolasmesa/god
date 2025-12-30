"""
ARM Generic Timer configuration.

The ARM Generic Timer is built into every ARM64 CPU. Unlike the UART (which
we emulate via MMIO) or the GIC (which KVM emulates), the timer is tightly
coupled to the CPU and accessed via system registers (MRS/MSR instructions).

KVM handles timer emulation automatically:
- Guest reads/writes to timer system registers are trapped and emulated
- Timer interrupts are delivered through the GIC
- The counter frequency matches the host hardware

Our only job is to describe the timer in the Device Tree so Linux knows:
1. The timer exists (compatible = "arm,armv8-timer")
2. Which interrupts it uses (the four timer PPIs)

The frequency is NOT specified in Device Tree—Linux reads CNTFRQ_EL0 directly.
"""

# Timer PPI numbers (as seen by the GIC)
# These are Private Peripheral Interrupts - each CPU has its own
TIMER_PPI_SECURE_PHYS = 29      # Secure physical timer (EL3)
TIMER_PPI_NONSECURE_PHYS = 30   # Non-secure physical timer (EL1)
TIMER_PPI_VIRTUAL = 27          # Virtual timer (what Linux uses in VMs)
TIMER_PPI_HYPERVISOR = 26       # Hypervisor timer (EL2)

# Device Tree uses PPI-relative numbers (subtract 16 from raw PPI)
# This is because PPIs are GIC interrupts 16-31, and DT specifies
# the index within the PPI range, not the absolute interrupt number
_DT_PPI_OFFSET = 16


class Timer:
    """
    ARM Generic Timer configuration.

    This class doesn't emulate anything—KVM handles the timer. It provides:
    1. Constants for timer interrupt numbers
    2. Device Tree properties for Linux to discover the timer

    Usage:
        timer = Timer()
        dt_props = timer.get_device_tree_props()
        # Add dt_props to your Device Tree generation
    """

    def __init__(self):
        """Create timer configuration."""
        # Store the PPI numbers for reference
        self.ppi_secure_phys = TIMER_PPI_SECURE_PHYS
        self.ppi_nonsecure_phys = TIMER_PPI_NONSECURE_PHYS
        self.ppi_virtual = TIMER_PPI_VIRTUAL
        self.ppi_hypervisor = TIMER_PPI_HYPERVISOR

    def get_device_tree_props(self) -> dict:
        """
        Get Device Tree properties for the timer node.

        Returns a dict that can be used with a Device Tree library:

            timer_node = fdt.add_node(root, "timer")
            for key, value in timer.get_device_tree_props().items():
                fdt.set_property(timer_node, key, value)

        The interrupts are specified as:
            <type> <number> <flags>

        Where:
            type = 1 for PPI
            number = PPI number - 16 (Device Tree convention)
            flags = 0x04 for level-sensitive, active-low
        """
        # Convert raw PPI numbers to Device Tree format
        def ppi_to_dt(ppi: int) -> tuple[int, int, int]:
            return (1, ppi - _DT_PPI_OFFSET, 0x04)

        return {
            "compatible": "arm,armv8-timer",
            "interrupts": [
                *ppi_to_dt(self.ppi_secure_phys),      # Secure physical
                *ppi_to_dt(self.ppi_nonsecure_phys),   # Non-secure physical
                *ppi_to_dt(self.ppi_virtual),          # Virtual (Linux uses this)
                *ppi_to_dt(self.ppi_hypervisor),       # Hypervisor
            ],
            "always-on": True,
        }

    def __repr__(self) -> str:
        return (
            f"Timer(virtual_ppi={self.ppi_virtual}, "
            f"phys_ppi={self.ppi_nonsecure_phys})"
        )
