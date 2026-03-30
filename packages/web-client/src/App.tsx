import { useState } from "react";
import { SessionList } from "./SessionList";
import { RemoteControlView } from "./RemoteControlView";
import { ConnectScreen } from "./ConnectScreen";
import type { RelayConfig } from "./ConnectScreen";

/** Parse URL query params for direct-link auto-connect. */
function getUrlParams(): { relay: string | null; session: string | null; secret: string | null } {
  const params = new URLSearchParams(window.location.search);
  return {
    relay: params.get("relay"),
    session: params.get("session"),
    secret: params.get("s") || params.get("secret"),
  };
}

const _urlParams = getUrlParams();

/**
 * Derive the WebSocket URL from an HTTP URL (or vice versa).
 */
function deriveUrls(input: string): { http: string; ws: string } {
  const base = input.replace(/\/+$/, "");

  if (base.startsWith("ws://")) {
    return { ws: base + "/ws", http: base.replace("ws://", "http://") };
  }
  if (base.startsWith("wss://")) {
    return { ws: base + "/ws", http: base.replace("wss://", "https://") };
  }

  const ws = base.replace(/^http/, "ws") + "/ws";
  return { http: base, ws };
}

/**
 * NOTE: We deliberately do NOT persist relay config (URL, tokens, secrets) in
 * localStorage or sessionStorage. Credentials should be entered fresh each time.
 * This prevents XSS from extracting stored secrets.
 */
export function App() {
  // Auto-connect from URL query params: ?session=...&s=...&relay=...
  // ?relay= is used when an external viewer (e.g. GitHub Pages) receives a deep link
  // from a remote relay. Initialize state directly to skip intermediate screens.
  const isDirectLink = Boolean(_urlParams.session && _urlParams.secret);
  const [config, setConfig] = useState<RelayConfig | null>(() => {
    if (isDirectLink) {
      const relayUrl = _urlParams.relay || window.location.origin;
      return { url: relayUrl, sessionSecret: _urlParams.secret! };
    }
    return null;
  });
  const [sessionId, setSessionId] = useState<string | null>(
    () => _urlParams.session,
  );

  const handleDisconnect = () => {
    setConfig(null);
    setSessionId(null);
    window.history.replaceState({}, "", window.location.pathname);
  };

  if (!config) {
    return <ConnectScreen onConnect={setConfig} />;
  }

  const { http, ws } = deriveUrls(config.url);

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <div style={styles.headerLeft}>
          <button style={styles.backButton} onClick={isDirectLink ? handleDisconnect : sessionId ? () => setSessionId(null) : handleDisconnect}>
            &larr; {isDirectLink ? "Disconnect" : sessionId ? "Sessions" : "Disconnect"}
          </button>
          <h1 style={styles.title}>Agent Remote Control</h1>
        </div>
        <div style={styles.headerRight}>
          <span style={styles.relayUrl}>{config.url}</span>
        </div>
      </header>

      {sessionId ? (
        <RemoteControlView
          sessionId={sessionId}
          relayWsUrl={ws}
          sessionSecret={config.sessionSecret}
        />
      ) : (
        <SessionList
          relayHttpUrl={http}
          sessionSecret={config.sessionSecret}
          onSelect={setSessionId}
        />
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    background: "#0a0a0a",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 20px",
    borderBottom: "1px solid #222",
    background: "#111",
    flexShrink: 0,
  },
  headerLeft: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
  },
  headerRight: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
  },
  title: {
    fontSize: "16px",
    fontWeight: 600,
    color: "#fff",
  },
  backButton: {
    background: "none",
    border: "1px solid #333",
    color: "#aaa",
    padding: "4px 10px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
  },
  relayUrl: {
    fontSize: "11px",
    color: "#555",
    fontFamily: "monospace",
  },
};
