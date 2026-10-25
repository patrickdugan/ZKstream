#!/usr/bin/env python3
"""ZKstream farm: spread independent proving jobs across local, ssh and WSL workers.

Each job is one input file proven by one worker at a time. The coordinator
copies the input, starts the prover detached on the worker, polls for an
exit-code file, runs a verify command on the worker, pulls the proof back and
optionally copies it (plus sibling files) to a sink. State lives in one JSON
file, so the coordinator can be stopped and restarted at any point.

Safety rules learned the hard way:
  * A worker must pass an identity check (e.g. the compiled program vkey)
    before it gets work, so a worker built from different sources cannot
    contribute proofs.
  * A failed or timed-out transport call while polling means "unknown, poll
    again", never "job failed". Under memory pressure WSL calls time out, and
    treating that as failure relaunches jobs beside themselves.
  * Before a retry starts, any surviving unit of the earlier attempt is stopped.

Stdlib only. Commands are argument vectors (ssh, wsl.exe, bash); no shell is
interpreted on the coordinator side.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

STATE_VERSION = 1


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


def wsl_path(p: Path) -> str:
    """C:\\a\\b -> /mnt/c/a/b so a WSL worker can read coordinator files."""
    p = Path(p).resolve()
    drive = p.drive.rstrip(":").lower()
    return f"/mnt/{drive}" + p.as_posix()[len(p.drive):]


def fill(template: str, **kw: str) -> str:
    """Substitute shell-quoted values into a command template."""
    return template.format(**{k: shlex.quote(v) for k, v in kw.items()})


class Worker:
    """A place that can run bash scripts and move files.

    kind = "local" | "ssh" | "wsl". Override run/push/pull for other transports.
    """

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.kind = spec["kind"]
        self.workdir = spec["workdir"]
        if self.kind not in ("local", "ssh", "wsl"):
            raise SystemExit(f"worker {self.name}: kind must be local, ssh or wsl")

    def _argv(self) -> list[str]:
        if self.kind == "ssh":
            argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
            if self.spec.get("key"):
                argv += ["-o", "IdentitiesOnly=yes", "-i", str(Path(self.spec["key"]).expanduser())]
            return argv + [self.spec["host"], "bash", "-s"]
        if self.kind == "wsl":
            return ["wsl.exe", "-d", self.spec["distro"], "-u", self.spec.get("user", "root"), "--", "bash", "-s"]
        return [self.spec.get("bash", shutil.which("bash") or "bash"), "-s"]

    def run(self, script: str, timeout: int = 120) -> tuple[int, str]:
        """Run a bash script on the worker; returns (rc, stdout+stderr)."""
        try:
            done = subprocess.run(self._argv(), input=script, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except OSError as exc:
            return 127, str(exc)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def _scp(self, src: str, dst: str) -> None:
        argv = ["scp", "-q", "-o", "BatchMode=yes"]
        if self.spec.get("key"):
            argv += ["-o", "IdentitiesOnly=yes", "-i", str(Path(self.spec["key"]).expanduser())]
        subprocess.run(argv + [src, dst], check=True, timeout=600)

    def push(self, local: Path, remote: str) -> None:
        if self.kind == "ssh":
            self._scp(str(local), f"{self.spec['host']}:{remote}")
            return
        src = wsl_path(local) if self.kind == "wsl" else str(local)
        rc, out = self.run(f"cp {shlex.quote(src)} {shlex.quote(remote)}")
        if rc != 0:
            raise RuntimeError(f"push to {self.name} failed: {out.strip()}")

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "ssh":
            self._scp(f"{self.spec['host']}:{remote}", str(local))
            return
        dst = wsl_path(local) if self.kind == "wsl" else str(local)
        rc, out = self.run(f"cp {shlex.quote(remote)} {shlex.quote(dst)}")
        if rc != 0:
            raise RuntimeError(f"pull from {self.name} failed: {out.strip()}")


class Farm:
    def __init__(self, config_path: Path, worker_factory: Callable[[dict], Worker] = Worker):
        self.cfg = json.loads(Path(config_path).read_text())
        self.root = (Path(config_path).parent / self.cfg["jobs_dir"]).resolve()
        self.state_path = self.root / "state.json"
        self.workers = {w["name"]: worker_factory(w) for w in self.cfg["workers"] if w.get("enabled", True)}
        self.sink = worker_factory(self.cfg["sink"]) if self.cfg.get("sink") else None
        self.state = self._load_state()

    # ---- state ---------------------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state.get("version") != STATE_VERSION:
                raise SystemExit("state.json version mismatch")
            return state
        return {"version": STATE_VERSION, "jobs": {}, "workers": {}}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def scan(self) -> None:
        """Register every input file matching input_glob as a job (idempotent)."""
        for f in sorted((self.root / "inputs").glob(self.cfg.get("input_glob", "*"))):
            self.state["jobs"].setdefault(f.name, {"status": "pending", "attempts": 0, "worker": None})

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.state["jobs"].values():
            out[s["status"]] = out.get(s["status"], 0) + 1
        return out

    # ---- helpers -------------------------------------------------------------
    def out_name(self, job: str) -> str:
        return f"{Path(job).stem}{self.cfg['output_suffix']}"

    def in_sink(self, job: str) -> bool:
        if not self.sink:
            return False
        rc, out = self.sink.run(f"test -s {shlex.quote(self.sink.workdir + '/' + self.out_name(job))} && echo present")
        return rc == 0 and "present" in out

    def check_identity(self, w: Worker) -> bool:
        ident = self.cfg.get("identity")
        if not ident:
            return True
        rc, out = w.run(ident["command"], timeout=300)
        ok = rc == 0 and ident["expect"] in out
        log(f"identity {w.name}: {'ok' if ok else 'MISMATCH'}")
        if not ok:
            log(out.strip()[-500:])
        return ok

    def next_job(self, w: Worker) -> str | None:
        pending = [j for j, s in self.state["jobs"].items() if s["status"] == "pending"]
        if not pending:
            return None
        return pending[-1] if w.spec.get("order", "forward") == "reverse" else pending[0]

    # ---- job lifecycle -------------------------------------------------------
    def start(self, w: Worker, job: str) -> None:
        if self.in_sink(job):
            self.state["jobs"][job].update(status="done", worker="sink", seconds=0)
            log(f"{w.name}: {job} already present at the sink; skipped")
            return
        wd = w.workdir
        inp, out = f"{wd}/{job}", f"{wd}/{self.out_name(job)}"
        w.run(f"mkdir -p {shlex.quote(wd)}")
        w.push(self.root / "inputs" / job, inp)
        cmd = fill(w.spec["prove"], input=inp, output=out)
        rcfile, logfile = f"{inp}.rc", f"{inp}.log"
        body = f"{cmd} > {shlex.quote(logfile)} 2>&1; echo $? > {shlex.quote(rcfile)}"
        stem = Path(job).stem
        if w.spec.get("launcher", "nohup") == "systemd-run":
            # WSL kills setsid/nohup children when wsl.exe returns; a transient
            # systemd unit lives independently of the calling session. Units left
            # from an earlier attempt of this job are stopped first.
            unit = f"zks-{stem}-{self.state['jobs'][job]['attempts'] + 1}-{int(time.time())}"
            env = "".join(f" --setenv={k}={shlex.quote(v)}" for k, v in w.spec.get("env", {"HOME": "/root"}).items())
            launcher = (f"systemctl stop 'zks-{stem}-*' 2>/dev/null; rm -f {shlex.quote(rcfile)} {shlex.quote(out)}; "
                        f"systemd-run --quiet --collect --unit={unit}{env} bash -c {shlex.quote(body)} && echo started")
        else:
            launcher = (f"rm -f {shlex.quote(rcfile)} {shlex.quote(out)}; "
                        f"setsid nohup bash -c {shlex.quote(body)} > /dev/null 2>&1 < /dev/null & echo started")
        rc, msg = w.run(launcher)
        if rc != 0 or "started" not in msg:
            raise RuntimeError(f"{w.name} failed to start {job}: {msg.strip()}")
        s = self.state["jobs"][job]
        s.update(status="running", worker=w.name, started=time.time())
        s["attempts"] += 1
        self.state["workers"][w.name] = job
        log(f"{w.name}: started {job} (attempt {s['attempts']})")

    def poll(self, w: Worker, job: str) -> None:
        wd = w.workdir
        inp, out = f"{wd}/{job}", f"{wd}/{self.out_name(job)}"
        rc, txt = w.run(f"cat {shlex.quote(inp + '.rc')} 2>/dev/null || echo running")
        if rc != 0:
            log(f"{w.name}: unreachable while polling {job} (rc={rc}); will poll again")
            return
        txt = txt.strip().splitlines()[-1] if txt.strip() else "running"
        if txt == "running":
            return
        if not txt.isdigit():
            log(f"{w.name}: unexpected rc content for {job}: {txt[:80]!r}; will poll again")
            return
        s = self.state["jobs"][job]
        self.state["workers"][w.name] = None
        if txt != "0":
            _, tail = w.run(f"tail -5 {shlex.quote(inp + '.log')}")
            log(f"{w.name}: {job} FAILED rc={txt}: {tail.strip()[-300:]}")
            s["status"] = "pending" if s["attempts"] < self.cfg.get("max_attempts", 2) else "failed"
            s["worker"] = None
            return
        if w.spec.get("verify"):
            vrc, vout = w.run(fill(w.spec["verify"], input=inp, output=out), timeout=900)
            if vrc != 0 or self.cfg.get("verify_expect", "") not in vout:
                log(f"{w.name}: {job} proof did NOT verify: {vout.strip()[-300:]}")
                s.update(status="failed", worker=None)
                return
        local = self.root / "outputs" / self.out_name(job)
        w.pull(out, local)
        if self.sink:
            for extra in self.cfg.get("sink_extra_inputs", []):
                src = self.root / "inputs" / f"{Path(job).stem}{extra}"
                if src.exists():
                    self.sink.push(src, f"{self.sink.workdir}/{src.name}")
            self.sink.push(self.root / "inputs" / job, f"{self.sink.workdir}/{job}")
            self.sink.push(local, f"{self.sink.workdir}/{local.name}")
        s.update(status="done", seconds=round(time.time() - s.get("started", time.time()), 1))
        log(f"{w.name}: {job} done and verified in {s['seconds']} s")

    # ---- main loop -----------------------------------------------------------
    def step(self, healthy: dict[str, bool]) -> None:
        for name, w in self.workers.items():
            if not healthy.get(name):
                continue
            current = self.state["workers"].get(name)
            try:
                if current:
                    self.poll(w, current)
                else:
                    job = self.next_job(w)
                    if job:
                        self.start(w, job)
            except Exception as exc:  # one worker's error must not stop the farm
                log(f"{name}: error {exc}")
            self.save()

    def loop(self, once: bool = False, sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
        self.scan()
        healthy = {n: self.check_identity(w) for n, w in self.workers.items()}
        self.save()
        while True:
            self.step(healthy)
            active = any(s["status"] in ("pending", "running") for s in self.state["jobs"].values())
            if once or not active or not any(healthy.values()):
                log(f"summary {self.counts()}")
                return self.counts()
            sleep(self.cfg.get("poll_seconds", 30))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--once", action="store_true", help="one scheduling pass, then exit")
    ap.add_argument("--status", action="store_true", help="print job counts and exit")
    args = ap.parse_args()
    farm = Farm(args.config)
    if args.status:
        farm.scan()
        print(json.dumps(farm.counts()))
        return 0
    farm.loop(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
