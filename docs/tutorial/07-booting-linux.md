# Chapter 7: Booting Linux - The Grand Finale

This is the culmination of everything we've built. In this chapter, we'll boot a real Linux kernel in our virtual machine!

## ARM64 CPU Boot State

### Exception Levels at Boot

When we start the vCPU, we need to configure it correctly:

**Exception Level**: EL1 (kernel mode)
- Linux expects to start at EL1
- We set PSTATE to EL1h (using SP_EL1)

**MMU**: Disabled
- Linux enables the MMU itself during early boot
- SCTLR_EL1.M bit should be 0

**Caches**: Disabled (or enabled, Linux handles either)
- SCTLR_EL1.C and SCTLR_EL1.I bits

**Interrupts**: Masked
- PSTATE.I, PSTATE.F, PSTATE.A bits set

### Initial Register State

The ARM64 Linux boot protocol defines:

| Register | Contents |
|----------|----------|
| x0 | Physical address of Device Tree Blob (DTB) |
| x1 | 0 (reserved) |
| x2 | 0 (reserved) |
| x3 | 0 (reserved) |
| PC | Kernel entry point |
| SP | Not used initially (kernel sets up its own) |

That's it! Just set x0 to the DTB address and PC to the kernel entry.

## The Linux Kernel Image Format

### Getting the Kernel

Linux compiles to a file called `Image` (uncompressed) or `Image.gz` (compressed). For simplicity, we use the uncompressed `Image`.

To get a kernel:
```bash
# Option 1: Download a pre-built kernel
# (From distro packages or kernel.org)

# Option 2: Build it yourself
make ARCH=arm64 defconfig
make ARCH=arm64 Image
# Result is in arch/arm64/boot/Image
```

### The ARM64 Kernel Header

The first 64 bytes of an ARM64 Image contain a header:

```c
struct arm64_image_header {
    __le32 code0;           // Executable code (branch instruction)
    __le32 code1;           // Executable code
    __le64 text_offset;     // Image load offset (typically 0x80000)
    __le64 image_size;      // Effective Image size
    __le64 flags;           // Kernel flags
    __le64 res2;            // Reserved
    __le64 res3;            // Reserved
    __le64 res4;            // Reserved
    __le32 magic;           // Magic number: 0x644d5241 ("ARM\x64")
    __le32 res5;            // Reserved (PE header offset for UEFI)
};
```

Key fields:
- **magic**: Must be `0x644d5241` ("ARM\x64" in little-endian)
- **text_offset**: Where to load the kernel relative to RAM base (usually 0x80000 = 512KB)
- **image_size**: Size of the kernel image

### Kernel Placement

Load the kernel at:
```
kernel_address = RAM_BASE + text_offset
```

For us:
```
kernel_address = 0x40000000 + 0x80000 = 0x40080000
```

The entry point is at the start of the kernel:
```
entry_point = kernel_address = 0x40080000
```

## Device Tree - Hardware Description

### What is a Device Tree?

ARM systems use **Device Tree** to describe hardware. Unlike PCs (which have BIOS/UEFI discovery), ARM needs explicit description of every device.

The Device Tree is a hierarchical data structure describing:
- Memory location and size
- CPUs and their properties
- Devices and their addresses/interrupts
- Boot arguments and kernel configuration

### DTS vs DTB

- **DTS (Device Tree Source)**: Human-readable text format
- **DTB (Device Tree Blob)**: Compiled binary format

The kernel reads the DTB at boot.

### Device Tree Structure

