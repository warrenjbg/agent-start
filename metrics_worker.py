"""MetricsWorker: the hc-metrics thread's body, moved out of main.AgentStartApp with its behaviour unchanged.

While the engine serves (or stops) the worker polls the engine's monitoring data every LIVE_POLL_S and hands each
sample to the Tk thread (the status line, the graphs, a pending stop); it feeds the usage history (serving time,
requests, GPU energy) and the live GPU-fit check (_FitProbe). While idle it pauses the energy meter and retries a
usage-file save that failed.
It owns the serving seconds not yet in the usage history (served_acc / served_epoch / served_model); everything else
it reads from the app through its host, at the moment it reads it, exactly as the loop read the app before.

Everything here runs on the worker thread, except flush_served, which the app's destroy() also calls (Tk thread).
No Tk call is made here: results go back through the host's apply_metrics / set_gpu_fit_live, which post them.
A raising line must never end the loop: each poll's whole body sits in one try / except.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import psutil

import config_store
from engines.base import CAP_LIVE_ACTIVITY, CAP_REQUEST_END, CAP_REQUEST_HISTORY, MetricsSample

log = logging.getLogger("agent_start")

ACTIVE, STOPPING = "ACTIVE", "STOPPING"         # the host's state names while the engine serves / stops

USAGE_FLUSH_S = 60.0                     # usage history: serving time is added and the file saved this often
USAGE_RETRY_S = 30.0                     # ...and while idle a save that failed is retried this often
LIVE_POLL_S = 1.0                        # /metrics poll while serving (model state on the status line), as Strata's
                                         # own Monitor; stopped while the window is minimised


class _FitProbe:
    """The live GPU-fit check's measurements, kept by the metrics worker (never touched from the Tk thread): the
    engine's CPU share and the video memory, fed to fit_watch.decide once per poll. Read-only on processes."""

    def __init__(self) -> None:
        self.epoch: int | None = None
        self.state: Any = None
        self.procs: list = []
        self.procs_at = 0.0
        self.clean_s = 0.0
        self.last: float | None = None
        self.pre_cleared = False

    def _cpu_pct(self, now: float, endpoint: Any) -> float | None:
        if not self.procs or now - self.procs_at > 30.0:
            self.procs_at = now
            procs = []
            pids = getattr(endpoint, "engine_pids", None) or endpoint.protected_pids
            for pid in pids():
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(None)          # primes the counter: the first reading is from the next poll
                    procs.append(p)
                except psutil.Error:
                    continue
            known = {p.pid for p in self.procs}
            fresh = {p.pid for p in procs} != known
            self.procs = procs if fresh or not self.procs else self.procs
            if fresh:
                return None
        total, seen = 0.0, False
        for p in self.procs:
            try:
                total += p.cpu_percent(None)
                seen = True
            except psutil.Error:
                continue
        if not seen:
            self.procs = []
            return None
        return total / max(1, psutil.cpu_count() or 1)

    def observe(self, epoch: int, now: float, sample: Any, vram: Any, endpoint: Any, cfg: Any):
        """-> None (nothing to tell) or (slow | None, pre_cleared) for the Tk thread."""
        import fit_watch
        if epoch != self.epoch:
            self.__init__()  # type: ignore[misc]
            self.epoch = epoch
            self.state = fit_watch.FitState()
        live = getattr(sample, "live", None)
        generating = getattr(live, "state", None) == "generating"
        used = total = None
        try:
            snap = vram.snapshot()
            fresh = snap.samples and getattr(snap, "now", None) is not None and snap.now - snap.samples[-1][0] <= 10.0
            if getattr(snap, "available", True) and fresh and snap.total_mb:
                used, total = snap.samples[-1][1], snap.total_mb
        except Exception:  # noqa: BLE001
            used = total = None
        cpu = self._cpu_pct(now, endpoint) if generating else None
        fcfg = fit_watch.FitConfig.from_config(cfg)
        changed, self.state = fit_watch.decide(self.state, now, generating=generating, vram_used_mb=used,
                                               vram_total_mb=total, cpu_share_pct=cpu, cfg=fcfg)
        cleared = False
        if generating and not self.pre_cleared:
            clean = fit_watch.symptoms(fcfg, used, total, cpu) is False
            if clean and self.last is not None and now - self.last <= fit_watch.MAX_GAP_S:
                self.clean_s += now - self.last
            elif not clean:
                self.clean_s = 0.0
            if self.clean_s >= fcfg.clear_s:     # enough clean generating: the pre-check's "will not fit" is overruled
                self.pre_cleared = cleared = True
        self.last = now if generating else None
        if changed or cleared:
            return (self.state.slow if changed else None, cleared)
        return None


