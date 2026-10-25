#!/usr/bin/env bash
# memsample.sh [INTERVAL_S] [PROCESS_NAME] > samples.log
#
# Sample system RAM, swap, load and per-process CPU/RSS of the prover until no
# process of that name is left. Use it to see whether a proof's peak is a short
# spike (swap absorbs it cheaply) or a plateau (swap will thrash).
set -u
interval=${1:-15}; name=${2:-}
seen=0
while :; do
  read -r used avail < <(free -m | awk '/^Mem:/{print $3, $7}')
  read -r sw < <(free -m | awk '/^Swap:/{print $3}')
  procs=""
  if [[ -n "$name" ]]; then
    procs=$(ps -C "$name" -o pcpu=,rss= | awk '{printf "%s%%/%dMiB ", $1, $2/1024}')
    if [[ -n "$procs" ]]; then seen=1; elif [[ $seen -eq 1 ]]; then echo "$(date -u +%T) done"; exit 0; fi
  fi
  echo "$(date -u +%T) used_mib=$used avail_mib=$avail swap_mib=$sw load=$(cut -d' ' -f1 /proc/loadavg) $procs"
  sleep "$interval"
done
