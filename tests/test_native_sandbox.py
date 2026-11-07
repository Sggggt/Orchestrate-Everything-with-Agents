"""Opt-in real backend tests; temporary inputs never enter the source tree."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from oea.workspace.native import NativeProcess, spawn
from oea.contracts.errors import RuntimeFault


class NativeProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_component_with_partial_control_frame_does_not_claim_stop_or_hang(self):
        component = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", "pass",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        reader = asyncio.StreamReader()
        reader.feed_data(b'{')
        writer, server = Mock(wait_closed=AsyncMock()), Mock(wait_closed=AsyncMock())
        process = NativeProcess(component, reader, writer, server)
        process.task = asyncio.create_task(process.finish())
        with self.assertRaises(RuntimeFault) as fault:
            await asyncio.wait_for(process.wait(), 3)
        self.assertEqual(fault.exception.code, "SANDBOX_CONTROL_LOST")
        self.assertIsNone(process.terminal)
        self.assertIsNone(process.returncode)
        self.assertEqual(component.returncode, 0)

    async def test_terminal_receipt_closes_component_stdin_before_reaping(self):
        component = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"event":"exited","exit_code":0,"process_stopped":true}\n')
        reader.feed_eof()
        writer = Mock(wait_closed=AsyncMock())
        server = Mock(wait_closed=AsyncMock())
        process = NativeProcess(component, reader, writer, server)
        process.task = asyncio.create_task(process.finish())
        try:
            self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
            self.assertTrue(component.stdin.is_closing())
            self.assertTrue(process.terminal["process_stopped"])
            self.assertEqual(component.returncode, 0)
        finally:
            if component.returncode is None:
                component.kill()
            await component.wait()
            if not process.task.done():
                process.task.cancel()
            await asyncio.gather(process.task, return_exceptions=True)


@unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
class NativeSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work with space"
        self.work.mkdir()
        self.component = Path(os.environ["OEA_TEST_NATIVE_COMPONENT"])
        self.environment = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "SystemDrive", "COMSPEC") if key in os.environ}
        self.environment.update(TMP=str(self.work), TEMP=str(self.work), HOME=str(self.work),
                                USERPROFILE=str(self.work), LOCALAPPDATA=str(self.work), APPDATA=str(self.work), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        self.live = []

    async def asyncTearDown(self):
        for process in self.live:
            if process.task and not process.task.done():
                await process.stop()

    async def launch(self, code, *, args=(), timeout=10000, network="deny", readonly=()):
        process = await spawn({"operation":"exec", "argv":[sys.executable, "-B", "-c", code, *args],
            "cwd":str(self.work), "readwrite":[str(self.work)],
            "readonly":list(dict.fromkeys([sys.prefix, sys.base_prefix, *map(str, readonly)])),
            "environment":self.environment, "network":network, "timeout_ms":timeout}, component=self.component)
        self.live.append(process)
        return process

    async def capture(self, process, data=b""):
        if data:
            process.stdin.write(data)
            await process.stdin.drain()
        process.stdin.close()
        out, err, _ = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
        return out, err

    async def test_arguments_are_lossless_and_shell_characters_are_data(self):
        arguments = ["", "with space", 'a"b', "a'b", "\\", "end\\", "你好🌍", "$(touch forbidden)", "x;y&z|q<>%PATH%"]
        process = await self.launch("import sys,json; print(json.dumps(sys.argv[1:],ensure_ascii=False))", args=arguments)
        out, err = await self.capture(process)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(json.loads(out), arguments)
        self.assertFalse((self.work / "forbidden").exists())

    async def test_streams_input_and_nonzero_exit_are_independent(self):
        process = await self.launch("import sys; print('ready',flush=True); print(sys.stdin.readline().strip(),flush=True); print('diagnostic',file=sys.stderr); sys.exit(7)")
        self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 3)).rstrip(b"\r\n"), b"ready")
        out, err = await self.capture(process, "héllo\n".encode())
        self.assertIn("héllo".encode(), out)
        self.assertIn(b"diagnostic", err)
        self.assertEqual(process.returncode, 7)

    async def test_filesystem_policy_blocks_outside_and_readonly_write(self):
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        material = self.root / "materials"
        material.mkdir()
        (material / "source.txt").write_text("source", encoding="utf-8")
        code = """import pathlib,sys,json
