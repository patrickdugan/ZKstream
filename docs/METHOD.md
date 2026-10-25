# Streaming ZK proving through small machines

ZKstream is a working method for proving long, splittable statements (header
chains, batches of independent claims) on machines that individually have
too little RAM, by combining four things:

1. **Split the statement into independent jobs** whose start state is cheap
   to compute natively (milliseconds), so jobs can run anywhere, in any order.
2. **Cap every prover in its own memory scope** so an overshoot is killed
   inside the scope, never the service running beside it.
3. **Let short memory spikes page to NVMe swap** instead of buying RAM for
   the peak; refuse to page a steady working set.
4. **Gate workers on program identity** and verify every proof on the worker
   that produced it, so a heterogeneous fleet can only contribute proofs of
   the same pinned program.

The numbers below come from SP1 6.9.0 proving Litecoin header-chain segments
(Lighto H1/H2, October 2026) on an 8-vCPU / 31 GiB VPS and a Ryzen AI 9 365
laptop.

## 1. Find the memory shape before buying hardware

SP1's peak RSS tracks trace size up to its shard limit, and the peak is a
**spike**, not a plateau. `memsample.sh` during one compressed proof:

    16:03:30 used=14.7 GiB   16:05:00 used=30.4 GiB  (avail 1.6 GiB)
    16:05:30 used=18.8 GiB   16:07:00 used=10.2 GiB

| Job | Instructions | Peak RSS | Time (8 vCPU) |
|---|---:|---:|---:|
| MWEB membership, 8 outputs | 0.46M | 10.5 GiB | 106 s |
| MWEB reserve, 8 openings | 1.74M | 17.3 GiB | 181 s |
| Header segment, 252 headers, compressed | ~1.1M | 17.6-19.3 GiB | 281-355 s |
| Header segment, 504 headers, core | ~2.3M | 20.9 GiB | 204 s |
| Header segment, 1,008 headers, core | ~4.6M | > 21 GiB | killed at cap |
| Aggregate of 4 compressed segments | - | 23.0 GiB | 372 s |
| Scrypt PoW, 1 header, core | 7.2M | 30.4 GiB | 419 s |
| Aggregate verify + 2 Scrypt samples | ~15M+ | 29 GiB + 21.4 GiB swap | 730 s |

Rule of thumb from this table: about 10 GiB of fixed overhead, then roughly
5-6 GiB per million instructions until the shard limit. Choose job size so
the *spike* fits RAM + swap and the *plateau* fits RAM.

## 2. Swap is the cheap "stream from disk"

Unlike MoE inference, a prover's big buffers are freshly generated trace and
LDE matrices touched several times each, so you cannot stream selectively.
But because the peak is short, plain swap behaves like streaming: the hot
working set stays in RAM and the spike pages out. The 2-sample PoW proof above
completed with 21.4 GiB of swap on a virtual disk; NVMe is faster.

What does **not** work: two concurrent 18 GiB jobs on a 19 GiB WSL VM with
swap. Both plateaus page, load hits 29, `wsl.exe` itself times out, and
anything that treats those timeouts as job failures relaunches jobs beside
themselves. One job per box until RAM >= 2 x plateau.

## 3. Cap every prover

`capped-run.sh RAM SWAP -- cmd` puts the prover in a systemd scope with
`MemoryMax` and `MemorySwapMax`. On the VPS, provers ran capped at 21-22 GiB
next to a syncing Litecoin Core node with 6 GiB of dbcache; several jobs were
OOM-killed in their scope and the node never noticed. The kernel log names
the scope (`CONSTRAINT_MEMCG ... Killed process ... pow-prove`), so the
failure is unambiguous.

## 4. Pin the program, then let anyone prove

Workers are only useful if they prove the **same** program. Two traps:

- **Path-dependent ELFs.** Local SP1 guest builds differed by checkout path
  even with `--remap-path-prefix` and stripped symbols (identical compiler,
  sources and lockfiles; same path gave identical ELFs). SP1's Docker build
  (`ghcr.io/succinctlabs/sp1:v6.9.0`, fixed in-container path) was
  byte-identical at two paths on one machine and across two machines.
- **Mixed SP1 versions.** `sp1-sdk = "=6.9.0"` pins only the SDK; a fresh
  resolve pulls 6.10.0 sub-crates. Seed lockfiles from a known-good lock and
  pin every `sp1-*`/`slop-*` crate with `cargo update -p NAME@VER --precise`.

The farm's `identity` check runs on every worker before it gets work (e.g.
`chain-identity` must print the pinned vkey), and every proof is verified on
its worker before it is collected.

## 5. Operational traps we hit

- WSL kills `setsid nohup` children when `wsl.exe` returns: launch WSL jobs as
  transient systemd units (`"launcher": "systemd-run"`).
- `pgrep -f pattern` matches its own `bash -c` command line.
- PowerShell 5.1 strips embedded double quotes from native arguments; send
  remote scripts on stdin (`bash -s`), as the farm does.
- A transport error is not a job result. The farm only acts on a numeric exit
  code file; anything else means "poll again".
- Keep build steps off a box that is already running a prover beside a
  service: a 2 GiB compile can be what tips the system into a global OOM.

## 6. Composition

Split jobs only help if their proofs compose: segment statements carry a start
and end state, an aggregator verifies k segment proofs in-circuit and checks
`end(i) == start(i+1)`, and a node aggregator folds aggregates as a tree. The
aggregation steps are the memory-heavy ones (23 GiB for 4 children here), so
they are the ones to schedule on the biggest box, or rent by the hour.
