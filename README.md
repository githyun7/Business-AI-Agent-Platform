# Business AI Agent Platform

A scalable backend system for managing Claude computer-use agent sessions.
Each session runs in an isolated Docker container with its own virtual
desktop, VNC stream, and agent loop.

## Architecture
User → HTML/JS Frontend
↓ REST + WebSocket
FastAPI Backend (main.py)
↓ docker-py
Agent Containers (one per session)
↓ sampling_loop()
Claude API (computer-use)
↓
PostgreSQL (session + message persistence)

## Stack

- **Backend:** Python, FastAPI, WebSockets, SQLAlchemy, asyncpg
- **Database:** PostgreSQL
- **Agent:** Anthropic Claude computer-use, docker-py
- **Frontend:** HTML, JavaScript, CSS
- **Infrastructure:** Docker, Docker Compose, AWS EC2

## Setup

### Prerequisites
- Docker + Docker Compose
- Anthropic API key

### Run locally

```bash
# 1. Clone
git clone <your-repo-url>
cd <repo>

# 2. Add your API key
cp .env.example .env
# edit .env and add your ANTHROPIC_API_KEY

# 3. Build agent image (once)
docker build -f Dockerfile.agent -t agent-session:latest .

# 4. Start backend + postgres
docker-compose up --build

# 5. Open frontend
open frontend/index.html
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `DATABASE_URL` | PostgreSQL connection string |
| `AGENT_IMAGE` | Docker image name for agent containers |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/sessions` | Create new agent session |
| GET | `/sessions` | List all sessions |
| GET | `/sessions/{id}` | Get session status |
| POST | `/sessions/{id}/message` | Send task to agent |
| DELETE | `/sessions/{id}` | Stop session |
| WS | `/ws/{id}` | Real-time event stream |

## Concurrent Sessions

Each session spawns an isolated Docker container with:
- Its own Xvfb virtual display
- Its own noVNC stream on a dynamic port
- 256MB memory limit for t2.micro compatibility
- Auto-cleanup after 30 minutes of inactivity