```dts
/dts-v1/;

/ {                              // Root node
    compatible = "linux,dummy-virt";
    #address-cells = <2>;        // 64-bit addresses
    #size-cells = <2>;           // 64-bit sizes

    chosen {                     // Boot configuration
        bootargs = "console=ttyAMA0 earlycon=pl011,0x09000000";
        stdout-path = "/pl011@9000000";
    };

    memory@40000000 {            // RAM
        device_type = "memory";
        reg = <0x00 0x40000000 0x00 0x40000000>;  // 1GB at 0x40000000
    };

    cpus {                       // CPU topology
        #address-cells = <1>;
        #size-cells = <0>;

        cpu@0 {
            device_type = "cpu";
            compatible = "arm,cortex-a53";
            reg = <0>;
            enable-method = "psci";
        };
    };

    psci {                       // Power State Coordination Interface
        compatible = "arm,psci-1.0", "arm,psci-0.2";
        method = "hvc";
    };

    intc: interrupt-controller@8000000 {  // GIC
        compatible = "arm,gic-v3";
        #interrupt-cells = <3>;
        interrupt-controller;
        reg = <0x00 0x08000000 0x00 0x10000>,  // Distributor
              <0x00 0x080a0000 0x00 0x100000>; // Redistributor
    };

    timer {                      // ARM timer
        compatible = "arm,armv8-timer";
        interrupts = <1 13 0x04>, <1 14 0x04>,
                     <1 11 0x04>, <1 10 0x04>;
        always-on;
    };

    pl011@9000000 {              // UART
        compatible = "arm,pl011", "arm,primecell";
        reg = <0x00 0x09000000 0x00 0x1000>;
        interrupts = <0 1 4>;
        clock-names = "uartclk", "apb_pclk";
        clocks = <&apb_pclk>, <&apb_pclk>;
    };

    apb_pclk: clock {            // Clock for UART
        compatible = "fixed-clock";
        #clock-cells = <0>;
        clock-frequency = <24000000>;
    };
};
```

### Key Device Tree Nodes

**/ (root)**
- `compatible`: Machine type
- `#address-cells`, `#size-cells`: How to interpret `reg` properties

**chosen**
- `bootargs`: Kernel command line
- `stdout-path`: Console device
- `linux,initrd-start`, `linux,initrd-end`: Initramfs location

**memory**
- `reg`: RAM location and size

**cpus**
- One `cpu@N` node per CPU
- `enable-method`: How to start secondary CPUs

**interrupt-controller**
- GIC configuration
- `#interrupt-cells`: How many values per interrupt

**timer**
- Timer interrupt assignments

**Devices (pl011, virtio_mmio, etc.)**
- `reg`: MMIO address range
- `interrupts`: Interrupt assignment

### Interrupt Specifiers

In Device Tree, interrupts are described with multiple cells:

For GIC:
```
interrupts = <type number flags>;
```

Where:
- **type**: 0 = SPI (shared), 1 = PPI (per-CPU)
- **number**: Interrupt number (0-based for SPI, 0-15 for PPI)
- **flags**: Trigger type (1=edge rising, 4=level high, etc.)

Example: UART at SPI 1, level-sensitive:
```
interrupts = <0 1 4>;
```

## Initramfs - Initial Filesystem

### What is Initramfs?

**Initramfs** (Initial RAM Filesystem) is a compressed archive loaded into RAM at boot. It provides:
- Minimal userspace tools (shell, mount, etc.)
- Early hardware setup
- Root filesystem mounting logic

For our simple VM, initramfs IS our root filesystem.

### CPIO Archive Format

Initramfs uses the CPIO archive format:
```bash
# Create a minimal initramfs
mkdir -p initramfs/{bin,dev,proc,sys}
cp /path/to/busybox initramfs/bin/
cd initramfs/bin
ln -s busybox sh
ln -s busybox ls
# ... more symlinks for busybox applets

# Create init script
cat > initramfs/init << 'EOF'
#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
echo "Hello from init!"
exec /bin/sh
EOF
chmod +x initramfs/init

# Create the archive
cd initramfs
find . | cpio -o -H newc | gzip > ../initramfs.cpio.gz
```

### Using BusyBox

**BusyBox** is a single binary that implements many Unix utilities. It's perfect for minimal initramfs:

```bash
# Get a statically-linked BusyBox for ARM64
# Either download pre-built or compile with:
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- busybox
```

### Initramfs Placement

Load initramfs after the kernel in RAM. Tell the kernel where it is via Device Tree:

