# calls sampling_loop, streams to WS
"""
Runs inside the agent container.
Receives tasks from the FastAPI backend and executes them
using the existing sampling_loop from loop.py.
"""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import sys

sys.path.insert(0, "/home/computeruse")

from computer_use_demo.loop import sampling_loop, APIProvider

PORT = int(os.getenv("AGENT_PORT", "9000"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SESSION_ID = os.getenv("SESSION_ID", "unknown")

# Shared queue: backend pushes tasks, agent loop consumes them
task_queue: asyncio.Queue = None
result_callbacks: dict = {}


class AgentHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/task":
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            # Put task into the async queue from the sync HTTP handler
            asyncio.run_coroutine_threadsafe(
                task_queue.put(body), loop
            )
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"queued")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # Silence request logs


async def agent_loop():
    """Consume tasks from queue and run the sampling loop."""
    global task_queue
    task_queue = asyncio.Queue()

    while True:
        task = await task_queue.get()
        messages = task.get("messages", [])
        api_key = task.get("api_key", ANTHROPIC_API_KEY)
        callback_url = task.get("callback_url", "")

        async def output_cb(block):
            # Stream back to backend via HTTP POST
            if callback_url:
                try:
                    import httpx
                    async with httpx.AsyncClient() as c:
                        await c.post(callback_url, json={
                            "type": "output",
                            "block": block,
                            "session_id": SESSION_ID,
                        }, timeout=5)
                except Exception:
                    pass

        async def tool_cb(result, tool_id):
            if callback_url:
                try:
                    import httpx
                    async with httpx.AsyncClient() as c:
                        await c.post(callback_url, json={
                            "type": "tool_result",
                            "tool_id": tool_id,
                            "output": result.output,
                            "error": result.error,
                            "session_id": SESSION_ID,
                        }, timeout=5)
                except Exception:
                    pass

        async def api_cb(request, response, error):
            pass  # Could log to backend if needed

        try:
            await sampling_loop(
                model="claude-sonnet-4-20250514",
                provider=APIProvider.ANTHROPIC,
                system_prompt_suffix="",
                messages=messages,
                output_callback=output_cb,
                tool_output_callback=tool_cb,
                api_response_callback=api_cb,
                api_key=api_key,
                only_n_most_recent_images=3,
                tool_version="computer_use_20250429",
                max_tokens=4096,
            )
        except Exception as e:
            if callback_url:
                import httpx
                async with httpx.AsyncClient() as c:
                    await c.post(callback_url, json={
                        "type": "error",
                        "error": str(e),
                        "session_id": SESSION_ID,
                    }, timeout=5)


loop = asyncio.new_event_loop()


def start_http_server():
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    server.serve_forever()


if __name__ == "__main__":
    # HTTP server in a thread, async agent loop in main thread
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()
    print(f"Agent server running on port {PORT}")
    loop.run_until_complete(agent_loop())