@dataclass
class MetricsHost:
    """What the worker reads from the app. Every callable is called on the worker thread (flush_served's model_id
    and energy also from destroy() on the Tk thread), each time the loop reads that value - never cached."""
    stop_evt: Callable[[], Any]                  # the app's stop event (threading.Event): set when the app closes
    state: Callable[[], str]                     # "IDLE" | "STARTING" | "ACTIVE" | "STOPPING" | "ERROR"
    metrics_epoch: Callable[[], int]             # bumped per start: a late poll of the old server is dropped
    cfg: Callable[[], Any]                       # the live config (endpoint_url, the GPU-fit settings)
    engine: Callable[[], Any]                    # the current engine object (capabilities, its metrics poller)
    endpoint: Callable[[], Any]                  # the server supervisor (status / log path, engine pids, engine_http)
    vram: Callable[[], Any]                      # the VRAM sampler (snapshot, energy_j when it meters energy)
    usage: Callable[[], Any]                     # usage_history.UsageHistory, or None
    energy: Callable[[], Any]                    # usage_history.EnergyMeter, or None
    loaded: Callable[[], Any]                    # True once the served model is loaded (None: unknown)
    model_id: Callable[[], Any]                  # the model name the server reports
    fetch_metrics: Callable[[], Any]             # test seam: the metrics fetcher (None = the engine's own)
    fetch_status: Callable[[], Any]              # test seam: the status fetcher (None = none)
    apply_metrics: Callable[..., None]           # apply_metrics(epoch, sample): posted to the Tk thread
    set_gpu_fit_live: Callable[..., None]        # set_gpu_fit_live(epoch, slow, pre_cleared): posted to the Tk thread
    stats_ms: Callable[[], float]                # the stats redraw period (main.STATS_MS): a gap longer than 3 of
                                                 # these makes the next poll a catch-up


