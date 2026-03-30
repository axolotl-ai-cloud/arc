/**
 * Setup command — interactive configuration wizard.
 *
 * Walks the user through configuring:
 *   1. Relay URL (hosted vs self-hosted)
 *   2. Agent token (generates one if needed)
 *   3. Python deps + relay server start (self-hosted)
 *   4. Framework detection
 *   5. Skill installation
 *
 * Supports shorthand flags:
 *   arc setup --hermes
 *   arc setup --hosted
 *   arc setup --deepagent --hosted
 */

import { createInterface } from "node:readline";
import { randomBytes } from "node:crypto";
import { execSync, spawn } from "node:child_process";
import { existsSync, writeFileSync, mkdirSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig, saveConfig, getConfigPath, getConfigDir } from "./config.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
import type { ArcConfig } from "./config.js";
import { installSkill, detectFramework } from "./skill-installer.js";

export interface SetupOptions {
  framework?: ArcConfig["framework"];
  hosted?: boolean;
  selfHosted?: boolean;
  advanced?: boolean;
}

function prompt(rl: ReturnType<typeof createInterface>, question: string): Promise<string> {
  return new Promise((resolve) => rl.question(question, resolve));
}

/** Find a working Python 3 binary name. */
function findPython(): string | null {
  for (const bin of ["python3", "python"]) {
    try {
      const version = execSync(`${bin} --version 2>&1`, { encoding: "utf-8" }).trim();
      if (version.includes("Python 3.")) return bin;
    } catch {
      // not found
    }
  }
  return null;
}

/** Find pip for a given python binary. */
function findPip(python: string): string | null {
  // Try using python -m pip first (most reliable)
  try {
    execSync(`${python} -m pip --version 2>&1`, { encoding: "utf-8" });
    return `${python} -m pip`;
  } catch {
    // fall through
  }
  for (const bin of ["pip3", "pip"]) {
    try {
      execSync(`${bin} --version 2>&1`, { encoding: "utf-8" });
      return bin;
    } catch {
      // not found
    }
  }
  return null;
}

/** Find the relay directory — either next to the CLI (repo checkout) or CWD. */
function findRelayDir(): string | null {
  // When installed from repo: CLI is at packages/cli/, relay is at relay/
  const repoRoot = resolve(__dirname, "..", "..", "..");
  const repoRelay = join(repoRoot, "relay");
  if (existsSync(join(repoRelay, "requirements.txt"))) {
    return repoRoot; // Return repo root so `python -m relay` works
  }
  // Check CWD
  const cwdRelay = join(process.cwd(), "relay");
  if (existsSync(join(cwdRelay, "requirements.txt"))) {
    return process.cwd();
  }
  return null;
}

/** Wait for the relay to be healthy (up to timeoutMs). */
async function waitForRelay(url: string, timeoutMs: number = 10_000): Promise<boolean> {
  const healthUrl = url
    .replace("ws://", "http://")
    .replace("wss://", "https://")
    .replace("/ws", "/health");

  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const resp = await fetch(healthUrl);
      if (resp.ok) return true;
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

/** Write the relay PID so we can show it later / clean up. */
function savePid(pid: number): void {
  const dir = getConfigDir();
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "relay.pid"), String(pid), { mode: 0o600 });
}

