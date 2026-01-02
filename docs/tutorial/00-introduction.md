# Chapter 0: Introduction - What Are We Building?

Welcome to this tutorial series where we'll build a **Virtual Machine Monitor** (VMM) from scratch using Python. By the end, we'll boot a real Linux kernel inside our creation.

This isn't just about copying code—it's about deeply understanding how virtualization works at every level. We'll explain every concept, every acronym, and every design decision along the way.

## The Big Picture

### What is a Virtual Machine?

A **virtual machine** (VM) is a software-based computer running inside your real computer. From the perspective of software running inside the VM (called the "guest"), it appears to have its own CPU, memory, disk, and devices. But in reality, all of these are being simulated or virtualized by the host system.

Think of it like The Matrix—the guest operating system lives in a simulated world, completely unaware (in most cases) that it's not running on real hardware.

**Why do people use virtual machines?**

1. **Isolation**: Run untrusted code without risking your real system
2. **Testing**: Test software on different operating systems without buying different computers
3. **Server consolidation**: Run many small servers on one big physical machine
4. **Development**: Create reproducible development environments
5. **Cloud computing**: The entire cloud runs on VMs (and containers, which build on similar concepts)

### What is a VMM (Virtual Machine Monitor)?

A **Virtual Machine Monitor**, also called a **hypervisor**, is the software that creates and manages virtual machines. It's the "architect" of our Matrix analogy—the thing that constructs and maintains the simulation.

There are two types:

**Type 1 (Bare-metal)**: Runs directly on the hardware, with guest operating systems running on top. Examples: VMware ESXi, Xen, Hyper-V.

```
┌─────────┐ ┌─────────┐ ┌─────────┐
│ Guest 1 │ │ Guest 2 │ │ Guest 3 │
├─────────┴─┴─────────┴─┴─────────┤
│         Hypervisor (Type 1)      │
├──────────────────────────────────┤
│            Hardware              │
└──────────────────────────────────┘
```

**Type 2 (Hosted)**: Runs as an application on top of a regular operating system. Examples: VirtualBox, VMware Workstation, Parallels.

```
┌─────────┐ ┌─────────┐
│ Guest 1 │ │ Guest 2 │
├─────────┴─┴─────────┤
│  Hypervisor (Type 2) │
├──────────────────────┤
│    Host OS (Linux)   │
├──────────────────────┤
│      Hardware        │
└──────────────────────┘
```

### Where Does KVM Fit In?

**KVM** (Kernel-based Virtual Machine) is interesting because it blurs the line between Type 1 and Type 2. It's a Linux kernel module that turns Linux itself into a hypervisor. The Linux kernel runs directly on hardware (like Type 1), but you can also run regular applications alongside VMs (like Type 2).

```
┌─────────┐ ┌─────────┐ ┌─────────────┐
│ Guest 1 │ │ Guest 2 │ │ Regular App │
├─────────┴─┴─────────┴─┴─────────────┤
│   Linux Kernel (with KVM module)     │
├──────────────────────────────────────┤
│              Hardware                │
└──────────────────────────────────────┘
```

**What KVM does for us:**

KVM leverages hardware virtualization features built into modern CPUs (Intel VT-x, AMD-V, ARM VHE). Instead of emulating every CPU instruction in software (which would be incredibly slow), KVM lets guest code run directly on the real CPU in a special "guest mode." The CPU hardware itself traps certain operations (like accessing devices) and returns control to our VMM so we can handle them.

**What we need to build:**

KVM handles the hard part—running guest CPU code efficiently. Our job is to:

1. Tell KVM to create virtual machines and virtual CPUs
2. Set up memory for the guest
3. Emulate devices (serial port, disk, etc.)
4. Handle situations where the CPU returns control to us

## Why Build a VMM from Scratch?

### Understanding vs. Using

You can use VirtualBox or Docker without understanding how they work. But there's a profound difference between using a tool and understanding it.

By building a VMM from scratch, you'll understand:

