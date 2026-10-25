#!/usr/bin/env bash
# capped-run.sh RAM SWAP -- COMMAND...
#
# Run COMMAND in its own systemd scope with a hard RAM cap and a swap budget,
# so a prover that overshoots is OOM-killed inside its scope instead of taking
# down a co-resident service (a syncing node, a database, the desktop).
#
#   capped-run.sh 22G 0   -- chain-prove seg.bin seg.proof    # no swap: fail fast
#   capped-run.sh 29G 28G -- pow-prove agg.proof 2 pow.proof  # let spikes page
#
# Prints the scope's peak memory and swap from the journal when it ends.
# Requires systemd (Linux, or WSL with systemd enabled) and sudo for non-root.
set -euo pipefail
if [[ $# -lt 4 || "$3" != "--" ]]; then
  echo "usage: $0 RAM SWAP -- COMMAND..." >&2; exit 2
fi
ram=$1 swap=$2; shift 3
unit="zks-capped-$$-$(date +%s)"
sudo_cmd=(); [[ $(id -u) -eq 0 ]] || sudo_cmd=(sudo)
env_args=(--setenv=HOME="$HOME" --setenv=PATH="$PATH")
for v in $(env | grep -oE '^(SP1_[A-Z0-9_]+|LIGHTO_[A-Z0-9_]+|RAITO_[A-Z0-9_]+|RUST_LOG|SHARD_SIZE|ELEMENT_THRESHOLD)=' | tr -d '='); do
  env_args+=(--setenv="$v=${!v}")
done
set +e
"${sudo_cmd[@]}" systemd-run --scope --quiet --unit="$unit" -p MemoryMax="$ram" -p MemorySwapMax="$swap" \
  --uid="$(id -u)" --gid="$(id -g)" "${env_args[@]}" "$@"
rc=$?
set -e
"${sudo_cmd[@]}" journalctl --no-pager -q -u "$unit.scope" 2>/dev/null | grep -E "Consumed|oom" | tail -2 >&2 || true
exit $rc
