# ARC — Agent Remote Control

Universal remote control for AI agent frameworks. Connect any agent to a browser-based interface for real-time monitoring and remote interaction.

## Agent Support

| Agent | Status | Viewer → Agent | Agent → Viewer | How |
|-------|--------|----------------|----------------|-----|
| **Hermes Agent** | ✅ Supported | ✅ Messages | ✅ Tool calls, responses, status | Native plugin + `/remote-control` |
| DeepAgent | 🔜 Planned | — | — | `@axolotlai/arc-adapter-deepagent` |
| OpenClaw | 🔜 Planned | — | — | `@axolotlai/arc-adapter-openclaw` |
| Claude Code | 🔜 Planned | — | — | Skill file |

## Quick Start

```bash
# Install and configure
curl -fsSL https://raw.githubusercontent.com/axolotl-ai-cloud/arc/refs/heads/main/install.sh | sh
arc setup --hermes  # uses hosted beta relay — no server needed

# Start Hermes, then type /remote-control
hermes
```

The `/remote-control` skill starts a session, opens the viewer in your browser, and streams all tool calls and responses to the viewer in real-time.

> **Note:** `arc setup --hermes` patches the `hermes` entrypoint to enable
> bidirectional viewer ↔ agent messaging. If `hermes update` overwrites the
> patch, run `arc update` to re-apply it. Once
> [NousResearch/hermes-agent#3778](https://github.com/NousResearch/hermes-agent/pull/3778)
> is merged, the patch is unnecessary.

## How It Works

```
Agent (Hermes, DeepAgent, OpenClaw, any)
  │
  │  WebSocket
  ▼
Relay Server (arc-beta.axolotl.ai or self-hosted)
  │
  │  WebSocket
  ▼
Web Viewer (browser)
  • Live trace view (tool calls, messages, status)
  • Send messages to the agent
  • Approve/deny tool calls
```

Traces flow agent → relay → viewer. Commands flow viewer → relay → agent.

## Self-Hosted Relay

For users who want to run their own relay server:

```bash
# Option 1: Docker
docker compose up relay

# Option 2: Python directly
pip install -r relay/requirements.txt
python -m relay

# Option 3: Use the CLI (handles everything)
arc setup --self-hosted
```

### Environment Variables

```bash
# Required — set a fixed token, or one is auto-generated at startup
export AGENT_TOKEN="your-strong-random-token"

# Optional
export PORT=8600                        # Listen port (default: 8600)
export MAX_SESSIONS=100                 # Max concurrent sessions
export MAX_TRACE_LOG=2000               # Max trace events per session
export SESSION_TTL_HOURS=24             # Idle session cleanup
export ALLOWED_ORIGINS="*"              # CORS origins (comma-separated)
export REQUIRE_TLS=false                # Reject non-TLS connections
export AGENT_TOKEN_PREFIX=""            # Accept prefix-based tokens (beta mode)
```

### Running with `ARC_ENV=dev`

For local development, use `ARC_ENV=dev` to default to self-hosted mode:

```bash
# arc setup will default to localhost relay, auto-start it
ARC_ENV=dev arc setup --hermes

# Or start the relay manually
AGENT_TOKEN=dev-token python -m relay

# Then configure the CLI to use it
ARC_ENV=dev arc setup --self-hosted
```

### Verify the relay is running

```bash
curl http://localhost:8600/health
# {"status": "ok", "sessions": 0}
```

Open `http://localhost:8600/viewer` to access the web viewer (if web-client is built).

## Supported Frameworks

| Framework | Integration | How |
|-----------|-------------|-----|
| **Hermes Agent** | Native Python plugin | `/remote-control` or `arc_start_session` tool |
| **DeepAgent** (LangChain) | TypeScript middleware | `@axolotlai/arc-adapter-deepagent` |
| **OpenClaw** | TypeScript plugin | `@axolotlai/arc-adapter-openclaw` |
| **Claude Code** | Skill file | `/remote-control` slash command |
## CLI Commands

```bash
arc setup [--hermes|--self-hosted]  # Configure relay URL, token, framework
arc update                         # Pull, rebuild, reinstall skills
arc install-skill                  # Install /remote-control skill
arc status                         # Show current configuration
```

## Security Model

- **Agent token** — required to register sessions. Prevents unauthorized agents.
- **Session secret** — generated per-session, required for viewer subscription. Only share with trusted parties.
- **Role enforcement** — viewers can't send traces, agents can't send commands.
- **Rate limiting** — per-IP limits on HTTP (60/min) and WebSocket (120 msg/min).
- **TLS** — set `REQUIRE_TLS=true` in production.
- **Prefix tokens** — beta mode (`AGENT_TOKEN_PREFIX`) gives each agent its own session namespace.

## Extensibility

The relay is extensible via protocol interfaces:

```python
from relay import create_app, RelayConfig

app = create_app(RelayConfig(
    auth=MyAuthProvider(...),      # Authenticate agents, viewers, admins
    store=MySessionStore(...),     # Persist session state
    policy=MySessionPolicy(...),   # Enforce session limits
    hooks=MyLifecycleHooks(...),   # React to session events
))
```

## Project Structure

```
├── relay/                     # OSS relay server (Python/FastAPI)
├── packages/
│   ├── protocol/              # Wire types, client, crypto
│   ├── cli/                   # `arc` CLI (setup, connect, update)
│   ├── web-client/            # React SPA viewer
│   ├── adapter-hermes/        # Hermes Agent TypeScript adapter
│   ├── adapter-deepagent/     # DeepAgent adapter
│   └── adapter-openclaw/      # OpenClaw adapter
├── hermes-plugin/             # Native Hermes Python plugin
├── tests/                     # Integration tests
├── install.sh                 # curl | sh installer
└── docker-compose.yml         # Local dev
```

## Testing

```bash
npm run test:ts                # TypeScript tests
npm run test:py                # Python tests (needs pytest, pytest-asyncio, httpx)
npm test                       # All tests
```

## Roadmap

- [x] Per-session authentication
- [x] CLI with `arc setup` / `/remote-control`
- [x] Tool approval/deny from viewer
- [x] Native Hermes Agent plugin with lifecycle hooks
- [x] Beta relay with prefix-based tokens
- [x] Web viewer auto-connect from URL params
- [ ] Viewer → agent message injection (pending Hermes PR)
- [ ] Mobile app (iOS/iPadOS)
- [ ] Multi-agent dashboard view
- [ ] Persistent trace storage

## License

MIT. See [LICENSE](LICENSE).
