# TidyCPU ⚡

> A CPU affinity optimization utility for Linux — balances core load by intelligently reassigning process affinity.

```
╔══════════════════════════════════════════════════════════╗
║          TidyCPU  —  CPU Affinity Optimizer              ║
║          Linux x64  |  Requires root                     ║
╚══════════════════════════════════════════════════════════╝
```

---

## Overview

TidyCPU identifies CPU hotspots and crowded cores, then proposes (and optionally applies) affinity changes to redistribute load across idle cores — all from a clean, color-coded CLI interface.

It's useful when you notice certain cores pegged at 90%+ while others sit idle, and you want a fast way to diagnose and fix the imbalance without manual `taskset` guesswork.

---

## Requirements

- **OS:** Linux x64 (Debian / Ubuntu recommended)
- **Python:** 3.10+ (uses `list[int]` type hints)
- **Privileges:** Must be run as `root`
- **System tools:** `taskset` (from `util-linux`), `ps`

Install `taskset` if missing:
```bash
sudo apt install util-linux
```

---

## Usage

### Standard Rebalance Mode
```bash
sudo python3 tidycpu.py
```

### Live Monitoring Mode (5 seconds, refreshing every 500ms)
```bash
sudo python3 tidycpu.py --live
```

### Live Monitoring with Custom Duration (10 seconds)
```bash
sudo python3 tidycpu.py --live --duration 10
```

### Monitor Specific Process and Its Threads
```bash
sudo python3 tidycpu.py --pid 1234
```

### Show Thread Information in Standard Mode
```bash
sudo python3 tidycpu.py --threads
```

### Include CPU Frequency Information
```bash
sudo python3 tidycpu.py --cpu-freq
```

### Export Reports

Export your analysis to a file for sharing or archiving:

```bash
# Export to HTML (beautifully styled, view in browser)
sudo python3 tidycpu.py --export-html report.html

# Export to plain text (preserves layout)
sudo python3 tidycpu.py --export-text report.txt

# Combine with other options
sudo python3 tidycpu.py --cpu-freq --export-html detailed_report.html
```

### Command-Line Options

| Option | Short | Description |
|--------|-------|-------------|
| `--help` | `-h` | Show help message and exit |
| `--live` | `-l` | Live monitoring mode (refreshes every 500ms) |
| `--duration N` | `-d N` | Duration for live mode in seconds (default: 5) |
| `--pid PID` | `-p PID` | Filter to show specific PID and its threads |
| `--threads` | `-t` | Show thread information for processes |
| `--cpu-freq` | `-f` | Show CPU frequency information (min/max/current MHz) |
| `--export-html FILE` | | Export report to HTML file with styling |
| `--export-text FILE` | | Export report to plain text file |

---

## Features

### 0. System Information Display

TidyCPU shows comprehensive hardware information before any analysis:

- **CPU Model** — Detected from `/proc/cpuinfo`
- **Memory** — Total and available RAM from `/proc/meminfo`
- **Kernel Command Line** — Boot parameters from `/proc/cmdline`
- **CPU Frequency** (optional with `--cpu-freq` flag)
  - Min/Max range
  - Current frequency per core

Example output:

```
  SYSTEM INFORMATION
  ────────────────────────────────────────────────────────
  CPU Model:
    Intel(R) Core(TM) i9-12900K @ 3.20GHz

  Memory:
    Total:     32.0 GiB
    Available: 24.3 GiB

  CPU Frequency:
    Range:   800 MHz - 5200 MHz
    Current: 3600 MHz

  Kernel Command Line:
    BOOT_IMAGE=/boot/vmlinuz-5.15.0-91-generic 
    root=UUID=abc123 ro quiet splash intel_iommu=on
```

### 1. Physical/Logical CPU Topology View

TidyCPU displays CPU usage in a **space-efficient two-column layout**:

- Real-time usage percentage and visual bar for each logical core
- Color-coded status: 🔴 HOT (≥80%), 🟡 WARM (40-79%), 🔵 COLD (<40%)
- Compact side-by-side display maximizing terminal space
- Automatic physical CPU and hyperthreading detection

Example output for a 64-core system:

```
  CPU TOPOLOGY
  ──────────────────────────────────────────────────────────
  Core   Usage  Bar                     Status    Core   Usage  Bar                     Status
  ────   ─────  ──────────────────────  ──────    ────   ─────  ──────────────────────  ──────
  CPU0    2.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD      CPU32   6.4%  █░░░░░░░░░░░░░░░░░░░░░  COLD
  CPU1    6.0%  █░░░░░░░░░░░░░░░░░░░░░  COLD      CPU33  11.7%  ██░░░░░░░░░░░░░░░░░░░░  COLD
  CPU2    2.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD      CPU34   4.5%  ░░░░░░░░░░░░░░░░░░░░░░  COLD
  CPU3  100.0%  ██████████████████████  HOT       CPU35   6.6%  █░░░░░░░░░░░░░░░░░░░░░  COLD
  ...

  Summary: ● 11 Hot  ● 2 Warm  ● 51 Cold  |  2 physical CPU(s), 64 physical cores, 64 logical cores
  Hyperthreading: Enabled
```