```dts
chosen {
    linux,initrd-start = <0x00 0x48000000>;
    linux,initrd-end = <0x00 0x48500000>;
};
```

## The Kernel Command Line

The kernel command line configures boot behavior. Key options:

**console=**
```
console=ttyAMA0        # Use PL011 UART
console=ttyS0,115200   # Serial with baud rate
console=hvc0           # Virtio console
```

**earlycon=**
Early console before driver initialization:
```
earlycon=pl011,0x09000000
```

**root=**
Root filesystem (if not using initramfs):
```
root=/dev/vda1
root=LABEL=root
```

**debug/loglevel=**
```
debug              # Verbose output
loglevel=7         # Max verbosity
quiet              # Minimal output
```

**init=**
```
init=/bin/sh       # Override init program
```

**Other useful options:**
```
panic=5            # Reboot 5 seconds after panic
no_hash_pointers   # Show real pointer values
```

## Boot Protocol Implementation

### Memory Layout

```
Guest Physical Address Space during boot:

0x40000000 ┌─────────────────────────────────────┐ RAM_BASE
           │                                     │
0x40080000 ├─────────────────────────────────────┤ kernel_address
           │         Linux Kernel Image          │
           │         (~10-20 MB typically)       │
           │                                     │
0x42000000 ├─────────────────────────────────────┤ initramfs_address
           │           Initramfs                 │
           │         (~5-50 MB typically)        │
           │                                     │
0x47F00000 ├─────────────────────────────────────┤ dtb_address
           │        Device Tree Blob             │
           │          (~64 KB typically)         │
           │                                     │
0x80000000 └─────────────────────────────────────┘ RAM_END
```

### Loading Files

```python
def load_for_boot(vm, kernel_path, initrd_path=None, dtb_path=None):
    """Load kernel, initramfs, and DTB into guest memory."""

    # Load kernel at RAM + text_offset
    kernel_addr = RAM_BASE + 0x80000
    kernel_size = vm.memory.load_file(kernel_addr, kernel_path)
    print(f"Loaded kernel at 0x{kernel_addr:x} ({kernel_size} bytes)")

    # Load initramfs after kernel (aligned to 4KB)
    initrd_addr = (kernel_addr + kernel_size + 0xFFF) & ~0xFFF
    initrd_size = 0
    if initrd_path:
        initrd_size = vm.memory.load_file(initrd_addr, initrd_path)
        print(f"Loaded initramfs at 0x{initrd_addr:x} ({initrd_size} bytes)")

    # Load/generate DTB before end of RAM
    dtb_addr = RAM_BASE + vm.ram_size - 0x100000  # 1MB before end
    # Generate or load DTB...

    return {
        'kernel_addr': kernel_addr,
        'initrd_addr': initrd_addr,
        'initrd_size': initrd_size,
        'dtb_addr': dtb_addr,
    }
```

### Setting Up vCPU

```python
def setup_boot_vcpu(vcpu, boot_info):
    """Configure vCPU for kernel boot."""

    # x0 = DTB address
    vcpu.set_register(X0, boot_info['dtb_addr'])

    # x1, x2, x3 = 0 (reserved)
    vcpu.set_register(X1, 0)
    vcpu.set_register(X2, 0)
    vcpu.set_register(X3, 0)

    # PC = kernel entry point
    vcpu.set_pc(boot_info['kernel_addr'])

    # PSTATE = EL1h with interrupts masked
    pstate = PSTATE_MODE_EL1H | PSTATE_A | PSTATE_I | PSTATE_F
    vcpu.set_pstate(pstate)
```

## Device Tree Generation

We can generate the DTB programmatically:

```python
import libfdt  # pip install libfdt

def generate_dtb(memory_size, initrd_start=None, initrd_end=None):
    """Generate a Device Tree Blob for our VM."""

    # Create empty DTB with initial size
    fdt = libfdt.Fdt.create_empty(1024 * 16)

    # ... add nodes programmatically ...
    # This is complex - see the full implementation

    return fdt.as_bytearray()
```

