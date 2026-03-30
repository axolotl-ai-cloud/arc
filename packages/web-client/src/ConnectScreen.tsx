import { useState } from "react";

export interface RelayConfig {
  url: string;
  sessionSecret: string;
}

interface Props {
  onConnect: (config: RelayConfig) => void;
}

export function ConnectScreen({ onConnect }: Props) {
  const params = new URLSearchParams(window.location.search);
  const [url, setUrl] = useState(params.get("relay") || "http://localhost:8600");
  const [sessionSecret, setSessionSecret] = useState("");
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConnect = async () => {
    const trimmedUrl = url.trim().replace(/\/+$/, "");
    const trimmedSecret = sessionSecret.trim();

    if (!trimmedUrl) return;
    if (!trimmedSecret) {
      setError("Session secret is required — get it from the agent operator");
      return;
    }

    setTesting(true);
    setError(null);

    // Detect mixed content: HTTPS viewer → HTTP relay is blocked by browsers
    const relayIsHttp = trimmedUrl.startsWith("http://") || trimmedUrl.startsWith("ws://");
    if (window.location.protocol === "https:" && relayIsHttp) {
      setError(
        "Mixed content blocked: this viewer is served over HTTPS but the relay URL is HTTP. " +
        "Open the relay's built-in viewer directly (e.g. http://192.168.x.x:8600/viewer) instead.",
      );
      setTesting(false);
      return;
    }

    try {
      // Quick health check (no auth needed for /health)
      const base = trimmedUrl.replace(/^wss?:\/\//, (m) =>
        m === "ws://" ? "http://" : "https://",
      );
      const res = await fetch(`${base}/health`, { signal: AbortSignal.timeout(5000) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onConnect({ url: trimmedUrl, sessionSecret: trimmedSecret });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`Health check failed (${msg}) — connecting anyway`);
      setTimeout(() => onConnect({ url: trimmedUrl, sessionSecret: trimmedSecret }), 1500);
    } finally {
      setTesting(false);
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <h1 style={styles.title}>Agent Remote Control</h1>
        <p style={styles.subtitle}>
          Connect to a relay server to monitor and control your agents.
        </p>

        <div style={styles.field}>
          <label style={styles.label}>Relay Server URL</label>
          <input
            style={styles.input}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="http://localhost:8600"
          />
        </div>

        <div style={styles.field}>
          <label style={styles.label}>Session Secret</label>
          <input
            style={{ ...styles.input, fontFamily: "monospace" }}
            type="password"
            value={sessionSecret}
            onChange={(e) => setSessionSecret(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleConnect();
            }}
            placeholder="Paste the session secret from the agent"
          />
          <p style={styles.hint}>
            The agent logs the session secret when it connects. Only share
            it with people who should have access to this agent.
          </p>
        </div>

        {error && <p style={styles.error}>{error}</p>}

        <button
          style={{
            ...styles.connectButton,
            opacity: testing ? 0.6 : 1,
          }}
          onClick={handleConnect}
          disabled={testing}
        >
          {testing ? "Connecting..." : "Connect"}
        </button>

        <div style={styles.footer}>
          <p style={styles.footerText}>
            Self-host: <code style={styles.code}>docker compose up</code> or{" "}
            <code style={styles.code}>python relay/relay.py</code>
          </p>
          <div style={styles.githubBox}>
            <a
              href="https://github.com/axolotl-ai-cloud/arc"
              target="_blank"
              rel="noopener noreferrer"
              style={styles.githubLink}
            >
              axolotl-ai-cloud/arc
            </a>
            {" — "}open-source universal remote control for AI agents
          </div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    height: "100%",
    background: "#0a0a0a",
    padding: "20px",
  },
  card: {
    maxWidth: "420px",
    width: "100%",
    background: "#111",
    border: "1px solid #222",
    borderRadius: "16px",
    padding: "32px",
  },
  title: {
    fontSize: "20px",
    fontWeight: 700,
    color: "#fff",
    marginBottom: "8px",
  },
  subtitle: {
    fontSize: "14px",
    color: "#666",
    marginBottom: "24px",
    lineHeight: 1.5,
  },
  field: {
    marginBottom: "16px",
  },
  label: {
    display: "block",
    fontSize: "12px",
    fontWeight: 600,
    color: "#888",
    textTransform: "uppercase" as const,
    letterSpacing: "0.05em",
    marginBottom: "6px",
  },
  input: {
    width: "100%",
    background: "#1a1a1a",
    border: "1px solid #333",
    borderRadius: "8px",
    padding: "10px 14px",
    color: "#e5e5e5",
    fontSize: "14px",
    outline: "none",
    boxSizing: "border-box" as const,
  },
  hint: {
    fontSize: "12px",
    color: "#555",
    marginTop: "6px",
    lineHeight: 1.4,
  },
  error: {
    fontSize: "13px",
    color: "#f59e0b",
    marginBottom: "12px",
  },
  connectButton: {
    width: "100%",
    background: "#2563eb",
    border: "none",
    color: "#fff",
    padding: "12px",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "15px",
    fontWeight: 600,
    marginBottom: "20px",
  },
  footer: {
    borderTop: "1px solid #222",
    paddingTop: "16px",
    display: "flex",
    flexDirection: "column" as const,
    gap: "6px",
  },
  footerText: {
    fontSize: "12px",
    color: "#555",
    lineHeight: 1.6,
  },
  githubBox: {
    border: "1px solid #7c3aed",
    background: "rgba(124, 58, 237, 0.08)",
    borderRadius: "8px",
    padding: "10px 12px",
    fontSize: "12px",
    color: "#999",
    lineHeight: 1.6,
  },
  githubLink: {
    color: "#a78bfa",
    textDecoration: "none",
  },
  code: {
    background: "#1a1a1a",
    padding: "2px 6px",
    borderRadius: "4px",
    fontSize: "11px",
    color: "#888",
  },
};
