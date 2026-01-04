# Chapter 7: Booting Linux

In this chapter, we'll boot a real Linux kernel in our virtual machine. This is an exciting milestone—we'll see all our previous work (memory management, vCPU handling, UART, GIC, timer) come together to run a full operating system.

## What Does "Booting" Mean?

When you press the power button on a computer, a complex sequence of events unfolds:

1. **Hardware initialization**: The CPU starts executing from a fixed address, usually in ROM/flash
2. **Firmware runs**: BIOS/UEFI (on x86) or boot ROM (on ARM) initializes hardware
3. **Bootloader runs**: GRUB, U-Boot, or similar loads the kernel into RAM
4. **Kernel starts**: The OS kernel takes over, initializes drivers, mounts filesystems
5. **Init runs**: The first userspace process starts (`/sbin/init` or `/init`)

On real ARM hardware, there's typically firmware (like ARM Trusted Firmware) that handles early setup. In our VMM, **we play the role of firmware**. We're responsible for:

- Setting up memory
- Loading the kernel and initramfs into the right locations
- Preparing the CPU state
- Telling the kernel about the hardware (via Device Tree)

Then we hand off to the kernel and let it run.

## ARM64 Exception Levels

Before we can set up the CPU for Linux boot, we need to understand ARM64's privilege model.

### The Privilege Hierarchy

ARM64 has four **Exception Levels (EL)**, from most privileged to least:

| Level | Name | Purpose | Example Software |
|-------|------|---------|------------------|
| **EL3** | Secure Monitor | Highest privilege, manages security states | ARM Trusted Firmware |
| **EL2** | Hypervisor | Virtualization support | KVM, Xen, our VMM |
| **EL1** | OS Kernel | Operating system | Linux kernel |
| **EL0** | User | Applications | Your programs |

The naming might seem backwards (higher numbers = lower privilege), but think of it as "exception level"—EL3 handles the most critical exceptions.

```
┌─────────────────────────────────────────────────────────────────┐
│                          EL3 (Secure Monitor)                   │
│                     Highest privilege - security                │
├─────────────────────────────────────────────────────────────────┤
│                          EL2 (Hypervisor)                       │
│                 Virtualization - manages VMs                    │
├─────────────────────────────────────────────────────────────────┤
│                          EL1 (Kernel)                           │
│                   Operating system kernel                       │
├─────────────────────────────────────────────────────────────────┤
│                          EL0 (User)                             │
│                     User applications                           │
└─────────────────────────────────────────────────────────────────┘
```

### Why This Matters for Booting

Linux expects to start at **EL1** (kernel mode). On real hardware, firmware (running at EL3/EL2) does early setup and drops to EL1 before jumping to the kernel.

In our VMM:
- KVM runs at EL2 on the host
- Our guest vCPU starts at EL1 by default
- We configure the vCPU state and jump directly to the kernel

This is actually simpler than real hardware—we don't need to implement EL3/EL2 firmware.

## PSTATE: Processor State

**PSTATE** is a collection of fields that control how the ARM64 CPU operates. It's not a single register you can read/write directly—instead, it's a conceptual grouping of bits spread across special registers. When using KVM, we can set PSTATE as a single 64-bit value.

### PSTATE Fields

| Field | Bits | Purpose |
|-------|------|---------|
| **N, Z, C, V** | 31-28 | Condition flags (Negative, Zero, Carry, Overflow) |
| **SS** | 21 | Software Step (debugging) |
| **IL** | 20 | Illegal Execution State |
| **D** | 9 | Debug mask |
| **A** | 8 | SError (asynchronous abort) mask |
| **I** | 7 | IRQ mask |
| **F** | 6 | FIQ mask |
| **M[4:0]** | 4-0 | Mode (current exception level + stack pointer selection) |

### The Mode Field

The bottom 5 bits encode the current exception level and which stack pointer to use:

| Mode Value | Meaning |
|------------|---------|
| `0b00000` (0x0) | EL0 with SP_EL0 |
| `0b00100` (0x4) | EL1 with SP_EL0 (EL1t) |
| `0b00101` (0x5) | EL1 with SP_EL1 (EL1h) |
| `0b01000` (0x8) | EL2 with SP_EL0 (EL2t) |
| `0b01001` (0x9) | EL2 with SP_EL2 (EL2h) |

The "t" and "h" suffixes mean:
- **t (thread)**: Use SP_EL0 (the user stack pointer)
- **h (handler)**: Use SP_ELn (the dedicated stack pointer for that level)

Linux uses **EL1h** (mode 0x5)—running at EL1 with its own dedicated stack pointer.

### Interrupt Masks

The A, I, and F bits control whether the CPU responds to interrupts:

| Bit | When Set (1) | When Clear (0) |
|-----|--------------|----------------|
| **A** | SError exceptions masked (ignored) | SError exceptions taken |
| **I** | IRQs masked (ignored) | IRQs taken |
| **F** | FIQs masked (ignored) | FIQs taken |

During early boot, Linux wants **all interrupts masked**. The kernel will unmask them after setting up interrupt handlers.

### PSTATE for Linux Boot

For booting Linux, we set PSTATE to:

```python
PSTATE_MODE_EL1H = 0x5  # EL1, using SP_EL1
PSTATE_D = 1 << 9        # Mask Debug exceptions
PSTATE_A = 1 << 8        # Mask SError
PSTATE_I = 1 << 7        # Mask IRQ
PSTATE_F = 1 << 6        # Mask FIQ

boot_pstate = PSTATE_MODE_EL1H | PSTATE_D | PSTATE_A | PSTATE_I | PSTATE_F
# Result: 0x3C5
```

This says: "Run at EL1 with dedicated stack pointer, all exceptions masked." We include PSTATE_D to mask debug exceptions during early boot—these would otherwise cause VM exits before the kernel sets up its own handlers.

## The MMU (Memory Management Unit)

The **Memory Management Unit** translates virtual addresses to physical addresses. It's what allows each process to have its own address space, and it enables features like:

- **Memory protection**: Processes can't access each other's memory
- **Virtual memory**: Programs see a flat address space regardless of physical RAM layout
- **Paging**: Memory can be swapped to disk

### Virtual vs Physical Addresses

Without MMU (MMU disabled):
```
CPU uses address 0x40000000
         │
         └──► Physical RAM at 0x40000000
```

With MMU (MMU enabled):
```
CPU uses address 0x0000000000400000 (virtual)
         │
         ▼
    ┌─────────────┐
    │  MMU does   │
    │  page table │
    │   lookup    │
    └─────────────┘
         │
         ▼
Physical RAM at 0x80200000 (physical)
```

### MMU at Boot

**The MMU must be disabled when the kernel starts.**

Why? The kernel needs to set up its own page tables before enabling the MMU. If the MMU were already on with some random page tables, the kernel would crash immediately trying to access memory.

The ARM64 boot protocol specifies:
- MMU off
- Data cache can be on or off (kernel handles either)
- Instruction cache can be on or off

The kernel's early boot code (`arch/arm64/kernel/head.S`) creates page tables and enables the MMU itself.

### SCTLR_EL1: System Control Register

The **SCTLR_EL1** (System Control Register for EL1) controls MMU and cache behavior:

| Bit | Name | Purpose |
|-----|------|---------|
| 0 | M | MMU enable (0 = off, 1 = on) |
| 2 | C | Data cache enable |
| 12 | I | Instruction cache enable |

KVM initializes SCTLR_EL1 with the MMU disabled by default, so we don't need to do anything special—but it's important to understand why.

## ARM64 Linux Boot Protocol

Now that we understand exception levels, PSTATE, and the MMU, let's look at what Linux specifically requires.

The ARM64 boot protocol is documented in `Documentation/arch/arm64/booting.rst` in the Linux source. Here's what the kernel expects:

### CPU State Requirements

| Requirement | Value | Why |
|-------------|-------|-----|
| Exception Level | EL1 (or EL2 if using EL2 kernel) | Kernel runs at EL1 |
| MMU | Disabled | Kernel sets up its own page tables |
| Data cache | On or off | Kernel handles either |
| Interrupts | Masked (PSTATE.DAIF = 0xF) | No handlers installed yet |

### Register Requirements

| Register | Contents | Notes |
|----------|----------|-------|
| **x0** | Physical address of DTB | Device Tree Blob location |
| **x1** | 0 | Reserved for future use |
| **x2** | 0 | Reserved for future use |
| **x3** | 0 | Reserved for future use |
| **PC** | Kernel entry point | Start of kernel Image |
| **SP** | Not used | Kernel sets up its own stack |

That's it! Just set x0 to the DTB address and PC to the kernel entry point. This is the **Linux ARM64 boot protocol**, not an ARM architecture requirement—other operating systems could use different conventions.

## Building the Linux Kernel

Rather than downloading a pre-built kernel, let's build one ourselves. This gives us full control and helps us understand what goes into a kernel.

### Getting the Source

```bash
# Clone the Linux kernel repository (this is large, ~3GB)
# The --depth=1 flag gets only the latest commit to save time/space
git clone --depth=1 https://github.com/torvalds/linux.git
cd linux

# Or get a specific stable version
git clone --depth=1 --branch v6.12 https://github.com/torvalds/linux.git
```

### Understanding Kernel Configuration

The kernel is highly configurable. The `.config` file controls what features to build:

```bash
# See all available options (there are thousands!)
make ARCH=arm64 menuconfig
```

Key configuration areas:
- **Processor type**: Which ARM cores to support
- **Device drivers**: UART, block devices, network, etc.
- **Filesystems**: ext4, FAT, initramfs support
- **Kernel features**: SMP, preemption, debugging

### Creating a Minimal Configuration

