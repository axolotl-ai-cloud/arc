import { useEffect, useRef, useState, useCallback } from "react";

// ─── E2E Decryption (inline, browser Web Crypto API) ──────────────

async function deriveViewerKey(sessionSecret: string, sessionId: string): Promise<CryptoKey> {
  const enc = new TextEncoder();
  const keyMaterial = await crypto.subtle.importKey("raw", enc.encode(sessionSecret), "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: enc.encode(sessionId), info: enc.encode("arc-e2e-v1") },
    keyMaterial,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

function b64ToBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function bufferToB64(buf: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buf)));
}

async function decryptPayload<T>(key: CryptoKey, payload: { ciphertext: string; nonce: string }, sessionId: string): Promise<T> {
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: b64ToBuffer(payload.nonce), additionalData: new TextEncoder().encode(sessionId) },
    key,
    b64ToBuffer(payload.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}

async function encryptPayload(key: CryptoKey, data: unknown, sessionId: string): Promise<{ ciphertext: string; nonce: string }> {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: new TextEncoder().encode(sessionId) },
    key,
    new TextEncoder().encode(JSON.stringify(data)),
  );
  return { ciphertext: bufferToB64(ciphertext), nonce: bufferToB64(nonce.buffer as ArrayBuffer) };
}

// ─── Types (inline to avoid needing protocol build) ─────────────────

type AgentStatus = "idle" | "thinking" | "executing" | "waiting_for_input" | "approval_required" | "error";

interface TraceEntry {
  id: string;
  timestamp: string;
  type: string;
  [key: string]: unknown;
}

interface PendingToolCall {
  toolCallId: string;
  toolName: string;
  toolInput: Record<string, unknown>;
  timestamp: string;
}

interface AgentQuestion {
  id: string;
  prompt: string;
  choices?: string[];
  timestamp: string;
}

interface Props {
  sessionId: string;
  relayWsUrl: string;
  sessionSecret: string;
}

// ─── Component ──────────────────────────────────────────────────────