- How CPUs really work (privilege levels, registers, traps)
- How memory management works (virtual memory, page tables, address translation)
- How devices communicate with the CPU (interrupts, memory-mapped I/O)
- How operating systems boot (what happens before `main()`)
- Why virtualization is fast (hardware support vs. emulation)

### The Joy of "Hello World"

There's something magical about seeing "Hello, World!" printed by code running inside a virtual machine you built yourself. It's a moment where abstract concepts become tangible.

### What You'll Learn Along the Way

- **CPU architecture**: How ARM64 processors work, exception levels, registers
- **Memory systems**: Virtual memory, address translation, page tables
- **Device I/O**: How devices communicate, interrupts, MMIO
- **Operating system internals**: How Linux boots, device trees, kernel initialization
- **Systems programming**: Working close to the hardware with Python and C interop

## Key Concepts Glossary

Before we dive in, let's define all the terminology we'll use throughout this series. Bookmark this section—you'll want to come back to it.

### Virtualization Concepts

**Virtual Machine (VM)**: A software-based computer. The guest OS thinks it has real hardware, but it's all simulated.

**Guest**: The operating system and software running inside the virtual machine. It "guests" on the host's resources.

**Host**: The physical machine and operating system running the VMM. It "hosts" the virtual machines.

**VMM (Virtual Machine Monitor)**: Also called a "hypervisor." The software that creates and manages VMs.

**vCPU (Virtual CPU)**: A virtualized processor that the guest OS sees and uses. One physical CPU can run multiple vCPUs (by time-sharing).

**VM Exit**: When execution transfers from guest mode back to the host. This happens when the guest does something that requires host intervention (like accessing a device). Also just called "exit" for short.

**VM Entry**: When execution transfers from host to guest mode. The opposite of VM exit.

### KVM-Specific Concepts

**KVM (Kernel-based Virtual Machine)**: A Linux kernel module that provides hardware virtualization capabilities. Our VMM communicates with KVM to create and run VMs.

**/dev/kvm**: A special file that provides access to KVM. We open this file and send it commands.

**ioctl (I/O Control)**: A system call used to send commands to device drivers. Pronounced "eye-ock-tull." We use ioctl to communicate with KVM.

**File Descriptor**: A number that represents an open file or resource in Unix. When we open /dev/kvm, we get a file descriptor. When we create a VM, we get another file descriptor. When we create a vCPU, we get yet another.

### Memory Concepts

**Physical Address**: An actual address in RAM hardware. On a 16GB machine, physical addresses go from 0 to about 16 billion.

**Virtual Address**: An address that software uses, which gets translated to a physical address by the CPU's memory management unit. This translation allows each process to have its own private address space.

**Guest Physical Address (GPA)**: An address in the guest's view of physical memory. The guest thinks these are real physical addresses, but they're actually virtual.

**Host Virtual Address (HVA)**: An address in the host's virtual address space. Our Python program uses these addresses.

**Memory-Mapped I/O (MMIO)**: A way for the CPU to communicate with devices by reading/writing to specific memory addresses. When the guest writes to address 0x09000000, it's not writing to RAM—it's talking to our emulated serial port.

**mmap**: A system call that maps memory into a process's address space. We use it to allocate memory for the guest.

### Device Concepts

**UART (Universal Asynchronous Receiver/Transmitter)**: Hardware that handles serial communication. Think of old-school terminals connected by cables. We'll emulate ARM's PL011 UART.

**PL011**: ARM's standard UART implementation. "PL" stands for PrimeCell, ARM's peripheral IP brand. Linux has built-in support for it.

**Interrupt**: A signal from a device to the CPU saying "hey, I need attention!" The CPU stops what it's doing, runs a handler routine, then returns to what it was doing.

**GIC (Generic Interrupt Controller)**: ARM's standard interrupt controller. It routes interrupt signals from devices to CPU cores. Think of it as a traffic cop for interrupts.

**Virtio**: A standard for efficient paravirtualized devices. Instead of emulating real hardware exactly, virtio uses a cooperative protocol where the guest knows it's virtualized and uses efficient shared-memory communication.

