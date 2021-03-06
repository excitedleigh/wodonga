import typing as t
from contextlib import asynccontextmanager
from signal import SIGINT
from socket import AF_INET6

import trio
from structlog import BoundLogger

from .platform.darwin import cleanup_dead_process


class Service:
    _service_wanted: trio.Event
    _service_started: trio.Event
    _process: t.Optional[trio.Process] = None
    _port_map: t.Optional[t.Mapping[int, int]] = None
    _users = 0
    _nursery: trio.Nursery
    _log: BoundLogger
    signal: int
    _executable_last_started: float = 0.0

    def __init__(
        self,
        *,
        name,
        command,
        workdir,
        ports,
        nursery,
        logger: BoundLogger,
        env={},
        signal=SIGINT,
    ):
        self.name = name
        self.command = command
        self.workdir = workdir
        self.env = env
        self.ports = ports
        self.signal = signal
        self._nursery = nursery
        self._service_wanted = trio.Event()
        self._service_started = trio.Event()
        self._log = logger
        nursery.start_soon(self._run_loop)

    # ## Run loop
    #
    # This task is responsible for managing the subprocess that runs the
    # actual service.
    # It starts up in `__init__`, and should stay running for the
    # lifetime of the server.
    #
    # It runs a loop that waits for the service to be requested, starts it up,
    # waits for it to exit, cleans up, and repeats.
    async def _run_loop(self):
        while True:
            await self._service_wanted.wait()

            # TODO: flapping detection / exponential backoff
            await trio.sleep_until(self._executable_last_started + 10.0)
            self._executable_last_started = trio.current_time()

            # We start off by constructing a mapping between the ports we're
            # listening on, and the ports we're expecting the service to
            # listen on for us to proxy to.
            self._port_map = {}
            # Trio doesn't give us a way to call `nursery.start_soon()` and
            # then access the result, so we can't call `get_free_port()`
            # directly.
            # Instead, we define an async closure that awaits on it and
            # assigns the result to `self._port_map()`, and call that.

            async def get_port(port_to_map):
                self._port_map[port_to_map] = await get_free_port()

            async with trio.open_nursery() as nursery:
                for port in self.ports:
                    nursery.start_soon(get_port, port)

            # The mapping is communicated to our subprocess via environment
            # variables.
            env = {
                **self.env,
                **{
                    f"PORT_{port}": str(alloc_port)
                    for port, alloc_port in self._port_map.items()
                },
            }

            # We then start up the process and fire the `_service_started` event.
            try:
                self._process = await trio.open_process(
                    self.command,
                    cwd=self.workdir,
                    env=env,
                    start_new_session=True,
                )
                self._log.info("process started", pid=self._process.pid)

                self._service_started.set()

                # With the service started, we wait for it to stop, and clean up.
                #
                # Note that we _don't_ reset `self._service_wanted` here! If the
                # service was killed by the shutdown timer, it would have reset
                # `_service_wanted` itself. If the service crashed, we want to
                # restart it straight away.
                await self._process.wait()
            finally:
                await cleanup_dead_process(
                    self._process, signal=self.signal, logger=self._log
                )
            self._process = None
            self._service_started = trio.Event()

    # ## Shutdown timer
    #
    # This task is started when the service is running, but there are no open
    # connections; it's responsible for terminating the task after an idle
    # timeout.
    async def _shutdown_timer(self, task_status):
        self._log.debug("shutdown timer started")
        task_status.started()
        self._service_wanted = trio.Event()

        # If the service gets used while we're waiting for the timeout to expire, `self._service_wanted` will fire, and this task stops.
        with trio.move_on_after(600):
            await self._service_wanted.wait()
            self._log.debug("shutdown timer stopped because service was wanted")
            return

        # Otherwise, we shut down the process.
        self._log.debug("shutting down")
        await self.stop()

    # ## Public API
    # Any code that expects this service to be up and running should be wrapped in the `.use()` async context manager.
    @asynccontextmanager
    async def use(self):
        self._log.debug("use entered", existing_users=self._users)
        self._service_wanted.set()
        await self._service_started.wait()
        self._users += 1
        try:
            yield self._port_map
        finally:
            self._users -= 1
            self._log.debug("use exited", existing_users=self._users)
            if not self._users:
                # We're using start here, instead of start_soon, because we
                # want self._shutdown_timer to initialise synchronously. If
                # we'd used start_soon, another task could have entered a
                # use() context between us launching it and it
                # unsetting _service_wanted.
                await self._nursery.start(self._shutdown_timer)

    # To stop the process, call `.stop()`. Note that if the service is still expected to be running (e.g. there is still an active caller of `.use()`, or the idle timeout hasn't completed yet) the process will start back up again.
    async def stop(self):
        if self._process is not None:
            with trio.move_on_after(5):
                self._log.debug("politely asking process to stop")
                self._process.send_signal(self.signal)
                exit_code = await self._process.wait()
                self._log.debug("process stopped", exit_code=exit_code)
                return
            self._log.debug("slightly less politely asking OS to stop process")
            self._process.kill()
            exit_code = await self._process.wait()
            self._log.debug("process stopped", exit_code=exit_code)


async def get_free_port():
    s = trio.socket.socket(family=AF_INET6)
    await s.bind(("::1", 0, 0, 0))
    _, alloc_port, _, _ = s.getsockname()
    s.close()
    return alloc_port


async def wait_for_port(port):
    while True:
        try:
            stream = await trio.open_tcp_stream(host="::1", port=port)
        except OSError:
            pass
        else:
            await stream.aclose()
            break
        await trio.sleep(0.1)