export async function runSetup(options: SetupOptions = {}): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const existing = loadConfig();

  console.log("");
  console.log("┌─────────────────────────────────────────┐");
  console.log("│       Agent Remote Control — Setup        │");
  console.log("└─────────────────────────────────────────┘");
  console.log("");

  // 1. Hosted (default) vs self-hosted
  //    Default: beta relay (no prompt needed)
  //    --self-hosted or ARC_ENV=dev: local relay
  const isDev = process.env.ARC_ENV === "dev";
  let hosted: boolean;
  if (options.selfHosted || isDev) {
    hosted = false;
    if (isDev) console.log("  Mode: self-hosted (ARC_ENV=dev)");
    else console.log("  Mode: self-hosted");
  } else {
    hosted = true;
  }

  let relayUrl: string;
  let agentToken: string;
  let viewerBase: string | undefined;

  if (hosted) {
    relayUrl = "wss://arc-beta.axolotl.ai/ws";

    // Generate or reuse a beta token: axolotl_beta_ + 43-char hash
    const existingToken = existing.agentToken || "";
    if (existingToken.startsWith("axolotl_beta_") && existingToken.length >= 56) {
      agentToken = existingToken;
    } else {
      agentToken = "axolotl_beta_" + randomBytes(32).toString("base64url");
    }
    console.log("  Relay:  arc-beta.axolotl.ai");

    // Advanced: allow power users to specify a custom viewer (e.g. OSS GitHub Pages UI)
    if (options.advanced) {
      console.log("");
      console.log("  Advanced: viewer URL override");
      console.log("  Default viewer is bundled into the relay (arc-beta.axolotl.ai/viewer).");
      console.log("  To use the OSS viewer you can inspect from source:");
      console.log("    https://axolotl-ai-cloud.github.io/arc");
      console.log("");
      const viewerAnswer = await prompt(rl, `  Custom viewer base URL (Enter to skip): `);
      const trimmed = viewerAnswer.trim();
      if (trimmed) {
        viewerBase = trimmed;
        console.log(`  ✓ Viewer: ${viewerBase}`);
      }
    }
  } else {
    // ── Self-hosted setup ──────────────────────────────────────

    const defaultUrl = existing.relayUrl || "ws://localhost:8600/ws";
    const urlAnswer = await prompt(rl, `Relay WebSocket URL [${defaultUrl}]: `);
    relayUrl = urlAnswer.trim() || defaultUrl;

    // Token: reuse existing, accept user input, or generate
    const defaultToken = existing.agentToken || "";
    if (defaultToken) {
      const tokenAnswer = await prompt(rl, `Agent token [${defaultToken.slice(0, 8)}...]: `);
      agentToken = tokenAnswer.trim() || defaultToken;
    } else {
      console.log("");
      console.log("  You need an agent token to authenticate with the relay.");
      console.log("  If you already have one, enter it below.");
      console.log("  Otherwise, press Enter to generate one.");
      console.log("");
      const tokenAnswer = await prompt(rl, `Agent token (Enter to generate): `);
      agentToken = tokenAnswer.trim() || randomBytes(32).toString("base64url");
    }

    // Check if the relay is already running
    let relayAlreadyRunning = false;
    try {
      const healthUrl = relayUrl
        .replace("ws://", "http://")
        .replace("wss://", "https://")
        .replace("/ws", "/health");
      const resp = await fetch(healthUrl);
      relayAlreadyRunning = resp.ok;
    } catch {
      // not running
    }

    if (relayAlreadyRunning) {
      console.log("");
      console.log("  ✓ Relay is already running at " + relayUrl);
    } else {
      // Offer to start the relay
      console.log("");
      const startAnswer = await prompt(rl, `Start the relay server now? [Y/n]: `);

      if (!startAnswer.toLowerCase().startsWith("n")) {
        await startRelay(rl, relayUrl, agentToken);
      } else {
        console.log("");
        console.log("  To start the relay later, run:");
        console.log(`    AGENT_TOKEN=${agentToken} python -m relay`);
        console.log("");
      }
    }
  }

  // 2. Detect framework
  console.log("");
  let framework: ArcConfig["framework"];

  if (options.framework) {
    framework = options.framework;
    console.log(`  Framework: ${framework}`);
  } else {
    const detected = detectFramework();
    if (detected !== "generic") {
      const confirmAnswer = await prompt(
        rl,
        `Detected framework: ${detected}. Correct? [Y/n]: `,
      );
      framework = confirmAnswer.toLowerCase().startsWith("n")
        ? (await prompt(rl, "Framework (hermes/deepagent/openclaw/generic): ")).trim() as ArcConfig["framework"] || "generic"
        : detected;
    } else {
      const fwAnswer = await prompt(
        rl,
        `Framework (hermes/deepagent/openclaw/generic) [generic]: `,
      );
      framework = (fwAnswer.trim() as ArcConfig["framework"]) || "generic";
    }
  }

  // 3. E2E encryption toggle
  let e2e = existing.e2e || false;
  const e2eAnswer = await prompt(
    rl,
    `  Enable end-to-end encryption? (free during beta) [y/N]: `,
  );
  e2e = e2eAnswer.toLowerCase().startsWith("y");
  if (e2e) {
    console.log("  ✓ E2E encryption enabled — traces encrypted with AES-256-GCM");
  }

  // 4. Save config
  saveConfig({ relayUrl, agentToken, framework, hosted: false, e2e, viewerBase });

  console.log("");
  console.log(`  ✓ Config saved to ${getConfigPath()}`);

  // 4. Install skill
  const installAnswer = await prompt(
    rl,
    `  Install /remote-control skill for ${framework}? [Y/n]: `,
  );

  if (!installAnswer.toLowerCase().startsWith("n")) {
    const result = installSkill(framework);
    if (result.installed) {
      console.log(`  ✓ Skill installed at ${result.path}`);
    } else {
      console.log(`  ⚠ ${result.message}`);
    }
  }

  console.log("");
  if (framework === "hermes") {
    console.log("  Setup complete! Start Hermes and type /remote-control:");
    console.log("    hermes");
  } else {
    console.log("  Setup complete! Start a remote control session with:");
    console.log("    arc connect");
  }
  console.log("");

  rl.close();
}