### ARM64-Specific Concepts

**Exception Level (EL)**: ARM's privilege levels. There are four:
- EL0: User applications
- EL1: Operating system kernel
- EL2: Hypervisor
- EL3: Secure monitor / firmware

**Device Tree**: A data structure that describes hardware to the operating system. ARM systems use this because they don't have PC-style hardware discovery mechanisms. We'll generate one for our virtual machine.

**DTB (Device Tree Blob)**: The compiled binary form of a device tree. The kernel reads this at boot to learn what hardware exists.

### Boot Concepts

**Kernel**: The core of an operating system. Linux is a kernel. We'll boot the Linux kernel in our VM.

**Initramfs**: A small initial filesystem loaded into RAM at boot time. It contains just enough tools (like busybox) to mount the real root filesystem. For our VM, it will BE the root filesystem.

**Bootloader**: Software that loads and starts the kernel. Examples: GRUB, U-Boot. We won't use a bootloader—we'll load the kernel directly.

## Project Setup

### Prerequisites

You'll need:

1. **An M4 Mac** (or other Apple Silicon Mac)
2. **Python 3.14** (we use the latest features)
3. **Lima** (to run a Linux VM where we'll test our VMM)
4. **Basic familiarity with Python**
5. **Willingness to learn** (no prior systems programming required!)

### Installing Lima

Lima lets us run Linux VMs on macOS. Since we're on Apple Silicon, Lima will create ARM64 Linux VMs—perfect for our ARM64 VMM.

```bash
# Install Lima using Homebrew
brew install lima
```

This project includes a custom Lima configuration (`lima/god.yaml`) that sets up everything we need: Docker, uv, Claude Code, and proper KVM access.

```bash
# From the project root, start our custom VM
limactl start lima/god.yaml

# This takes a few minutes on first run. Lima downloads an Ubuntu image
# and runs our provisioning scripts.

# Once it's running, shell into it:
limactl shell god
```

Inside the Lima VM, verify KVM is available:

```bash
# Check if /dev/kvm exists
ls -la /dev/kvm

# You should see something like:
# crw-rw---- 1 root kvm 10, 232 Dec 26 00:00 /dev/kvm

# KVM requires root access, which is why we use sudo for commands
sudo cat /dev/kvm
# You'll see binary garbage - that's fine, it means we have access!
```

**Note**: We need `sudo` to access KVM. The Lima VM is configured with a `god` alias that handles this automatically.

### Setting Up the Python Project

Our project is called "god" (as in "the god of this virtual world we're creating"). It's already set up with the basic structure.

**Inside the Lima VM:**

```bash
# The project is mounted at ~/workplace/veleiro-god
cd ~/workplace/veleiro-god

# Use the god alias (handles sudo and venv automatically)
god --version

# Or explicitly with sudo (required for KVM access)
sudo uv run god --version
```

**Why sudo?** KVM requires root access. The Lima VM is configured so that:
- `uv` is installed system-wide (available to both user and root)
- The Python venv lives at `/opt/god/venv` (inside the VM, not on the mounted filesystem)
- The `UV_PROJECT_ENVIRONMENT` variable is preserved when using `sudo`

This setup means you can seamlessly run `sudo uv run god ...` and it uses the correct virtual environment.

The project structure we'll build:

```
veleiro-god/
├── lima/
│   └── god.yaml            # Lima VM configuration (start here!)
├── src/god/
│   ├── __init__.py
│   ├── cli.py              # Command-line interface
│   ├── kvm/                # KVM interface layer
│   ├── vm/                 # Virtual machine management
│   ├── vcpu/               # Virtual CPU management
│   ├── devices/            # Emulated devices
│   └── boot/               # Linux boot support
├── tests/
│   └── guest_code/         # Test programs to run in VM
└── docs/
    └── tutorial/           # You are here!
```

## How This Tutorial Works

### Each Chapter Builds on the Last

We'll build the VMM incrementally. Each chapter adds one major capability:

1. **Chapter 1**: Talk to KVM, verify it works
2. **Chapter 2**: Create a VM, set up memory
3. **Chapter 3**: Create a vCPU, run simple guest code
4. **Chapter 4**: Emulate a serial port, see "Hello World"
5. **Chapter 5**: Set up the interrupt controller
6. **Chapter 6**: Add a timer
7. **Chapter 7**: Boot Linux!
8. **Chapter 8**: Add virtio devices (better I/O)
9. **Chapter 9**: Reference and appendix

### Test at Every Step

At the end of each chapter, we'll have something working that we can test. Don't skip ahead—make sure each phase works before moving on.

### Code is Incremental

We'll show code changes incrementally. When we modify a file, we'll show what changed rather than the whole file (unless it's short). The full code is always in the repository.

### Structure of Each Chapter

Every chapter follows this pattern:

1. **Theory**: What are we building and why? Concepts explained before code.
2. **Implementation**: Step-by-step code with explanations.
3. **Deep Dives**: Optional explorations of interesting tangents.
4. **Testing**: How to verify it works.
5. **Gotchas**: Common mistakes and how to avoid them.
6. **What's Next**: Bridge to the following chapter.

## Our Architecture

Here's what we're building:

```
┌──────────────────────────────────────────────────────────────────┐
│                     Python VMM ("god")                           │
├──────────────────────────────────────────────────────────────────┤
│  Command Line Interface (using Typer library)                    │
│    Commands: god kvm info, god run, god boot, etc.               │
├──────────────────────────────────────────────────────────────────┤
│  VMM Core                                                        │
│    ├── VM Lifecycle Manager (create, destroy VMs)                │
│    ├── Memory Manager (allocate and map guest RAM)               │
│    ├── Virtual CPU Manager (create and run vCPUs)                │
│    └── Device Manager (coordinate emulated devices)              │
├──────────────────────────────────────────────────────────────────┤
│  Emulated Devices                                                │
│    ├── PL011 Serial Port (text input/output)                     │
│    ├── Interrupt Controller (KVM-backed, handles interrupts)     │
│    ├── Timer (lets guest track time, schedule tasks)             │
│    └── Virtio Devices (efficient paravirtualized devices)        │
├──────────────────────────────────────────────────────────────────┤
│  KVM Interface (using cffi)                                      │
│    Python bindings to /dev/kvm system calls                      │
├──────────────────────────────────────────────────────────────────┤
│  Linux Kernel (KVM module - this is part of Linux, not our code) │
└──────────────────────────────────────────────────────────────────┘
```

### What is cffi?

**cffi** stands for **C Foreign Function Interface**. It lets Python code call C functions and work with C data structures.

KVM's interface is defined in C (in Linux kernel headers). We need cffi to:

1. Define the C structures KVM expects
2. Call the ioctl system call with the right parameters
3. Read data back from KVM

We're using cffi instead of writing everything in C because it keeps our code in Python where it's easier to understand and debug.

**Trade-off**: cffi adds some overhead compared to native C. A production VMM (like QEMU or Firecracker) would use C or Rust for performance-critical parts. But our goal is learning, not performance, and cffi makes the code much more readable.

## Success Criteria

Here's what "done" looks like for each chapter:

| Chapter | Success Criterion |
|---------|-------------------|
| 1 | `god kvm info` shows KVM version 12 and capabilities |
| 2 | VM created, memory visible in `/proc/pid/maps` |
| 3 | Guest program runs and halts, writes value to memory |
| 4 | "Hello, World!" printed from guest to terminal |
| 5 | Guest can receive and handle interrupts |
| 6 | Timer interrupt fires, guest can track time |
| 7 | Virtio console and block device work |
| 8 | **Linux kernel boots to shell!** |

## Let's Begin!

In the next chapter, we'll establish communication with KVM and verify that everything is set up correctly. We'll write our first cffi bindings and run `god kvm info` to see what our system supports.

[Continue to Chapter 1: Talking to KVM →](01-kvm-foundation.md)
