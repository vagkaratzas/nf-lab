"""Demo scenarios: the dashboard's control panel.

A scenario starts a fresh adapter (new ledger, the example's simulated machines, empty decks),
runs an example pipeline (EXAMPLES), and plays the person at the deck: it loads and unloads
plates, presses e-stop, Succeeded or Failed, all through the adapter's operator
API, as a person on the deck page would. It narrates each step.

No pause: Nextflow and the robots can't be paused, so a paused narrator would fall
out of step with them. Stop ends the pipeline (TERM: jobs before the stop boundary are safe-stopped) and
leaves the adapter up to look around.

Never imports adapter code: it starts the adapter as a process and talks HTTP.
"""

import asyncio
import ctypes
import json
import os
import shutil
import signal
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

# shortcut: Linux only (prctl); elsewhere a killed dashboard can still leave its adapter holding the port
_prctl = getattr(ctypes.CDLL(None), "prctl", None)


def _die_with_dashboard() -> None:
    """In the child: SIGTERM when the dashboard dies, also by SIGKILL or SIGHUP, when shutdown() never runs."""
    _prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


CHILD = {"start_new_session": True, "preexec_fn": _die_with_dashboard if _prctl else None}


@dataclass
class Settings:
    nf_lab: Path
    port: int = 8000
    backend: str = "plr"  # plr: PyLabRobot simulation with deck views; fake: tests
    step: float = 1.5  # seconds per simulated robot step: slow enough to follow (a plate takes ~15-20 s)
    nextflow: list[str] = field(default_factory=lambda: [os.environ.get("NXF_CMD", "nextflow")])
    beat: float = 5.0  # wait before each person step, and the least a narration stays up: time to follow
    tick: float = 0.5  # how often the person looks at the decks
    runs: Path | None = None  # one folder per scenario (ledger, logs, results); default nf-labrobotics/build/demo


def auth() -> dict:
    """The adapter's bearer token from NF_LAB_TOKEN, if set (the demo's own adapter inherits it too)."""
    return {"Authorization": f"Bearer {t}"} if (t := os.environ.get("NF_LAB_TOKEN")) else {}


# every capability once, two liquid handlers: the 11 PyLabRobot models a deck view for, and 5 it doesn't
TOUR_DEVICES = ("liquid-handling:2,plate-reading:1,thermocycling:1,qpcr:1,shaking:1,heating-shaking:1,temperature-control:1,"
                "tilting:1,centrifugation:1,incubation:1,weighing:1,pumping:1,powder-dispensing:1,sealing:1,peeling:1,"
                "barcode-reading:1")

# example -> (title, the adapter's NF_LAB_DEVICES, the scenarios scripted for it). Manual runs any example.
EXAMPLES: dict[str, tuple[str, str, list[str]]] = {
    "plate_transfer": ("Plate transfer", "2", ["pool", "estop", "manual"]),
    "plate_assay": ("Plate assay: transfer, then read", "liquid-handling:2,plate-reading:1", ["manual"]),
    "parallel_extraction": ("Parallel extraction: RNA and DNA side by side, then PCR", "liquid-handling:2,thermocycling:1",
                            ["side_by_side", "manual"]),  # one plate (the example's default map)
    "qpcr": ("qPCR screen: 1000 samples, extraction then RT-qPCR", "liquid-handling:4,qpcr:6", ["scale", "manual"]),
    "lab_tour": ("Lab tour: five routines side by side, every simulated machine", TOUR_DEVICES, ["tour", "manual"]),
}


class Busy(Exception):
    """A scenario is already running."""


Devices = list[dict]


