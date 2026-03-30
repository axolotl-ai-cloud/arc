/**
 * Connect command — starts a remote control session.
 *
 * This is the core runtime used by both the CLI and framework skills.
 * It connects to the relay, prints the session secret, and keeps
 * the connection alive until the agent disconnects.
 *
 * For framework-specific adapters (hermes, deepagent, openclaw), it
 * starts the full adapter that bridges events between the agent's API
 * and the relay. For generic, it just registers a session.
 */

import { RemoteControlClient } from "@axolotlai/arc-protocol/client";
import type { RemoteControlClientOptions, } from "@axolotlai/arc-protocol/client";
import type { SessionInfo, RemoteCommand } from "@axolotlai/arc-protocol";
import { HermesRemoteControl } from "@axolotlai/arc-adapter-hermes";
import { writeFileSync, mkdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { join } from "node:path";
import { loadConfig, getConfigDir } from "./config.js";
import type { ArcConfig } from "./config.js";

export interface ConnectOptions {
  /** Override relay URL from config */
  relayUrl?: string;
  /** Override agent token from config */
  agentToken?: string;
  /** Override framework from config */
  framework?: ArcConfig["framework"];
  /** Session ID (auto-generated if not set) */
  sessionId?: string;
  /** Human-readable agent name */
  agentName?: string;
  /** Hermes API URL (default: http://localhost:3000) */
  hermesApiUrl?: string;
  /** Callback when a command is received from a viewer */
  onCommand?: (command: RemoteCommand) => void | Promise<void>;
  /** Callback when disconnected */
  onDisconnect?: () => void;
  /** If true, suppress console output */
  quiet?: boolean;
  /** If true, output session info as JSON (machine-readable) */
  json?: boolean;
}

export interface ConnectResult {
  client: RemoteControlClient;
  sessionId: string;
  sessionSecret: string;
  viewerUrl: string;
  disconnect: () => void;
}

/**
 * Connect to the relay server and register a new session.
 *
 * For hermes: starts the HermesRemoteControl adapter that bridges
 * Hermes SSE events to the relay and relay commands back to Hermes.
 *
 * Returns the session secret and a viewer URL that can be shared.
 * The connection stays alive until disconnect() is called.
 */
export async function connect(options: ConnectOptions = {}): Promise<ConnectResult> {
  const config = loadConfig();

  const relayUrl = options.relayUrl || config.relayUrl;
  const agentToken = options.agentToken || config.agentToken;
  const framework = options.framework || config.framework;

  if (!agentToken) {
    throw new Error(
      "Agent token not configured. Run `arc setup` or set ARC_AGENT_TOKEN env var.",
    );
  }

  let client: RemoteControlClient;
  let disconnect: () => void;

  if (framework === "hermes") {
    // Try the full Hermes adapter (SSE bridge) if Hermes is running as a server.
    // Fall back to a bare session if Hermes is in terminal/CLI mode.
    const hermesApiUrl = options.hermesApiUrl
      || process.env.HERMES_API_URL
      || "http://localhost:3000";

    let useAdapter = false;
    try {
      const healthCheck = await fetch(`${hermesApiUrl}/api/stream`, {
        method: "HEAD",
        signal: AbortSignal.timeout(2000),
      }).catch(() => null);
      useAdapter = healthCheck !== null && healthCheck.status !== 404;
    } catch {
      // Hermes HTTP API not available
    }

    if (useAdapter) {
      const adapter = new HermesRemoteControl({
        relayUrl,
        hermesApiUrl,
        agentToken,
        agentName: options.agentName,
        sessionId: options.sessionId,
      });

      if (options.onCommand) {
        adapter.client.onCommand(options.onCommand);
      }
      if (options.onDisconnect) {
        adapter.client.onDisconnect(options.onDisconnect);
      }

      await adapter.start();
      client = adapter.client;
      disconnect = () => adapter.stop();
    } else {
      // Hermes in CLI/terminal mode — just keep a session alive
      const clientOpts: RemoteControlClientOptions = {
        agentToken,
        agentName: options.agentName || "hermes",
        sessionId: options.sessionId,
        autoReconnect: true,
        maxReconnectAttempts: 10,
      };

      client = new RemoteControlClient(relayUrl, "hermes", clientOpts);

      if (options.onCommand) {
        client.onCommand(options.onCommand);
      }
      if (options.onDisconnect) {
        client.onDisconnect(options.onDisconnect);
      }

      await client.connect();
      disconnect = () => client.disconnect();
    }
  } else {
    // Generic: just register a session (no event bridging)
    const frameworkMap: Record<string, SessionInfo["agentFramework"]> = {
      deepagent: "deepagent",
      openclaw: "openclaw",
      generic: "hermes",
    };

    const clientOpts: RemoteControlClientOptions = {
      agentToken,
      agentName: options.agentName,
      sessionId: options.sessionId,
      autoReconnect: true,
      maxReconnectAttempts: 10,
    };

    client = new RemoteControlClient(
      relayUrl,
      frameworkMap[framework] ?? "hermes",
      clientOpts,
    );

    if (options.onCommand) {
      client.onCommand(options.onCommand);
    }
    if (options.onDisconnect) {
      client.onDisconnect(options.onDisconnect);
    }

    await client.connect();
    disconnect = () => client.disconnect();
  }

  const sessionId = client.session.sessionId;
  const sessionSecret = client.sessionSecret!;

  // Build viewer URL
  const relayHttpUrl = relayUrl
    .replace("ws://", "http://")
    .replace("wss://", "https://")
    .replace("/ws", "");

  let viewerUrl: string;
  if (config.viewerBase) {
    // Power-user override: external viewer (e.g. OSS GitHub Pages UI) receives
    // relay URL + session credentials as query params.
    const base = config.viewerBase.replace(/\/+$/, "");
    viewerUrl = `${base}?relay=${encodeURIComponent(relayHttpUrl)}&session=${sessionId}&s=${sessionSecret}`;
  } else {
    // Default: use the relay's built-in viewer (works for beta and self-hosted)
    viewerUrl = `${relayHttpUrl}/viewer?session=${sessionId}&s=${sessionSecret}`;
  }

  // Write session info for other tools to read
  const sessionInfo = { sessionId, sessionSecret, viewerUrl, relayUrl, framework };
  try {
    mkdirSync(getConfigDir(), { recursive: true });
    writeFileSync(
      join(getConfigDir(), "session.json"),
      JSON.stringify(sessionInfo, null, 2) + "\n",
      { mode: 0o600 },
    );
    // Write session info as simple key:value pairs — easy for agents to parse
    // and reconstruct the URL from parts if terminal truncates it
    writeFileSync(
      join(getConfigDir(), "viewer-url"),
      `session_id:\n${sessionId}\nrelay_credential:\n${sessionSecret}\nviewer_url:\n${viewerUrl}\n`,
      { mode: 0o600 },
    );
  } catch {
    // non-fatal
  }

  // Try to copy viewer URL to clipboard
  let copiedToClipboard = false;
  try {
    const platform = process.platform;
    if (platform === "darwin") {
      execSync(`printf '%s' ${JSON.stringify(viewerUrl)} | pbcopy`, { stdio: "pipe" });
      copiedToClipboard = true;
    } else if (platform === "linux") {
      execSync(`printf '%s' ${JSON.stringify(viewerUrl)} | xclip -selection clipboard 2>/dev/null || printf '%s' ${JSON.stringify(viewerUrl)} | xsel --clipboard 2>/dev/null`, { stdio: "pipe", shell: "/bin/sh" });
      copiedToClipboard = true;
    }
  } catch {
    // clipboard not available
  }

  if (options.json) {
    console.log(JSON.stringify(sessionInfo));
  } else if (!options.quiet) {
    console.log("");
    console.log("ARC — Remote control active");
    console.log("");
    console.log(`  Session:  ${sessionId}`);
    console.log(`  Viewer:   ${viewerUrl}`);
    if (copiedToClipboard) {
      console.log("  (Copied to clipboard)");
    }
    if (framework === "hermes") {
      console.log(`  Hermes:   bridging events from ${options.hermesApiUrl || process.env.HERMES_API_URL || "http://localhost:3000"}`);
    }
    console.log("");
  }

  // Keep the WebSocket alive with periodic pings
  const keepalive = setInterval(() => {
    if (client.connected) {
      client.send({ kind: "ping" });
    }
  }, 30_000);

  const originalDisconnect = disconnect;
  disconnect = () => {
    clearInterval(keepalive);
    originalDisconnect();
  };

  return {
    client,
    sessionId,
    sessionSecret,
    viewerUrl,
    disconnect,
  };
}
