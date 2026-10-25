# ZKstream

Prove big, splittable statements on small machines: split into independent
jobs, cap each prover's memory, let short peaks page to NVMe swap, and farm the
jobs across whatever boxes you have (a VPS, a laptop's WSL, a rented box) with
every worker gated on the pinned program identity.

Built while proving Litecoin header chains with SP1 6.9.0 on an 8-vCPU / 31 GiB
VPS plus a laptop, without buying RAM. See [docs/METHOD.md](docs/METHOD.md)
for the measurements and the traps.

## Kit

| File | What it does |
|---|---|
| `zkstream/farm.py` | Job farm over `local`, `ssh` and `wsl` workers: identity gate, detached launch (systemd-run on WSL), exit-code polling that ignores transport errors, per-worker verification, sink collection, resumable JSON state, retries. Stdlib Python 3.10+. |
| `zkstream/capped-run.sh` | Run a prover in a systemd scope with a RAM cap and a swap budget, so overshoot kills the prover, not the node beside it. |
| `zkstream/memsample.sh` | Sample RAM, swap, load and prover CPU/RSS to tell a spike (swap it) from a plateau (don't). |
| `examples/sp1-header-segments/farm.json` | Laptop-WSL + VPS farm for SP1 header segments. |

## Quick start

    mkdir -p jobs/inputs && cp my-jobs/*.bin jobs/inputs/
    cp examples/sp1-header-segments/farm.json jobs/   # edit workers, commands, identity
    python zkstream/farm.py jobs/farm.json --status
    python zkstream/farm.py jobs/farm.json            # runs until the queue is empty

Each job is one input file; `prove` and `verify` are command templates with
`{input}` and `{output}`. A worker gets work only after its `identity.command`
output contains `identity.expect`. Verified outputs land in `jobs/outputs/` and,
if a `sink` is set, are copied there with any `sink_extra_inputs` siblings.

## Tests

    python -m unittest discover -s tests -v

## Status

Research tooling, used for one overnight run. No authentication beyond your
own ssh keys; commands come from your config file and run as given.

MIT licensed.