class Demo:
    def __init__(self, settings: Settings):
        self.s = settings
        self.url = f"http://127.0.0.1:{settings.port}"
        self.name: str | None = None
        self.example = "plate_transfer"
        self.status = "idle"  # running, finished, failed, stopped
        self.log: list[dict] = []
        self.highlight: dict | None = None  # the device the last person step acted on
        self.task: asyncio.Task | None = None
        self.adapter: asyncio.subprocess.Process | None = None
        self.pipeline: asyncio.subprocess.Process | None = None
        self.run_dir: Path | None = None

    # --- control ---

    def view(self) -> dict:
        return {"scenarios": [{"id": k, "title": t, "blurb": b} for k, (t, b, _) in SCENARIOS.items()],
                "examples": [{"id": k, "title": t, "scenarios": sc} for k, (t, _, sc) in EXAMPLES.items()],
                "example": self.example, "active": self.name, "status": self.status,
                "log": self.log[-60:], "highlight": self.highlight,
                "can_rerun": bool(self.run_dir and (self.run_dir / "ledger.sqlite").exists())}

    def start(self, name: str, example: str = "plate_transfer") -> None:
        if name not in EXAMPLES[example][2]:
            raise KeyError(f"no {name!r} scenario for {example}")
        if self.task and not self.task.done():
            raise Busy(f"{self.name} is running: stop it first")
        self.name, self.example, self.status, self.log, self.highlight = name, example, "running", [], None
        self.run_dir = (self.s.runs or self.s.nf_lab / "build" / "demo") / example / name
        self.task = asyncio.create_task(self._run(SCENARIOS[name][2]))

    def rerun(self) -> None:
        """Run the pipeline again with -resume in the last demo's folder, on its ledger."""
        if self.task and not self.task.done():
            raise Busy(f"{self.name} is running: stop it first")
        if not self.view()["can_rerun"]:
            raise Busy("nothing to rerun: start a demo first")
        self.status = "running"
        self.task = asyncio.create_task(self._run(rerun))

    async def _run(self, scenario: Callable[["Demo"], Awaitable[None]]) -> None:
        try:
            await scenario(self)
            self.status = "finished"
        except asyncio.CancelledError:
            self.status = "stopped"
        except Exception as e:
            self.note("error", f"{type(e).__name__}: {e}")
            self.status = "failed"

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.note("say", "Stopped. The pipeline was asked to stop (a robot past the stop boundary finishes its plate); "
                             "the adapter stays up.")
        await self._end(self.pipeline, timeout=60)

    async def shutdown(self) -> None:
        await self.stop()
        await self._end(self.adapter, timeout=10)

    # --- steps scenarios are made of ---

    def note(self, kind: str, text: str, device: str | None = None) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        if device:
            self.highlight = {"device": device, "n": len(self.log)}

    async def say(self, text: str) -> None:
        """Narrate, then give the audience time to read it: at least a beat, ~20 characters a second."""
        self.note("say", text)
        await asyncio.sleep(max(self.s.beat, len(text) / 20) if self.s.beat else 0)

    async def person(self, text: str, device: str, method: str, path: str, body: dict | None = None,
                     still: Callable[[Devices], bool] | None = None) -> dict | None:
        """A person step: a beat for the audience, then press the button.

        `still(devices)` re-checks the decks right before the press: during the beat the moment may have
        passed (the plate finished). Returns None, pressing nothing, when it has, or when the adapter
        refuses the press (409: the state changed between the check and the press)."""
        await asyncio.sleep(self.s.beat)
        if still and not still(await self.api("GET", "/devices")):
            return None
        self.note("person", text, device)
        try:
            return await self.api(method, path, body)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            return None

    async def api(self, method: str, path: str, body: dict | None = None):
        def call():
            data = json.dumps(body if body is not None else {"by": "demo operator"}).encode() if method == "POST" else None
            req = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **auth()})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        return await asyncio.to_thread(call)

    async def ensure_adapter(self) -> None:
        """The adapter this panel started, still up; else start it again on the folder's ledger, as it is."""
        if self.adapter and self.adapter.returncode is None:
            return
        await self.fresh_adapter(wipe=False)

    async def fresh_adapter(self, wipe: bool = True, simulated_operator: bool = False, step: float | None = None) -> None:
        """Stop the adapter this panel started, wipe the scenario's folder (new ledger), start a new adapter on it.
        `simulated_operator`: the adapter loads and unloads plates itself, with no narrated person steps.
        `step`: seconds per robot step, in place of the settings' (a long scenario runs faster)."""
        await self._end(self.adapter, timeout=10)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", self.s.port)) == 0:
                raise RuntimeError(f"port {self.s.port} is in use: stop the adapter you started yourself, then retry")
        python = self.s.nf_lab / "adapter/.venv/bin/python"
        if not python.exists():
            raise RuntimeError(f"no adapter environment at {python}: run `uv sync` in adapter/")
        if wipe:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # `nextflow lineage` reads only the launch dir's config, never the pipeline's: let it open this run's store
        (self.run_dir / "nextflow.config").write_text("lineage.enabled = true\n")
        env = {**os.environ, "NF_LAB_BACKEND": self.s.backend, "NF_LAB_DEVICES": EXAMPLES[self.example][1], "NF_LAB_STEP": str(step or self.s.step),
               "NF_LAB_SIMULATED_OPERATOR": "1" if simulated_operator else "0", "NF_LAB_VISUALIZER": "1" if self.s.backend == "plr" else "0",
               "NF_LAB_LEDGER": str(self.run_dir / "ledger.sqlite")}
        self.adapter = await asyncio.create_subprocess_exec(
            str(python), "-m", "uvicorn", "nf_lab_adapter.app:main", "--factory", "--port", str(self.s.port),
            "--app-dir", str(self.s.nf_lab / "adapter/src"),
            stdout=(self.run_dir / "adapter.log").open("w"), stderr=asyncio.subprocess.STDOUT, env=env,
            **CHILD)  # own session: Ctrl-C on the dashboard doesn't hit it first; shutdown() ends it
        for _ in range(240):
            if self.adapter.returncode is not None:
                raise RuntimeError(f"the adapter exited: see {self.run_dir / 'adapter.log'}")
            try:
                await self.api("GET", "/devices")
                return
            except (urllib.error.URLError, OSError):
                await asyncio.sleep(0.25)
        raise RuntimeError("the adapter didn't start within 60 s")

    async def start_pipeline(self, resume: bool = False) -> None:
        cmd = [*self.s.nextflow, "run", str(self.s.nf_lab / "examples" / self.example), "-ansi-log", "false",
               "-output-dir", str(self.run_dir / "results"), *(["-resume"] if resume else [])]
        self.pipeline = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.run_dir, env={**os.environ, "NF_LAB_ADAPTER": self.url},
            stdout=(self.run_dir / "nextflow.out").open("a"), stderr=asyncio.subprocess.STDOUT, **CHILD)

    def pipeline_ended(self, _: Devices) -> bool:
        return self.pipeline is not None and self.pipeline.returncode is not None

    async def serve_until(self, done: Callable[[Devices], bool]) -> Devices:
        """Play the person at the decks (load plates, unload done ones) until `done(devices)`."""
        while True:
            devices = await self.api("GET", "/devices")
            if done(devices):
                return devices
            await self.serve_once(devices)
            await asyncio.sleep(self.s.tick)

    async def serve_once(self, devices: Devices) -> None:
        """One person step at most, then look again: the decks change after each."""
        for d in devices:
            if d["status"] == "unload":
                plate = d["held"]["plate"]
                await self.person(f"Plate {plate} is done on {d['device_id']}: the operator takes it off (Unload).",
                                  d["device_id"], "POST", f"/devices/{d['device_id']}/unload")
                return
        on_decks = {d["plate"] for d in devices if d["plate"]}
        waiting = [j for j in await self.api("GET", "/queue") if j["plate"] not in on_decks]
        for d in devices:
            if d["status"] == "empty" and (job := next((j for j in waiting if j["capability"] == d["capability"]), None)):
                plate = job["plate"]
                await self.person(f"The operator puts plate {plate} on {d['device_id']} and confirms the deck (Load).",
                                  d["device_id"], "POST", f"/devices/{d['device_id']}/load",
                                  {"plate": plate, "by": "demo operator"})
                return

    async def _end(self, proc: asyncio.subprocess.Process | None, timeout: float) -> None:
        if proc is None or proc.returncode is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)  # the whole group: Nextflow's tasks, the adapter's workers
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()


