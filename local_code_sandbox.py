"""Process-isolated QuickJS executor with a narrow local MCP RPC bridge."""

import asyncio
import concurrent.futures
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

import quickjs


MAX_CODE_CHARS = 20_000
MAX_RESULT_CHARS = 100_000
MAX_MEMORY_BYTES = 16 * 1024 * 1024
MAX_STACK_BYTES = 512 * 1024
MAX_RPC_CALLS = 3
MAX_RUNTIME_SECONDS = 30


def _worker_rpc(tool_name: str, raw_args: str) -> str:
    """Send an RPC request to the trusted parent and wait for its response."""
    print(json.dumps({
        "type": "rpc",
        "tool": tool_name,
        "arguments": json.loads(raw_args or "{}"),
    }, ensure_ascii=False), flush=True)
    line = sys.stdin.readline()
    if not line:
        return json.dumps({"ok": False, "error": "RPC bridge closed."})
    reply = json.loads(line)
    return json.dumps(reply, ensure_ascii=False)


def _worker_main() -> None:
    """Run the supplied expression in QuickJS; stdout is a private JSON protocol."""
    try:
        request = json.loads(sys.stdin.readline())
        code = request.get("code", "")
        if not code or len(code) > MAX_CODE_CHARS:
            raise ValueError(f"Generated code must be between 1 and {MAX_CODE_CHARS} characters.")

        context = quickjs.Context()
        context.set_memory_limit(MAX_MEMORY_BYTES)
        set_max_stack_size = getattr(context, "set_max_stack_size", None)
        if set_max_stack_size is not None:
            set_max_stack_size(MAX_STACK_BYTES)
        context.add_callable("__host_rpc", _worker_rpc)
        context.eval(
            "globalThis.mcp = Object.freeze({call: (name, args) => {"
            # Be tolerant of model-generated calls that pass SQL as a string
            # instead of the MCP tool's expected {sql: ...} argument object.
            "const rpcArgs = typeof args === 'string' ? {sql: args} : (args || {});"
            "const envelope = JSON.parse(__host_rpc(name, JSON.stringify(rpcArgs)));"
            "if (!envelope.ok) throw new Error(envelope.error);"
            "return envelope.result;"
            "}});"
        )
        # Parent enforces a hard wall-clock timeout and terminates this process.
        try:
            # Preferred contract: a single expression, typically an IIFE.
            output = context.eval(
                "(() => {"
                f"const generated = ({code});"
                "const value = typeof generated === 'function' ? generated() : generated;"
                "return JSON.stringify(value);"
                "})()"
            )
        except quickjs.JSException as error:
            if "SyntaxError" not in str(error):
                raise
            # Also accept a function body (declarations plus `return`) when a
            # model omits the requested IIFE wrapper. Parsing still happens in
            # the isolated QuickJS process and uses the same restricted RPC.
            output = context.eval(
                "(() => {"
                f"let value = (() => {{\n{code}\n}})();"
                "if (typeof value === 'function') value = value();"
                "return JSON.stringify(value);"
                "})()"
            )
        if not isinstance(output, str):
            raise TypeError(
                "Sandbox code must return a JSON-serializable value (usually an array of result rows)."
            )
        if len(output) > MAX_RESULT_CHARS:
            raise RuntimeError(f"Sandbox result exceeded {MAX_RESULT_CHARS} characters.")
        value = json.loads(output)
        print(json.dumps({"type": "result", "value": value}, ensure_ascii=False), flush=True)
    except BaseException as error:
        print(json.dumps({"type": "error", "error": f"{type(error).__name__}: {error}"}), flush=True)


def _worker_environment() -> dict[str, str]:
    """Pass only runtime necessities; never inherit Groq/MySQL credentials."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    python_dir = str(Path(sys.executable).parent)
    safe_path = os.pathsep.join([python_dir, str(Path(sys.prefix)), str(Path(system_root) / "System32")])
    env = {"PATH": safe_path, "SystemRoot": system_root, "WINDIR": system_root}
    for name in ("TEMP", "TMP"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _execute_worker(code: str, rpc_handler: Callable[[str, dict[str, Any]], Awaitable[str]], loop: asyncio.AbstractEventLoop) -> Any:
    """Blocking worker/protocol loop; runs off the app event-loop thread."""
    if not code or len(code) > MAX_CODE_CHARS:
        raise ValueError(f"Generated code must be between 1 and {MAX_CODE_CHARS} characters.")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(Path(__file__).resolve().parent),
        env=_worker_environment(),
        creationflags=flags,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    messages: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for output_line in process.stdout:
                messages.put(output_line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    process.stdin.write(json.dumps({"code": code}, ensure_ascii=False) + "\n")
    process.stdin.flush()
    deadline = loop.time() + MAX_RUNTIME_SECONDS
    rpc_count = 0
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Generated code exceeded the sandbox runtime limit.")
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("Generated code exceeded the sandbox runtime limit.") from error
            if line is None:
                stderr = process.stderr.read()
                raise RuntimeError(f"Sandbox process exited without a result. {stderr}")
            message = json.loads(line)
            if message.get("type") == "rpc":
                rpc_count += 1
                if rpc_count > MAX_RPC_CALLS:
                    reply = {"ok": False, "error": f"Sandbox is limited to {MAX_RPC_CALLS} MCP queries."}
                elif message.get("tool") != "run_ticket_query":
                    reply = {"ok": False, "error": "Only the read-only run_ticket_query MCP tool is exposed."}
                else:
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            rpc_handler(message["tool"], message.get("arguments") or {}), loop
                        )
                        result = future.result(timeout=max(0.1, deadline - loop.time()))
                        reply = {"ok": True, "result": result}
                    except Exception as error:
                        reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                process.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
                process.stdin.flush()
            elif message.get("type") == "result":
                process.wait(timeout=max(0.1, deadline - loop.time()))
                return message.get("value")
            elif message.get("type") == "error":
                raise RuntimeError(f"Sandbox execution failed: {message.get('error', 'unknown error')}")
            else:
                raise RuntimeError("Sandbox returned an invalid protocol message.")
    except (asyncio.TimeoutError, TimeoutError) as error:
        raise RuntimeError("Sandbox execution exceeded its 30-second time limit and was stopped.") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


async def execute_code(code: str, rpc_handler: Callable[[str, dict[str, Any]], Awaitable[str]]) -> Any:
    """Execute generated JS in a child process and service limited MCP RPCs."""
    try:
        return await asyncio.to_thread(_execute_worker, code, rpc_handler, asyncio.get_running_loop())
    except (concurrent.futures.TimeoutError, TimeoutError) as error:
        raise RuntimeError("Sandbox execution exceeded its 30-second time limit and was stopped.") from error


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker_main()
