"""
Command-line interface for the god VMM.

This module defines all CLI commands using the Typer library.
"""

from typing import Annotated

import typer

from god import __version__

app = typer.Typer(
    name="god",
    help="god - A Virtual Machine Monitor built from scratch",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        typer.echo(f"god {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", "-v", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """god - A Virtual Machine Monitor built from scratch."""
    pass


# Create a subcommand group for KVM-related commands
kvm_app = typer.Typer(help="KVM-related commands")
app.add_typer(kvm_app, name="kvm")


@kvm_app.command("info")
def kvm_info() -> None:
    """
    Display KVM system information.

    Shows the KVM API version, vCPU mmap size, and all supported capabilities.
    This is useful for verifying that KVM is working correctly and understanding
    what features are available on this system.
    """
    from god.kvm.capabilities import format_capabilities, query_capabilities
    from god.kvm.system import KVMError, KVMSystem

    try:
        with KVMSystem() as kvm:
            print("KVM System Information")
            print("=" * 60)
            print()
            print("Device:            /dev/kvm")
            print(f"API Version:       {kvm.api_version} (expected: 12)")
            print(f"vCPU mmap size:    {kvm.get_vcpu_mmap_size()} bytes")
            print()
            print("Capabilities:")
            print("-" * 60)
            capabilities = query_capabilities(kvm)
            print(format_capabilities(capabilities))
            print()
            print("KVM is ready!")

    except KVMError as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1) from e


@app.command("test-vm")
def test_vm(
    ram_mb: int = typer.Option(
        1024,
        "--ram",
        "-r",
        help="RAM size in megabytes",
    ),
):
    """
    Test VM creation and memory setup.

    Creates a VM, allocates memory, writes some data, reads it back,
    and verifies everything works.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vm.memory import MemoryError

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")
    print()

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                print(f"VM created: {vm}")
                print()
                print("Memory slots:")
                for slot in vm.memory.slots:
                    print(f"  {slot}")
                print()

                # Write some data to memory
                test_address = vm.ram_base
                test_data = b"Hello from the VMM!"

                print(f"Writing test data to 0x{test_address:08x}...")
                vm.memory.write(test_address, test_data)

                # Read it back
                print(f"Reading back from 0x{test_address:08x}...")
                read_back = vm.memory.read(test_address, len(test_data))

                if read_back == test_data:
                    print(f"Success! Read: {read_back}")
                else:
                    print(f"MISMATCH! Wrote: {test_data}, Read: {read_back}")
                    raise typer.Exit(code=1)

                print()
                print("VM test passed!")

    except (KVMError, VMError, MemoryError) as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1)


# Create a subcommand group for build commands
build_app = typer.Typer(help="Build kernel and userspace components")
app.add_typer(build_app, name="build")


@build_app.command("kernel")
def build_kernel(
    version: str = typer.Option("6.12", "--version", "-v", help="Kernel version"),
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
    configure_only: bool = typer.Option(
        False, "--configure", help="Only configure, don't build"
    ),
):
    """
    Download and build the Linux kernel.

    Downloads the specified kernel version, configures it for our VMM,
    and builds the Image file.
    """
    from god.build import KernelBuilder

    builder = KernelBuilder(work_dir)
    builder.download(version)
    builder.configure(minimal=True)

    if not configure_only:
        image_path = builder.build()
        print(f"\nKernel built successfully: {image_path}")


@build_app.command("kernel-clean")
def build_kernel_clean(
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
    full: bool = typer.Option(
        False, "--full", "-f", help="Full clean (mrproper) - removes .config too"
    ),
):
    """
    Clean kernel build artifacts.

    Use --full for mrproper (needed if build is corrupted).
    """
    from god.build import KernelBuilder

    builder = KernelBuilder(work_dir)
    if full:
        builder.mrproper()
    else:
        builder.clean()


@build_app.command("busybox")
def build_busybox(
    version: str = typer.Option("1_36_1", "--version", "-v", help="BusyBox version"),
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
):
    """
    Download and build BusyBox.

    Downloads the specified BusyBox version and builds it with static linking.
    """
    from god.build import BusyBoxBuilder

    builder = BusyBoxBuilder(work_dir)
    builder.download(version)
    builder.configure()
    binary_path = builder.build()
    print(f"\nBusyBox built successfully: {binary_path}")


@build_app.command("initramfs")
def build_initramfs(
    busybox: str = typer.Option(None, "--busybox", "-b", help="Path to busybox binary"),
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
    compress: bool = typer.Option(False, "--compress", "-z", help="Compress with gzip"),
):
    """
    Create an initramfs image.

    Creates a minimal initramfs with BusyBox. If no BusyBox path is provided,
    uses the one in the build directory.
    """
    from pathlib import Path

    from god.build import InitramfsBuilder

    builder = InitramfsBuilder(work_dir)

    # Find BusyBox
    if busybox:
        busybox_path = Path(busybox)
    else:
        busybox_path = Path(work_dir) / "busybox" / "busybox"
        if not busybox_path.exists():
            print(f"BusyBox not found at {busybox_path}")
            print("Run 'god build busybox' first or specify --busybox path")
            raise typer.Exit(code=1)

    builder.create_structure()
    builder.install_busybox(busybox_path)
    builder.create_init()
    cpio_path = builder.pack(compress=compress)
    print(f"\nInitramfs created: {cpio_path}")


@build_app.command("all")
def build_all(
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
):
    """
    Build everything needed to boot Linux.

    Downloads and builds the kernel, BusyBox, and creates an initramfs.
    """
    from god.build import BusyBoxBuilder, InitramfsBuilder, KernelBuilder

    print("=" * 60)
    print("Building all components")
    print("=" * 60)
    print()

    # Build kernel
    print("Step 1: Building Linux kernel")
    print("-" * 40)
    kernel_builder = KernelBuilder(work_dir)
    kernel_builder.download()
    kernel_builder.configure()
    kernel_path = kernel_builder.build()
    print()

    # Build BusyBox
    print("Step 2: Building BusyBox")
    print("-" * 40)
    busybox_builder = BusyBoxBuilder(work_dir)
    busybox_builder.download()
    busybox_builder.configure()
    busybox_path = busybox_builder.build()
    print()

    # Create initramfs
    print("Step 3: Creating initramfs")
    print("-" * 40)
    initramfs_builder = InitramfsBuilder(work_dir)
    initramfs_builder.create_structure()
    initramfs_builder.install_busybox(busybox_path)
    initramfs_builder.create_init()
    cpio_path = initramfs_builder.pack()
    print()

    print("=" * 60)
    print("Build complete!")
    print("=" * 60)
    print()
    print(f"Kernel:    {kernel_path}")
    print(f"Initramfs: {cpio_path}")
    print()
    print("To boot Linux:")
    print(f"  god boot {kernel_path} --initrd {cpio_path}")


@app.command("boot")
def boot_linux(
    kernel: str = typer.Argument(..., help="Path to kernel Image"),
    initrd: str = typer.Option(
        None, "--initrd", "-i", help="Path to initramfs (cpio or cpio.gz)"
    ),
    cmdline: str = typer.Option(
        "console=ttyAMA0 earlycon=pl011,0x09000000",
        "--cmdline",
        "-c",
        help="Kernel command line",
    ),
    ram_mb: int = typer.Option(1024, "--ram", "-r", help="RAM size in megabytes"),
    dtb: str = typer.Option(
        None,
        "--dtb",
        "-d",
        help="Path to custom DTB file (optional, generates one if not provided)",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Show debug output (MMIO accesses, exit stats)",
    ),
    interactive: bool = typer.Option(
        True,
        "--interactive/--no-interactive",
        help="Enable interactive console (stdin input to guest)",
    ),
):
    """
    Boot a Linux kernel.

    Loads the kernel and optional initramfs into the VM and starts execution.
    A Device Tree is generated automatically unless a custom one is provided.

    By default, interactive mode is enabled, allowing you to type commands
    in the guest shell. Use --no-interactive for non-interactive boot.

    Examples:
        god boot Image --initrd initramfs.cpio
        god boot Image -i rootfs.cpio.gz -c "console=ttyAMA0 debug"
        god boot Image --dtb custom.dtb --ram 2048 --no-interactive
    """
    from pathlib import Path

    from god.boot import BootLoader, DeviceTreeGenerator, DTBConfig, KernelError
    from god.boot.kernel import KernelImage
    from god.devices import DeviceRegistry, PL011UART
    from god.kvm.system import KVMError, KVMSystem
    from god.vcpu.runner import RunnerError, VMRunner
    from god.vm.layout import RAM_BASE
    from god.vm.vm import VirtualMachine, VMError

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Booting Linux with {ram_mb} MB RAM")
    print(f"Kernel: {kernel}")
    if initrd:
        print(f"Initrd: {initrd}")
    print(f"Command line: {cmdline}")
    print()

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                # Set up devices
                devices = DeviceRegistry()
                uart = PL011UART()
                devices.register(uart)

                # Create runner (sets up GIC)
                runner = VMRunner(vm, kvm, devices)
                vcpu = runner.create_vcpu()

                # Create boot loader
                loader = BootLoader(vm.memory, ram_bytes)

                # Generate or load DTB
                if dtb:
                    # Use provided DTB
                    with open(dtb, "rb") as f:
                        dtb_data = f.read()
                    print(f"Using custom DTB: {dtb}")
                else:
                    # Generate DTB (we need to know initrd location first,
                    # so we do a two-pass approach)
                    # First, load kernel to get its size
                    kernel_img = KernelImage.load(kernel)
                    kernel_addr = RAM_BASE + kernel_img.text_offset
                    kernel_end = kernel_addr + len(kernel_img.data)
                    # Place initrd high in RAM (128MB offset) to avoid conflicts
                    initrd_addr = RAM_BASE + (128 * 1024 * 1024)
                    initrd_addr = (initrd_addr + 0xFFF) & ~0xFFF

                    # Calculate initrd end if we have one
                    initrd_start = 0
                    initrd_end = 0
                    if initrd:
                        initrd_size = Path(initrd).stat().st_size
                        initrd_start = initrd_addr
                        initrd_end = initrd_addr + initrd_size

                    # Generate DTB with initrd info
                    dtb_gen = DeviceTreeGenerator()
                    dtb_config = DTBConfig(
                        ram_size=ram_bytes,
                        cmdline=cmdline,
                        initrd_start=initrd_start,
                        initrd_end=initrd_end,
                    )
                    dtb_data = dtb_gen.generate(dtb_config)
                    print("Generated Device Tree")
                    if initrd:
                        print(f"  DTB initrd_start=0x{initrd_start:08x}")
                        print(f"  DTB initrd_end=0x{initrd_end:08x}")
                        # Verify DTB by parsing it back
                        import fdt
                        dt = fdt.parse_dtb(dtb_data)
                        chosen = dt.get_node("/chosen")
                        if chosen:
                            start_prop = chosen.get_property("linux,initrd-start")
                            end_prop = chosen.get_property("linux,initrd-end")
                            if start_prop and end_prop:
                                start_val = (start_prop.data[0] << 32) | start_prop.data[1]
                                end_val = (end_prop.data[0] << 32) | end_prop.data[1]
                                print(f"  Parsed back from DTB: start=0x{start_val:08x}, end=0x{end_val:08x}")

                # Load everything
                boot_info = loader.load(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    dtb_data=dtb_data,
                )

                # Verify DTB addresses match actual load addresses
                if initrd and not dtb:
                    if boot_info.initrd_addr != initrd_start:
                        print(f"WARNING: DTB initrd_start (0x{initrd_start:08x}) != "
                              f"actual load addr (0x{boot_info.initrd_addr:08x})")
                    if boot_info.initrd_end != initrd_end:
                        print(f"WARNING: DTB initrd_end (0x{initrd_end:08x}) != "
                              f"actual end addr (0x{boot_info.initrd_end:08x})")

                # Set up vCPU for boot
                loader.setup_vcpu(vcpu, boot_info)

                print()
                print("=" * 60)
                print("Starting Linux...")
                print("=" * 60)
                print()

                # Run!
                stats = runner.run(max_exits=10_000_000, quiet=not debug, interactive=interactive)

                print()
                print("=" * 60)
                if stats.get("hlt"):
                    print("VM halted")
                else:
                    print(f"VM stopped: {stats.get('exit_reason')}")
                print(f"Total exits: {stats.get('exits')}")

    except (KVMError, VMError, RunnerError, KernelError) as e:
        print(f"\nError: {e}")
        raise typer.Exit(code=1)
    except FileNotFoundError as e:
        print(f"\nFile not found: {e}")
        raise typer.Exit(code=1)


@app.command("run")
def run_binary(
    binary: str = typer.Argument(..., help="Path to the binary to run"),
    entry: str = typer.Option(
        "0x40080000",
        "--entry",
        "-e",
        help="Entry point address (hex)",
    ),
    ram_mb: int = typer.Option(
        64,
        "--ram",
        "-r",
        help="RAM size in megabytes",
    ),
    with_uart: bool = typer.Option(
        True,
        "--uart/--no-uart",
        help="Enable PL011 UART for serial console output",
    ),
):
    """
    Run a binary in the VM.

    Loads the binary at the entry point address and runs until it halts.
    By default, the PL011 UART is enabled so guest code can print output.

    Example:
        god run tests/guest_code/hello.bin
        god run my_kernel.bin --entry 0x40000000 --ram 128
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vcpu.runner import VMRunner, RunnerError
    from god.vcpu import registers
    from god.devices import DeviceRegistry, PL011UART

    # Parse entry point (support hex with 0x prefix or decimal)
    entry_point = int(entry, 16) if entry.startswith("0x") else int(entry)

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                # Set up device registry
                devices = DeviceRegistry()

                if with_uart:
                    uart = PL011UART()
                    devices.register(uart)

                runner = VMRunner(vm, kvm, devices)
                vcpu = runner.create_vcpu()

                # Set initial register state
                # PC = entry point (where code starts)
                vcpu.set_pc(entry_point)

                # SP = top of RAM (stack grows down)
                stack_top = vm.ram_base + vm.ram_size
                vcpu.set_sp(stack_top)

                # PSTATE = EL1h with all interrupts masked
                # EL1h means: Exception Level 1, using SP_EL1
                # This is "kernel mode" on ARM64
                pstate = (
                    registers.PSTATE_MODE_EL1H |  # Exception Level 1, SP_EL1
                    registers.PSTATE_A |           # Mask async aborts
                    registers.PSTATE_I |           # Mask IRQs
                    registers.PSTATE_F             # Mask FIQs
                )
                vcpu.set_pstate(pstate)

                print(f"PC = 0x{entry_point:016x}")
                print(f"SP = 0x{stack_top:016x}")
                print()

                # Load the binary
                runner.load_binary(binary, entry_point)

                # Run!
                print("=" * 60)
                print("Guest output:")
                print("-" * 60)

                stats = runner.run(quiet=True)

                print("-" * 60)
                print()
                print(f"Guest {'halted' if stats['hlt'] else 'stopped'} "
                      f"after {stats['exits']} exits")

    except (KVMError, VMError, RunnerError) as e:
        print(f"\nError: {e}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
