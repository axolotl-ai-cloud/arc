#!/usr/bin/env node

/**
 * arc — Agent Remote Control CLI
 *
 * Commands:
 *   arc setup              Interactive configuration wizard
 *   arc connect            Start a remote control session
 *   arc install-skill      Install /remote-control skill for your framework
 *   arc status             Show current configuration
 */

import { loadConfig, saveConfig, configExists, getConfigPath } from "./config.js";
import { connect } from "./connect.js";
import { runSetup } from "./setup.js";
import { installSkill, detectFramework } from "./skill-installer.js";

const [command, ...args] = process.argv.slice(2);

function printUsage(): void {
  console.log(`
  arc — Agent Remote Control

  Usage:
    arc setup [options]    Configure relay URL, token, and framework
    arc config [options]   Get or set individual config values
    arc sessions                    List your sessions (shows agent/viewer status)
    arc sessions --clear            Close all your sessions
    arc sessions --clear --inactive Close only sessions with no active agent
    arc install-skill      Install /remote-control skill for your framework
    arc update             Update ARC and reinstall skills
    arc status             Show current configuration
    arc help               Show this help message

  Setup options:
    --hermes                   Configure for Hermes Agent framework
    --hosted                   Use hosted relay at arc.axolotl.ai (default)
    --self-hosted              Use your own relay server
    --advanced                 Show advanced options (custom viewer URL, etc.)

  Config options:
    --viewer <url>             Set viewer base URL (or "default" to reset)
    --relay <url>              Set relay WebSocket URL
    --token <token>            Set agent token
    --framework <fw>           Set framework (hermes|deepagent|openclaw|generic)

  Environment variables:
    ARC_RELAY_URL              Relay WebSocket URL
    ARC_AGENT_TOKEN            Agent authentication token
    ARC_FRAMEWORK              Agent framework
    ARC_HOSTED=true            Use hosted relay (arc.axolotl.ai)
    ARC_VIEWER_BASE=<url>      Override viewer URL

  Quick start:
    curl -fsSL https://raw.githubusercontent.com/axolotl-ai-cloud/arc/refs/heads/main/install.sh | sh
    arc setup --hermes
    # Then start Hermes and type /remote-control
`);
}

function parseArgs(args: string[]): Record<string, string | boolean> {
  const parsed: Record<string, string | boolean> = {};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = args[i + 1];
      if (next && !next.startsWith("--")) {
        parsed[key] = next;
        i++;
      } else {
        parsed[key] = true;
      }
    }
  }
  return parsed;
}

