import { useEffect, useState } from "react";
import { fetchConnectToken, type ConnectToken } from "./api";
import { IconCheck, IconControlTower } from "./icons";

// The manual-fallback token page (#73), reachable at /settings/token. The
// factory-vscode extension's `factory.getToken` opens this when the one-click
// vscode:// deep link is awkward; the operator copies the token by hand. Rendered
// as a standalone page (outside the cockpit shell) since it has its own URL.
export default function TokenPage() {
  const [data, setData] = useState<ConnectToken | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchConnectToken()
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);

  function copy() {
    if (!data?.token) return;
    void navigator.clipboard.writeText(data.token).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="token-page">
      <div className="token-card">
        <div className="token-brand">
          <IconControlTower size={22} />
          <span>CFactory</span>
        </div>
        <h1>API token</h1>
        <p className="token-lede">
          Use this token to connect an editor or external client to CFactory. In VS Code,
          paste it when prompted by <code>Factory: Get Token</code>, or use the one-click{" "}
          <code>Connect via Browser</code> flow which hands it over automatically.
        </p>

        {err && <div className="banner banner--error">{err}</div>}

        {data && !data.configured && (
          <div className="token-open">
            <p>
              CFactory is running in <strong>open mode</strong> — no API keys are configured,
              so there is no token to copy and every request is accepted. To issue a real token,
              set <code>CFACTORY_API_KEYS</code> in the deployment.
            </p>
          </div>
        )}

        {data && data.configured && (
          <>
            {data.connect_url && (
              <div className="token-field">
                <span className="token-field-label">API URL</span>
                <code className="token-value">{data.connect_url}</code>
              </div>
            )}
            <div className="token-field">
              <span className="token-field-label">Token</span>
              <div className="token-box">
                <code className="token-value">{data.token}</code>
                <button className="btn token-copy" type="button" onClick={copy}>
                  {copied ? (
                    <>
                      <IconCheck size={15} /> Copied
                    </>
                  ) : (
                    "Copy"
                  )}
                </button>
              </div>
            </div>
          </>
        )}

        {!data && !err && <p className="token-lede">Loading…</p>}

        <a className="token-back" href="/">
          ← Back to the cockpit
        </a>
      </div>
    </div>
  );
}
