"""Farm scheduling tests with scripted fake workers (no ssh, wsl or provers)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zkstream"))
import farm  # noqa: E402


class FakeWorker:
    """Answers the farm's scripts by pattern; poll answers come from a per-job queue."""

    registry: dict[str, "FakeWorker"] = {}

    def __init__(self, spec: dict):
        self.spec, self.name, self.kind, self.workdir = spec, spec["name"], spec["kind"], spec["workdir"]
        self.started: list[str] = []
        self.polls: dict[str, list[tuple[int, str]]] = {}
        self.identity_output = spec.get("_identity", "vkey=GOOD")
        self.verify_output = spec.get("_verify", "status=verified")
        self.files: set[str] = set(spec.get("_files", []))
        self.pushed: list[str] = []
        FakeWorker.registry[self.name] = self

    def run(self, script: str, timeout: int = 120):
        if "echo started" in script:
            job = script.split("bash -c")[-1]
            name = next(j for j in self.polls if j in job)
            self.started.append(name)
            return 0, "started"
        if script.startswith("cat ") and ".rc" in script:
            name = next(j for j in self.polls if j in script)
            queue = self.polls[name]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if script.startswith("test -s"):
            path = script.split()[2].strip("'")
            return (0, "present") if path in self.files else (1, "")
        if script.startswith("identity"):
            return 0, self.identity_output
        if script.startswith("verify"):
            return 0, self.verify_output
        return 0, ""

    def push(self, local, remote):
        self.pushed.append(remote)
        self.files.add(remote)

    def pull(self, remote, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(b"proof")


def make_farm(tmp: Path, jobs: list[str], workers: list[dict], sink: dict | None = None, **cfg) -> farm.Farm:
    (tmp / "inputs").mkdir()
    for j in jobs:
        (tmp / "inputs" / j).write_bytes(b"input")
        (tmp / "inputs" / f"{Path(j).stem}.end").write_bytes(b"end")
    config = {
        "jobs_dir": ".", "input_glob": "*.seg", "output_suffix": ".proof", "max_attempts": 2,
        "identity": {"command": "identity", "expect": "vkey=GOOD"}, "verify_expect": "status=verified",
        "workers": workers, "sink_extra_inputs": [".end"], **cfg,
    }
    if sink:
        config["sink"] = sink
    (tmp / "farm.json").write_text(json.dumps(config))
    FakeWorker.registry.clear()
    return farm.Farm(tmp / "farm.json", worker_factory=FakeWorker)


def worker(name="w1", **extra) -> dict:
    return {"name": name, "kind": "local", "workdir": f"/work/{name}", "prove": "prove {input} {output}",
            "verify": "verify {input} {output}", **extra}


def run_until_idle(f: farm.Farm, passes: int = 20) -> dict:
    return f.loop(sleep=lambda _: None) if passes else {}


class FarmTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_proves_verifies_and_delivers_to_sink(self):
        f = make_farm(self.tmp, ["001.seg", "002.seg"], [worker()], sink={"name": "sink", "kind": "local", "workdir": "/sink"})
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "running"), (0, "0")], "002.seg": [(0, "0")]}
        counts = run_until_idle(f)
        self.assertEqual(counts, {"done": 2})
        self.assertEqual(w.started, ["001.seg", "002.seg"])
        sink = FakeWorker.registry["sink"]
        for name in ("/sink/001.seg", "/sink/001.end", "/sink/001.proof", "/sink/002.proof"):
            self.assertIn(name, sink.pushed)
        self.assertTrue((self.tmp / "outputs" / "001.proof").exists())

    def test_transport_errors_while_polling_never_fail_or_relaunch(self):
        """Regression: wsl.exe timeouts under memory pressure relaunched jobs beside themselves."""
        f = make_farm(self.tmp, ["001.seg"], [worker()])
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(124, "timeout"), (1, "Wsl/Service/WSAETIMEDOUT"), (0, "running"), (0, "0")]}
        self.assertEqual(run_until_idle(f), {"done": 1})
        self.assertEqual(w.started, ["001.seg"], "a transport error must not cause a second launch")
        self.assertEqual(f.state["jobs"]["001.seg"]["attempts"], 1)

    def test_garbage_rc_content_is_not_a_result(self):
        f = make_farm(self.tmp, ["001.seg"], [worker()])
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "cat: permission denied"), (0, "0")]}
        self.assertEqual(run_until_idle(f), {"done": 1})
        self.assertEqual(w.started, ["001.seg"])

    def test_nonzero_exit_retries_then_fails(self):
        f = make_farm(self.tmp, ["001.seg"], [worker()])
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "137")]}
        self.assertEqual(run_until_idle(f), {"failed": 1})
        self.assertEqual(w.started, ["001.seg", "001.seg"])

    def test_identity_mismatch_gets_no_work(self):
        f = make_farm(self.tmp, ["001.seg"], [worker(_identity="vkey=OTHER")])
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "0")]}
        self.assertEqual(run_until_idle(f), {"pending": 1})
        self.assertEqual(w.started, [])

    def test_jobs_already_at_the_sink_are_skipped(self):
        f = make_farm(self.tmp, ["001.seg", "002.seg"], [worker()],
                      sink={"name": "sink", "kind": "local", "workdir": "/sink", "_files": ["/sink/001.proof"]})
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "0")], "002.seg": [(0, "0")]}
        self.assertEqual(run_until_idle(f), {"done": 2})
        self.assertEqual(w.started, ["002.seg"])
        self.assertEqual(f.state["jobs"]["001.seg"]["worker"], "sink")

    def test_reverse_order_takes_from_the_end(self):
        f = make_farm(self.tmp, ["001.seg", "002.seg", "003.seg"], [worker(order="reverse")])
        w = FakeWorker.registry["w1"]
        w.polls = {j: [(0, "0")] for j in ("001.seg", "002.seg", "003.seg")}
        run_until_idle(f)
        self.assertEqual(w.started, ["003.seg", "002.seg", "001.seg"])

    def test_unverified_proof_is_failed_not_collected(self):
        f = make_farm(self.tmp, ["001.seg"], [worker(_verify="Error: invalid proof")],
                      sink={"name": "sink", "kind": "local", "workdir": "/sink"})
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "0")]}
        self.assertEqual(run_until_idle(f), {"failed": 1})
        self.assertNotIn("/sink/001.proof", FakeWorker.registry["sink"].pushed)
        self.assertFalse((self.tmp / "outputs" / "001.proof").exists())

    def test_state_survives_a_restart(self):
        f = make_farm(self.tmp, ["001.seg"], [worker()])
        FakeWorker.registry["w1"].polls = {"001.seg": [(0, "running")]}
        f.loop(once=True, sleep=lambda _: None)
        self.assertEqual(f.state["jobs"]["001.seg"]["status"], "running")
        again = farm.Farm(self.tmp / "farm.json", worker_factory=FakeWorker)
        w = FakeWorker.registry["w1"]
        w.polls = {"001.seg": [(0, "0")]}
        self.assertEqual(again.loop(sleep=lambda _: None), {"done": 1})
        self.assertEqual(w.started, [], "a restarted coordinator resumes polling, it does not relaunch")

    def test_wsl_path_and_quoting(self):
        self.assertEqual(farm.wsl_path(Path("C:/Users/a b/x.bin")), "/mnt/c/Users/a b/x.bin")
        self.assertEqual(farm.fill("p {input}", input="a b;rm -rf /"), "p 'a b;rm -rf /'")


if __name__ == "__main__":
    unittest.main()