### 2. Live Monitoring Mode

Watch CPU usage update in real-time with automatic screen refresh:

```bash
sudo python3 tidycpu.py --live --duration 10
```

- Refreshes every 500ms
- Shows topology + top processes
- Press Ctrl+C to exit early

### 3. Thread-Level Analysis

View individual threads within multi-threaded processes:

```bash
# Monitor all top processes with their threads
sudo python3 tidycpu.py --threads

# Focus on a specific process and its threads
sudo python3 tidycpu.py --pid 5000
```

Example thread view:
```
  PID    Process                  CPU%   Cores
  5000   my_multithreaded_app    67.3%   0,1
   └─ Threads (4):
      TID 5001  main_thread      25.3%  cores:[0,1]
      TID 5002  worker_1         18.7%  cores:[0]
      TID 5003  worker_2         15.2%  cores:[1]
      TID 5004  io_handler        8.1%  cores:[0,1]
```

### 4. Report Export (HTML & Text)

Save your analysis to share with team members or keep for historical comparison:

**HTML Export** — Beautiful, styled report viewable in any browser:
```bash
sudo python3 tidycpu.py --export-html server_report.html
```

Features:
- 📊 Interactive styled tables with color-coded status
- 🎨 Dark theme with syntax highlighting
- 📱 Responsive layout for any screen size
- 🔍 Two-column CPU topology view preserved
- ⚡ Visual progress bars for CPU usage

**Text Export** — Plain text with preserved formatting:
```bash
sudo python3 tidycpu.py --export-text server_report.txt
```

Features:
- 📄 Clean ASCII layout with box-drawing characters
- 📋 Copy-paste friendly format
- 📧 Perfect for email reports or documentation
- 🔤 Unicode bars (█░) for visual usage display

Both formats include:
- Complete system information
- Full CPU topology breakdown
- Top process list with affinity details
- Timestamp for historical tracking

---

## How It Works

TidyCPU runs through five sequential steps each time it's invoked.

**1. Telemetry**
Takes two snapshots of `/proc/stat` 500ms apart and computes a real delta-based CPU percentage for every logical core. Cores are then classified:

| Label | Threshold |
|-------|-----------|
| 🔴 HOT  | ≥ 80% usage |
| 🟡 WARM | 40–79% usage |
| 🔵 COLD | < 40% usage |

**2. Process Analysis**
Queries the top 5 CPU-consuming processes via `ps`, then maps each one's current core affinity using `taskset -p <PID>`. The hex affinity mask is decoded into a human-readable core list.

**3. Conflict Detection**
Flags any process that is pinned to a HOT core while COLD cores exist and are available. Multiple heavy processes crowding the same core are prime candidates for rebalancing.

**4. Rebalancing Plan**
Generates a proposed migration table:
```
  PID    Process              CPU%   From → To
  12345  stress-ng           78.3%  Core [0]   → Core [4]
   8891  python3             42.1%  Core [1]   → Core [5]
   4412  mysqld              29.8%  Core [0,1] → Core [6,7]
```

**5. Execution**
Prompts for confirmation before applying any changes:
```
Apply these changes? (y/n):
```
- **`y`** — runs `taskset -pc <core_list> <PID>` for each planned change.
- **`n`** — prints the equivalent manual commands and exits cleanly.

If a process cannot be modified (kernel thread, permission denied, etc.), the error is caught per-action and displayed as a **Manual Suggestion** with the exact command to run, while all other changes still apply normally.

---

## Sample Output

