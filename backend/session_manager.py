# docker-py container spawning
import asyncio
import socket
import uuid
import os
import docker
from docker.errors import DockerException

AGENT_IMAGE = os.getenv("AGENT_IMAGE", "agent-session:latest")
# Memory limit per container — keeps t2.micro alive with 2-3 sessions
CONTAINER_MEMORY_LIMIT = "256m"
CONTAINER_CPU_QUOTA = 50000   # 50% of one CPU core


def _find_free_port() -> int:
    """Ask the OS for a free port — avoids hardcoding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def get_docker_client() -> docker.DockerClient:
    return docker.from_env()


async def spawn_session_container(session_id: str) -> dict:
    """
    Spawn one agent container per session.
    Returns ports needed for VNC and the agent HTTP server.
    Runs in a thread because docker-py is synchronous.
    """
    def _spawn():
        client = get_docker_client()

        vnc_port = _find_free_port()
        agent_port = _find_free_port()

        container = client.containers.run(
            image=AGENT_IMAGE,
            name=f"agent-session-{session_id}",
            detach=True,
            # Each container gets its own network namespace
            # so DISPLAY conflicts are impossible
            environment={
                "DISPLAY_NUM": "1",
                "WIDTH": "1024",
                "HEIGHT": "768",
                "SESSION_ID": session_id,
                "AGENT_PORT": str(agent_port),
            },
            ports={
                # noVNC web UI
                "6080/tcp": vnc_port,
                # Internal agent HTTP server (receives tasks)
                f"{agent_port}/tcp": agent_port,
            },
            mem_limit=CONTAINER_MEMORY_LIMIT,
            cpu_quota=CONTAINER_CPU_QUOTA,
            # Auto-remove when stopped — no manual cleanup needed
            auto_remove=True,
            # Prevent container from accessing host network directly
            network_mode="bridge",
        )
        return {
            "container_id": container.id,
            "vnc_port": vnc_port,
            "agent_port": agent_port,
        }

    # Run blocking docker call in thread pool
    return await asyncio.get_event_loop().run_in_executor(None, _spawn)


async def stop_session_container(container_id: str):
    """Stop and remove a session container."""
    def _stop():
        try:
            client = get_docker_client()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
        except DockerException:
            pass  # Already stopped or removed

    await asyncio.get_event_loop().run_in_executor(None, _stop)


async def wait_for_agent_ready(agent_port: int, timeout: int = 30) -> bool:
    """
    Poll until the agent HTTP server inside the container is up.
    The container needs a few seconds to start Xvfb + VNC.
    """
    import httpx
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(
                    f"http://localhost:{agent_port}/health",
                    timeout=2.0
                )
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
    return False