# --- scenarios ---

async def pool(demo: Demo) -> None:
    await demo.say("Pool: one pipeline, four plates, two simulated liquid handlers. Starting fresh: a new ledger "
                   "and empty decks.")
    await demo.fresh_adapter()
    await demo.say("nextflow run examples/plate_transfer: each wet task asks the adapter for a capability, "
                   "'liquid-handling', not for a robot.")
    await demo.start_pipeline()
    await demo.say("The jobs queue in the adapter. A robot takes a job only when it is free and the job's plate is on "
                   "its deck, so the operator loads plates as robots free up.")
    await demo.serve_until(demo.pipeline_ended)
    await demo.serve_until(lambda ds: all(d["status"] != "unload" for d in ds))  # take the last plates off
    code = demo.pipeline.returncode
    await demo.say(f"Run finished (exit {code}). Both robots worked at once, one plate each at a time. "
                   f"results/samples.csv names the robot that pipetted every well.")


def mid_plate(d: dict) -> bool:
    """Running, and liquid has moved: an e-stop now spoils the plate."""
    return d["status"] == "running" and bool(d["held"]) and not d["held"]["stoppable"]


async def estop(demo: Demo) -> None:
    await demo.say("E-stop: the same run, but a person stops one robot mid-plate. Starting fresh.")
    await demo.fresh_adapter()
    await demo.start_pipeline()
    await demo.say("Four plates, two robots. Once liquid has moved on a robot, the operator will stop it.")
    while True:
        devices = await demo.serve_until(lambda ds: demo.pipeline_ended(ds) or any(mid_plate(d) for d in ds))
        target = next((d for d in devices if mid_plate(d)), None)
        if target is None:  # every plate finished before the operator got to one
            await demo.say(f"Every plate finished before the operator reached E-stop (exit {demo.pipeline.returncode}): "
                           "the run succeeded. Start E-stop again to see the stop.")
            return
        first, job = target["device_id"], target["held"]
        same_job = lambda ds: any(d["device_id"] == first and mid_plate(d) and d["held"]["job_id"] == job["job_id"]
                                  for d in ds)
        if await demo.person(f"Something looks wrong on {first}: the operator presses E-stop.", first,
                             "POST", f"/devices/{first}/estop", still=same_job) is not None:
            break
        await demo.say(f"Too late: plate {job['plate']} on {first} finished before the operator reached E-stop. "
                       "They'll stop the next plate instead.")
    await demo.say(f"{first} stops at once, mid-plate: its job ends 'stopped' and the robot stays held for a person. "
                   "Nextflow's errorStrategy 'finish' doesn't stop the other robot: tasks already running finish, "
                   "nothing new starts, and the run will end failed.")
    await demo.serve_until(lambda ds: any(d["device_id"] == first and d["status"] == "needs_person" for d in ds))
    await demo.person(f"The operator checks the deck: plate {job['plate']} is spoiled. They press Failed: the job is "
                      "voided, so it may run again.", first, "POST", f"/jobs/{job['job_id']}/resolve",
                      {"outcome": "void", "by": "demo operator", "note": "spoiled by the e-stop"})
    await demo.person(f"...and take plate {job['plate']} off {first} (Unload). Unload waits for Succeeded or Failed.",
                      first, "POST", f"/devices/{first}/unload")
    await demo.serve_until(demo.pipeline_ended)
    await demo.serve_until(lambda ds: all(d["status"] != "unload" for d in ds))
    await demo.say(f"First run over (exit {demo.pipeline.returncode}): plate {job['plate']} failed, the plates "
                   "already submitted were finished. Now they prepare the plate again and rerun with -resume.")
    await demo.start_pipeline(resume=True)
    await demo.say(f"-resume: Nextflow reuses the finished tasks; the ledger lets only voided plate {job['plate']} "
                   "run again, as attempt 2. Nothing else touches a robot.")
    await demo.serve_until(demo.pipeline_ended)
    await demo.serve_until(lambda ds: all(d["status"] != "unload" for d in ds))
    await demo.say(f"Rerun finished (exit {demo.pipeline.returncode}). The ledger shows plate {job['plate']}: "
                   "attempt 1 voided after the e-stop, attempt 2 done.")