```
  ✔  Running as root.

  SYSTEM INFORMATION
  ────────────────────────────────────────────────────────
  CPU Model:
    Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz

  Memory:
    Total:     64.0 GiB
    Available: 48.2 GiB

  CPU Frequency:
    Range:   1200 MHz - 3300 MHz
    Current: 2400 MHz

  Kernel Command Line:
    BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet


  CPU TOPOLOGY
  ──────────────────────────────────────────────────────────
  Core   Usage  Bar                     Status    Core   Usage  Bar                     Status
  ────   ─────  ──────────────────────  ──────    ────   ─────  ──────────────────────  ──────
  CPU0    2.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD      CPU4    0.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD
  CPU1    6.0%  █░░░░░░░░░░░░░░░░░░░░░  COLD      CPU5    0.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD
  CPU2    2.0%  ░░░░░░░░░░░░░░░░░░░░░░  COLD      CPU6   12.0%  ██░░░░░░░░░░░░░░░░░░░░  COLD
  CPU3  100.0%  ██████████████████████  HOT       CPU7   26.0%  █████░░░░░░░░░░░░░░░░░  COLD

  Summary: ● 3 Hot  ● 0 Warm  ● 5 Cold  |  1 physical CPU(s), 8 physical cores, 8 logical cores
  Hyperthreading: Disabled


  CORE TELEMETRY  (8 logical cores detected)
  ────────────────────────────────────────────────────────
  Core   Usage  Bar                     Status
  CPU0   93.4%  ██████████████████░░    HOT
  CPU1   85.1%  █████████████████░░░    HOT
  CPU2   61.2%  ████████████░░░░░░░░    WARM
  CPU3   44.7%  ████████░░░░░░░░░░░░    WARM
  CPU4    7.3%  █░░░░░░░░░░░░░░░░░░░    COLD
  CPU5    4.1%  ░░░░░░░░░░░░░░░░░░░░    COLD
  CPU6    2.9%  ░░░░░░░░░░░░░░░░░░░░    COLD
  CPU7    1.5%  ░░░░░░░░░░░░░░░░░░░░    COLD

  Summary: ● 2 Hot  ● 2 Warm  ● 4 Cold


  TOP CPU CONSUMERS
  ────────────────────────────────────────────────────────
      PID  Process                  CPU%   Affinity Mask  Cores
    12345  stress-ng               78.3%       00000001   0
     8891  python3                 42.1%       00000002   1
     4412  mysqld                  29.8%       00000003   0,1


  REBALANCING PLAN
  ────────────────────────────────────────────────────────
  3 change(s) proposed.

  Apply these changes? (y/n): y


  EXECUTION RESULTS
  ────────────────────────────────────────────────────────
  ✔  PID 8891 (python3)  →  pinned to core(s) [5]
  ✔  PID 4412 (mysqld)   →  pinned to core(s) [6,7]

  ⚠  Manual Suggestions (could not auto-apply):
  ↪  PID 12345 (stress-ng)  — run manually:
       sudo taskset -pc 4 12345
       Reason: Operation not permitted (kernel thread)
```

---

## Error Handling

| Situation | Behavior |
|-----------|----------|
| Not running as root | Fatal error with re-run hint, exits immediately |
| `taskset` not installed | Graceful error message per process |
| Kernel thread / permission denied | Caught per-action, shown as Manual Suggestion |
| Process exits between sample and apply | `taskset` error caught and reported cleanly |
| No conflicts detected | Exits with a "load looks balanced" message |

---

## Code Structure

```
tidycpu.py
├── C                         # ANSI color constants
├── SystemInfo                # Dataclass: CPU model, memory, cmdline, freq
├── CPUTopology               # Dataclass: physical CPU, core, logical mapping
├── CoreStat                  # Dataclass: core id, usage %, label, topology
├── ThreadInfo                # Dataclass: thread id, parent, cpu%, cores
├── ProcessInfo               # Dataclass: pid, name, cpu%, cores, threads
├── RebalanceAction           # Dataclass: from/to cores, manual flag, error
│
├── check_root()              # Step 0 — privilege guard
├── get_system_info()         # Parse /proc/cpuinfo, meminfo, cmdline, cpufreq
├── get_cpu_topology()        # Parse /sys topology info (physical/logical)
├── get_core_usage()          # Step 1 — /proc/stat delta sampling
├── get_threads_for_pid()     # Thread-level analysis for specific PID
├── get_top_processes()       # Step 2 — ps + taskset -p per pid + threads
├── build_rebalance_plan()    # Step 3 — conflict detection + plan
├── apply_action()            # Step 4 — taskset -pc execution
│
├── print_system_info()       # UI: system hardware info panel
├── print_topology()          # UI: physical/logical CPU tree view
├── print_core_table()        # UI: core telemetry panel
├── print_process_table()     # UI: top consumers panel + optional threads
├── print_rebalance_plan()    # UI: migration plan panel
├── print_results()           # UI: execution results panel
├── live_monitor()            # Live refresh mode with filtering
└── main()                    # Argument parser + mode dispatcher
```

---

## Caveats & Notes

- **Ephemeral changes** — `taskset` changes are not persistent. They reset on process restart or reboot. For permanent pinning, configure affinity at the service level (e.g., `CPUAffinity=` in a systemd unit file).
- **Kernel threads** — Many system threads cannot have their affinity changed even as root. TidyCPU handles these gracefully rather than aborting.
- **NUMA systems** — On NUMA (Non-Uniform Memory Access) machines, moving a process to a core on a different NUMA node may hurt memory performance even if CPU load improves. Cross-check with `numactl` on such systems.
- **Snapshot accuracy** — The 500ms sampling window is a point-in-time view. Bursty or short-lived processes may not be captured accurately.

---

## License

MIT — use freely, modify as needed.