results={}
for name,path,mode in [('outside',sys.argv[1],'r'),('readonly_write',sys.argv[2],'w')]:
 try:
  with open(path,mode) as f: f.read() if mode=='r' else f.write('changed')
  results[name]='allowed'
 except OSError: results[name]='denied'
results['source']=pathlib.Path(sys.argv[2]).read_text()
pathlib.Path('output.txt').write_text('done')
print(json.dumps(results))
"""
        process = await self.launch(code, args=[str(outside), str(material / "source.txt")], readonly=[material])
        out, err = await self.capture(process)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(json.loads(out), {"outside":"denied", "readonly_write":"denied", "source":"source"})
        self.assertEqual((self.work / "output.txt").read_text(), "done")

    async def test_cancel_and_timeout_report_actual_stop(self):
        process = await self.launch("import time; print('ready',flush=True); time.sleep(60)")
        self.assertEqual((await process.stdout.readline()).rstrip(b"\r\n"), b"ready")
        await asyncio.wait_for(process.stop(), 5)
        self.assertTrue(process.terminal["process_stopped"])
        self.assertEqual(process.terminal["termination"], "cancelled")
        process = await self.launch("import time; time.sleep(60)", timeout=300)
        await asyncio.wait_for(self.capture(process), 5)
        self.assertTrue(process.terminal["process_stopped"])
        self.assertEqual(process.terminal["termination"], "timeout")

    async def test_host_loopback_denied(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0))
            listener.listen()
            code = "import socket,sys; s=socket.socket(); s.settimeout(0.5); print(s.connect_ex(('127.0.0.1',int(sys.argv[1]))))"
            process = await self.launch(code, args=[str(listener.getsockname()[1])])
            out, err = await self.capture(process)
            self.assertEqual(process.returncode, 0, err)
            self.assertNotEqual(int(out), 0)

    async def test_child_process_obeys_policy(self):
        private = self.root / "private.txt"
        private.write_text("private", encoding="utf-8")
        code = "import subprocess,sys; p=subprocess.run([sys.executable,'-c','import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())',sys.argv[1]],capture_output=True); print(p.returncode)"
        process = await self.launch(code, args=[str(private)])
        out, err = await self.capture(process)
        self.assertEqual(process.returncode, 0, err)
        self.assertNotEqual(int(out), 0)

    async def test_udp_and_tcp_host_traffic_are_denied_on_both_ip_families(self):
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                with self.subTest(family=family, kind=kind), socket.socket(family, kind) as listener:
                    try:
                        listener.bind((address, 0))
                    except OSError:
                        if family == socket.AF_INET6:
                            continue
                        raise
                    if kind == socket.SOCK_STREAM:
                        listener.listen()
                    listener.setblocking(False)
                    code = """import socket,sys
s=socket.socket(int(sys.argv[1]),int(sys.argv[2])); s.settimeout(.2)
try:
 if s.type==socket.SOCK_STREAM: s.connect((sys.argv[3],int(sys.argv[4]))); s.sendall(b'network-fixture')
 else: s.sendto(b'\\x00\\x01\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x07example\\x04test\\x00\\x00\\x01\\x00\\x01',(sys.argv[3],int(sys.argv[4])))
except OSError: pass
"""
                    process = await self.launch(code, args=[str(family), str(kind), address, str(listener.getsockname()[1])])
                    out, err = await self.capture(process)
                    self.assertEqual(process.returncode, 0, err)
                    with self.assertRaises(BlockingIOError):
                        listener.accept() if kind == socket.SOCK_STREAM else listener.recvfrom(1024)

    async def test_foreground_exit_stops_descendants(self):
        marker = self.work / "late.txt"
        child = "import pathlib,time,sys; time.sleep(.5); pathlib.Path(sys.argv[1]).write_text('late')"
        process = await self.launch("import subprocess,sys; subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]); print('parent-exit')",
                                    args=[child, str(marker)])
        await asyncio.wait_for(self.capture(process), 4)
        await asyncio.sleep(.7)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows process memory boundary")
    async def test_workload_cannot_read_host_process_memory(self):
        import ctypes
        marker = ctypes.create_string_buffer(b"synthetic-process-boundary")
        code = """import ctypes,sys
