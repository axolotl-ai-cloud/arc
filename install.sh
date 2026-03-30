#!/usr/bin/env bash
#
# Agent Remote Control — Quick Installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/axolotl-ai-cloud/arc/refs/heads/main/install.sh | sh
#
# Dev mode (install from local repo checkout):
#   ./install.sh --local              # from repo root
#   ./install.sh --local=/path/to/arc
#
# What this does:
#   1. Checks for Node.js (>= 18)
#   2. Installs @axolotlai/arc-cli globally via npm (or from local source in dev mode)
#
# Then run:
#   arc setup              # interactive config wizard
#   arc setup --hermes     # skip framework prompt
#   arc setup --hosted     # configure for hosted relay
#

set -euo pipefail

# ── Parse flags ────────────────────────────────────────────────

LOCAL_DIR=""
for arg in "$@"; do
  case "$arg" in
    --local=*) LOCAL_DIR="${arg#--local=}" ;;
    --local)   LOCAL_DIR="." ;;
  esac
done

# ── Colors ──────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${BLUE}[info]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[ok]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[warn]${NC}  %s\n" "$*"; }
fail()  { printf "${RED}[error]${NC} %s\n" "$*"; exit 1; }

# ── Banner ──────────────────────────────────────────────────────

printf "\n"
printf "${BOLD}  ╔══════════════════════════════════════════╗${NC}\n"
printf "${BOLD}  ║   Agent Remote Control — Installer       ║${NC}\n"
printf "${BOLD}  ╚══════════════════════════════════════════╝${NC}\n"
printf "\n"

# ── Check prerequisites ────────────────────────────────────────

if ! command -v node &> /dev/null; then
  fail "Node.js is required but not installed. Install Node.js 18+ from https://nodejs.org"
fi

NODE_VERSION=$(node -v | sed 's/^v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
  fail "Node.js 18+ is required (found v$(node -v)). Please upgrade."
fi
ok "Node.js $(node -v)"

if ! command -v npm &> /dev/null; then
  fail "npm is required but not installed."
fi
ok "npm $(npm -v)"

# ── Install CLI ─────────────────────────────────────────────────

info "Installing @axolotlai/arc-cli..."

if command -v arc &> /dev/null; then
  CURRENT=$(arc --version 2>/dev/null || echo "unknown")
  warn "arc already installed (${CURRENT}), upgrading..."
fi

if [ -n "$LOCAL_DIR" ]; then
  # Dev mode: install from local repo checkout
  LOCAL_DIR=$(cd "$LOCAL_DIR" && pwd)
  if [ ! -f "$LOCAL_DIR/packages/cli/package.json" ]; then
    fail "Cannot find packages/cli/package.json in $LOCAL_DIR — run from the repo root or pass --local=/path/to/arc"
  fi
  info "Installing from local repo: $LOCAL_DIR"
  (cd "$LOCAL_DIR" && npm install && npm run build) 2>&1 | tail -5
  npm install -g "$LOCAL_DIR/packages/cli" 2>&1 | tail -1
  ok "Installed @axolotlai/arc-cli from local source"
else
  npm install -g @axolotlai/arc-cli@latest 2>&1 | tail -1
  ok "Installed @axolotlai/arc-cli"
fi

# Verify installation
if ! command -v arc &> /dev/null; then
  warn "arc not found in PATH. You may need to add npm global bin to your PATH:"
  warn "  export PATH=\"\$(npm prefix -g)/bin:\$PATH\""
else
  # Refresh installed framework skills/plugins so they pick up the latest code
  info "Refreshing installed skills..."
  arc install-skill 2>/dev/null && ok "Skills refreshed" || true
fi

# ── Done ───────────────────────────────────────────────────────

printf "\n"
printf "${GREEN}${BOLD}  Installation complete!${NC}\n"
printf "\n"
printf "  Next steps:\n"
printf "    ${BOLD}arc setup${NC}              Configure relay URL, token, and framework\n"
printf "    ${BOLD}arc setup --hermes${NC}     Configure for Hermes Agent\n"
printf "\n"
printf "  Then start Hermes and type ${BOLD}/remote-control${NC}\n"
printf "\n"
printf "  Docs: https://github.com/axolotl-ai-cloud/arc\n"
printf "\n"