async function main(): Promise<void> {
  switch (command) {
    case "setup":
    case "init": {
      const opts = parseArgs(args);

      // Detect framework shorthand flags
      let framework: string | undefined;
      if (opts["hermes"]) framework = "hermes";
      else if (opts["deepagent"]) framework = "deepagent";
      else if (opts["openclaw"]) framework = "openclaw";

      await runSetup({
        framework: framework as any,
        hosted: opts["hosted"] === true ? true : undefined,
        selfHosted: opts["self-hosted"] === true,
        advanced: opts["advanced"] === true,
      });
      break;
    }

    case "connect":
    case "start": {
      if (!configExists() && !process.env.ARC_AGENT_TOKEN) {
        console.error("Not configured. Run `arc setup` first or set ARC_AGENT_TOKEN.");
        process.exit(1);
      }

      const opts = parseArgs(args);
      const result = await connect({
        relayUrl: opts["relay-url"] as string | undefined,
        agentToken: opts["token"] as string | undefined,
        framework: opts["framework"] as any,
        sessionId: opts["session-id"] as string | undefined,
        agentName: opts["name"] as string | undefined,
        hermesApiUrl: opts["hermes-url"] as string | undefined,
        quiet: opts["quiet"] === true,
        json: opts["json"] === true,
        onDisconnect: () => {
          console.log("\nDisconnected from relay.");
          process.exit(0);
        },
      });

      // Keep process alive
      process.on("SIGINT", () => {
        console.log("\nDisconnecting...");
        result.disconnect();
        process.exit(0);
      });

      process.on("SIGTERM", () => {
        result.disconnect();
        process.exit(0);
      });

      break;
    }

    case "install-skill": {
      const config = loadConfig();
      const framework = (args[0] as any) || config.framework || detectFramework();
      const result = installSkill(framework);

      if (result.installed) {
        console.log(`✓ Installed /remote-control skill at ${result.path}`);
      } else {
        console.log(`⚠ ${result.message} (${result.path})`);
      }
      break;
    }

    case "update":
    case "upgrade": {
      const { runUpdate } = await import("./update.js");
      await runUpdate();
      break;
    }

    case "config": {
      const opts = parseArgs(args);

      // arc config --viewer <url>   set viewerBase (or clear with "default")
      // arc config --relay <url>    set relayUrl
      // arc config --token <token>  set agentToken
      // arc config --framework <fw> set framework
      // arc config (no args)        print current config

      const updates: Record<string, string | undefined> = {};
      let didUpdate = false;

      if (opts["viewer"] !== undefined) {
        const v = opts["viewer"] as string;
        updates["viewerBase"] = (v === "default" || v === "") ? undefined : v;
        didUpdate = true;
      }
      if (opts["relay"] !== undefined) {
        updates["relayUrl"] = opts["relay"] as string;
        didUpdate = true;
      }
      if (opts["token"] !== undefined) {
        updates["agentToken"] = opts["token"] as string;
        didUpdate = true;
      }
      if (opts["framework"] !== undefined) {
        updates["framework"] = opts["framework"] as string;
        didUpdate = true;
      }

      if (didUpdate) {
        saveConfig(updates as any);
        console.log(`✓ Config updated (${getConfigPath()})`);
      }

      // Always print current config after update (or with no args)
      const config = loadConfig();
      const viewerDesc = config.viewerBase
        ? config.viewerBase
        : `${config.relayUrl.replace("wss://", "https://").replace("ws://", "http://").replace("/ws", "")}/viewer`;
      console.log(`
  Configuration (${getConfigPath()}):
    Relay URL:  ${config.relayUrl}
    Token:      ${config.agentToken ? config.agentToken.slice(0, 12) + "..." : "(not set)"}
    Framework:  ${config.framework}
    Viewer:     ${viewerDesc}
`);
      break;
    }

    case "status": {
      if (!configExists()) {
        console.log("Not configured. Run `arc setup` to get started.");
        break;
      }

      const config = loadConfig();
      const viewerDesc = config.viewerBase
        ? config.viewerBase
        : `${config.relayUrl.replace("wss://", "https://").replace("ws://", "http://").replace("/ws", "")}/viewer`;
      console.log(`
  Configuration (${getConfigPath()}):
    Relay URL:  ${config.relayUrl}
    Token:      ${config.agentToken ? config.agentToken.slice(0, 12) + "..." : "(not set)"}
    Framework:  ${config.framework}
    Viewer:     ${viewerDesc}
`);
      break;
    }

    case "sessions": {
      const config = loadConfig();
      if (!config.agentToken) {
        console.error("No agent token configured. Run `arc setup` first.");
        process.exit(1);
      }

      const opts = parseArgs(args);
      const relayHttpUrl = config.relayUrl
        .replace("wss://", "https://")
        .replace("ws://", "http://")
        .replace("/ws", "");

      // List sessions
      const res = await fetch(`${relayHttpUrl}/sessions`, {
        headers: { Authorization: `Bearer ${config.agentToken}` },
      });
      if (!res.ok) {
        console.error(`Failed to list sessions: HTTP ${res.status}`);
        process.exit(1);
      }
      const sessions = await res.json() as Array<{
        sessionId: string;
        agentName?: string;
        lastActivity?: number;
        agentConnected?: boolean;
        viewerCount?: number;
      }>;

      if (sessions.length === 0) {
        console.log("No active sessions.");
        break;
      }

      console.log(`\n  Sessions (${sessions.length}):\n`);
      for (const s of sessions) {
        const age = s.lastActivity ? Math.round((Date.now() / 1000 - s.lastActivity) / 60) + "m ago" : "";
        const agentStatus = s.agentConnected === false ? "disconnected" : "connected";
        const viewers = s.viewerCount ? `${s.viewerCount} viewer(s)` : "no viewers";
        console.log(`    ${s.sessionId}  ${s.agentName || ""}  [agent: ${agentStatus}] [${viewers}]  ${age}`);
      }
      console.log("");

      const disconnected = sessions.filter(s => s.agentConnected === false);

      if (opts["clear"]) {
        const toClear = opts["inactive"] ? disconnected : sessions;
        if (toClear.length === 0) {
          console.log("  No sessions to close.");
        } else {
          console.log(`  Closing ${toClear.length} session(s)...`);
          for (const s of toClear) {
            const del = await fetch(`${relayHttpUrl}/sessions/${s.sessionId}`, {
              method: "DELETE",
              headers: { Authorization: `Bearer ${config.agentToken}` },
            });
            if (del.ok) {
              console.log(`  ✓ Closed ${s.sessionId}`);
            } else {
              console.log(`  ✗ Failed to close ${s.sessionId}: HTTP ${del.status}`);
            }
          }
        }
        console.log("");
      } else {
        const hints = ["Run `arc sessions --clear` to close all sessions."];
        if (disconnected.length > 0)
          hints.push("Or `arc sessions --clear --inactive` to close only the " + disconnected.length + " disconnected one(s).");
        console.log(`  ${hints.join("\n  ")}\n`);
      }
      break;
    }

    case "help":
    case "--help":
    case "-h":
    case undefined: {
      printUsage();
      break;
    }

    default: {
      console.error(`Unknown command: ${command}`);
      printUsage();
      process.exit(1);
    }
  }
}

main().catch((err) => {
  console.error("Error:", err.message);
  process.exit(1);
});