export function RemoteControlView({ sessionId, relayWsUrl, sessionSecret }: Props) {
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [framework, setFramework] = useState<string>("");
  const [agentName, setAgentName] = useState<string>("");
  const [connected, setConnected] = useState(false);
  const [input, setInput] = useState("");
  const [pendingTools, setPendingTools] = useState<PendingToolCall[]>([]);
  const [agentQuestion, setAgentQuestion] = useState<AgentQuestion | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const e2eKeyRef = useRef<CryptoKey | null>(null);
  const [e2eActive, setE2eActive] = useState(false);
  const [reconnectTrigger, setReconnectTrigger] = useState(0);
  const lastSentRef = useRef<string | null>(null);
  const sessionErrorRef = useRef(false);
  const reconnectAttemptRef = useRef(0);
  const pendingEncryptedRef = useRef<Array<{event: unknown, sessionId: string}>>([]);

  // Auto-scroll
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [traces]);

  // WebSocket connection
  useEffect(() => {
    const ws = new WebSocket(relayWsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      console.log("[arc] WebSocket connected, subscribing to", sessionId); // TODO: remove debug log before v1
      ws.send(JSON.stringify({ kind: "subscribe", sessionId, sessionSecret }));
    };

    // Keep connection alive with pings (Fly.io drops idle WebSockets after 60s)
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ kind: "ping" }));
      }
    }, 30_000);

    ws.onmessage = async (e) => {
      try {
        const envelope = JSON.parse(e.data);

        // Handle relay errors (session not found, auth failure, etc.)
        if (envelope.error) {
          const errorMsg = envelope.error as string;
          const isAuthError = errorMsg.includes("invalid session") || errorMsg.includes("unauthorized");

          setStatus("error");
          setConnected(false);
          setTraces((prev) => {
            // Only add one error trace, not one per reconnect attempt
            if (prev.some(t => t.type === "error" && (t.code as string) === "RELAY")) return prev;
            return [
              ...prev,
              {
                id: `relay-error-${Date.now()}`,
                timestamp: new Date().toISOString(),
                type: "error",
                code: "RELAY",
                message: isAuthError
                  ? `${errorMsg}. Ask the agent for a new viewer URL.`
                  : `${errorMsg}. Waiting for agent to reconnect...`,
              },
            ];
          });

          if (isAuthError) {
            // Auth errors are permanent — stop reconnecting
            sessionErrorRef.current = true;
          }
          // For "session not found" / "too many attempts", keep reconnecting
          // with backoff — the agent may re-register the session
          ws.close();
          return;
        }

        if (envelope.kind === "register" && envelope.session) {
          // Successful subscribe — reset reconnect state
          reconnectAttemptRef.current = 0;
          sessionErrorRef.current = false;
          setStatus("idle");
          setConnected(true);
          // Clear any previous RELAY error traces
          setTraces((prev) => prev.filter(t => !((t.code as string) === "RELAY")));
          setFramework(envelope.session.agentFramework);
          setAgentName(envelope.session.agentName ?? "");
          // Initialize E2E key if session uses session_secret encryption
          if (envelope.session.e2e === "session_secret") {
            if (!crypto?.subtle) {
              // crypto.subtle is only available in secure contexts (https:// or localhost).
              // Accessing the viewer via http://192.168.x.x or any plain-HTTP LAN address
              // will land here — traces will remain encrypted and unreadable.
              setTraces((prev) => [
                ...prev,
                {
                  id: "e2e-insecure-context",
                  timestamp: new Date().toISOString(),
                  type: "error",
                  code: "E2E",
                  message:
                    "E2E decryption unavailable: Web Crypto requires a secure context. " +
                    "Access this viewer over https:// or http://localhost instead of a plain-HTTP LAN address.",
                },
              ]);
            } else {
              try {
                e2eKeyRef.current = await deriveViewerKey(sessionSecret, sessionId);
                setE2eActive(true);
                // Decrypt any traces that arrived before key was ready
                if (pendingEncryptedRef.current.length > 0) {
                  const pending = pendingEncryptedRef.current.splice(0);
                  const decrypted: TraceEntry[] = [];
                  for (const p of pending) {
                    try {
                      const event = await decryptPayload(e2eKeyRef.current, p.event as {ciphertext: string; nonce: string}, p.sessionId);
                      decrypted.push(event as TraceEntry);
                    } catch {
                      // skip undecryptable (wrong key, old session)
                    }
                  }
                  if (decrypted.length > 0) {
                    setTraces((prev) => [...prev, ...decrypted]);
                  }
                }
              } catch (err) {
                console.error("[e2e] Failed to derive key:", err);
              }
            }
          }
        }

        if (envelope.kind === "trace" && envelope.event) {
          let event = envelope.event;
          // Decrypt if encrypted
          if (envelope.encrypted) {
            if (e2eKeyRef.current) {
              try {
                event = await decryptPayload(e2eKeyRef.current, event, sessionId);
              } catch {
                // Can't decrypt — skip (wrong key, corrupted, or old session)
                return;
              }
            } else {
              // Key not ready yet — buffer for later decryption
              pendingEncryptedRef.current.push({ event, sessionId });
              return;
            }
          }
          // Deduplicate: if this is a user_message that we already added locally, skip
          if (event.type === "user_message" && lastSentRef.current && event.content === lastSentRef.current) {
            lastSentRef.current = null;
            // Don't add — already in traces from handleSend
          } else {
            setTraces((prev) => {
              const next = [...prev, event];
              return next.length > 2000 ? next.slice(-2000) : next;
            });
          }

          if (event.type === "status_change") {
            setStatus(event.status);

            // Agent is asking a question with optional choices
            if (event.status === "waiting_for_input" && event.detail) {
              setAgentQuestion({
                id: event.id,
                prompt: event.detail as string,
                choices: (event as Record<string, unknown>).choices as string[] | undefined,
                timestamp: event.timestamp,
              });
            } else {
              setAgentQuestion(null);
            }
          }

          // Track pending tool calls that need approval
          if (event.type === "tool_call" && event.status === "started") {
            const needsApproval = (event as Record<string, unknown>).requiresApproval;
            if (needsApproval) {
              setPendingTools((prev) => [
                ...prev,
                {
                  toolCallId: event.id,
                  toolName: event.toolName as string,
                  toolInput: event.toolInput as Record<string, unknown>,
                  timestamp: event.timestamp,
                },
              ]);
            }
          }

          // Remove from pending when tool completes
          if (event.type === "tool_result") {
            setPendingTools((prev) =>
              prev.filter((t) => t.toolCallId !== event.toolCallId),
            );
          }
        }
      } catch {
        // ignore
      }
    };

    ws.onclose = (ev) => {
      setConnected(false);
      console.log("[arc] WebSocket closed", ev.code, ev.reason, "intentional:", intentionalClose, "sessionError:", sessionErrorRef.current); // TODO: remove debug log before v1
      // Only auto-reconnect if we haven't hit a session error
      // (session not found, invalid secret, etc.)
      if (!intentionalClose && !sessionErrorRef.current) {
        const delay = Math.min(2000 * Math.pow(2, reconnectAttemptRef.current), 30000);
        reconnectAttemptRef.current++;
        setTimeout(() => {
          setReconnectTrigger((n) => n + 1);
        }, delay);
      }
    };
    ws.onerror = () => setConnected(false);

    let intentionalClose = false;
    return () => { intentionalClose = true; clearInterval(pingInterval); ws.close(); };
  }, [sessionId, relayWsUrl, sessionSecret, reconnectTrigger]);

  const sendCommand = useCallback(
    async (type: string, extra: Record<string, unknown> = {}) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      const command = {
        id: `cmd-${crypto.randomUUID()}`,
        sessionId,
        timestamp: new Date().toISOString(),
        type,
        ...extra,
      };
      if (e2eKeyRef.current && e2eActive) {
        const encrypted = await encryptPayload(e2eKeyRef.current, command, sessionId);
        wsRef.current.send(JSON.stringify({ kind: "command", command: encrypted, encrypted: true }));
      } else {
        wsRef.current.send(JSON.stringify({ kind: "command", command }));
      }
    },
    [sessionId, e2eActive],
  );

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    sendCommand("inject_message", { content: text });
    // Add a local trace immediately for responsiveness.
    // If the plugin echoes it back (pre_llm_call), deduplicate via lastSentRef.
    lastSentRef.current = text;
    setTraces((prev) => [
      ...prev,
      {
        id: `local-${crypto.randomUUID()}`,
        timestamp: new Date().toISOString(),
        type: "user_message",
        content: text,
      },
    ]);
    setInput("");
    setAgentQuestion(null);
  };

  const handleApproveTool = (toolCallId: string) => {
    sendCommand("approve_tool", { toolCallId });
    setPendingTools((prev) => prev.filter((t) => t.toolCallId !== toolCallId));
  };

  const handleDenyTool = (toolCallId: string, reason?: string) => {
    sendCommand("deny_tool", { toolCallId, reason: reason || "Denied by viewer" });
    setPendingTools((prev) => prev.filter((t) => t.toolCallId !== toolCallId));
  };

  const handleChoiceSelect = (choice: string) => {
    sendCommand("inject_message", { content: choice });
    setAgentQuestion(null);
  };

  return (
    <div style={styles.container}>
      {/* Status bar */}
      <div style={styles.statusBar}>
        <div style={styles.statusLeft}>
          <span
            style={{
              ...styles.statusDot,
              background: !connected ? "#ef4444" : status === "error" ? "#f59e0b" : "#4ade80",
            }}
          />
          <span style={styles.statusText}>
            {!connected ? "Disconnected" : status === "error" ? "Agent disconnected" : "Connected"}
          </span>
          {framework && (
            <span style={styles.frameworkTag}>{framework}</span>
          )}
          {agentName && (
            <span style={styles.agentNameTag}>{agentName}</span>
          )}
          {e2eActive && (
            <span style={styles.e2eTag}>&#x1F512; E2E</span>
          )}
        </div>
        <div style={styles.statusRight}>
          <span style={statusLabelStyle(status)}>{status.replace(/_/g, " ")}</span>
        </div>
      </div>

      {/* Trace log */}
      <div ref={scrollRef} style={styles.traceLog}>
        {traces.length === 0 && (
          <div style={styles.emptyLog}>
            Waiting for agent activity...
          </div>
        )}
        {traces.map((t) => (
          <TraceItem key={t.id} trace={t} />
        ))}
      </div>

      {/* Pending tool approval banner */}
      {pendingTools.length > 0 && (
        <div style={styles.approvalBanner}>
          {pendingTools.map((tool) => (
            <div key={tool.toolCallId} style={styles.approvalCard}>
              <div style={styles.approvalHeader}>
                <span style={styles.approvalIcon}>&#9888;</span>
                <span style={styles.approvalTitle}>
                  Tool requires approval: <strong>{tool.toolName}</strong>
                </span>
              </div>
              <pre style={styles.approvalCode}>
                {JSON.stringify(tool.toolInput, null, 2)}
              </pre>
              <div style={styles.approvalActions}>
                <button
                  style={styles.approveButton}
                  onClick={() => handleApproveTool(tool.toolCallId)}
                >
                  Approve
                </button>
                <button
                  style={styles.denyButton}
                  onClick={() => handleDenyTool(tool.toolCallId)}
                >
                  Deny
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Agent question with choices */}
      {agentQuestion && agentQuestion.choices && agentQuestion.choices.length > 0 && (
        <div style={styles.choiceBanner}>
          <div style={styles.choicePrompt}>{agentQuestion.prompt}</div>
          <div style={styles.choiceGrid}>
            {agentQuestion.choices.map((choice, i) => (
              <button
                key={i}
                style={styles.choiceButton}
                onClick={() => handleChoiceSelect(choice)}
              >
                {choice}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input area */}
      <div style={styles.inputArea}>
        <input
          style={styles.input}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder={
            status === "waiting_for_input"
              ? "Agent is waiting for input..."
              : "Send a message to the agent..."
          }
        />
        <button style={styles.sendButton} onClick={handleSend}>
          Send
        </button>
        <button
          style={styles.cancelButton}
          onClick={() => sendCommand("cancel", { reason: "User cancelled" })}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── Trace Item Renderer ────────────────────────────────────────────

function TraceItem({ trace }: { trace: TraceEntry }) {
  const time = new Date(trace.timestamp).toLocaleTimeString();

  switch (trace.type) {
    case "agent_message":
      return (
        <div style={{ ...styles.traceItem, background: "#0f1a2e", borderLeftColor: "#6366f1" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#6366f122", color: "#a5b4fc" }}>
              {trace.role as string}
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <div style={styles.traceContent}>{trace.content as string}</div>
        </div>
      );

    case "user_message":
      return (
        <div style={{ ...styles.traceItem, background: "#0a1628", borderLeftColor: "#2563eb" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#2563eb33", color: "#60a5fa" }}>
              you
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <div style={styles.traceContent}>{trace.content as string}</div>
        </div>
      );

    case "tool_call":
      return (
        <div style={{ ...styles.traceItem, borderLeftColor: "#f59e0b" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#f59e0b22", color: "#fbbf24" }}>
              tool: {trace.toolName as string}
            </span>
            <span style={toolStatusStyle(trace.status as string)}>
              {trace.status as string}
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          {trace.status === "started" && (
            <pre style={styles.codeBlock}>
              {JSON.stringify(trace.toolInput, null, 2)}
            </pre>
          )}
        </div>
      );

    case "tool_result":
      return (
        <div
          style={{
            ...styles.traceItem,
            borderLeftColor: trace.isError ? "#ef4444" : "#22c55e",
          }}
        >
          <div style={styles.traceHeader}>
            <span
              style={{
                ...styles.traceTag,
                background: trace.isError ? "#ef444422" : "#22c55e22",
                color: trace.isError ? "#f87171" : "#4ade80",
              }}
            >
              result
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <pre style={styles.codeBlock}>{trace.output as string}</pre>
        </div>
      );

    case "subagent_spawn":
      return (
        <div style={{ ...styles.traceItem, borderLeftColor: "#8b5cf6" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#8b5cf622", color: "#a78bfa" }}>
              subagent: {trace.subagentName as string}
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <div style={styles.traceContent}>{trace.task as string}</div>
        </div>
      );

    case "subagent_result":
      return (
        <div style={{ ...styles.traceItem, borderLeftColor: "#8b5cf6" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#8b5cf622", color: "#a78bfa" }}>
              subagent result
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <pre style={styles.codeBlock}>{trace.output as string}</pre>
        </div>
      );

    case "status_change":
      return (
        <div style={styles.statusEvent}>
          <span style={styles.statusEventDot}>●</span>
          Status: {(trace.status as string).replace(/_/g, " ")}
          {trace.detail ? ` — ${trace.detail}` : ""}
          <span style={styles.traceTime}>{time}</span>
        </div>
      );

    case "error":
      return (
        <div style={{ ...styles.traceItem, borderLeftColor: "#ef4444" }}>
          <div style={styles.traceHeader}>
            <span style={{ ...styles.traceTag, background: "#ef444422", color: "#f87171" }}>
              error: {trace.code as string}
            </span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <div style={{ ...styles.traceContent, color: "#f87171" }}>
            {trace.message as string}
          </div>
        </div>
      );

    case "stream_delta":
      return null; // stream deltas are typically aggregated, skip in log

    default:
      return (
        <div style={styles.traceItem}>
          <div style={styles.traceHeader}>
            <span style={styles.traceTag}>{trace.type}</span>
            <span style={styles.traceTime}>{time}</span>
          </div>
          <pre style={styles.codeBlock}>{JSON.stringify(trace, null, 2)}</pre>
        </div>
      );
  }
}

// ─── Style helpers ──────────────────────────────────────────────────

function statusLabelStyle(status: AgentStatus): React.CSSProperties {
  const colors: Record<AgentStatus, string> = {
    idle: "#666",
    thinking: "#f59e0b",
    executing: "#3b82f6",
    waiting_for_input: "#8b5cf6",
    approval_required: "#f59e0b",
    error: "#ef4444",
  };
  return {
    fontSize: "12px",
    fontWeight: 500,
    color: colors[status],
    textTransform: "uppercase",
    letterSpacing: "0.04em",
  };
}

function toolStatusStyle(status: string): React.CSSProperties {
  const color = status === "completed" ? "#4ade80" : status === "failed" ? "#f87171" : "#fbbf24";
  return {
    fontSize: "11px",
    color,
    fontWeight: 500,
  };
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    flexDirection: "column",
    flex: 1,
    overflow: "hidden",
  },
  statusBar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "8px 20px",
    borderBottom: "1px solid #1a1a1a",
    background: "#0f0f0f",
    flexShrink: 0,
  },
  statusLeft: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
  },
  statusRight: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
  },
  statusDot: {
    width: "6px",
    height: "6px",
    borderRadius: "50%",
  },
  statusText: {
    fontSize: "12px",
    color: "#888",
  },
  frameworkTag: {
    fontSize: "11px",
    padding: "1px 6px",
    borderRadius: "4px",
    background: "#ffffff0a",
    color: "#666",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
  },
  agentNameTag: {
    fontSize: "12px",
    color: "#aaa",
    fontWeight: 500,
  },
  e2eTag: {
    fontSize: "10px",
    padding: "1px 6px",
    borderRadius: "4px",
    background: "#22c55e22",
    color: "#4ade80",
    fontWeight: 600,
    letterSpacing: "0.06em",
  },
  traceLog: {
    flex: 1,
    overflow: "auto",
    padding: "12px 20px",
  },
  emptyLog: {
    textAlign: "center",
    color: "#444",
    padding: "48px",
    fontSize: "14px",
  },
  traceItem: {
    marginBottom: "8px",
    padding: "8px 12px",
    borderLeft: "3px solid #333",
    borderRadius: "0 6px 6px 0",
    background: "#141414",
  },
  traceHeader: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    marginBottom: "4px",
  },
  traceTag: {
    fontSize: "11px",
    fontWeight: 600,
    padding: "1px 6px",
    borderRadius: "4px",
    background: "#ffffff0a",
    color: "#888",
  },
  traceTime: {
    fontSize: "11px",
    color: "#444",
    marginLeft: "auto",
    fontFamily: "monospace",
  },
  traceContent: {
    fontSize: "14px",
    color: "#d4d4d4",
    lineHeight: 1.5,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  codeBlock: {
    fontSize: "12px",
    color: "#a3a3a3",
    background: "#0a0a0a",
    padding: "8px",
    borderRadius: "4px",
    overflow: "auto",
    maxHeight: "200px",
    fontFamily: "monospace",
    lineHeight: 1.4,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  statusEvent: {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    fontSize: "12px",
    color: "#555",
    padding: "4px 0",
  },
  statusEventDot: {
    fontSize: "8px",
    color: "#444",
  },
  inputArea: {
    display: "flex",
    gap: "8px",
    padding: "12px 20px",
    borderTop: "1px solid #222",
    background: "#111",
    flexShrink: 0,
  },
  input: {
    flex: 1,
    background: "#1a1a1a",
    border: "1px solid #333",
    borderRadius: "8px",
    padding: "10px 14px",
    color: "#e5e5e5",
    fontSize: "14px",
    outline: "none",
  },
  sendButton: {
    background: "#2563eb",
    border: "none",
    color: "#fff",
    padding: "10px 20px",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "14px",
    fontWeight: 500,
  },
  cancelButton: {
    background: "none",
    border: "1px solid #333",
    color: "#888",
    padding: "10px 14px",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "13px",
  },
  approvalBanner: {
    padding: "8px 20px",
    borderTop: "1px solid #f59e0b33",
    background: "#1a1500",
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    flexShrink: 0,
  },
  approvalCard: {
    border: "1px solid #f59e0b44",
    borderRadius: "8px",
    padding: "12px",
    background: "#1a1a0a",
  },
  approvalHeader: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    marginBottom: "8px",
  },
  approvalIcon: {
    color: "#f59e0b",
    fontSize: "16px",
  },
  approvalTitle: {
    fontSize: "13px",
    color: "#e5e5e5",
  },
  approvalCode: {
    fontSize: "11px",
    color: "#a3a3a3",
    background: "#0a0a0a",
    padding: "8px",
    borderRadius: "4px",
    overflow: "auto",
    maxHeight: "120px",
    fontFamily: "monospace",
    lineHeight: 1.4,
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
    marginBottom: "8px",
  },
  approvalActions: {
    display: "flex",
    gap: "8px",
  },
  approveButton: {
    background: "#166534",
    border: "1px solid #22c55e44",
    color: "#4ade80",
    padding: "6px 16px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 500,
  },
  denyButton: {
    background: "#450a0a",
    border: "1px solid #ef444444",
    color: "#f87171",
    padding: "6px 16px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 500,
  },
  choiceBanner: {
    padding: "12px 20px",
    borderTop: "1px solid #8b5cf633",
    background: "#0f0a1a",
    flexShrink: 0,
  },
  choicePrompt: {
    fontSize: "13px",
    color: "#c4b5fd",
    marginBottom: "8px",
  },
  choiceGrid: {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: "8px",
  },
  choiceButton: {
    background: "#1e1b4b",
    border: "1px solid #8b5cf644",
    color: "#a78bfa",
    padding: "8px 16px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 500,
  },
};
