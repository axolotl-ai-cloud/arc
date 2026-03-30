/**
 * Skill installer — installs /remote-control for each framework.
 *
 * Framework-specific installation:
 *   - Claude Code / Agent SDK: .claude/skills/remote-control/SKILL.md
 *   - Hermes Agent: .hermes/skills/remote-control/SKILL.md
 *   - OpenClaw: .openclaw/plugins/arc/plugin.ts
 *   - DeepAgent: .deepagent/middleware/remote-control.json
 *   - Generic: .claude/skills/remote-control/SKILL.md (Claude Code compatible)
 */

import { existsSync, lstatSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, cpSync, readdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ArcConfig } from "./config.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Marker comment to detect if hermes entrypoint is already patched
const ARC_PATCH_MARKER = "# ARC_PATCHED";

/**
 * Patch the hermes CLI entrypoint to add inject_message monkeypatch.
 * Reads the existing script, injects the patch before main(), writes back.
 * Idempotent — skips if already patched.
 */
export function patchHermesEntrypoint(homeDir: string, _hermesPython: string): void {
  // Find the hermes script
  let hermesScript: string | null = null;
  for (const candidate of [
    join(homeDir, ".local", "bin", "hermes"),
    "/usr/local/bin/hermes",
  ]) {
    if (existsSync(candidate)) {
      hermesScript = candidate;
      break;
    }
  }
  if (!hermesScript) {
    try {
      hermesScript = execSync("which hermes", { encoding: "utf-8" }).trim();
    } catch {
      return; // hermes not found
    }
  }
  if (!hermesScript || !existsSync(hermesScript)) return;

  try {
    const content = readFileSync(hermesScript, "utf-8");

    // Already patched?
    if (content.includes(ARC_PATCH_MARKER)) return;

    // Must be a Python script with hermes_cli import
    if (!content.includes("hermes_cli")) return;

    // Backup the original
    writeFileSync(hermesScript + ".bak", content, { mode: 0o755 });

    // Inject the monkeypatch before the main() import/call
    // The patch goes right after the shebang and imports
    const lines = content.split("\n");
    const patchCode = `
${ARC_PATCH_MARKER}
def _arc_patch():
    try:
        from hermes_cli.plugins import PluginContext, PluginManager
        if hasattr(PluginContext, "inject_message"):
            return
        if not hasattr(PluginManager, "_cli_ref"):
            PluginManager._cli_ref = None
        def inject_message(self, content, role="user"):
            cli = self._manager._cli_ref
            if cli is None:
                return False
            msg = content if role == "user" else f"[{role}] {content}"
            if getattr(cli, "_agent_running", False):
                cli._interrupt_queue.put(msg)
            else:
                cli._pending_input.put(msg)
            return True
        PluginContext.inject_message = inject_message
    except Exception:
        pass
    try:
        import cli as _hcli
        _orig = _hcli.HermesCLI.run
        def _run(self, *a, **kw):
            try:
                from hermes_cli.plugins import get_plugin_manager
                get_plugin_manager()._cli_ref = self
            except Exception:
                pass
            return _orig(self, *a, **kw)
        _hcli.HermesCLI.run = _run
    except Exception:
        pass
_arc_patch()
`;

    // Insert before the "from hermes_cli" import line
    const importIdx = lines.findIndex(l => l.includes("from hermes_cli"));
    if (importIdx === -1) return; // Can't find insertion point

    lines.splice(importIdx, 0, patchCode);
    writeFileSync(hermesScript, lines.join("\n"), { mode: 0o755 });
  } catch {
    // Non-fatal — hermes will work without inject_message
  }
}

/** Find the Python that Hermes uses by reading the shebang from the hermes CLI. */
function findHermesPython(homeDir: string): string {
  // 1. Read shebang from hermes entrypoint
  for (const hermesPath of [
    join(homeDir, ".local", "bin", "hermes"),
    "/usr/local/bin/hermes",
  ]) {
    if (existsSync(hermesPath)) {
      try {
        const content = readFileSync(hermesPath, "utf-8");
        const shebang = content.split("\n")[0];
        if (shebang.startsWith("#!") && shebang.includes("python")) {
          const pythonPath = shebang.slice(2).trim();
          if (existsSync(pythonPath)) return pythonPath;
        }
      } catch { /* not readable */ }
    }
  }

  // 2. Try which hermes
  try {
    const hermesPath = execSync("which hermes", { encoding: "utf-8" }).trim();
    if (hermesPath && existsSync(hermesPath)) {
      const content = readFileSync(hermesPath, "utf-8");
      const shebang = content.split("\n")[0];
      if (shebang.startsWith("#!") && shebang.includes("python")) {
        const pythonPath = shebang.slice(2).trim();
        if (existsSync(pythonPath)) return pythonPath;
      }
    }
  } catch { /* hermes not found */ }

  // 3. Known venv locations
  const venv = join(homeDir, ".hermes", "hermes-agent", "venv", "bin", "python3");
  if (existsSync(venv)) return venv;

  return "python3";
}

