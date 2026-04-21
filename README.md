# TidyCPU — CPU Affinity Optimization Utility

Balances CPU load by reassigning process affinity across cores. Shows per-core usage, identifies hot/warm/cold cores, detects crowded processes, and can pin them to idle cores via `taskset`.

**Target:** Linux x64 (Debian / Ubuntu)  
**Requires:** root privileges, `taskset` (`util-linux`), `ps` (`procps`)

---

## Installation

```bash
# No pip install needed — single file
git clone <repo>
cd tidycpu

# Optional: install taskset if missing
apt install util-linux

# Install as a system command (run once)
sudo cp tidycpu.py /usr/local/bin/tidycpu
sudo chmod +x /usr/local/bin/tidycpu
```

After installation you can invoke it directly without `python3`:

```bash
sudo tidycpu --live
```

---

## Usage

```
sudo tidycpu [OPTIONS]
```

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--live` | `-l` | Live monitoring mode, refreshes every 3 s |
| `--duration N` | `-d` | Number of iterations for live mode (default: `5`) |
| `--pid SPEC` | `-p` | Monitor specific process(es) — see below |
| `--threads` | `-t` | Show top processes and rebalancing plan |
| `--cpu-freq` | `-f` | Show CPU frequency (min / max / current) |
| `--check-pid PID` | | Inspect a specific PID: affinity, cores, threads |
| `--export-html FILE` | | Export report to an HTML file |
| `--export-text FILE` | | Export report to a plain-text file |
| `--export-excel FILE` | | Export report to an Excel file (requires `openpyxl`) |
| `--ignore-col COLS` | | Hide columns from the topology table (comma-separated, e.g. `Bar,Usage`) |
| `--specify PARENTS` | | Highlight rows whose parent process matches any of these names (comma-separated) |

---

## Examples

```bash
# View CPU topology snapshot (default mode)
sudo tidycpu

# Live monitor — 5 iterations, 3 s each
sudo tidycpu --live

# Live monitor — 10 iterations
sudo tidycpu --live --duration 10

# Monitor a single process by PID
sudo tidycpu --pid 1234

# Monitor a single process by name
sudo tidycpu --pid nginx

# Monitor multiple processes (pipe-separated names or PIDs)
sudo tidycpu --pid "nginx|php-fpm"
sudo tidycpu --pid "nginx|php-fpm|1234"

# Show top CPU consumers and propose rebalancing
sudo tidycpu --threads

# Inspect a specific PID (affinity, core status, threads)
sudo tidycpu --check-pid 1234

# Include CPU frequency info
sudo tidycpu --cpu-freq

# Export a live-monitor run to HTML (tabbed per iteration)
sudo tidycpu --live --duration 5 --export-html /tmp/report.html

# Export to plain text
sudo tidycpu --live --duration 5 --export-text /tmp/report.txt

# Export to Excel (requires openpyxl — see Dependencies)
sudo tidycpu --live --duration 5 --export-excel /tmp/report.xlsx

# Combine: monitor nginx + php-fpm live, export to HTML
sudo tidycpu --pid "nginx|php-fpm" --duration 10 --export-html /tmp/report.html

# Hide the Bar column from the live table
sudo tidycpu --live --ignore-col Bar

# Hide multiple columns
sudo tidycpu --live --ignore-col Bar,Usage

# Show all cores but highlight rows belonging to nginx or php-fpm
sudo tidycpu --live --specify nginx,php-fpm

# Combine: focus on specific parents and hide bar column
sudo tidycpu --live --specify nginx --ignore-col Bar
```

---

## What It Shows

### CPU Topology Table

Printed every iteration. On hyper-threaded systems two cores are shown side-by-side, with the bar on the outer edges and the core IDs in the centre:

```
  ┌────────────────────────┬────────┬─────────────────┬─────────────────┬───────┬───────┬─────────────────┬─────────────────┬────────┬────────────────────────┐
  │          Bar           │ Usage  │    Process      │     Parent      │ Core  │ Core  │     Parent      │    Process      │ Usage  │          Bar           │
  ├────────────────────────┼────────┼─────────────────┼─────────────────┼───────┼───────┼─────────────────┼─────────────────┼────────┼────────────────────────┤
  │ ████████████████░░░░░░ │ 72.3%  │ worker          │ nginx           │ CPU0  │ CPU4  │ php-fpm         │ php-fpm         │  4.1%  │ █░░░░░░░░░░░░░░░░░░░░░ │
  └────────────────────────┴────────┴─────────────────┴─────────────────┴───────┴───────┴─────────────────┴─────────────────┴────────┴────────────────────────┘
```

- **Bar** — visual usage bar; colour reflects load: green (cold), yellow (warm), red (hot)
- **Usage** — per-core CPU percentage over the last 0.5 s sample window
- **Process** — name of the thread with the most cumulative CPU time on that core
- **Parent** — name of the process that owns that thread (from `/proc/<pid>/comm`)
- **Core** — logical CPU ID

Columns can be hidden with `--ignore-col` and specific parent processes highlighted with `--specify`.

### Process / Thread Detail (`--pid`)

When filtering by PID or name, each matched process gets its own section showing all threads, their CPU%, and which cores they run on.

### Rebalancing Plan (`--threads`)

Identifies processes pinned to HOT cores while COLD cores sit idle, and proposes `taskset` commands to redistribute them. Prompts before applying any changes.

---

## Live Monitoring

```
Live Monitor Mode — Refresh: 3s  Iteration: 2/5
```

Each iteration:
1. **Samples CPU for 0.5 s** invisibly (while the previous frame is still on screen)
2. **Clears and prints** the new frame instantly
3. **Waits 2.5 s** so you can read it before the next refresh

This means the screen stays stable and readable for ~2.5 seconds per iteration with no flickering.

Press **Ctrl+C** at any time to stop early.

---

## HTML Export

The HTML report is a self-contained dark-themed file. When exported from a live-monitor run it includes a **tab navigator** — one tab per iteration — so you can click between snapshots without scrolling:

```
[ #1  14:22:01 ]  [ #2  14:22:04 ]  [ #3  14:22:07 ]
```

Each tab shows the full CPU topology table including the **Process** column.

---

## Core Labels

| Label | Threshold | Colour |
|-------|-----------|--------|
| HOT   | ≥ 80%     | Red    |
| WARM  | ≥ 40%     | Yellow |
| COLD  | < 40%     | Cyan   |

---

## Requirements

- Python 3.10+ (uses `list[int]` type hints)
- Linux only (`/proc/stat`, `/proc/*/task/*/stat`, `/sys/devices/system/cpu`)
- `taskset` from `util-linux` (for affinity reads and writes)
- `ps` from `procps`
- Root (`sudo`) for reading other processes' affinity and applying changes

## Dependencies

The core tool has **no pip dependencies**. Excel export requires one extra package:

```bash
pip install openpyxl
```

Without it, `--export-excel` will raise an error at runtime. HTML and text export work without any additional packages.