async def side_by_side(demo: Demo) -> None:
    await demo.say("Side by side: one sample plate, two simulated liquid handlers and a thermocycler. Starting fresh: "
                   "a new ledger and empty decks.")
    await demo.fresh_adapter()
    await demo.say("nextflow run examples/parallel_extraction: plate P1 is split into an RNA and a DNA aliquot, "
                   "both are extracted at once on the two liquid handlers, then each eluate goes through PCR.")
    await demo.start_pipeline()
    await demo.say("The split first. Once it's done, the RNA and DNA extractions queue together: with both robots "
                   "free they run at the same time, one aliquot each. Then both eluates queue for the thermocycler.")
    await demo.serve_until(demo.pipeline_ended)
    await demo.serve_until(lambda ds: all(d["status"] != "unload" for d in ds))  # take the last plates off
    await demo.say(f"Run finished (exit {demo.pipeline.returncode}). results/samples.csv gives each sample's RNA and DNA "
                   "yield with the robot that extracted it and the thermocycler that ran its PCR; results/qc flags low yields.")


async def scale(demo: Demo) -> None:
    await demo.say("Scale: an RT-qPCR screen of 1000 samples on 11 plates, over 4 liquid handlers and 6 qPCR "
                   "instruments. Starting fresh: a new ledger and empty decks.")
    await demo.fresh_adapter(simulated_operator=True, step=demo.s.step / 3)  # 11 plates in about 3 minutes
    await demo.say("At this scale nobody narrates every plate move: the adapter's simulated operator (a plate mover, "
                   "or a busy technician) loads and unloads plates as robots free up.")
    await demo.say("nextflow run examples/qpcr: each plate's RNA extraction asks for 'liquid-handling', its qPCR for "
                   "'qpcr'. The same pipeline runs one plate or a hundred; only the lab's robots set the pace.")
    await demo.start_pipeline()
    while demo.pipeline.returncode is None:
        devices, queue = await demo.api("GET", "/devices"), await demo.api("GET", "/queue")
        busy = {c: sum(d["status"] == "running" for d in devices if d["capability"] == c) for c in ("liquid-handling", "qpcr")}
        if busy["liquid-handling"] or busy["qpcr"] or queue:
            await demo.say(f"{busy['liquid-handling']} of 4 liquid handlers and {busy['qpcr']} of 6 qPCR instruments "
                           f"running; {len(queue)} jobs waiting for a robot.")
        await asyncio.sleep(max(demo.s.beat * 2, demo.s.tick))
    samples = demo.run_dir / "results" / "samples.csv"
    calls = samples.read_text().splitlines()[1:] if samples.exists() else []
    detected = sum(",detected," in c for c in calls)
    await demo.say(f"Run finished (exit {demo.pipeline.returncode}). results/samples.csv: {len(calls)} samples, "
                   f"{detected} detected, each with its Ct, its plate's control check and the two robots that "
                   "handled it. The lab report lists every job.")