// ─── Relay Startup Logic ─────────────────────────────────────────────

async function startRelay(
  rl: ReturnType<typeof createInterface>,
  relayUrl: string,
  agentToken: string,
): Promise<void> {
  // 1. Check Python
  const python = findPython();
  if (!python) {
    console.log("");
    console.log("  ⚠ Python 3 not found. Install Python 3.10+ to run the relay.");
    console.log("    https://www.python.org/downloads/");
    console.log("");
    console.log("  Or start the relay with Docker:");
    console.log(`    docker compose up -d relay`);
    console.log("");
    return;
  }
  console.log(`  ✓ Found ${execSync(`${python} --version`, { encoding: "utf-8" }).trim()}`);

  // 2. Find relay source
  const relayDir = findRelayDir();
  if (!relayDir) {
    console.log("");
    console.log("  ⚠ Relay source not found. Expected relay/ directory in the repo.");
    console.log("    Clone the repo:  git clone https://github.com/axolotl-ai-cloud/arc");
    console.log("    Then run setup from the repo root.");
    console.log("");
    console.log("  Or start with Docker:");
    console.log(`    docker compose up -d relay`);
    console.log("");
    return;
  }

  // 3. Install Python dependencies
  const pip = findPip(python);
  if (!pip) {
    console.log("  ⚠ pip not found. Install pip to manage Python dependencies.");
    return;
  }

  const reqFile = join(relayDir, "relay", "requirements.txt");
  console.log("  Installing relay dependencies...");
  try {
    execSync(`${pip} install -q -r ${reqFile}`, {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    console.log("  ✓ Dependencies installed");
  } catch {
    console.log("  ⚠ Failed to install dependencies. Try manually:");
    console.log(`    ${pip} install -r ${reqFile}`);
    return;
  }

  // 4. Parse port from URL
  let port = 8600;
  try {
    const urlObj = new URL(relayUrl.replace("ws://", "http://").replace("wss://", "https://"));
    if (urlObj.port) port = parseInt(urlObj.port, 10);
  } catch {
    // use default
  }

  // 5. Start relay in the background
  console.log(`  Starting relay on port ${port}...`);

  // Log relay output to ~/.arc/relay.log
  const { openSync } = await import("node:fs");
  const logFile = join(getConfigDir(), "relay.log");
  const logFd = openSync(logFile, "a");

  const relayProcess = spawn(python, ["-m", "relay"], {
    cwd: relayDir,
    env: {
      ...process.env,
      AGENT_TOKEN: agentToken,
      PORT: String(port),
    },
    detached: true,
    stdio: ["ignore", logFd, logFd],
  });

  relayProcess.unref();

  if (relayProcess.pid) {
    savePid(relayProcess.pid);
  }

  // 6. Wait for relay to be ready
  const ready = await waitForRelay(relayUrl);
  if (ready) {
    console.log(`  ✓ Relay running at ${relayUrl} (pid: ${relayProcess.pid})`);
  } else {
    console.log("");
    console.log("  ⚠ Relay started but not responding yet.");
    console.log("    Check logs or try manually:");
    console.log(`    AGENT_TOKEN=${agentToken} PORT=${port} ${python} -m relay`);
    console.log("");
  }
}