export interface InstallResult {
  installed: boolean;
  path: string;
  message: string;
}

/**
 * Detect the primary agent framework from the current project.
 * Returns the first detected framework, or "generic" if none found.
 */
export function detectFramework(): ArcConfig["framework"] {
  const all = detectAllFrameworks();
  return all[0] ?? "generic";
}

/**
 * Detect ALL agent frameworks present in the current project.
 * A project may use multiple frameworks simultaneously.
 */
export function detectAllFrameworks(projectDir?: string): ArcConfig["framework"][] {
  const cwd = projectDir || process.cwd();
  const detected: ArcConfig["framework"][] = [];

  // Check for Hermes config (project-level or global ~/.hermes)
  const homeDir = process.env.HOME || process.env.USERPROFILE || "";
  if (
    existsSync(join(cwd, ".hermes")) ||
    existsSync(join(cwd, "hermes.config.json")) ||
    existsSync(join(cwd, "hermes.config.ts")) ||
    existsSync(join(homeDir, ".hermes"))
  ) {
    detected.push("hermes");
  }

  // Check for DeepAgent config
  if (
    existsSync(join(cwd, ".deepagent")) ||
    existsSync(join(cwd, "deepagent.config.json")) ||
    existsSync(join(cwd, "deepagent.config.ts"))
  ) {
    detected.push("deepagent");
  }

  // Check for OpenClaw config
  if (
    existsSync(join(cwd, ".openclaw")) ||
    existsSync(join(cwd, "openclaw.config.json")) ||
    existsSync(join(cwd, "openclaw.config.ts"))
  ) {
    detected.push("openclaw");
  }

  // Check for Claude Code
  if (existsSync(join(cwd, ".claude")) || existsSync(join(cwd, "CLAUDE.md"))) {
    detected.push("generic");
  }

  return detected.length > 0 ? detected : ["generic"];
}

/**
 * Install skills for ALL detected frameworks.
 */
export function installAllSkills(projectDir?: string): InstallResult[] {
  const frameworks = detectAllFrameworks(projectDir);
  return frameworks.map((fw) => installSkill(fw, projectDir));
}

/**
 * Install the /remote-control skill for the given framework.
 */
export function installSkill(
  framework: ArcConfig["framework"],
  projectDir?: string,
): InstallResult {
  const cwd = projectDir || process.cwd();

  switch (framework) {
    case "hermes":
      return installHermesSkill(cwd);
    case "deepagent":
      return installDeepAgentSkill(cwd);
    case "openclaw":
      return installOpenClawPlugin(cwd);
    case "generic":
    default:
      return installClaudeCodeSkill(cwd);
  }
}

// ── Claude Code / Agent SDK / Generic ───────────────────────────

function installClaudeCodeSkill(cwd: string): InstallResult {
  const skillDir = join(cwd, ".claude", "skills", "remote-control");
  const skillFile = join(skillDir, "SKILL.md");

  if (existsSync(skillFile)) {
    return { installed: false, path: skillFile, message: "Skill already installed" };
  }

  mkdirSync(skillDir, { recursive: true });
  writeFileSync(skillFile, CLAUDE_CODE_SKILL);

  return { installed: true, path: skillFile, message: "Installed Claude Code skill" };
}

// ── Hermes Agent ────────────────────────────────────────────────