class MetricsWorker:
    def __init__(self, host: MetricsHost) -> None:
        self.host = host
        self.served_epoch = 0
        self.served_acc = 0.0                    # serving seconds not yet in the usage history (metrics worker)
        self.served_model: str | None = None

    def poller(self):
        """The engine's poller. The test seams are looked up per call (tests swap the fetchers after building the
        app); without seams the engine uses its own HTTP fetchers."""
        seams = {}
        if self.host.fetch_metrics() is not None:
            seams["fetch_metrics"] = lambda r, **kw: self.host.fetch_metrics()(r, **kw)
            seams["fetch_status"] = lambda r: self.host.fetch_status()(r) if self.host.fetch_status() is not None \
                else None
        http = getattr(self.host.endpoint(), "engine_http", None)  # engines that ask the server themselves (live state)
        if http is not None:
            seams["http"] = http
        return self.host.engine().metrics_poller(self.host.cfg(), **seams)

    def run_log_path(self) -> str | None:
        """The server log of the run this app started (None for an adopted server): some engines only report
        per-request figures there."""
        try:
            st = self.host.endpoint().status()
            return None if st.get("adopted") else (st.get("log_path") or None)
        except Exception:  # noqa: BLE001
            return None

    def loop(self) -> None:
        """Worker: poll the engine's monitoring data every LIVE_POLL_S while it is serving."""
        last_poll = 0.0
        last_epoch = None
        poller = None
        served_last = None                       # serving time for the usage history, added once a minute
        fit = _FitProbe()                        # GPU fit: the live check (engines with watch_gpu_fit)
        usage_retry = 0.0                        # an unwritable usage file is retried this often, not every poll
        while not self.host.stop_evt().is_set():
            try:
                # Every LIVE_POLL_S while serving: the status line shows what the model is doing (reading /
                # generating / queued) and the stats graphs use the same samples.
                # Also while minimised / hidden in the tray: Turn off from the tray must still know whether a reply
                # or an agent's tool call is running (the request phases are only seen by polling).
                if self.host.state() in (ACTIVE, STOPPING):
                    epoch = self.host.metrics_epoch()
                    root = config_store.server_root(str(self.host.cfg().get("endpoint_url") or ""))
                    reports = any(self.host.engine().can(c) for c in (CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY,
                                                                      CAP_REQUEST_END))
                    if poller is None:
                        poller = self.poller()
                    # Engines return only their newest finished requests per poll: after a gap (section collapsed,
                    # window minimised, a new server) ask once for all of them so the 30 bars are complete.
                    now = time.monotonic()
                    catch_up = epoch != last_epoch or now - last_poll > 3 * self.host.stats_ms() / 1000.0
                    if epoch != last_epoch:          # a new server run: forget the previous one's counters
                        poller.reset(log_path=self.run_log_path())
                    if reports:
                        sample = poller.poll(root, catch_up) if root else None
                    else:
                        # nothing to ask: an empty sample ("nothing reported"), not a failed poll - a pending
                        # stop then waits the engine's idle time instead of being treated as a dead server
                        sample = MetricsSample()
                    last_poll, last_epoch = now, epoch
                    if getattr(self.host.engine(), "watch_gpu_fit", False) and sample is not None:
                        verdict = fit.observe(epoch, now, sample, self.host.vram(), self.host.endpoint(),
                                              self.host.cfg())
                        if verdict is not None:
                            self.host.set_gpu_fit_live(epoch, *verdict)
                    usage = self.host.usage()
                    if usage is not None:
                        if epoch != self.served_epoch:  # another server run: its model is not the last one's
                            self.flush_served(usage)
                            self.served_epoch, self.served_model = epoch, None
                        meter = self.host.energy()
                        fresh = 0
                        if sample is not None:
                            # requests seen while GPU energy was being counted also count toward tokens per Wh
                            fresh = usage.record(sample, now=time.time(),
                                                 metered=meter is not None and meter.metering)
                            if sample.model:             # the same name the requests are counted under
                                self.served_model = sample.model
                        serving = self.host.state() == ACTIVE and self.host.loaded() is not False
                        if serving:
                            if served_last is not None:      # capped: sleep / hibernation is not serving time
                                self.served_acc += min(max(0.0, now - served_last), 5.0)
                            served_last = now
                        else:
                            served_last = None
                        if meter is not None:                # the whole card's energy, only while serving
                            try:                             # an extra: it must never cost the poll itself
                                live = sample.live if sample is not None else None
                                busy = fresh > 0 or (live is not None and live.state in ("reading", "generating"))
                                read = getattr(self.host.vram(), "energy_j", None)   # a sampler without energy metering
                                meter.step(read() if callable(read) else None, now, serving, busy)
                            except Exception:  # noqa: BLE001
                                meter.pause()
                                log.debug("GPU energy step failed", exc_info=True)
                        if self.served_acc >= USAGE_FLUSH_S:
                            self.flush_served(usage)
                            usage.flush()
                            usage_retry = now
                    self.host.apply_metrics(epoch, sample)
                else:
                    served_last = None
                    if self.host.energy() is not None:
                        self.host.energy().pause()
                    usage = self.host.usage()
                    mono = time.monotonic()
                    if usage is not None and (self.served_acc > 0 or usage.dirty) \
                            and mono - usage_retry >= USAGE_RETRY_S:
                        usage_retry = mono
                        self.flush_served(usage)
                        usage.flush()
            except Exception:  # noqa: BLE001
                log.debug("metrics poll failed", exc_info=True)
            self.host.stop_evt().wait(LIVE_POLL_S)

    def flush_served(self, usage: Any) -> None:
        """Move the serving seconds collected so far into the usage history (metrics worker; destroy())."""
        acc, self.served_acc = self.served_acc, 0.0
        if acc > 0:
            usage.add_served(self.served_model or self.host.model_id(), acc, now=time.time())
        if self.host.energy() is not None:
            wh, busy_wh = self.host.energy().take()
            if wh > 0:
                usage.add_energy(self.served_model or self.host.model_id(), wh, busy_wh, now=time.time())
