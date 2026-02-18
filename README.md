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

### Command-Line Options

| Option | Short | Description |
|--------|-------|-------------|
| `--help` | `-h` | Show help message and exit |
| `--live` | `-l` | Live monitoring mode (refreshes every 500ms) |
| `--duration N` | `-d N` | Duration for live mode in seconds (default: 5) |
| `--pid PID` | `-p PID` | Filter to show specific PID and its threads |
| `--threads` | `-t` | Show thread information for processes |

---

## Features

### 1. Physical/Logical CPU Topology View

TidyCPU automatically detects and displays your system's CPU architecture:

- **Physical CPUs** (sockets)
- **Physical Cores** within each CPU
- **Hyperthreading** detection (shows primary ⊲ and sibling ⊳ threads)
- **Real-time usage** for each logical core

Example output for a dual-socket system with hyperthreading:

```
  Physical CPU 0 (8 logical cores)
    ├─ Core 0 (HT):
       ⊲ CPU 0   95.2% ██████████████░  HOT
       ⊳ CPU 1   88.1% █████████████░░  HOT
    ├─ Core 1 (HT):
       ⊲ CPU 2   72.4% ██████████░░░░░  WARM
       ⊳ CPU 3   65.8% █████████░░░░░░  WARM
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
  CPU TOPOLOGY
  ──────────────────────────────────────────────────────────
  Physical CPU 0 (8 logical cores)
    ├─ Core 0 (HT):
       ⊲ CPU 0   95.2% ██████████████░  HOT
       ⊳ CPU 1   88.1% █████████████░░  HOT
    ├─ Core 1 (HT):
       ⊲ CPU 2   72.4% ██████████░░░░░  WARM
       ⊳ CPU 3   65.8% █████████░░░░░░  WARM

  Physical CPU 1 (8 logical cores)
    ├─ Core 0 (HT):
       ⊲ CPU 8    3.2% ░░░░░░░░░░░░░░░  COLD
       ⊳ CPU 9    2.1% ░░░░░░░░░░░░░░░  COLD

  Summary: 2 physical CPU(s), 8 physical core(s), 16 logical core(s)
  Hyperthreading: Enabled (⊲ primary, ⊳ sibling)


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
├── CPUTopology               # Dataclass: physical CPU, core, logical mapping
├── CoreStat                  # Dataclass: core id, usage %, label, topology
├── ThreadInfo                # Dataclass: thread id, parent, cpu%, cores
├── ProcessInfo               # Dataclass: pid, name, cpu%, cores, threads
├── RebalanceAction           # Dataclass: from/to cores, manual flag, error
│
├── check_root()              # Step 0 — privilege guard
├── get_cpu_topology()        # Parse /sys topology info (physical/logical)
├── get_core_usage()          # Step 1 — /proc/stat delta sampling
├── get_threads_for_pid()     # Thread-level analysis for specific PID
├── get_top_processes()       # Step 2 — ps + taskset -p per pid + threads
├── build_rebalance_plan()    # Step 3 — conflict detection + plan
├── apply_action()            # Step 4 — taskset -pc execution
│
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