function installHermesSkill(_cwd: string): InstallResult {
  const homeDir = process.env.HOME || process.env.USERPROFILE || "";

  // 1. Install the native plugin to ~/.hermes/plugins/arc-remote-control/
  //    Repo checkout: symlink for live updates
  //    npm install: copy bundled plugin files
  const pluginTarget = join(homeDir, ".hermes", "plugins", "arc-remote-control");
  const repoRoot = resolve(__dirname, "..", "..", "..");
  const repoPluginSource = join(repoRoot, "hermes-plugin", "arc-remote-control");
  const npmPluginSource = join(__dirname, "..", "hermes-plugin"); // bundled in npm package

  const pluginSource = existsSync(join(repoPluginSource, "__init__.py"))
    ? repoPluginSource
    : existsSync(join(npmPluginSource, "__init__.py"))
      ? npmPluginSource
      : null;

  if (pluginSource) {
    const isRepo = pluginSource === repoPluginSource;

    // Repo checkout: create a symlink for live updates (first install only)
    if (isRepo && !existsSync(pluginTarget)) {
      try {
        symlinkSync(pluginSource, pluginTarget);
      } catch {
        // Fall through to copy
      }
    }

    // If target is a symlink (live repo checkout), skip copy — changes are live
    const isSymlink = existsSync(pluginTarget) &&
      (() => { try { return lstatSync(pluginTarget).isSymbolicLink(); } catch { return false; } })();

    if (!isSymlink) {
      // Always copy/overwrite plugin files so reinstalls pick up latest code
      mkdirSync(pluginTarget, { recursive: true });
      try {
        cpSync(pluginSource, pluginTarget, { recursive: true });
      } catch {
        // Fallback for older Node: copy individual files
        for (const file of readdirSync(pluginSource)) {
          const src = join(pluginSource, file);
          writeFileSync(join(pluginTarget, file), readFileSync(src));
        }
      }
    }
  }

  // 2. Install websocket-client into Hermes's Python
  const hermesPython = findHermesPython(homeDir);
  try {
    execSync(`${hermesPython} -m pip install -q websocket-client`, {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch {
    // Non-fatal — the plugin auto-installs at load time too
  }

  // 3. Patch the hermes entrypoint to add inject_message support
  patchHermesEntrypoint(homeDir, hermesPython);

  // 4. Install the skill (SKILL.md for /remote-control slash command)
  const skillDir = join(homeDir, ".hermes", "skills", "devops", "remote-control");
  const skillFile = join(skillDir, "SKILL.md");

  if (existsSync(skillFile)) {
    return { installed: false, path: skillFile, message: "Skill already installed" };
  }

  mkdirSync(skillDir, { recursive: true });
  writeFileSync(skillFile, HERMES_SKILL);

  return { installed: true, path: skillFile, message: "Installed Hermes Agent skill" };
}

// ── DeepAgent ───────────────────────────────────────────────────

function installDeepAgentSkill(cwd: string): InstallResult {
  const middlewareDir = join(cwd, ".deepagent", "middleware");
  const configFile = join(middlewareDir, "remote-control.json");

  if (existsSync(configFile)) {
    return { installed: false, path: configFile, message: "Middleware already configured" };
  }

  mkdirSync(middlewareDir, { recursive: true });
  writeFileSync(configFile, DEEPAGENT_MIDDLEWARE_CONFIG);

  return { installed: true, path: configFile, message: "Installed DeepAgent middleware config" };
}

// ── OpenClaw ────────────────────────────────────────────────────

function installOpenClawPlugin(cwd: string): InstallResult {
  const pluginDir = join(cwd, ".openclaw", "plugins", "arc");
  const pluginFile = join(pluginDir, "plugin.ts");

  if (existsSync(pluginFile)) {
    return { installed: false, path: pluginFile, message: "Plugin already installed" };
  }

  mkdirSync(pluginDir, { recursive: true });
  writeFileSync(pluginFile, OPENCLAW_PLUGIN);

  return { installed: true, path: pluginFile, message: "Installed OpenClaw plugin" };
}

// ════════════════════════════════════════════════════════════════
// Skill/Plugin Templates
// ════════════════════════════════════════════════════════════════

const CLAUDE_CODE_SKILL = `---
name: remote-control
description: Start a remote control session to let a viewer observe and interact with this agent from a browser
allowed_tools:
  - Bash
---

# Remote Control

Start a remote control session that streams your traces (tool calls, messages,
subagent activity) to a relay server where a viewer can watch and send commands.

## Instructions

This skill is not yet implemented for Claude Code. To use ARC with Hermes Agent:

\`\`\`bash
arc setup --hermes
# Then start Hermes and type /remote-control
\`\`\`

## Notes

- The viewer URL contains a session secret — only share it with trusted parties
- All traces (tool calls, outputs, messages) are forwarded to connected viewers
- Viewers can inject messages and approve/deny tool calls
- If you hit the session limit: \`arc sessions --clear\`
`;

const HERMES_SKILL = `---
name: remote-control
description: Start a remote control session — lets you observe and interact with this agent from a web browser in real-time
version: 1.0.0
metadata:
  hermes:
    tags: [collaboration, observability, remote]
    category: devops
    requires_toolsets: [arc_relay]
---

# Remote Control

Connect this agent session to a relay server so a viewer can watch your work
and send commands from a web browser in real-time.

## When to Use

- The user wants someone else to watch or interact with this session remotely
- The user asks to share, stream, or broadcast their agent session
- The user says "remote control", "let someone watch", or "share my session"
- The user wants to monitor an agent from their phone or another device

## Procedure

Call the \`arc_start_session\` tool. It returns JSON with a \`viewer_url\` field.

Then tell the user:

1. Print the **complete viewer_url** from the tool result. Do NOT shorten or abbreviate it.
2. If \`browser_opened\` is true, tell them the viewer has been opened in their browser.
3. If not, tell them to open the URL in a browser manually.
4. Explain that the viewer can:
   - Watch all tool calls and outputs in real-time
   - Send messages to this agent
   - Approve or deny tool execution

All tool calls and LLM responses are automatically streamed to the viewer
via the ARC plugin hooks — no manual forwarding needed.

To stop, call \`arc_stop_session\`.

## Pitfalls

- If the arc_relay toolset is not available, the ARC plugin may not be installed.
  Tell the user to run: \`arc setup --hermes\`
- If the error mentions "websocket-client not installed", run:
  \`pip install websocket-client\` (must be in the same Python environment as Hermes)
- If the error mentions "session limit reached", the user has hit the per-token session cap.
  Tell them to run: \`arc sessions --clear\` to close all existing sessions, then retry.
- If connection fails, the relay may not be running or the token is invalid.
  Tell the user: \`arc setup\`

## Verification

The \`arc_start_session\` tool returns \`{"status": "connected", "viewer_url": "..."}\`
on success. If it returns an error, surface it verbatim — it will say what went wrong.
`;

const DEEPAGENT_MIDDLEWARE_CONFIG = JSON.stringify(
  {
    name: "remote-control",
    description: "Stream agent traces to a relay server for remote observation and control",
    package: "@axolotlai/arc-adapter-deepagent",
    config: {
      relayUrl: "${ARC_RELAY_URL:-wss://arc-beta.axolotl.ai/ws}",
      agentToken: "${ARC_AGENT_TOKEN}",
    },
    docs: "https://github.com/axolotl-ai-cloud/arc#deepagent",
  },
  null,
  2,
) + "\n";

const OPENCLAW_PLUGIN = `/**
 * ARC — OpenClaw Plugin
 *
 * Registers a /remote-control command that connects this OpenClaw session
 * to a relay server for real-time remote observation and control.
 *
 * Install: arc setup --framework openclaw
 * Usage:   /remote-control (in OpenClaw)
 */

import type { PluginContext } from "@openclaw/sdk";

export function register(ctx: PluginContext) {
  ctx.registerCommand({
    name: "remote-control",
    description: "Start a remote control session for browser-based observation",
    async execute(args) {
      const { connect } = await import("@axolotlai/arc-cli");

      const result = await connect({
        framework: "openclaw",
        agentName: ctx.agentName || "openclaw-agent",
        onCommand: (cmd) => {
          // Route commands back to OpenClaw
          if (cmd.type === "inject_message") {
            ctx.injectMessage(cmd.content, cmd.role || "user");
          } else if (cmd.type === "cancel") {
            ctx.cancel(cmd.reason);
          } else if (cmd.type === "approve_tool") {
            ctx.approveTool(cmd.toolCallId);
          } else if (cmd.type === "deny_tool") {
            ctx.denyTool(cmd.toolCallId, cmd.reason);
          }
        },
      });

      ctx.log(\`Remote control active: \${result.viewerUrl}\`);

      // Clean up on session end
      ctx.onDispose(() => result.disconnect());
    },
  });
}
`;

