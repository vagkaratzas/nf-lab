"""Demo scenarios: the dashboard's control panel.

A scenario starts a fresh adapter (new ledger, two simulated robots, empty decks),
runs the example pipeline, and plays the person at the deck: it loads and unloads
plates, presses e-stop, Succeeded or Failed, all through the adapter's operator
API, as a person on the deck page would. It narrates each step.

Pause holds the scenario before its next narration or person step; Nextflow and
the robots keep going (a paused demo leaves plates waiting to be loaded).
Stop ends the pipeline (TERM: jobs before the stop boundary are safe-stopped) and
leaves the adapter up to look around.

Never imports adapter code: it starts the adapter as a process and talks HTTP.
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    nf_lab: Path
    port: int = 8000
    backend: str = "plr"  # plr: PyLabRobot simulation with deck views; fake: tests
    step: float = 1.0  # seconds per simulated robot step: slow enough to follow (a plate takes ~12 s)
    nextflow: list[str] = field(default_factory=lambda: [os.environ.get("NXF_CMD", "nextflow")])
    beat: float = 1.5  # pause before each person step, so the audience sees it coming
    tick: float = 0.5  # how often the person looks at the decks
    runs: Path | None = None  # one folder per scenario (ledger, logs, results); default nf-lab/build/demo


class Busy(Exception):
    """A scenario is already running."""


Devices = list[dict]


class Demo:
    def __init__(self, settings: Settings):
        self.s = settings
        self.url = f"http://127.0.0.1:{settings.port}"
        self.name: str | None = None
        self.status = "idle"  # running, finished, failed, stopped
        self.log: list[dict] = []
        self.highlight: dict | None = None  # the device the last person step acted on
        self.playing = asyncio.Event()
        self.playing.set()
        self.task: asyncio.Task | None = None
        self.adapter: asyncio.subprocess.Process | None = None
        self.pipeline: asyncio.subprocess.Process | None = None
        self.run_dir: Path | None = None

    # --- control ---

    def view(self) -> dict:
        return {"scenarios": [{"id": k, "title": t, "blurb": b} for k, (t, b, _) in SCENARIOS.items()],
                "active": self.name, "status": self.status, "paused": not self.playing.is_set(),
                "log": self.log[-60:], "highlight": self.highlight}

    def start(self, name: str) -> None:
        if self.task and not self.task.done():
            raise Busy(f"{self.name} is running: stop it first")
        self.name, self.status, self.log, self.highlight = name, "running", [], None
        self.playing.set()
        self.run_dir = (self.s.runs or self.s.nf_lab / "build" / "demo") / name
        self.task = asyncio.create_task(self._run(SCENARIOS[name][2]))

    async def _run(self, scenario: Callable[["Demo"], Awaitable[None]]) -> None:
        try:
            await scenario(self)
            self.status = "finished"
        except asyncio.CancelledError:
            self.status = "stopped"
        except Exception as e:
            self.note("error", f"{type(e).__name__}: {e}")
            self.status = "failed"

    def pause(self) -> None:
        self.playing.clear()

    def play(self) -> None:
        self.playing.set()

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.note("say", "Stopped. The pipeline was asked to stop (a robot past the stop boundary finishes its plate); "
                             "the adapter stays up.")
        await self._end(self.pipeline, timeout=60)
        self.playing.set()

    async def shutdown(self) -> None:
        await self.stop()
        await self._end(self.adapter, timeout=10)

    # --- steps scenarios are made of ---

    def note(self, kind: str, text: str, device: str | None = None) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        if device:
            self.highlight = {"device": device, "n": len(self.log)}

    async def say(self, text: str) -> None:
        """Narrate, then give the audience time to read it (longer text, longer pause)."""
        await self.playing.wait()
        self.note("say", text)
        await asyncio.sleep(self.s.beat * (1 + len(text) / 60))

    async def person(self, text: str, device: str, method: str, path: str, body: dict | None = None) -> dict:
        """A person step: wait if paused, a beat for the audience, then press the button."""
        await self.playing.wait()
        await asyncio.sleep(self.s.beat)
        await self.playing.wait()
        self.note("person", text, device)
        return await self.api(method, path, body)

    async def api(self, method: str, path: str, body: dict | None = None):
        def call():
            data = json.dumps(body if body is not None else {"by": "demo operator"}).encode() if method == "POST" else None
            req = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        return await asyncio.to_thread(call)

    async def fresh_adapter(self) -> None:
        """Stop the adapter this panel started, wipe the scenario's folder, start a new adapter on it."""
        await self._end(self.adapter, timeout=10)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", self.s.port)) == 0:
                raise RuntimeError(f"port {self.s.port} is in use: stop the adapter you started yourself, then retry")
        python = self.s.nf_lab / "adapter/.venv/bin/python"
        if not python.exists():
            raise RuntimeError(f"no adapter environment at {python}: run `uv sync` in adapter/")
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True)
        env = {**os.environ, "NF_LAB_BACKEND": self.s.backend, "NF_LAB_DEVICES": "2", "NF_LAB_STEP": str(self.s.step),
               "NF_LAB_SIMULATED_OPERATOR": "0", "NF_LAB_VISUALIZER": "1" if self.s.backend == "plr" else "0",
               "NF_LAB_LEDGER": str(self.run_dir / "ledger.sqlite")}
        self.adapter = await asyncio.create_subprocess_exec(
            str(python), "-m", "uvicorn", "nf_lab_adapter.app:main", "--factory", "--port", str(self.s.port),
            "--app-dir", str(self.s.nf_lab / "adapter/src"),
            stdout=(self.run_dir / "adapter.log").open("w"), stderr=asyncio.subprocess.STDOUT, env=env,
            start_new_session=True)  # Ctrl-C on the dashboard doesn't hit it first; shutdown() ends it
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
        cmd = [*self.s.nextflow, "run", str(self.s.nf_lab / "examples/plate_transfer"), "-ansi-log", "false",
               "-output-dir", str(self.run_dir / "results"), *(["-resume"] if resume else [])]
        self.pipeline = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.run_dir, env={**os.environ, "NF_LAB_ADAPTER": self.url},
            stdout=(self.run_dir / "nextflow.out").open("a"), stderr=asyncio.subprocess.STDOUT, start_new_session=True)

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
        waiting = [j["plate"] for j in await self.api("GET", "/queue") if j["plate"] not in on_decks]
        for d in devices:
            if d["status"] == "empty" and waiting:
                plate = waiting.pop(0)
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

