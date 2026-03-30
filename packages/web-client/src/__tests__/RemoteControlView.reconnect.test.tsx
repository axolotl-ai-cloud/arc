/**
 * Tests for viewer reconnect behavior.
 *
 * The bug: when the viewer's WebSocket disconnects and reconnects, the relay
 * replays its full trace history. Without clearing React state first, every
 * trace is shown twice (or more) — once from the initial connection and once
 * from the replay.
 *
 * The fix: on reconnect (reconnectAttemptRef.current > 0), clear traces before
 * the relay replay arrives so the viewer starts fresh.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { RemoteControlView } from "../RemoteControlView";

// ── WebSocket mock ────────────────────────────────────────────────────────────

interface MockWsInstance {
  url: string;
  sentMessages: string[];
  readyState: number;
  onopen: ((e: Event) => void) | null;
  onmessage: ((e: MessageEvent) => void) | null;
  onclose: ((e: CloseEvent) => void) | null;
  onerror: ((e: Event) => void) | null;
  /** Simulate the server opening the connection. */
  open(): void;
  /** Simulate the server sending a message. */
  receive(data: unknown): void;
  /** Simulate the server closing the connection. */
  serverClose(code?: number, reason?: string): void;
  send(data: string): void;
  close(): void;
}

let wsInstances: MockWsInstance[] = [];

function createMockWebSocket() {
  class MockWebSocket implements MockWsInstance {
    url: string;
    sentMessages: string[] = [];
    readyState = 0; // CONNECTING
    onopen: ((e: Event) => void) | null = null;
    onmessage: ((e: MessageEvent) => void) | null = null;
    onclose: ((e: CloseEvent) => void) | null = null;
    onerror: ((e: Event) => void) | null = null;

    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSING = 2;
    readonly CLOSED = 3;

    constructor(url: string) {
      this.url = url;
      wsInstances.push(this);
    }

    open() {
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }

    receive(data: unknown) {
      const event = new MessageEvent("message", {
        data: typeof data === "string" ? data : JSON.stringify(data),
      });
      this.onmessage?.(event);
    }

    serverClose(code = 1006, reason = "") {
      this.readyState = 3;
      const ev = new CloseEvent("close", { code, reason, wasClean: code === 1000 });
      this.onclose?.(ev);
    }

    send(data: string) {
      this.sentMessages.push(data);
    }

    close() {
      this.readyState = 3;
    }
  }

  return MockWebSocket;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeRegisterMsg(sessionId = "test-session") {
  return {
    kind: "register",
    session: {
      sessionId,
      agentFramework: "hermes",
      agentName: "test-agent",
      startedAt: "2025-01-01T00:00:00Z",
    },
  };
}

function makeTrace(id: string, content: string, sessionId = "test-session") {
  return {
    kind: "trace",
    event: {
      id,
      sessionId,
      timestamp: "2025-01-01T00:00:01Z",
      type: "agent_message",
      role: "assistant",
      content,
    },
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("RemoteControlView — reconnect trace replay", () => {
  beforeEach(() => {
    wsInstances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", createMockWebSocket());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("displays traces from the initial connection", async () => {
    render(
      <RemoteControlView
        sessionId="test-session"
        relayWsUrl="ws://localhost:8600/ws"
        sessionSecret="test-secret"
      />,
    );

    const ws = wsInstances[0];
    await act(async () => { ws.open(); });
    await act(async () => { ws.receive(makeRegisterMsg()); });
    await act(async () => { ws.receive(makeTrace("t1", "Hello from agent")); });
    await act(async () => { ws.receive(makeTrace("t2", "Second message")); });

    expect(screen.getByText("Hello from agent")).toBeDefined();
    expect(screen.getByText("Second message")).toBeDefined();
  });

  it("clears traces on reconnect so relay replay is not doubled", async () => {
    render(
      <RemoteControlView
        sessionId="test-session"
        relayWsUrl="ws://localhost:8600/ws"
        sessionSecret="test-secret"
      />,
    );

    // ── First connection ──────────────────────────────────────────────
    const ws1 = wsInstances[0];
    await act(async () => { ws1.open(); });
    await act(async () => { ws1.receive(makeRegisterMsg()); });
    await act(async () => { ws1.receive(makeTrace("t1", "Hello from agent")); });
    await act(async () => { ws1.receive(makeTrace("t2", "Second message")); });

    // Both traces visible once
    expect(screen.getAllByText("Hello from agent")).toHaveLength(1);
    expect(screen.getAllByText("Second message")).toHaveLength(1);

    // ── Server drops the connection (relay restart / network blip) ────
    await act(async () => { ws1.serverClose(1006, "server gone"); });

    // Advance timers past the reconnect delay (2000ms * 2^0 = 2000ms for attempt 0→1)
    await act(async () => { vi.advanceTimersByTime(3000); });

    // ── Second connection (reconnect) ─────────────────────────────────
    // A new WebSocket should have been created
    expect(wsInstances).toHaveLength(2);
    const ws2 = wsInstances[1];

    await act(async () => { ws2.open(); });
    // Relay sends register + replays the same two traces
    await act(async () => { ws2.receive(makeRegisterMsg()); });
    await act(async () => { ws2.receive(makeTrace("t1", "Hello from agent")); });
    await act(async () => { ws2.receive(makeTrace("t2", "Second message")); });

    // Each trace must appear exactly once — traces were cleared before replay
    expect(screen.getAllByText("Hello from agent")).toHaveLength(1);
    expect(screen.getAllByText("Second message")).toHaveLength(1);
  });

  it("does NOT clear traces on the first (initial) connection", async () => {
    // On the very first connect, reconnectAttemptRef.current === 0,
    // so traces must not be cleared (they're empty anyway, but the code
    // path must not interfere with subsequent trace delivery).
    render(
      <RemoteControlView
        sessionId="test-session"
        relayWsUrl="ws://localhost:8600/ws"
        sessionSecret="test-secret"
      />,
    );

    const ws = wsInstances[0];
    await act(async () => { ws.open(); });
    await act(async () => { ws.receive(makeRegisterMsg()); });
    await act(async () => { ws.receive(makeTrace("t1", "First trace")); });

    expect(screen.getByText("First trace")).toBeDefined();
    // Only one WebSocket instance — no reconnect happened
    expect(wsInstances).toHaveLength(1);
  });

  it("shows the empty-state placeholder when traces are cleared on reconnect", async () => {
    render(
      <RemoteControlView
        sessionId="test-session"
        relayWsUrl="ws://localhost:8600/ws"
        sessionSecret="test-secret"
      />,
    );

    const ws1 = wsInstances[0];
    await act(async () => { ws1.open(); });
    await act(async () => { ws1.receive(makeRegisterMsg()); });
    await act(async () => { ws1.receive(makeTrace("t1", "Before disconnect")); });

    // Disconnect
    await act(async () => { ws1.serverClose(1006); });
    await act(async () => { vi.advanceTimersByTime(3000); });

    const ws2 = wsInstances[1];
    await act(async () => { ws2.open(); });

    // The moment register arrives on reconnect, traces are cleared.
    // Before the relay sends any replayed traces, empty state should be visible.
    await act(async () => { ws2.receive(makeRegisterMsg()); });

    expect(screen.getByText("Waiting for agent activity...")).toBeDefined();
  });
});