For our VMM, we want a minimal kernel that boots fast. We'll start with `defconfig` (the default configuration for ARM64) and then trim it down.

```bash
# Start with the default ARM64 configuration
make ARCH=arm64 defconfig
```

The default config includes many drivers we don't need. For a minimal VM, we want:

**Essential (must have):**
- ARM64 base support
- Device Tree support
- PL011 UART driver (for our serial console)
- GICv3 interrupt controller
- ARM architected timer
- initramfs support

**Optional (nice to have):**
- virtio drivers (for Chapter 8)
- Early printk for debugging

### Customizing the Configuration

Let's create a minimal config. You can use `menuconfig` interactively, or we'll provide a script that does it programmatically:

```bash
# Start with defconfig
make ARCH=arm64 defconfig

# Open the menu-based configurator
make ARCH=arm64 menuconfig
```

In menuconfig, navigate and disable unnecessary options:
- **General setup** → Disable "Kernel compression mode" extras
- **Platform selection** → Keep only "ARMv8 based platforms"
- **Device Drivers** → Disable most (keep Serial, Virtio)
- **File systems** → Keep only what's needed for initramfs

Or use our automated setup (we'll create this script later):

```bash
# We'll create this script to automate kernel config
python -m god.build kernel --configure
```

### Building the Kernel

Once configured:

```bash
# Build the kernel image
# -j$(nproc) uses all available CPU cores
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) Image

# The output is at:
# arch/arm64/boot/Image
```

The build takes a few minutes. When done, you'll have `arch/arm64/boot/Image`—the uncompressed kernel binary.

### Cross-Compilation Note

If you're building on x86, you need a cross-compiler. The Lima VM we use is ARM64, so we can build natively:

```bash
# On ARM64 (like our Lima VM), just:
make ARCH=arm64 -j$(nproc) Image

# On x86, you need cross-compiler:
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) Image
```

## The ARM64 Kernel Image Format

The kernel builds to a file called `Image`. Let's understand its structure.

### The 64-Byte Header

The first 64 bytes of `arch/arm64/boot/Image` contain a header that bootloaders (and VMMs like us) read:

```c
struct arm64_image_header {
    uint32_t code0;        // Executable code (branch instruction)
    uint32_t code1;        // Executable code
    uint64_t text_offset;  // Image load offset from RAM base
    uint64_t image_size;   // Effective Image size
    uint64_t flags;        // Kernel flags
    uint64_t res2;         // Reserved
    uint64_t res3;         // Reserved
    uint64_t res4;         // Reserved
    uint32_t magic;        // Magic number: 0x644d5241 ("ARM\x64")
    uint32_t res5;         // Reserved (PE header offset for UEFI)
};
```

### Header Fields Explained

**code0 and code1 (offset 0x00-0x07)**

These are actually executable ARM64 instructions! The first instruction is typically a branch that jumps over the header. This means you can execute the Image directly if you jump to offset 0—it will branch past the header and start executing.

**text_offset (offset 0x08)**

This tells us where to load the kernel relative to the start of RAM. The value is typically `0x80000` (512 KB).

```
Kernel load address = RAM_BASE + text_offset
                    = 0x40000000 + 0x80000
                    = 0x40080000
```

Why 512 KB offset? The kernel needs some space below itself for early boot data structures. The exact value can vary between kernel versions.

**image_size (offset 0x10)**

The size of the kernel image in bytes. We need to know this to avoid loading other data (initramfs, DTB) on top of the kernel.

**flags (offset 0x18)**

Kernel feature flags:

| Bit | Meaning |
|-----|---------|
| 0 | Kernel endianness (0 = little, 1 = big) |
| 1-2 | Page size (0 = unspecified, 1 = 4K, 2 = 16K, 3 = 64K) |
| 3 | Physical placement (0 = 2MB aligned anywhere, 1 = must be at base + text_offset) |

**magic (offset 0x38)**

Must be `0x644d5241`, which is the ASCII string "ARM\x64" in little-endian. This lets us verify we're looking at a valid ARM64 kernel image.

### Reading the Header

Let's look at a real kernel header:

```bash
# Hexdump the first 64 bytes
hexdump -C arch/arm64/boot/Image | head -4

# Example output:
00000000  4d 5a 00 91 ff ff ff 14  00 00 08 00 00 00 00 00  |MZ..............|
00000010  00 00 d0 01 00 00 00 00  0a 00 00 00 00 00 00 00  |................|
00000020  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  |................|
00000030  00 00 00 00 00 00 00 00  41 52 4d 64 00 00 00 00  |........ARMd....|
```

Let's decode this:
- `4d 5a 00 91` = `code0` (an ARM instruction: `add x13, x18, #0x16`)
- `ff ff ff 14` = `code1` (a branch instruction)
- `00 00 08 00 00 00 00 00` = `text_offset` = 0x80000 (little-endian)
- `00 00 d0 01 00 00 00 00` = `image_size` = 0x1d00000 (~29 MB)
- `41 52 4d 64` = `magic` = "ARMd" (0x644d5241 in little-endian)

## Device Tree: Describing Hardware to the Kernel

### Why Device Tree Exists

On x86 PCs, hardware discovery is (relatively) standardized:
- **PCI bus** enumerates devices automatically
- **ACPI tables** describe platform hardware
- The OS can probe and discover what's present

ARM systems have no such standardization. Every ARM board has different:
- Memory addresses for devices
- Interrupt routing
- Clock configurations
- GPIO assignments

Without a standard discovery mechanism, how does the kernel know what hardware exists? **Device Tree** is the answer.

### What is Device Tree?

**Device Tree** is a data structure that describes hardware. It originated in Open Firmware (used by PowerPC Macs and Sun workstations) and was adopted by Linux for ARM around 2011.

The Device Tree tells the kernel:
- What devices exist
- Where they are in memory (MMIO addresses)
- Which interrupts they use
- How they're connected (buses, clocks, etc.)
- Boot configuration (kernel command line, initramfs location)

It's not ARM-specific or Linux-specific—FreeBSD, Zephyr RTOS, and other OSes use it too. But it's most commonly associated with Linux on ARM.

### DTS vs DTB

Device Tree comes in two formats:

| Format | Extension | Description |
|--------|-----------|-------------|
| **DTS** | `.dts` | Device Tree Source - human-readable text |
| **DTB** | `.dtb` | Device Tree Blob - compiled binary |

The kernel reads the **DTB** (binary) format at boot. We can either:
1. Write a `.dts` file and compile it with `dtc` (Device Tree Compiler)
2. Generate the DTB programmatically using a library

We'll do both—first understand the text format, then generate it in Python.

### Device Tree Structure

A Device Tree is a hierarchy of **nodes**. Each node can have:
- **Properties**: Key-value pairs describing the node
- **Child nodes**: Nested nodes for sub-components

Here's a minimal example:

```dts
/dts-v1/;  // Device Tree version 1

/ {        // Root node (always "/")
    compatible = "my-board";

    memory@40000000 {
        device_type = "memory";
        reg = <0x40000000 0x40000000>;
    };
};
```

### Property Types

Properties can have different value types:

| Type | Example | Description |
|------|---------|-------------|
| **String** | `compatible = "arm,pl011"` | Text value |
| **String list** | `compatible = "arm,pl011", "arm,primecell"` | Multiple strings |
| **Integer** | `clock-frequency = <24000000>` | 32-bit value in angle brackets |
| **Integer array** | `reg = <0x0 0x40000000 0x0 0x40000000>` | Multiple 32-bit values |
| **Empty** | `always-on;` | Property exists but has no value (boolean true) |
| **Phandle** | `clocks = <&apb_pclk>` | Reference to another node |

### Understanding `#address-cells` and `#size-cells`

These properties are crucial and often confusing. They tell parsers how to interpret `reg` properties in child nodes.

```dts
/ {
    #address-cells = <2>;  // Addresses use 2 × 32-bit values = 64 bits
    #size-cells = <2>;     // Sizes use 2 × 32-bit values = 64 bits

    memory@40000000 {
        reg = <0x00 0x40000000 0x00 0x40000000>;
        //     ^^^^^^^^^^^^^ ^^^^^^^^^^^^^^
        //     address (2 cells)  size (2 cells)
        //     = 0x0000000040000000 = 0x0000000040000000
        //     = 1 GB               = 1 GB
    };
};
```

Why two cells? ARM64 has 64-bit addresses, but Device Tree was designed when 32-bit was common. Using `#address-cells = <2>` means each address is two 32-bit values: `<high_32_bits low_32_bits>`.

**Breaking down `reg = <0x00 0x40000000 0x00 0x40000000>`:**

| Cell | Value | Meaning |
|------|-------|---------|
| 1 | `0x00` | Address high 32 bits |
| 2 | `0x40000000` | Address low 32 bits → Address = 0x40000000 |
| 3 | `0x00` | Size high 32 bits |
| 4 | `0x40000000` | Size low 32 bits → Size = 0x40000000 (1 GB) |

If we used `#address-cells = <1>` and `#size-cells = <1>`, we'd write:
```dts
reg = <0x40000000 0x40000000>;  // Just 2 values instead of 4
```

But then we couldn't represent addresses above 4 GB.

### The `compatible` Property

The `compatible` property is how the kernel finds the right driver. It's a list of strings, from most specific to least specific:

```dts
pl011@9000000 {
    compatible = "arm,pl011", "arm,primecell";
};
```

The kernel tries drivers in order:
1. First, look for a driver claiming `"arm,pl011"`
2. If not found, try `"arm,primecell"`

This provides fallback compatibility—a generic driver can handle devices it doesn't know specifically.

### Our VM's Device Tree

Now let's build the Device Tree for our virtual machine. We need to describe:

1. **Machine identity** (root node)
2. **Memory** (RAM location and size)
3. **CPUs** (processor topology)
4. **Interrupt controller** (GIC)
5. **Timer** (ARM architected timer)
6. **Serial console** (PL011 UART)
7. **Boot configuration** (chosen node)

Here's the complete DTS:

```dts
/dts-v1/;

/ {
    compatible = "linux,dummy-virt";
    #address-cells = <2>;
    #size-cells = <2>;

    // Device aliases for consistent naming
    aliases {
        serial0 = "/soc/pl011@9000000";
    };

    // Boot configuration
    chosen {
        bootargs = "console=ttyAMA0 earlycon=pl011,0x09000000";
        stdout-path = "/soc/pl011@9000000";
        // linux,initrd-start and linux,initrd-end added dynamically
    };

    // RAM: 1 GB at 0x40000000
    memory@40000000 {
        device_type = "memory";
        reg = <0x00 0x40000000 0x00 0x40000000>;
    };

    // CPU topology
    cpus {
        #address-cells = <1>;
        #size-cells = <0>;

        cpu@0 {
            device_type = "cpu";
            compatible = "arm,cortex-a57";
            reg = <0>;
            enable-method = "psci";
        };
    };

    // Power State Coordination Interface
    psci {
        compatible = "arm,psci-1.0", "arm,psci-0.2";
        method = "hvc";
    };

    // Interrupt controller (GICv3)
    intc: interrupt-controller@8000000 {
        compatible = "arm,gic-v3";
        #interrupt-cells = <3>;
        interrupt-controller;
        reg = <0x00 0x08000000 0x00 0x10000>,   // Distributor: 64 KB
              <0x00 0x080a0000 0x00 0x100000>;  // Redistributor: 1 MB
        phandle = <1>;  // For interrupt-parent references
    };

    // ARM architected timer
    timer {
        compatible = "arm,armv8-timer";
        interrupt-parent = <&intc>;
        interrupts = <1 13 0x04>,  // Secure physical timer
                     <1 14 0x04>,  // Non-secure physical timer
                     <1 11 0x04>,  // Virtual timer
                     <1 10 0x04>;  // Hypervisor timer
        always-on;
    };

    // Fixed clock for UART (24 MHz)
    apb_pclk: apb-pclk {
        compatible = "fixed-clock";
        #clock-cells = <0>;
        clock-frequency = <24000000>;
        phandle = <2>;
    };

    // SOC bus containing platform devices
    soc {
        compatible = "simple-bus";
        #address-cells = <2>;
        #size-cells = <2>;
        ranges;  // 1:1 address mapping

        // Serial console (PL011 UART)
        pl011@9000000 {
            compatible = "arm,pl011", "arm,primecell";
            status = "okay";
            arm,primecell-periphid = <0x00241011>;  // PL011 peripheral ID
            reg = <0x00 0x09000000 0x00 0x1000>;
            interrupt-parent = <&intc>;
            interrupts = <0 1 4>;  // SPI 1, level triggered
            clock-names = "uartclk", "apb_pclk";
            clocks = <&apb_pclk>, <&apb_pclk>;
        };
    };
};
```

There are several important details in this Device Tree that ensure proper device driver binding:

**The `soc` node**: AMBA devices like the PL011 UART must be under a `simple-bus` compatible parent node. This tells Linux to probe child devices using the platform device model.

**The `aliases` node**: Provides stable device naming. `serial0 = "/soc/pl011@9000000"` ensures the UART is always named `ttyAMA0`.

**The `arm,primecell-periphid` property**: This is crucial! AMBA devices identify themselves via a peripheral ID register. When that register isn't available (as in our virtual UART), we must provide the ID in the Device Tree. `0x00241011` is the standard PL011 peripheral ID.

### Node-by-Node Explanation

#### Root Node (`/`)

```dts
/ {
    compatible = "linux,dummy-virt";
    #address-cells = <2>;
    #size-cells = <2>;
};
```

- `compatible`: Machine identifier. `"linux,dummy-virt"` is a generic virtual machine type that Linux recognizes.
- `#address-cells = <2>`: Child nodes use 64-bit addresses (2 × 32 bits)
- `#size-cells = <2>`: Child nodes use 64-bit sizes

#### Chosen Node

```dts
chosen {
    bootargs = "console=ttyAMA0 earlycon=pl011,0x09000000";
    stdout-path = "/soc/pl011@9000000";
};
```

The `chosen` node isn't hardware—it's boot configuration:

- `bootargs`: Kernel command line (like GRUB's command line)
  - `console=ttyAMA0`: Use PL011 UART as the console
  - `earlycon=pl011,0x09000000`: Enable early console before drivers load
- `stdout-path`: Where to send boot messages

For initramfs, we add:
```dts
linux,initrd-start = <0x00 0x48000000>;
linux,initrd-end = <0x00 0x48080000>;
```

These tell the kernel where we loaded the initramfs in RAM (at 128 MB offset from RAM base).

#### Memory Node

```dts
memory@40000000 {
    device_type = "memory";
    reg = <0x00 0x40000000 0x00 0x40000000>;
};
```

- `device_type = "memory"`: This node describes RAM
- `reg`: Location and size of RAM
  - Address: 0x40000000 (1 GB mark)
  - Size: 0x40000000 (1 GB)

The `@40000000` in the node name is the **unit address**—it should match the first address in `reg`. It's used for sorting and identification, not by drivers.

#### CPUs Node

```dts
cpus {
    #address-cells = <1>;
    #size-cells = <0>;

    cpu@0 {
        device_type = "cpu";
        compatible = "arm,cortex-a57";
        reg = <0>;
        enable-method = "psci";
    };
};
```

- `#address-cells = <1>`: CPU IDs are single 32-bit values
- `#size-cells = <0>`: CPUs don't have a "size"
- `compatible = "arm,cortex-a57"`: CPU type (KVM emulates a generic ARMv8 CPU)
- `reg = <0>`: CPU ID (first CPU is 0)
- `enable-method = "psci"`: How to power on secondary CPUs

#### PSCI Node

```dts
psci {
    compatible = "arm,psci-1.0", "arm,psci-0.2";
    method = "hvc";
};
```

**PSCI (Power State Coordination Interface)** is ARM's standard for power management calls. It's how the OS asks the firmware/hypervisor to:
- Power on/off CPUs
- Enter sleep states
- Reset or shutdown the system

The `method` property says HOW to invoke PSCI:

| Method | Instruction | When to Use |
|--------|-------------|-------------|
| `"hvc"` | `HVC #0` | Calling a hypervisor (our case) |
| `"smc"` | `SMC #0` | Calling secure firmware |

Since our VMM runs as a hypervisor (using KVM at EL2), guests use `HVC` (Hypervisor Call) to talk to us. When the guest executes `HVC #0` with a PSCI function ID in x0, KVM returns `KVM_EXIT_SYSTEM_EVENT` to our VMM.

#### Interrupt Controller Node

```dts
intc: interrupt-controller@8000000 {
    compatible = "arm,gic-v3";
    #interrupt-cells = <3>;
    interrupt-controller;
    reg = <0x00 0x08000000 0x00 0x10000>,
          <0x00 0x080a0000 0x00 0x100000>;
};
```

- `intc:` is a **label**—other nodes can reference this as `<&intc>`
- `compatible = "arm,gic-v3"`: GICv3 interrupt controller
- `#interrupt-cells = <3>`: Interrupt specifiers have 3 values
- `interrupt-controller`: This node IS an interrupt controller (empty property = true)
- `reg`: Two regions:
  - Distributor at 0x08000000 (64 KB)
  - Redistributor at 0x080A0000 (1 MB)

#### Understanding Interrupt Specifiers

With `#interrupt-cells = <3>`, each interrupt is described by three values:

```
interrupts = <type number flags>;
```

| Field | Values | Meaning |
|-------|--------|---------|
| **type** | 0 = SPI, 1 = PPI | Interrupt category |
| **number** | 0-N | Interrupt number within category |
| **flags** | Trigger type | How the interrupt is signaled |

**Trigger flags:**
| Value | Meaning |
|-------|---------|
| 1 | Edge triggered, rising edge |
| 2 | Edge triggered, falling edge |
| 4 | Level triggered, active high |
| 8 | Level triggered, active low |

Example: UART at SPI 1, level-triggered active high:
```dts
interrupts = <0 1 4>;
//            │ │ └── Level triggered, active high
//            │ └──── SPI number 1 (actual IRQ = 32 + 1 = 33)
//            └────── Type 0 = SPI
```

The timer uses PPIs (per-CPU interrupts):
```dts
interrupts = <1 13 0x04>,  // PPI 13 (actual = 16 + 13 = 29)
             <1 14 0x04>,  // PPI 14 (actual = 16 + 14 = 30)
             <1 11 0x04>,  // PPI 11 (actual = 16 + 11 = 27)
             <1 10 0x04>;  // PPI 10 (actual = 16 + 10 = 26)
```

#### Timer Node

```dts
timer {
    compatible = "arm,armv8-timer";
    interrupts = <1 13 0x04>,
                 <1 14 0x04>,
                 <1 11 0x04>,
                 <1 10 0x04>;
    always-on;
};
```

The ARM architected timer is built into every ARM64 CPU. The four interrupts are:
1. Secure physical timer (EL3)
2. Non-secure physical timer (EL1)
3. Virtual timer (what Linux uses in VMs)
4. Hypervisor timer (EL2)

`always-on` means the timer keeps running even in low-power states.

**Note**: There's no `reg` property because the timer uses system registers (MSR/MRS), not MMIO.

#### SOC Node

```dts
soc {
    compatible = "simple-bus";
    #address-cells = <2>;
    #size-cells = <2>;
    ranges;

    pl011@9000000 { ... };
};
```

The SOC node groups platform devices under a `simple-bus`. This is important for AMBA devices:

- `compatible = "simple-bus"`: Tells Linux to enumerate children as platform devices
- `ranges`: Creates 1:1 address mapping between parent and child address spaces
- Child devices (like our UART) go inside this node

Without the SOC wrapper, the PL011 driver might not properly probe the device.

#### UART Node

```dts
pl011@9000000 {
    compatible = "arm,pl011", "arm,primecell";
    status = "okay";
    arm,primecell-periphid = <0x00241011>;
    reg = <0x00 0x09000000 0x00 0x1000>;
    interrupt-parent = <&intc>;
    interrupts = <0 1 4>;
    clock-names = "uartclk", "apb_pclk";
    clocks = <&apb_pclk>, <&apb_pclk>;
};
```

Several properties here are critical:

- `status = "okay"`: Explicitly enables this device
- `arm,primecell-periphid = <0x00241011>`: The PL011 peripheral ID. AMBA devices normally identify via a hardware ID register; since we're emulating, we provide it here. Without this, the driver won't bind!
- `interrupt-parent = <&intc>`: References the GIC via its phandle
- `interrupts = <0 1 4>`: SPI 1 (IRQ 33), level-triggered
- `clock-names` and `clocks`: The UART needs clock references
  - `uartclk`: Baud rate generation
  - `apb_pclk`: Bus clock
  - Both reference the `apb_pclk` fixed clock node

#### Aliases Node

```dts
aliases {
    serial0 = "/soc/pl011@9000000";
};
```

Aliases provide stable device naming. Without this, device enumeration order might affect names. `serial0` maps to `ttyAMA0`, ensuring our UART is always the primary serial port.

#### Clock Node

```dts
apb_pclk: apb-pclk {
    compatible = "fixed-clock";
    #clock-cells = <0>;
    clock-frequency = <24000000>;
    phandle = <2>;
};
```

This defines a 24 MHz fixed clock. The UART driver reads this to calculate baud rate divisors.

- `apb_pclk:` is a label so other nodes can reference it as `<&apb_pclk>`
- `#clock-cells = <0>`: No additional specifier needed when referencing
- `phandle = <2>`: Explicit phandle for programmatic DTB generation

### Compiling DTS to DTB

If you write a `.dts` file, compile it with the Device Tree Compiler:

```bash
# Install dtc (Device Tree Compiler)
sudo apt install device-tree-compiler

# Compile DTS to DTB
dtc -I dts -O dtb -o virt.dtb virt.dts

# Decompile DTB back to DTS (for debugging)
dtc -I dtb -O dts -o recovered.dts virt.dtb
```

## Building BusyBox

Now we need userspace—the programs that run after the kernel boots. We'll use **BusyBox**, a single binary that provides dozens of Unix utilities.

### What is BusyBox?

BusyBox is called "The Swiss Army Knife of Embedded Linux." It combines tiny versions of many common Unix utilities into a single executable:

```bash
$ ls -la /bin/
busybox
ls -> busybox
cat -> busybox
sh -> busybox
mount -> busybox
# ... hundreds more symlinks
```

When you run `ls`, it's actually `busybox` checking how it was invoked (`argv[0]`) and behaving accordingly. This saves enormous space—one 1 MB binary instead of dozens of separate programs.

### Getting BusyBox Source

```bash
# Clone BusyBox repository
git clone --depth=1 https://git.busybox.net/busybox
cd busybox

# Or download a release
wget https://busybox.net/downloads/busybox-1.36.1.tar.bz2
tar xf busybox-1.36.1.tar.bz2
cd busybox-1.36.1
```

### Configuring BusyBox

BusyBox has its own configuration system (similar to the kernel's):

```bash
# Start with default configuration
make defconfig

# Or start with minimal configuration
make allnoconfig

# Interactive configuration
make menuconfig
```

**Critical setting**: We need **static linking**. A dynamically linked binary would require shared libraries (libc, etc.) which we'd have to include in our initramfs.

In menuconfig:
```
Settings --->
    [*] Build static binary (no shared libs)
```

Or set it directly:
```bash
sed -i 's/# CONFIG_STATIC is not set/CONFIG_STATIC=y/' .config
```

### Building BusyBox

```bash
# Build (on ARM64, no cross-compiler needed)
make -j$(nproc)

# Or cross-compile from x86
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc)

# The result is the 'busybox' binary
ls -la busybox
# -rwxr-xr-x 1 user user 1044576 ... busybox
```

### Installing BusyBox

BusyBox has an install target that creates the symlink structure:

```bash
# Install to a directory (we'll use this for initramfs)
make CONFIG_PREFIX=/path/to/initramfs install
```

This creates:
```
/path/to/initramfs/
├── bin/
│   ├── busybox
│   ├── sh -> busybox
│   ├── ls -> busybox
│   ├── cat -> busybox
│   └── ... (many symlinks)
├── sbin/
│   ├── init -> ../bin/busybox
│   ├── mount -> ../bin/busybox
│   └── ...
└── usr/
    ├── bin/
    └── sbin/
```

## Initramfs: The Initial RAM Filesystem

### What is Initramfs?

**Initramfs** (Initial RAM Filesystem) is a small filesystem loaded into RAM at boot. It provides the minimal environment needed to:

1. Load kernel modules (drivers)
2. Mount the real root filesystem
3. Run the init process

For our simple VM, initramfs IS our entire root filesystem—we won't mount anything else.

### Why Not Use Initramfs for a Full OS?

You could, but it has drawbacks:

| Initramfs | Disk-based Root |
|-----------|-----------------|
| Lives entirely in RAM | Only active files in RAM |
| Lost on reboot | Persistent storage |
| Size limited by RAM | Can be much larger |
| Fast (no disk I/O) | Slower initial load |

Initramfs is meant to be minimal—just enough to get to the real root. For embedded systems or VMs where persistence isn't needed, it works fine as the only filesystem.

### What is CPIO?

**CPIO** (Copy In/Out) is an archive format, like tar. The kernel expects initramfs in CPIO format (specifically, the "newc" variant).

The format is simple:
```
[header][filename][padding][file_data][padding]
[header][filename][padding][file_data][padding]
...
[TRAILER!!!]
```

Each file has a header with metadata (size, mode, etc.), followed by the filename and file contents.

### Creating the Initramfs Structure

Let's build our initramfs directory:

```bash
# Create the directory structure
mkdir -p initramfs/{bin,sbin,dev,proc,sys,etc,tmp}

# Copy BusyBox
cp /path/to/busybox initramfs/bin/

# Create essential symlinks
cd initramfs/bin
for cmd in sh ls cat echo mount umount mkdir rm cp mv; do
    ln -s busybox $cmd
done
cd ../sbin
ln -s ../bin/busybox init
cd ../..
```

### The Init Script

When the kernel finishes initialization, it runs `/init` (or `/sbin/init`). This is the first userspace process (PID 1). It must:

1. Mount essential filesystems
2. Set up the environment
3. Start services or a shell

Create `initramfs/init`:

```bash
#!/bin/sh

# Mount essential filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

# Display boot message
echo "=========================================="
echo "  Welcome to our VMM!"
echo "  Linux $(uname -r) on $(uname -m)"
echo "=========================================="

# Start an interactive shell
exec /bin/sh
```

Make it executable:
```bash
chmod +x initramfs/init
```

### Device Nodes

Linux needs certain device files in `/dev`. With `devtmpfs`, the kernel creates them automatically. But for very early boot (before mounting devtmpfs), we might need:

```bash
# Create console device (for kernel messages)
sudo mknod initramfs/dev/console c 5 1

# Create null device
sudo mknod initramfs/dev/null c 1 3
```

The `c` means character device. The numbers are major and minor device numbers.

### Creating the CPIO Archive

Pack everything into a CPIO archive:

```bash
cd initramfs

# Create the archive
find . | cpio -o -H newc > ../initramfs.cpio

# Optionally compress it (kernel must have decompression support)
gzip -k ../initramfs.cpio
# Result: initramfs.cpio.gz
```

The `-H newc` specifies the "new" CPIO format that Linux expects.

### Initramfs Size

Our minimal initramfs is quite small:
```bash
$ ls -lh initramfs.cpio*
-rw-r--r-- 1 user user 1.5M ... initramfs.cpio
-rw-r--r-- 1 user user 620K ... initramfs.cpio.gz
```

The kernel can handle both compressed and uncompressed. Compressed saves memory but requires decompression support in the kernel.

## Memory Layout for Boot

Now let's plan where everything goes in guest RAM:

```
Guest Physical Address Space:

0x40000000 ┌─────────────────────────────────────┐ RAM_BASE
           │  (reserved for early boot)           │
           │                                      │
0x40080000 ├──────────────────────────────────────┤ KERNEL_ADDR (text_offset=0x80000)
           │                                      │
           │         Linux Kernel Image           │
           │         (~3-30 MB)                   │
           │                                      │
           │  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │ (gap: kernel early allocations)
           │                                      │
0x48000000 ├──────────────────────────────────────┤ INITRD_ADDR (128 MB offset)
           │                                      │
           │         Initramfs                    │
           │         (~0.5-50 MB)                 │
           │                                      │
           ├──────────────────────────────────────┤ (page aligned after initramfs)
           │         Device Tree Blob             │
           │         (~2 KB)                      │
           ├──────────────────────────────────────┤
           │                                      │
           │         (free space)                 │
           │                                      │
0x80000000 └─────────────────────────────────────┘ RAM_END (with 1GB RAM)
```

### Placement Decisions

**Kernel at RAM_BASE + text_offset**
- Required by ARM64 boot protocol
- The `text_offset` from the kernel header tells us exactly where (typically 0x80000)
- Modern kernels with `text_offset = 0` can be placed anywhere

**Initramfs at 128 MB offset (0x48000000)**

This might seem wasteful—why not place it right after the kernel? The reason is **early kernel memory allocations**.

During boot, the kernel performs various allocations starting from the end of its loaded image. If we place the initramfs immediately after the kernel, these allocations can overwrite the initramfs before it's unpacked! The symptom is a cryptic error:

```
Initramfs unpacking failed: invalid magic at start of compressed archive
```

By placing the initramfs at a fixed 128 MB offset, we leave plenty of room for early allocations. The kernel's `memblock` allocator starts at low addresses, so our high-offset initramfs stays safe.

**DTB after initramfs**
- Placed immediately after initramfs (page-aligned)
- DTBs are small (~2 KB for our VM), so placement is flexible
- Must be within the kernel's initial identity-mapped region

### Why These Specific Addresses?

**RAM_BASE = 0x40000000 (1 GB)**

This is convention from QEMU's "virt" machine. The first 1 GB is reserved for:
- Flash/ROM at 0x00000000
- GIC at 0x08000000
- UART at 0x09000000
- Virtio at 0x0A000000
- PCI (if used) at various addresses

Starting RAM at 1 GB gives plenty of space for device MMIO.

**INITRD at RAM_BASE + 128 MB**

The 128 MB offset provides ample space for:
- The kernel image (~3-30 MB depending on config)
- Early kernel allocations (page tables, per-CPU data, etc.)
- A safety margin to avoid accidental overwrites

Firecracker and other production VMMs use similar strategies—placing the initramfs at a fixed high offset rather than immediately after the kernel.

## Implementation: The Boot Module

Let's implement the code to load and boot Linux. We'll create a new module: `src/god/boot/`.

### Boot Info Dataclass

First, define the data structures. Create `src/god/boot/__init__.py`:

```python
"""
Linux boot support.

This module handles loading and booting Linux kernels on ARM64.
"""

from .kernel import KernelImage, KernelError
from .dtb import DeviceTreeGenerator
from .loader import BootInfo, BootLoader

__all__ = [
    "KernelImage",
    "KernelError",
    "DeviceTreeGenerator",
    "BootInfo",
    "BootLoader",
]
```

Create `src/god/boot/kernel.py`:

```python
"""
ARM64 Linux kernel image handling.

This module parses the ARM64 kernel Image header to extract
boot parameters like text_offset and image_size.
"""

import struct
from dataclasses import dataclass
from pathlib import Path


class KernelError(Exception):
    """Exception raised for kernel-related errors."""
    pass


# ARM64 kernel magic number: "ARM\x64" in little-endian
ARM64_MAGIC = 0x644D5241


@dataclass
class KernelImage:
    """
    Parsed ARM64 kernel image.

    The ARM64 kernel Image has a 64-byte header containing
    boot parameters. This class parses that header and provides
    the information needed to load and boot the kernel.

    Attributes:
        path: Path to the kernel Image file
        text_offset: Offset from RAM base where kernel should be loaded
        image_size: Size of the kernel image in bytes
        flags: Kernel flags (endianness, page size, etc.)
        data: Raw kernel image bytes
    """
    path: Path
    text_offset: int
    image_size: int
    flags: int
    data: bytes

    @classmethod
    def load(cls, path: str | Path) -> "KernelImage":
        """
        Load and parse an ARM64 kernel image.

        Args:
            path: Path to the kernel Image file

        Returns:
            Parsed KernelImage

        Raises:
            KernelError: If the file is not a valid ARM64 kernel
            FileNotFoundError: If the file doesn't exist
        """
        path = Path(path)

        with open(path, "rb") as f:
            data = f.read()

        if len(data) < 64:
            raise KernelError(
                f"File too small ({len(data)} bytes) - not a valid kernel"
            )

        # Parse the 64-byte header
        # struct arm64_image_header {
        #     uint32_t code0;        // offset 0
        #     uint32_t code1;        // offset 4
        #     uint64_t text_offset;  // offset 8
        #     uint64_t image_size;   // offset 16
        #     uint64_t flags;        // offset 24
        #     uint64_t res2;         // offset 32
        #     uint64_t res3;         // offset 40
        #     uint64_t res4;         // offset 48
        #     uint32_t magic;        // offset 56
        #     uint32_t res5;         // offset 60
        # };

        (
            code0,
            code1,
            text_offset,
            image_size,
            flags,
            res2,
            res3,
            res4,
            magic,
            res5,
        ) = struct.unpack("<IIQQQQQQI I", data[:64])

        # Verify magic number
        if magic != ARM64_MAGIC:
            raise KernelError(
                f"Invalid magic number: 0x{magic:08x} "
                f"(expected 0x{ARM64_MAGIC:08x} 'ARM\\x64')"
            )

        # Sanity check text_offset
        if text_offset == 0:
            # Some kernels use 0 to mean "use default"
            text_offset = 0x80000  # 512 KB default

        # Sanity check image_size
        if image_size == 0:
            # Use actual file size
            image_size = len(data)

        return cls(
            path=path,
            text_offset=text_offset,
            image_size=image_size,
            flags=flags,
            data=data,
        )

    @property
    def is_little_endian(self) -> bool:
        """Check if kernel is little-endian."""
        return (self.flags & 1) == 0

    @property
    def page_size(self) -> int | None:
        """
        Get kernel's expected page size.

        Returns:
            Page size in bytes, or None if unspecified
        """
        ps = (self.flags >> 1) & 0x3
        if ps == 0:
            return None  # Unspecified
        elif ps == 1:
            return 4096  # 4 KB
        elif ps == 2:
            return 16384  # 16 KB
        elif ps == 3:
            return 65536  # 64 KB
        return None

    def __repr__(self) -> str:
        return (
            f"KernelImage(path={self.path}, "
            f"text_offset=0x{self.text_offset:x}, "
            f"image_size={self.image_size} bytes)"
        )
```

### Boot Loader

Create `src/god/boot/loader.py`:

```python
"""
Boot loader for Linux kernels.

This module handles loading the kernel, initramfs, and DTB into
guest memory and setting up the vCPU state for boot.
"""

from dataclasses import dataclass
from pathlib import Path

from god.vm.layout import RAM_BASE
from god.vm.memory import MemoryManager
from god.vcpu import registers


@dataclass
class BootInfo:
    """
    Information about loaded boot components.

    This is returned by BootLoader.load() and contains all the
    addresses and sizes needed to boot the kernel.

    Attributes:
        kernel_addr: Guest physical address of kernel
        kernel_size: Size of kernel in bytes
        initrd_addr: Guest physical address of initramfs (0 if none)
        initrd_size: Size of initramfs in bytes (0 if none)
        dtb_addr: Guest physical address of DTB
        dtb_size: Size of DTB in bytes
    """
    kernel_addr: int
    kernel_size: int
    initrd_addr: int
    initrd_size: int
    dtb_addr: int
    dtb_size: int

    @property
    def initrd_end(self) -> int:
        """End address of initramfs (for Device Tree)."""
        return self.initrd_addr + self.initrd_size


class BootLoader:
    """
    Loads Linux boot components into guest memory.

    This class handles:
    - Loading the kernel at the correct offset
    - Loading initramfs at a safe location (128 MB into RAM)
    - Placing the DTB after the initramfs
    - Setting up vCPU registers for boot

    Usage:
        loader = BootLoader(memory, ram_size)
        boot_info = loader.load(
            kernel_path="Image",
            initrd_path="initramfs.cpio",
            dtb_data=dtb_bytes,
        )
        loader.setup_vcpu(vcpu, boot_info)
    """

    def __init__(self, memory: MemoryManager, ram_size: int):
        """
        Create a boot loader.

        Args:
            memory: The guest memory manager
            ram_size: Size of guest RAM in bytes
        """
        self._memory = memory
        self._ram_size = ram_size
        self._ram_base = RAM_BASE

    def load(
        self,
        kernel_path: str | Path,
        initrd_path: str | Path | None = None,
        dtb_data: bytes | None = None,
    ) -> BootInfo:
        """
        Load boot components into guest memory.

        Args:
            kernel_path: Path to kernel Image file
            initrd_path: Path to initramfs (optional)
            dtb_data: DTB blob bytes (required)

        Returns:
            BootInfo with addresses of loaded components

        Raises:
            ValueError: If dtb_data is not provided
        """
        from .kernel import KernelImage

        if dtb_data is None:
            raise ValueError("DTB data is required")

        # Load kernel at RAM_BASE + text_offset
        kernel = KernelImage.load(kernel_path)
        kernel_addr = self._ram_base + kernel.text_offset
        self._memory.write(kernel_addr, kernel.data)
        print(f"Loaded kernel at 0x{kernel_addr:08x} ({len(kernel.data)} bytes)")

        # Place initramfs at 128 MB into RAM to avoid early kernel allocations
        # The kernel's memblock allocator can overwrite data placed immediately
        # after the kernel image, causing "invalid magic" errors during unpack.
        initrd_addr = self._ram_base + (128 * 1024 * 1024)  # 128 MB offset
        initrd_addr = (initrd_addr + 0xFFF) & ~0xFFF  # Align to 4KB

        # Load initramfs
        initrd_size = 0
        next_addr = initrd_addr
        if initrd_path is not None:
            initrd_path = Path(initrd_path)
            with open(initrd_path, "rb") as f:
                initrd_data = f.read()
            self._memory.write(initrd_addr, initrd_data)
            initrd_size = len(initrd_data)
            next_addr = initrd_addr + initrd_size
            print(f"Loaded initramfs at 0x{initrd_addr:08x} ({initrd_size} bytes)")

        # Place DTB right after initramfs (page-aligned)
        dtb_addr = (next_addr + 0xFFF) & ~0xFFF
        self._memory.write(dtb_addr, dtb_data)
        print(f"Loaded DTB at 0x{dtb_addr:08x} ({len(dtb_data)} bytes)")

        return BootInfo(
            kernel_addr=kernel_addr,
            kernel_size=len(kernel.data),
            initrd_addr=initrd_addr if initrd_size > 0 else 0,
            initrd_size=initrd_size,
            dtb_addr=dtb_addr,
            dtb_size=len(dtb_data),
        )

    def setup_vcpu(self, vcpu, boot_info: BootInfo) -> None:
        """
        Configure vCPU registers for Linux boot.

        Sets up the ARM64 Linux boot protocol:
        - x0 = DTB address (physical)
        - x1, x2, x3 = 0 (reserved)
        - PC = kernel entry point (physical)
        - PSTATE = EL1h with all exceptions masked

        Args:
            vcpu: The VCPU to configure
            boot_info: Boot information from load()
        """
        # x0 = DTB address (Linux boot protocol)
        vcpu.set_register(registers.X0, boot_info.dtb_addr)

        # x1, x2, x3 = 0 (reserved for future use)
        vcpu.set_register(registers.X1, 0)
        vcpu.set_register(registers.X2, 0)
        vcpu.set_register(registers.X3, 0)

        # PC = kernel entry point
        vcpu.set_pc(boot_info.kernel_addr)

        # PSTATE = EL1h with all exceptions masked
        # This is required by the ARM64 Linux boot protocol
        pstate = (
            registers.PSTATE_MODE_EL1H |  # EL1, using SP_EL1
            registers.PSTATE_D |           # Mask Debug exceptions
            registers.PSTATE_A |           # Mask SError
            registers.PSTATE_I |           # Mask IRQ
            registers.PSTATE_F             # Mask FIQ
        )
        vcpu.set_pstate(pstate)

        print(f"vCPU configured: PC=0x{boot_info.kernel_addr:08x}, "
              f"x0(DTB)=0x{boot_info.dtb_addr:08x}")
```

### Device Tree Generator

Create `src/god/boot/dtb.py`:

```python
"""
Device Tree Blob generation.

This module generates the DTB (Device Tree Blob) that describes
our virtual machine's hardware to the Linux kernel.

We use the fdt library for DTB generation. Install it with:
    uv add fdt
"""

from dataclasses import dataclass

from god.vm.layout import (
    RAM_BASE,
    GIC_DISTRIBUTOR,
    GIC_REDISTRIBUTOR,
    UART,
    UART_IRQ,
)
from god.devices.timer import Timer


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
    - PL011 UART (inside SOC node)
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
                "fdt is required for DTB generation. "
                "Install it with: uv add fdt"
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
        root.append(self._create_soc())  # UART inside SOC

        # Create the FDT and convert to bytes
        dt = fdt.FDT()
        dt.root = root

        return dt.to_dtb(version=17)

    def _create_aliases(self) -> "fdt.Node":
        """Create the aliases node for device naming."""
        import fdt

        aliases = fdt.Node("aliases")
        # Path includes /soc/ prefix since UART is under SOC node
        aliases.append(fdt.PropStrings("serial0", f"/soc/pl011@{UART.base:x}"))
        return aliases

    def _create_chosen(self, config: DTBConfig) -> "fdt.Node":
        """Create the chosen node (boot configuration)."""
        import fdt

        chosen = fdt.Node("chosen")
        chosen.append(fdt.PropStrings("bootargs", config.cmdline))
        # Path includes /soc/ prefix
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
        # phandle for interrupt-parent references
        gic.append(fdt.PropWords("phandle", 1))
        return gic

    def _create_timer(self) -> "fdt.Node":
        """Create the ARM timer node."""
        import fdt

        timer_node = fdt.Node("timer")
        timer_node.append(fdt.PropStrings("compatible", "arm,armv8-timer"))
        timer_node.append(fdt.PropWords("interrupt-parent", 1))

        # Timer interrupts (4 PPIs)
        timer = Timer()
        interrupts = []
        for ppi in [
            timer.ppi_secure_phys,      # 29 -> DT 13
            timer.ppi_nonsecure_phys,   # 30 -> DT 14
            timer.ppi_virtual,          # 27 -> DT 11
            timer.ppi_hypervisor,       # 26 -> DT 10
        ]:
            dt_num = ppi - 16  # Convert to DT-relative number
            interrupts.extend([1, dt_num, 4])  # PPI type, number, level-triggered

        timer_node.append(fdt.PropWords("interrupts", *interrupts))
        timer_node.append(fdt.Property("always-on"))
        return timer_node

    def _create_clock(self) -> "fdt.Node":
        """Create the fixed clock node for UART."""
        import fdt

        clock = fdt.Node("apb-pclk")
        clock.append(fdt.PropStrings("compatible", "fixed-clock"))
        clock.append(fdt.PropWords("#clock-cells", 0))
        clock.append(fdt.PropWords("clock-frequency", 24000000))  # 24 MHz
        clock.append(fdt.PropWords("phandle", 2))
        return clock

    def _create_soc(self) -> "fdt.Node":
        """Create the SOC node containing platform devices."""
        import fdt

        soc = fdt.Node("soc")
        soc.append(fdt.PropStrings("compatible", "simple-bus"))
        soc.append(fdt.PropWords("#address-cells", 2))
        soc.append(fdt.PropWords("#size-cells", 2))
        soc.append(fdt.Property("ranges"))

        # Add UART inside the SOC node
        soc.append(self._create_uart())
        return soc

    def _create_uart(self) -> "fdt.Node":
        """Create the PL011 UART node."""
        import fdt

        uart = fdt.Node(f"pl011@{UART.base:x}")
        uart.append(fdt.PropStrings("compatible", "arm,pl011", "arm,primecell"))
        uart.append(fdt.PropStrings("status", "okay"))
        # Peripheral ID is CRITICAL for AMBA device binding!
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
        uart.append(fdt.PropWords("interrupt-parent", 1))
        spi_num = UART_IRQ - 32  # Convert to SPI number
        uart.append(fdt.PropWords("interrupts", 0, spi_num, 4))
        uart.append(fdt.PropStrings("clock-names", "uartclk", "apb_pclk"))
        uart.append(fdt.PropWords("clocks", 2, 2))
        return uart
```

## Adding fdt Dependency

We need to add the `fdt` library to our project:

```bash
uv add fdt
```

This adds a pure Python library for creating and parsing Device Tree Blobs.

## The Boot CLI Command

Now let's add the `god boot` command. Update `src/god/cli.py`:

```python
@app.command("boot")
def boot_linux(
    kernel: str = typer.Argument(..., help="Path to kernel Image"),
    initrd: str = typer.Option(
        None,
        "--initrd", "-i",
        help="Path to initramfs (cpio or cpio.gz)"
    ),
    cmdline: str = typer.Option(
        "console=ttyAMA0 earlycon=pl011,0x09000000",
        "--cmdline", "-c",
        help="Kernel command line"
    ),
    ram_mb: int = typer.Option(
        1024,
        "--ram", "-r",
        help="RAM size in megabytes"
    ),
    dtb: str = typer.Option(
        None,
        "--dtb", "-d",
        help="Path to custom DTB file (optional, generates one if not provided)"
    ),
):
    """
    Boot a Linux kernel.

    Loads the kernel and optional initramfs into the VM and starts execution.
    A Device Tree is generated automatically unless a custom one is provided.

    Examples:
        god boot Image --initrd initramfs.cpio
        god boot Image -i rootfs.cpio.gz -c "console=ttyAMA0 debug"
        god boot Image --dtb custom.dtb --ram 2048
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vcpu.runner import VMRunner, RunnerError
    from god.devices import DeviceRegistry, PL011UART
    from god.boot import BootLoader, DeviceTreeGenerator, DTBConfig, KernelError

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
                    from god.boot.kernel import KernelImage
                    from god.vm.layout import RAM_BASE

                    kernel_img = KernelImage.load(kernel)
                    kernel_addr = RAM_BASE + kernel_img.text_offset

                    # Place initramfs at 128MB offset into RAM
                    # This avoids early kernel memory allocations that could corrupt it
                    initrd_addr = RAM_BASE + (128 * 1024 * 1024)
                    initrd_addr = (initrd_addr + 0xFFF) & ~0xFFF  # Page align

                    # Calculate initrd end if we have one
                    initrd_start = 0
                    initrd_end = 0
                    if initrd:
                        from pathlib import Path
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

                # Load everything
                boot_info = loader.load(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    dtb_data=dtb_data,
                )

                # Set up vCPU for boot
                loader.setup_vcpu(vcpu, boot_info)

                print()
                print("=" * 60)
                print("Starting Linux...")
                print("=" * 60)
                print()

                # Run!
                stats = runner.run(max_exits=10_000_000, quiet=True)

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
```

## Build Automation

Let's create a Python module to automate building the kernel and BusyBox. Create `src/god/build/`:

Create `src/god/build/__init__.py`:

```python
"""
Build automation for kernel and userspace components.
"""

from .kernel import KernelBuilder
from .busybox import BusyBoxBuilder
from .initramfs import InitramfsBuilder

__all__ = ["KernelBuilder", "BusyBoxBuilder", "InitramfsBuilder"]
```

Create `src/god/build/kernel.py`:

```python
"""
Linux kernel build automation.

This module handles downloading, configuring, and building the Linux kernel.
Built artifacts are cached to avoid rebuilding.
"""

import subprocess
import shutil
from pathlib import Path


class KernelBuilder:
    """
    Automates Linux kernel building.

    Usage:
        builder = KernelBuilder(work_dir="./build")
        builder.download(version="6.12")
        builder.configure()
        image_path = builder.build()
    """

    KERNEL_GIT = "https://github.com/torvalds/linux.git"

    def __init__(self, work_dir: str | Path = "./build"):
        """
        Create a kernel builder.

        Args:
            work_dir: Directory for source and build artifacts
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir = self.work_dir / "linux"
        self.image_path = self.source_dir / "arch/arm64/boot/Image"

    def download(self, version: str = "6.12") -> None:
        """
        Download kernel source.

        Args:
            version: Kernel version tag (e.g., "6.12", "6.6.10")
        """
        if self.source_dir.exists():
            print(f"Kernel source already exists at {self.source_dir}")
            return

        print(f"Cloning Linux kernel v{version}...")
        subprocess.run(
            [
                "git", "clone",
                "--depth=1",
                f"--branch=v{version}",
                self.KERNEL_GIT,
                str(self.source_dir),
            ],
            check=True,
        )
        print("Clone complete")

    def configure(self, minimal: bool = True) -> None:
        """
        Configure the kernel.

        Args:
            minimal: If True, create a minimal config for our VMM
        """
        print("Configuring kernel...")

        # Start with defconfig
        subprocess.run(
            ["make", "ARCH=arm64", "defconfig"],
            cwd=self.source_dir,
            check=True,
        )

        if minimal:
            # Apply our minimal config tweaks
            self._apply_minimal_config()

        print("Configuration complete")

    def _apply_minimal_config(self) -> None:
        """Apply minimal configuration for fast boot."""
        config_path = self.source_dir / ".config"

        # Read current config
        with open(config_path) as f:
            config = f.read()

        # Options to enable
        enable = [
            "CONFIG_SERIAL_AMBA_PL011=y",
            "CONFIG_SERIAL_AMBA_PL011_CONSOLE=y",
            "CONFIG_ARM_GIC_V3=y",
            "CONFIG_ARM_ARCH_TIMER=y",
            "CONFIG_BLK_DEV_INITRD=y",
            "CONFIG_VIRTIO=y",
            "CONFIG_VIRTIO_MMIO=y",
            "CONFIG_VIRTIO_BLK=y",
            "CONFIG_VIRTIO_CONSOLE=y",
            "CONFIG_EARLY_PRINTK=y",
        ]

        # Options to disable (speed up boot)
        disable = [
            "CONFIG_MODULES",  # No loadable modules
            "CONFIG_NETWORK_FILESYSTEMS",
            "CONFIG_NFS_FS",
            "CONFIG_CIFS",
        ]

        # Apply changes
        for opt in enable:
            key = opt.split("=")[0]
            # Remove any existing setting
            config = "\n".join(
                line for line in config.split("\n")
                if not line.startswith(key)
            )
            config += f"\n{opt}"

        for opt in disable:
            config = config.replace(f"{opt}=y", f"# {opt} is not set")
            config = config.replace(f"{opt}=m", f"# {opt} is not set")

        with open(config_path, "w") as f:
            f.write(config)

        # Run olddefconfig to resolve dependencies
        subprocess.run(
            ["make", "ARCH=arm64", "olddefconfig"],
            cwd=self.source_dir,
            check=True,
        )

    def build(self) -> Path:
        """
        Build the kernel.

        Returns:
            Path to the built Image file
        """
        if self.image_path.exists():
            print(f"Kernel already built at {self.image_path}")
            return self.image_path

        print("Building kernel (this may take a while)...")

        # Determine parallelism
        import os
        jobs = os.cpu_count() or 4

        subprocess.run(
            ["make", "ARCH=arm64", f"-j{jobs}", "Image"],
            cwd=self.source_dir,
            check=True,
        )

        print(f"Build complete: {self.image_path}")
        return self.image_path

    def clean(self) -> None:
        """Remove built artifacts."""
        subprocess.run(
            ["make", "ARCH=arm64", "clean"],
            cwd=self.source_dir,
            check=True,
        )
```

Create `src/god/build/busybox.py`:

```python
"""
BusyBox build automation.

This module handles downloading, configuring, and building BusyBox
for use in initramfs.
"""

import subprocess
from pathlib import Path


class BusyBoxBuilder:
    """
    Automates BusyBox building.

    Usage:
        builder = BusyBoxBuilder(work_dir="./build")
        builder.download()
        builder.configure()
        busybox_path = builder.build()
    """

    BUSYBOX_GIT = "https://git.busybox.net/busybox"

    def __init__(self, work_dir: str | Path = "./build"):
        """
        Create a BusyBox builder.

        Args:
            work_dir: Directory for source and build artifacts
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir = self.work_dir / "busybox"
        self.binary_path = self.source_dir / "busybox"

    def download(self, version: str = "1_36_1") -> None:
        """
        Download BusyBox source.

        Args:
            version: BusyBox version tag (e.g., "1_36_1")
        """
        if self.source_dir.exists():
            print(f"BusyBox source already exists at {self.source_dir}")
            return

        print(f"Cloning BusyBox {version}...")
        subprocess.run(
            [
                "git", "clone",
                "--depth=1",
                f"--branch={version}",
                self.BUSYBOX_GIT,
                str(self.source_dir),
            ],
            check=True,
        )
        print("Clone complete")

    def configure(self) -> None:
        """Configure BusyBox for static linking."""
        print("Configuring BusyBox...")

        # Start with default config
        subprocess.run(
            ["make", "defconfig"],
            cwd=self.source_dir,
            check=True,
        )

        # Enable static linking
        config_path = self.source_dir / ".config"
        with open(config_path) as f:
            config = f.read()

        # Enable static linking (critical!)
        config = config.replace(
            "# CONFIG_STATIC is not set",
            "CONFIG_STATIC=y"
        )

        with open(config_path, "w") as f:
            f.write(config)

        # Resolve dependencies
        subprocess.run(
            ["make", "oldconfig"],
            cwd=self.source_dir,
            input=b"\n" * 100,  # Accept defaults
            check=True,
        )

        print("Configuration complete")

    def build(self) -> Path:
        """
        Build BusyBox.

        Returns:
            Path to the built busybox binary
        """
        if self.binary_path.exists():
            print(f"BusyBox already built at {self.binary_path}")
            return self.binary_path

        print("Building BusyBox...")

        import os
        jobs = os.cpu_count() or 4

        subprocess.run(
            ["make", f"-j{jobs}"],
            cwd=self.source_dir,
            check=True,
        )

        print(f"Build complete: {self.binary_path}")
        return self.binary_path

    def install(self, prefix: Path) -> None:
        """
        Install BusyBox to a directory.

        Creates the symlink structure needed for a working system.

        Args:
            prefix: Directory to install to
        """
        print(f"Installing BusyBox to {prefix}...")

        subprocess.run(
            ["make", f"CONFIG_PREFIX={prefix}", "install"],
            cwd=self.source_dir,
            check=True,
        )

        print("Install complete")
```

Create `src/god/build/initramfs.py`:

```python
"""
Initramfs creation.

This module creates CPIO archives for use as initramfs.
"""

import os
import stat
import subprocess
from pathlib import Path


class InitramfsBuilder:
    """
    Creates initramfs archives.

    Usage:
        builder = InitramfsBuilder(work_dir="./build")
        builder.create_structure()
        builder.install_busybox(busybox_path)
        builder.create_init()
        cpio_path = builder.pack()
    """

    INIT_SCRIPT = """\
#!/bin/sh

# Mount essential filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

# Display banner
echo "=========================================="
echo "  Welcome to our VMM!"
echo "  Linux $(uname -r) on $(uname -m)"
echo "=========================================="
echo

# Start shell
exec /bin/sh
"""

    def __init__(self, work_dir: str | Path = "./build"):
        """
        Create an initramfs builder.

        Args:
            work_dir: Directory for build artifacts
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rootfs_dir = self.work_dir / "initramfs"
        self.cpio_path = self.work_dir / "initramfs.cpio"

    def create_structure(self) -> None:
        """Create the directory structure for initramfs."""
        print("Creating initramfs structure...")

        # Remove old if exists
        if self.rootfs_dir.exists():
            import shutil
            shutil.rmtree(self.rootfs_dir)

        # Create directories
        dirs = ["bin", "sbin", "dev", "proc", "sys", "etc", "tmp", "root"]
        for d in dirs:
            (self.rootfs_dir / d).mkdir(parents=True)

        # Create essential device nodes
        # (These require root/CAP_MKNOD, so we may need to skip)
        try:
            dev = self.rootfs_dir / "dev"
            os.mknod(dev / "console", stat.S_IFCHR | 0o600, os.makedev(5, 1))
            os.mknod(dev / "null", stat.S_IFCHR | 0o666, os.makedev(1, 3))
            print("Created device nodes")
        except PermissionError:
            print("Note: Could not create device nodes (need root)")
            print("      Kernel devtmpfs will handle this at boot")

        print("Structure created")

    def install_busybox(self, busybox_path: Path) -> None:
        """
        Install BusyBox and create symlinks.

        Args:
            busybox_path: Path to busybox binary
        """
        print("Installing BusyBox...")

        # Copy busybox binary
        import shutil
        dest = self.rootfs_dir / "bin" / "busybox"
        shutil.copy2(busybox_path, dest)
        dest.chmod(0o755)

        # Create symlinks for common commands
        commands = [
            # /bin commands
            ("bin", ["sh", "ash", "ls", "cat", "echo", "mkdir", "rm", "cp",
                     "mv", "ln", "chmod", "chown", "pwd", "sleep", "true",
                     "false", "test", "[", "[[", "printf", "kill", "ps",
                     "grep", "sed", "awk", "cut", "head", "tail", "sort",
                     "uniq", "wc", "tr", "vi", "clear", "reset", "stty",
                     "tty", "date", "uname", "hostname", "dmesg", "env",
                     "id", "whoami"]),
            # /sbin commands
            ("sbin", ["init", "mount", "umount", "poweroff", "reboot",
                      "halt", "mdev", "ifconfig", "route", "ip"]),
        ]

        for dir_name, cmds in commands:
            for cmd in cmds:
                link = self.rootfs_dir / dir_name / cmd
                if not link.exists():
                    # Relative symlink to busybox
                    if dir_name == "sbin":
                        link.symlink_to("../bin/busybox")
                    else:
                        link.symlink_to("busybox")

        print("BusyBox installed")

    def create_init(self, script: str | None = None) -> None:
        """
        Create the init script.

        Args:
            script: Custom init script content, or use default
        """
        init_path = self.rootfs_dir / "init"
        init_path.write_text(script or self.INIT_SCRIPT)
        init_path.chmod(0o755)
        print("Created /init script")

    def pack(self, compress: bool = False) -> Path:
        """
        Create the CPIO archive.

        Args:
            compress: If True, gzip the archive

        Returns:
            Path to the created archive
        """
        print("Creating CPIO archive...")

        # Use cpio to create archive
        # We use the "newc" format which Linux expects
        cpio_cmd = ["cpio", "-o", "-H", "newc"]

        # Get list of files
        result = subprocess.run(
            ["find", "."],
            cwd=self.rootfs_dir,
            capture_output=True,
            text=True,
            check=True,
        )

        # Create archive
        with open(self.cpio_path, "wb") as f:
            subprocess.run(
                cpio_cmd,
                cwd=self.rootfs_dir,
                input=result.stdout,
                stdout=f,
                text=True,
                check=True,
            )

        if compress:
            subprocess.run(
                ["gzip", "-f", str(self.cpio_path)],
                check=True,
            )
            self.cpio_path = self.cpio_path.with_suffix(".cpio.gz")

        size = self.cpio_path.stat().st_size
        print(f"Created {self.cpio_path} ({size} bytes)")

        return self.cpio_path
```

## Build CLI Commands

Add build commands to the CLI. Add to `src/god/cli.py`:

```python
# Create a subcommand group for build commands
build_app = typer.Typer(help="Build kernel and userspace components")
app.add_typer(build_app, name="build")


@build_app.command("kernel")
def build_kernel(
    version: str = typer.Option("6.12", "--version", "-v", help="Kernel version"),
    work_dir: str = typer.Option("./build", "--dir", "-d", help="Build directory"),
    configure_only: bool = typer.Option(False, "--configure", help="Only configure, don't build"),
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
    from pathlib import Path
    from god.build import KernelBuilder, BusyBoxBuilder, InitramfsBuilder

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
```

## Putting It All Together

Now you can build everything and boot Linux:

```bash
# Build all components (kernel, busybox, initramfs)
god build all

# Boot Linux!
god boot ./build/linux/arch/arm64/boot/Image --initrd ./build/initramfs.cpio
```

Expected output:

```
Booting Linux with 1024 MB RAM
Kernel: ./build/linux/arch/arm64/boot/Image
Initrd: ./build/initramfs.cpio
Command line: console=ttyAMA0 earlycon=pl011,0x09000000

Generated Device Tree
Loaded kernel at 0x40080000 (15728640 bytes)
Loaded initramfs at 0x41080000 (1572864 bytes)
Loaded DTB at 0x7fe00000 (4096 bytes)
vCPU configured: PC=0x40080000, x0(DTB)=0x7fe00000

============================================================
Starting Linux...
============================================================

[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x411fd070]
[    0.000000] Linux version 6.12.0 ...
[    0.000000] Machine model: linux,dummy-virt
[    0.000000] earlycon: pl011 at MMIO 0x0000000009000000 ...
[    0.000000] Memory: 1016352K/1048576K available ...
...
[    0.xxx000] Run /init as init process
==========================================
  Welcome to our VMM!
  Linux 6.12.0 on aarch64
==========================================

/ # ls
bin   dev   etc   init  proc  root  sbin  sys   tmp
/ # uname -a
Linux (none) 6.12.0 #1 SMP ... aarch64 GNU/Linux
/ #
```

**We booted Linux!**

## Debugging Boot Failures

Boot debugging can be tricky because failures often happen before any console output. Here are systematic debugging techniques.

### Adding Register Dump on Timeout

When the vCPU hangs, you need to see what's happening. Add system register reading to your VCPU class:

```python
# In src/god/vcpu/registers.py, add system register definitions:
KVM_REG_ARM64_SYSREG = 0x0013 << 16

def _sysreg(op0: int, op1: int, crn: int, crm: int, op2: int) -> int:
    """Create a system register ID."""
    return (
        KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM64_SYSREG
        | (op0 << 14) | (op1 << 11) | (crn << 7) | (crm << 3) | op2
    )

ESR_EL1 = _sysreg(3, 0, 5, 2, 0)   # Exception Syndrome Register
FAR_EL1 = _sysreg(3, 0, 6, 0, 0)   # Fault Address Register
ELR_EL1 = _sysreg(3, 0, 4, 0, 1)   # Exception Link Register
VBAR_EL1 = _sysreg(3, 0, 12, 0, 0) # Vector Base Address Register
SCTLR_EL1 = _sysreg(3, 0, 1, 0, 0) # System Control Register
```

Then add a `dump_registers()` method to your VCPU that reads and prints these registers. When debugging, you can set a timeout and dump registers if the vCPU doesn't make progress.

### No Output At All

If you see nothing after "Starting Linux...":

1. **Check earlycon address**: Must match our UART (0x09000000)
   ```
   earlycon=pl011,0x09000000
   ```

2. **Check DTB validity**: Decompile and verify
   ```bash
   dtc -I dtb -O dts -o check.dts your.dtb
   ```

3. **Verify kernel magic**: The kernel should have magic `0x644d5241` at offset 56

4. **Check PSTATE**: Must include D, A, I, F bits masked (0x3C5)

### "Booting Linux..." Then Silence

The kernel is crashing very early. The ESR_EL1 register tells you why:

| ESR Exception Class | Meaning |
|---------------------|---------|
| 0x20 | Instruction abort from lower EL |
| 0x21 | Instruction abort from same EL |
| 0x24 | Data abort from lower EL |
| 0x25 | Data abort from same EL |
| 0x15 | SVC instruction |
| 0x16 | HVC instruction |

Common causes:
1. **GIC not initialized**: Call `gic.finalize()` before running vCPU
2. **Timer node missing from DTB**: Kernel hangs waiting for timer
3. **Wrong kernel load address**: Check `text_offset` from kernel header

### Initramfs Unpacking Failed

```
Initramfs unpacking failed: invalid magic at start of compressed archive
```

This means the initramfs was corrupted before unpacking. Causes:

1. **Initramfs too close to kernel**: Early kernel allocations overwrote it. Move to 128 MB offset.
2. **Wrong DTB initrd addresses**: Verify `linux,initrd-start` and `linux,initrd-end` match actual load addresses.
3. **Corrupted file**: Check with `file initramfs.cpio.gz` and `gunzip -t initramfs.cpio.gz`

### UART Console Not Working

If kernel boots but no userspace output:

1. **Check UART driver binding**: Look for `ttyAMA0 at MMIO 0x9000000` in kernel output
2. **Missing `arm,primecell-periphid`**: AMBA devices need this for driver binding
3. **UART not under `soc` node**: Must be under a `simple-bus` compatible parent
4. **Check stdout-path**: Should point to `/soc/pl011@9000000`

### Using earlycon

**earlycon** (early console) provides output before normal console drivers load:

```
earlycon=pl011,0x09000000
```

This uses direct MMIO to the UART, bypassing the tty framework. If you see earlycon output but not regular console output, the problem is device binding (check DTB).

### Adding Kernel Debug Options

For verbose boot output, add to command line:

```
console=ttyAMA0 earlycon=pl011,0x09000000 debug loglevel=8 initcall_debug
```

- `debug`: Enable debug messages
- `loglevel=8`: Show all message levels
- `initcall_debug`: Show timing of each kernel init function

## What Happens During Linux Boot

Here's what the kernel does, step by step:

1. **head.S** (`arch/arm64/kernel/head.S`)
   - First code to run
   - Validates CPU state
   - Sets up initial page tables
   - Enables MMU
   - Jumps to C code

2. **start_kernel()** (`init/main.c`)
   - Parses command line
   - Initializes memory management
   - Sets up interrupts
   - Initializes device model
   - Starts scheduler

3. **rest_init()** / **kernel_init()**
   - Creates init process (PID 1)
   - Mounts initramfs
   - Executes `/init`

4. **/init** (our script)
   - Mounts /proc, /sys, /dev
   - Prints welcome message
   - Starts shell

## Summary

In this chapter, we:

1. **Learned ARM64 boot concepts**: Exception levels, PSTATE, MMU
2. **Built the Linux kernel**: From source with custom configuration
3. **Understood Device Tree**: Format, properties, and our VM's DTB
4. **Built BusyBox**: Static binary for our initramfs
5. **Created initramfs**: CPIO archive with init script
6. **Implemented boot support**: Kernel parsing, DTB generation, boot loader
7. **Added CLI commands**: `god boot` and `god build`
8. **Booted Linux!**

## What's Next?

In Chapter 8, we'll implement **virtio**—the paravirtualization standard for efficient I/O. Our current UART works but causes a VM exit for every character. Virtio batches operations, dramatically improving performance.

We'll implement:
- **virtio-console**: Efficient serial console
- **virtio-blk**: Block device (real filesystem!)
- **virtio-net**: Networking

[Continue to Chapter 8: Virtio Devices →](08-virtio-devices.md)