from ctypes import wintypes
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; k.OpenProcess.restype=wintypes.HANDLE
k.ReadProcessMemory.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]; k.ReadProcessMemory.restype=wintypes.BOOL
k.CloseHandle.argtypes=[wintypes.HANDLE]
h=k.OpenProcess(0x10,False,int(sys.argv[1])); result=False
if h:
 b=ctypes.create_string_buffer(int(sys.argv[3])); n=ctypes.c_size_t()
 result=bool(k.ReadProcessMemory(h,int(sys.argv[2]),b,len(b),ctypes.byref(n)))
 k.CloseHandle(h)
print(result)
"""
        process = await self.launch(code, args=[str(os.getpid()),str(ctypes.addressof(marker)),str(len(marker))])
        out, err = await self.capture(process)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out.strip(), b"False")

    async def test_lost_control_is_unknown_even_when_os_cleanup_stops_the_workload(self):
        marker = self.work / "late.txt"
        process = await self.launch("import pathlib,time,sys; print('ready',flush=True); time.sleep(.7); pathlib.Path(sys.argv[1]).write_text('late')", args=[str(marker)])
        await process.stdout.readline()
        process.process.kill()
        await process.process.wait()
        with self.assertRaises(RuntimeFault) as failure:
            await process.wait()
        self.assertEqual(failure.exception.code, "SANDBOX_CONTROL_LOST")
        self.assertIsNone(process.terminal)
        await asyncio.sleep(.9)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_NETWORK_ALLOW"), "External network validation is not enabled")
    async def test_allow_policy_can_make_an_actual_outbound_connection(self):
        code = "import socket; s=socket.create_connection(('example.com',80),timeout=8); s.sendall(b'GET / HTTP/1.0\\r\\nHost: example.com\\r\\n\\r\\n'); print(s.recv(64).startswith(b'HTTP/'))"
        process = await self.launch(code, network="allow", timeout=15000)
        out, err = await self.capture(process)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out.strip(), b"True")


@unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
class NativeWorkspaceTests(__import__("test_workspace").WorkspaceTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from unittest.mock import patch
        self.settings.sandbox = "native"
        native = patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"]))
        native.start()
        self.addCleanup(native.stop)

    async def test_default_shell_and_discovered_development_programs(self):
        result = await self.invoke("exec_command", {"command":"echo shell-ready", "yield_time_ms":1000})
        if result["state"] == "running":
            result = await self.invoke("write_stdin", {"command_session_id":result["command_session_id"], "yield_time_ms":5000})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("shell-ready", result["stdout"])
        for name in ("git", "rg", "node"):
            path = result["environment"]["programs"][name]
            if path:
                observed = await self.invoke("exec_command", {"argv":[path,"--version"], "yield_time_ms":1000})
                self.assertEqual(observed["exit_code"], 0, observed)

    async def test_terminate_remains_available_when_command_input_is_backpressured(self):
        result = await self.invoke("exec_command", {"argv":[sys.executable,"-B","-c","import time; time.sleep(30)"], "yield_time_ms":0})
        identity = result["command_session_id"]
        writing = asyncio.create_task(self.invoke("write_stdin", {"command_session_id":identity,"chars":"x" * 1048576}))
        await asyncio.sleep(.1)
        stopped = await asyncio.wait_for(self.invoke("write_stdin", {"command_session_id":identity,"terminate":True}), 5)
        await asyncio.wait_for(writing, 5)
        self.assertTrue(stopped["process_stopped"])

    async def test_unsupported_pty_does_not_launch_a_command(self):
        result = await self.invoke("exec_command", {"argv":[sys.executable,"-c","pass"], "tty":True})
        self.assertEqual(result["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertFalse(self.sessions.live)


if __name__ == "__main__":
    unittest.main()
