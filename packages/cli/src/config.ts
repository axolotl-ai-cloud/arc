/**
 * Configuration management for ARC (Agent Remote Control) CLI.
 *
 * Stores config in ~/.arc/config.json
 * Can also be overridden by env vars:
 *   ARC_RELAY_URL, ARC_AGENT_TOKEN, ARC_FRAMEWORK
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ArcConfig {
  relayUrl: string;
  agentToken: string;
  framework: "hermes" | "deepagent" | "openclaw" | "generic";
  /** Hosted relay (arc.axolotl.ai) vs self-hosted */
  hosted: boolean;
  /** Enable end-to-end encryption (derives key from session secret) */
  e2e: boolean;
  /**
   * Override the viewer base URL. When set, viewer links are constructed as:
   *   {viewerBase}?relay={relayHttpUrl}&session={id}&s={secret}
   *
   * Useful for power users who want to use the OSS GitHub Pages UI
   * (https://axolotl-ai-cloud.github.io/arc) against any relay, rather
   * than the viewer bundled into the relay server itself.
   *
   * Defaults to undefined (use relay's built-in /viewer endpoint).
   */
  viewerBase?: string;
}

const CONFIG_DIR = join(homedir(), ".arc");
const CONFIG_FILE = join(CONFIG_DIR, "config.json");

const IS_DEV = process.env.ARC_ENV === "dev";

const DEFAULTS: ArcConfig = {
  relayUrl: IS_DEV ? "ws://localhost:8600/ws" : "wss://arc-beta.axolotl.ai/ws",
  agentToken: "",
  framework: "generic",
  hosted: !IS_DEV,
  e2e: false,
};

export function getConfigDir(): string {
  return CONFIG_DIR;
}

export function getConfigPath(): string {
  return CONFIG_FILE;
}

export function loadConfig(): ArcConfig {
  let fileConfig: Partial<ArcConfig> = {};

  if (existsSync(CONFIG_FILE)) {
    try {
      fileConfig = JSON.parse(readFileSync(CONFIG_FILE, "utf-8"));
    } catch {
      // ignore malformed config
    }
  }

  // Env vars take precedence
  return {
    relayUrl: process.env.ARC_RELAY_URL || fileConfig.relayUrl || DEFAULTS.relayUrl,
    agentToken: process.env.ARC_AGENT_TOKEN || fileConfig.agentToken || DEFAULTS.agentToken,
    framework: (process.env.ARC_FRAMEWORK as ArcConfig["framework"]) || fileConfig.framework || DEFAULTS.framework,
    hosted: process.env.ARC_HOSTED === "true" || fileConfig.hosted || DEFAULTS.hosted,
    e2e: process.env.ARC_E2E === "true" || fileConfig.e2e || DEFAULTS.e2e,
    viewerBase: process.env.ARC_VIEWER_BASE || fileConfig.viewerBase,
  };
}

export function saveConfig(config: Partial<ArcConfig>): void {
  mkdirSync(CONFIG_DIR, { recursive: true });

  let existing: Partial<ArcConfig> = {};
  if (existsSync(CONFIG_FILE)) {
    try {
      existing = JSON.parse(readFileSync(CONFIG_FILE, "utf-8"));
    } catch {
      // overwrite malformed config
    }
  }

  const merged = { ...existing, ...config };
  writeFileSync(CONFIG_FILE, JSON.stringify(merged, null, 2) + "\n", { mode: 0o600 });
}

export function configExists(): boolean {
  return existsSync(CONFIG_FILE);
}
