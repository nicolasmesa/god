"""
Device Tree Blob generation.

This module generates the DTB (Device Tree Blob) that describes
our virtual machine's hardware to the Linux kernel.

We use the fdt (Flattened Device Tree) library for DTB generation.
"""

from dataclasses import dataclass

from god.devices.timer import Timer
from god.vm.layout import (
    GIC_DISTRIBUTOR,
    GIC_REDISTRIBUTOR,
    RAM_BASE,
    UART,
    UART_IRQ,
)


@dataclass
class DTBConfig:
    """
    Configuration for DTB generation.

    Attributes:
        ram_size: Guest RAM size in bytes
        cmdline: Kernel command line
        initrd_start: Initramfs start address (0 if none)
        initrd_end: Initramfs end address (0 if none)
        num_cpus: Number of CPUs
    """

    ram_size: int
    cmdline: str = "console=ttyAMA0 earlycon=pl011,0x09000000"
    initrd_start: int = 0
    initrd_end: int = 0
    num_cpus: int = 1


class DeviceTreeGenerator:
    """
    Generates Device Tree Blobs for our VM.

    This creates a DTB describing:
    - Memory (RAM location and size)
    - CPUs
    - GICv3 interrupt controller
    - ARM architected timer
    - PL011 UART
    - PSCI for power management

    Usage:
        gen = DeviceTreeGenerator()
        config = DTBConfig(ram_size=1024*1024*1024)
        dtb_bytes = gen.generate(config)
    """

    def generate(self, config: DTBConfig) -> bytes:
        """
        Generate a DTB for the given configuration.

        Args:
            config: DTB configuration

        Returns:
            DTB as bytes
        """
        try:
            import fdt
        except ImportError:
            raise ImportError(
                "fdt is required for DTB generation. " "Install it with: uv add fdt"
            )

        # Create root node
        root = fdt.Node("/")
        root.append(fdt.PropStrings("compatible", "linux,dummy-virt"))
        root.append(fdt.PropWords("#address-cells", 2))
        root.append(fdt.PropWords("#size-cells", 2))

        # Add all nodes
        root.append(self._create_aliases())
        root.append(self._create_chosen(config))
        root.append(self._create_memory(config))
        root.append(self._create_cpus(config))
        root.append(self._create_psci())
        root.append(self._create_gic())
        root.append(self._create_timer())
        root.append(self._create_clock())

        # Add SOC node containing platform devices
        root.append(self._create_soc())

        # Create the FDT and convert to bytes
        dt = fdt.FDT()
        dt.root = root

        # Version 17 is the most common/standard DTB version
        return dt.to_dtb(version=17)

    def _create_aliases(self) -> "fdt.Node":
        """Create the aliases node for device naming."""
        import fdt

        aliases = fdt.Node("aliases")
        aliases.append(fdt.PropStrings("serial0", f"/soc/pl011@{UART.base:x}"))
        return aliases

    def _create_chosen(self, config: DTBConfig) -> "fdt.Node":
        """Create the chosen node (boot configuration)."""
        import fdt

        chosen = fdt.Node("chosen")
        chosen.append(fdt.PropStrings("bootargs", config.cmdline))
        chosen.append(fdt.PropStrings("stdout-path", f"/soc/pl011@{UART.base:x}"))

        # Add initramfs location if present
        if config.initrd_start != 0 and config.initrd_end != 0:
            # These are 64-bit addresses stored as two 32-bit values
            chosen.append(
                fdt.PropWords(
                    "linux,initrd-start",
                    config.initrd_start >> 32,
                    config.initrd_start & 0xFFFFFFFF,
                )
            )
            chosen.append(
                fdt.PropWords(
                    "linux,initrd-end",
                    config.initrd_end >> 32,
                    config.initrd_end & 0xFFFFFFFF,
                )
            )

        return chosen

    def _create_memory(self, config: DTBConfig) -> "fdt.Node":
        """Create the memory node."""
        import fdt

        mem = fdt.Node(f"memory@{RAM_BASE:x}")
        mem.append(fdt.PropStrings("device_type", "memory"))

        # reg = <addr_hi addr_lo size_hi size_lo>
        mem.append(
            fdt.PropWords(
                "reg",
                RAM_BASE >> 32,
                RAM_BASE & 0xFFFFFFFF,
                config.ram_size >> 32,
                config.ram_size & 0xFFFFFFFF,
            )
        )

        return mem

    def _create_cpus(self, config: DTBConfig) -> "fdt.Node":
        """Create the cpus node."""
        import fdt

        cpus = fdt.Node("cpus")
        cpus.append(fdt.PropWords("#address-cells", 1))
        cpus.append(fdt.PropWords("#size-cells", 0))

        for i in range(config.num_cpus):
            cpu = fdt.Node(f"cpu@{i}")
            cpu.append(fdt.PropStrings("device_type", "cpu"))
            cpu.append(fdt.PropStrings("compatible", "arm,cortex-a57"))
            cpu.append(fdt.PropWords("reg", i))
            cpu.append(fdt.PropStrings("enable-method", "psci"))
            cpus.append(cpu)

        return cpus

    def _create_psci(self) -> "fdt.Node":
        """Create the PSCI node."""
        import fdt

        psci = fdt.Node("psci")
        psci.append(fdt.PropStrings("compatible", "arm,psci-1.0", "arm,psci-0.2"))
        psci.append(fdt.PropStrings("method", "hvc"))

        return psci

    def _create_gic(self) -> "fdt.Node":
        """Create the GIC interrupt controller node."""
        import fdt

        gic = fdt.Node(f"interrupt-controller@{GIC_DISTRIBUTOR.base:x}")
        gic.append(fdt.PropStrings("compatible", "arm,gic-v3"))
        gic.append(fdt.PropWords("#interrupt-cells", 3))
        gic.append(fdt.Property("interrupt-controller"))

        # reg = <dist_addr dist_size redist_addr redist_size>
        gic.append(
            fdt.PropWords(
                "reg",
                GIC_DISTRIBUTOR.base >> 32,
                GIC_DISTRIBUTOR.base & 0xFFFFFFFF,
                GIC_DISTRIBUTOR.size >> 32,
                GIC_DISTRIBUTOR.size & 0xFFFFFFFF,
                GIC_REDISTRIBUTOR.base >> 32,
                GIC_REDISTRIBUTOR.base & 0xFFFFFFFF,
                GIC_REDISTRIBUTOR.size >> 32,
                GIC_REDISTRIBUTOR.size & 0xFFFFFFFF,
            )
        )

        # Set phandle so other nodes can reference this
        gic.append(fdt.PropWords("phandle", 1))

        return gic

    def _create_timer(self) -> "fdt.Node":
        """Create the ARM timer node."""
        import fdt

        timer_node = fdt.Node("timer")
        timer_node.append(fdt.PropStrings("compatible", "arm,armv8-timer"))

        # Reference the GIC as interrupt parent (phandle 1)
        timer_node.append(fdt.PropWords("interrupt-parent", 1))

        # Timer interrupts (4 PPIs)
        # Format: <type number flags> for each
        timer = Timer()
        interrupts = []
        for ppi in [
            timer.ppi_secure_phys,  # 29 -> DT 13
            timer.ppi_nonsecure_phys,  # 30 -> DT 14
            timer.ppi_virtual,  # 27 -> DT 11
            timer.ppi_hypervisor,  # 26 -> DT 10
        ]:
            dt_num = ppi - 16  # Convert to DT-relative number
            # PPI type = 1, flags = 4 (level triggered)
            interrupts.extend([1, dt_num, 4])

        timer_node.append(fdt.PropWords("interrupts", *interrupts))
        timer_node.append(fdt.Property("always-on"))

        return timer_node

    def _create_soc(self) -> "fdt.Node":
        """Create the SOC node containing platform devices."""
        import fdt

        soc = fdt.Node("soc")
        soc.append(fdt.PropStrings("compatible", "simple-bus"))
        soc.append(fdt.PropWords("#address-cells", 2))
        soc.append(fdt.PropWords("#size-cells", 2))
        soc.append(fdt.Property("ranges"))

        # Add UART inside the soc node
        soc.append(self._create_uart())

        return soc

    def _create_uart(self) -> "fdt.Node":
        """Create the PL011 UART node."""
        import fdt

        uart = fdt.Node(f"pl011@{UART.base:x}")
        uart.append(fdt.PropStrings("compatible", "arm,pl011", "arm,primecell"))
        uart.append(fdt.PropStrings("status", "okay"))
        # PL011 peripheral ID for AMBA identification
        uart.append(fdt.PropWords("arm,primecell-periphid", 0x00241011))

        uart.append(
            fdt.PropWords(
                "reg",
                UART.base >> 32,
                UART.base & 0xFFFFFFFF,
                UART.size >> 32,
                UART.size & 0xFFFFFFFF,
            )
        )

        # Reference the GIC as interrupt parent (phandle 1)
        uart.append(fdt.PropWords("interrupt-parent", 1))

        # UART interrupt: SPI 1, level triggered
        # <type=0(SPI) number=1 flags=4(level)>
        spi_num = UART_IRQ - 32  # Convert to SPI number
        uart.append(fdt.PropWords("interrupts", 0, spi_num, 4))

        # Clock references
        uart.append(fdt.PropStrings("clock-names", "uartclk", "apb_pclk"))
        # Reference the clock phandle (we'll use phandle 2)
        uart.append(fdt.PropWords("clocks", 2, 2))

        return uart

    def _create_clock(self) -> "fdt.Node":
        """Create the fixed clock node for UART."""
        import fdt

        clock = fdt.Node("apb-pclk")
        clock.append(fdt.PropStrings("compatible", "fixed-clock"))
        clock.append(fdt.PropWords("#clock-cells", 0))
        clock.append(fdt.PropWords("clock-frequency", 24000000))  # 24 MHz
        clock.append(fdt.PropWords("phandle", 2))

        return clock