def robot(devices: Devices, i: int) -> dict:
    return devices[i]


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


async def estop(demo: Demo) -> None:
    await demo.say("E-stop: the same run, but a person stops one robot mid-plate. Starting fresh.")
    await demo.fresh_adapter()
    await demo.start_pipeline()
    await demo.say("Four plates, two robots. Watch the first robot: once liquid has moved, the operator will stop it.")
    devices = await demo.serve_until(lambda ds: robot(ds, 0)["status"] == "running"
                                     and robot(ds, 0)["held"] and not robot(ds, 0)["held"]["stoppable"])
    first, job = robot(devices, 0)["device_id"], robot(devices, 0)["held"]
    await demo.person(f"Something looks wrong on {first}: the operator presses E-stop.", first,
                      "POST", f"/devices/{first}/estop")
    await demo.say(f"{first} stops at once, mid-plate: its job ends 'stopped' and the robot stays held for a person. "
                   "Nextflow's errorStrategy 'finish' doesn't stop the other robot: tasks already running finish, "
                   "nothing new starts, and the run will end failed.")
    await demo.serve_until(lambda ds: robot(ds, 0)["status"] == "needs_person")
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


SCENARIOS: dict[str, tuple[str, str, Callable[[Demo], Awaitable[None]]]] = {
    "pool": ("Pool", "Four plates over two robots; the operator loads and unloads plates", pool),
    "estop": ("E-stop", "A person e-stops one robot mid-plate; Failed, Unload, then -resume reruns only that plate", estop),
}