async def tour(demo: Demo) -> None:
    await demo.say("Lab tour: five plates, five routines, 17 robots, one of each machine (two liquid handlers). "
                   "Starting fresh: a new ledger and empty decks.")
    await demo.fresh_adapter(simulated_operator=True, step=demo.s.step / 2)  # about 2.5 minutes
    await demo.say("The adapter's simulated operator moves the plates, so the deck views stay in front: a PCR set-up, "
                   "a cell viability assay, a DNA extraction with a yield check, a compound plate and an RT-qPCR plate.")
    await demo.start_pipeline()
    seen: set[str] = set()
    total = len(await demo.api("GET", "/devices"))
    while demo.pipeline.returncode is None:
        # from the ledger, not GET /devices: a short job can start and end while a narration line is up
        with sqlite3.connect(f"file:{demo.run_dir / 'ledger.sqlite'}?mode=ro", uri=True) as db:
            started = db.execute("SELECT device_id, protocol, plate FROM jobs WHERE started_at IS NOT NULL "
                                 "ORDER BY started_at").fetchall()
        new = {d: (p, pl) for d, p, pl in started if d not in seen}
        seen.update(new)
        if new:
            await demo.say("First job on " + ", ".join(f"{d} ({p} on plate {pl})" for d, (p, pl) in new.items()) +
                           f". {len(seen)} of {total} robots have worked so far.")
        await asyncio.sleep(max(demo.s.beat, demo.s.tick))
    steps = demo.run_dir / "results" / "steps.csv"
    rows = steps.read_text().splitlines()[1:] if steps.exists() else []
    await demo.say(f"Run finished (exit {demo.pipeline.returncode}). results/steps.csv: {len(rows)} jobs, each with "
                   "its plate, the robot that ran it and when; the plates' results are in results/<routine>/.")


async def manual(demo: Demo) -> None:
    await demo.say(f"Manual: a fresh ledger, empty decks. Starting examples/{demo.example}.")
    await demo.fresh_adapter()
    await demo.start_pipeline()
    await demo.say("Your turn, you are the operator. The queue in the deck controls says which plate each job waits "
                   "for: load plates, unload done ones, try E-stop, then Succeeded or Failed.")
    await demo.pipeline.wait()
    await demo.say(f"Run ended (exit {demo.pipeline.returncode}). ↻ Rerun runs it again with -resume on the same "
                   "ledger; Manual starts over.")


async def rerun(demo: Demo) -> None:
    await demo.ensure_adapter()
    await demo.say("Rerun with -resume on the same ledger: Nextflow reuses the finished tasks and the ledger returns "
                   "done jobs, so only plates that may run again (voided, or never run) reach a robot.")
    await demo.start_pipeline(resume=True)
    await demo.pipeline.wait()
    await demo.say(f"Rerun ended (exit {demo.pipeline.returncode}).")


SCENARIOS: dict[str, tuple[str, str, Callable[[Demo], Awaitable[None]]]] = {
    "pool": ("Pool", "Four plates over two robots; the operator loads and unloads plates", pool),
    "estop": ("E-stop", "A person e-stops one robot mid-plate; Failed, Unload, then -resume reruns only that plate", estop),
    "side_by_side": ("Side by side", "One plate: its RNA and DNA aliquots extracted at once on two robots, then PCR; "
                                     "the operator moves the plates", side_by_side),
    "scale": ("Scale", "1000 samples on 11 plates over 10 robots; the adapter's simulated operator moves the plates",
              scale),
    "tour": ("Tour", "Five plates, five routines over 17 robots; the adapter's simulated operator moves the plates",
             tour),
    "manual": ("Manual", "A fresh ledger, empty decks and the pipeline running: you are the operator", manual),
}