Or, create a .dts file and compile with:
```bash
dtc -I dts -O dtb -o virt.dtb virt.dts
```

## The Complete Boot Command

```python
@app.command("boot")
def boot_linux(
    kernel: str = typer.Argument(..., help="Path to kernel Image"),
    initrd: str = typer.Option(None, "--initrd", "-i", help="Path to initramfs"),
    dtb: str = typer.Option(None, "--dtb", "-d", help="Path to DTB file"),
    cmdline: str = typer.Option(
        "console=ttyAMA0 earlycon=pl011,0x09000000",
        "--cmdline",
        "-c",
        help="Kernel command line"
    ),
    ram_mb: int = typer.Option(1024, "--ram", "-r", help="RAM in MB"),
):
    """Boot a Linux kernel."""
    # ... full implementation ...
```

## The Moment of Truth

When everything is configured correctly, you'll see:

```
$ uv run god boot Image --initrd initramfs.cpio.gz

Creating VM with 1024 MB RAM...
GIC created at 0x08000000
Loading kernel at 0x40080000 (15728640 bytes)
Loading initramfs at 0x41100000 (5242880 bytes)
DTB at 0x7ff00000 (32768 bytes)

Booting Linux...
============================================================
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd034]
[    0.000000] Linux version 6.1.0 ...
[    0.000000] Machine model: linux,dummy-virt
[    0.000000] earlycon: pl011 at MMIO 0x0000000009000000 ...
[    0.000000] Memory: 1016352K/1048576K available ...
...
[    0.xxx] Run /init as init process
Hello from init!
/ # ls
bin   dev   init  proc  sys
/ # uname -a
Linux (none) 6.1.0 #1 SMP ... aarch64 GNU/Linux
/ #
```

**We booted Linux!**

## Debugging Boot Failures

### No Output At All

- Check that UART is at the correct address
- Verify `earlycon=` is in command line
- Ensure DTB is valid and at correct address

### "Booting Linux..." Then Nothing

- Kernel is crashing very early
- Check timer is configured
- Check GIC is initialized before vCPU
- Try adding `debug` to command line

### Kernel Panic

- Usually a driver issue
- Check DTB for correct device descriptions
- Try with simpler configuration

### Using earlycon

**earlycon** provides output before normal console drivers load:
```
earlycon=pl011,mmio,0x09000000
```

This uses direct hardware access, so it works even if there are driver issues.

## What Happens During Linux Boot

1. **Head.S** (arch/arm64/kernel/head.S)
   - First code to run
   - Sets up initial page tables
   - Enables MMU
   - Jumps to C code

2. **start_kernel()** (init/main.c)
   - Initializes subsystems
   - Parses command line
   - Initializes memory management
   - Starts scheduler

3. **rest_init()** / kernel_init()
   - Creates init process
   - Mounts initramfs
   - Executes /init

4. **/init**
   - Our shell script or program
   - Sets up final userspace

## Gotchas

### DTB Corruption

If the DTB is corrupted or at the wrong address, the kernel fails immediately. Validate your DTB with:
```bash
dtc -I dtb -O dts virt.dtb  # Should decompile without errors
```

### Kernel Placement

The kernel MUST be at RAM_BASE + text_offset. Wrong placement = immediate crash.

### Missing GIC

Without the GIC, interrupts don't work. No interrupts = no timer = kernel hangs.

### Initramfs Issues

If initramfs doesn't mount:
- Check compression (kernel must support it)
- Check CPIO format (use `-H newc`)
- Verify start/end addresses in DTB

## What's Next?

**Congratulations!** You've built a Virtual Machine Monitor from scratch and booted Linux!

In Chapter 8, we'll implement **virtio** - the paravirtualization standard that makes I/O much more efficient than the UART we're using now. With virtio, we can add:
- Efficient console (virtio-console)
- Block devices (virtio-blk) for real filesystems
- Networking (virtio-net)
- Graphics (virtio-gpu)

[Continue to Chapter 8: Virtio Devices →](08-virtio-devices.md)
