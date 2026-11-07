"""Private native component transport; workload pipes contain no protocol data."""

import asyncio
import json
import os
import secrets
import sys
import subprocess
from pathlib import Path
from contextlib import suppress
from functools import lru_cache

from oea.contracts.errors import RuntimeFault, require


PROTOCOL = 1
VERSION = "3.3.1"
PACKAGE_DIRECTORY = Path(__file__).resolve().parents[1]


def helper_environment():
    return {name: os.environ[name] for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR") if name in os.environ}


def executable():
    """The installed package owns the executable, never the Agent's PATH/cwd."""
    filename = "oea-sandbox.exe" if os.name == "nt" else "oea-sandbox"
    path = PACKAGE_DIRECTORY / "_native" / filename
    if not path.is_file():
        from importlib.metadata import distribution, PackageNotFoundError
        try:
            path = Path(distribution("orchestrate-everything-with-agents").locate_file("oea/_native/" + filename))
        except PackageNotFoundError:
            pass
    require(sys.platform in {"win32", "linux"} and path.is_file(), "SANDBOX_UNAVAILABLE",
            "Install the OEA native component for this operating system")
    return path


@lru_cache(maxsize=4)
def _inspect(path, revision):
    result = subprocess.run([path, "--inspect"], capture_output=True, timeout=15, env=helper_environment(),
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    require(result.returncode == 0, "SANDBOX_UNAVAILABLE", "Native component inspection failed",
            details={"exit_code": result.returncode, "stderr": result.stderr.decode("utf-8", errors="replace")[:8192]})
    info = json.loads(result.stdout)
    require(info["protocol"] == PROTOCOL and info["version"] == VERSION, "SANDBOX_VERSION_MISMATCH", "Install the matching native component")
    return {"state":"installed", **info}


async def inspect_component():
    try:
        path = executable()
        info = path.stat()
        return await asyncio.to_thread(_inspect, str(path), (info.st_mtime_ns, info.st_size, os.environ.get("PATH")))
    except (RuntimeFault, OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"state":"unavailable", "error": error.as_dict() if isinstance(error, RuntimeFault) else {"code":"SANDBOX_UNAVAILABLE", "safe_message":str(error)}}


class NativeProcess:
    def __init__(self, process, reader, writer, server):
        self.process = process
        self.reader, self.writer = reader, writer
        self.server = server
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self.pid = process.pid
        self.returncode = None
        self.terminal = None
        self.started = None
        self.task = None
        self.component_exited = asyncio.create_task(process.wait())

    async def event(self):
        reading = asyncio.create_task(self.reader.readline())
        try:
            done, _ = await asyncio.wait({reading, self.component_exited}, return_when=asyncio.FIRST_COMPLETED)
            if reading not in done:
                try:
                    # Drain a final frame already queued by the exited local
                    # component; never wait indefinitely for a lost delimiter.
                    line = await asyncio.wait_for(asyncio.shield(reading), 1)
                except TimeoutError:
                    raise RuntimeFault("SANDBOX_CONTROL_LOST", "Native component exited without a complete control receipt",
                                       details={"component_exit_code": self.process.returncode}) from None
            else:
                line = reading.result()
        except ConnectionError as error:
            raise RuntimeFault("SANDBOX_CONTROL_LOST", "Native component disconnected; workload outcome is unknown",
                               details={"cause": str(error)}) from error
        finally:
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        require(line, "SANDBOX_CONTROL_LOST", "Native component disconnected; workload outcome is unknown")
        try:
            event = json.loads(line)
        except ValueError as error:
            raise RuntimeFault("SANDBOX_PROTOCOL_ERROR", "Native component returned an invalid control frame") from error
        require(isinstance(event, dict) and isinstance(event.get("event"), str), "SANDBOX_PROTOCOL_ERROR", "Native control frame has no event")
        if event["event"] == "error":
            raise RuntimeFault("SANDBOX_EXECUTION_FAILED", event["message"], details={key:event.get(key) for key in ("launch_accepted", "process_stopped", "exit_code")})
        return event

    async def finish(self):
        try:
            event = await self.event()
            require(event["event"] == "exited", "SANDBOX_PROTOCOL_ERROR", "Expected native terminal receipt")
            require(event.get("process_stopped") is True and (event.get("exit_code") is None or type(event["exit_code"]) is int),
                    "SANDBOX_PROTOCOL_ERROR", "Native terminal receipt does not confirm a stopped workload")
            self.terminal = event
            self.returncode = event["exit_code"]
            self.stdin.close()
            await self.process.wait()
            return self.returncode
        finally:
            self.writer.close()
            with suppress(ConnectionError):
                await self.writer.wait_closed()
            await self.server.wait_closed()

    async def wait(self):
        return await asyncio.shield(self.task)

    async def stop(self):
        self.stdin.close()
        if not self.task.done():
            # A terminal frame can close the control channel while pipe capture
            # is still finishing. Its original receipt decides the outcome.
            with suppress(ConnectionError):
                self.writer.write(b"terminate\n")
                await self.writer.drain()
        await self.wait()

    def close(self):
        # Closing a live host channel asks the component to stop its workload.
        self.writer.close()


async def spawn(request, *, component=None, limit=65536):
    """Authenticate one short-lived loopback connection before sending policy."""
    component = Path(component) if component is not None else executable()
    require(component.is_absolute() and component.is_file(), "SANDBOX_UNAVAILABLE", "Native component is missing")
    loop = asyncio.get_running_loop()
    connected = loop.create_future()
    nonce = secrets.token_hex(32)

    async def accept(reader, writer):
        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), 10))
            if not isinstance(hello, dict) or hello.get("nonce") != nonce or connected.done():
                writer.close()
                return
            if hello.get("protocol") != PROTOCOL or hello.get("version") != VERSION:
                connected.set_exception(RuntimeFault("SANDBOX_VERSION_MISMATCH", "Install the matching native component",
                    details={"expected_version":VERSION,"component_version":hello.get("version"),"component_protocol":hello.get("protocol")}))
                writer.close()
                return
            connected.set_result((reader, writer))
        except (ValueError, TimeoutError, OSError):
            writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=1024 * 1024)
    env = helper_environment()
    env.update(OEA_NATIVE_PORT=str(server.sockets[0].getsockname()[1]), OEA_NATIVE_NONCE=nonce)
    process = None
    native = None
    try:
        process = await asyncio.create_subprocess_exec(str(component), env=env, cwd=component.parent,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=limit,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        exited = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait({connected, exited}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if connected not in done:
                require(False, "SANDBOX_UNAVAILABLE", "Native component exited or did not authenticate")
            reader, writer = connected.result()
        finally:
            exited.cancel()
        writer.write(json.dumps({"protocol": PROTOCOL, **request}, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        native = NativeProcess(process, reader, writer, server)
        native.started = await asyncio.wait_for(native.event(), 30)
        if request["operation"] == "probe":
            require(native.started["event"] == "capabilities", "SANDBOX_PROTOCOL_ERROR", "Expected native capabilities")
            writer.close()
            await process.wait()
        else:
            require(native.started["event"] == "started", "SANDBOX_PROTOCOL_ERROR", "Expected native launch receipt")
            native.task = asyncio.create_task(native.finish())
        return native
    except BaseException:
        if native:
            native.close()
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        server.close()
        # Python 3.12 waits for accepted connections too. Their lifetime is the
        # command lifetime, so waiting here would turn every yield into a wait.
        if native is None or native.task is None:
            await server.wait_closed()
