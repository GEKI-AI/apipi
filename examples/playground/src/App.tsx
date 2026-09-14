import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Agent,
  type Artifact,
  type Item,
  type PublicEvent,
  type Session,
  artifactUrl,
  createAgent,
  createSession,
  deleteSession,
  getSession,
  listAgents,
  listArtifacts,
  listEvents,
  listItems,
  listModels,
  listSessions,
  readEventStream,
  sendMessage,
} from "./api";

type ChatLine =
  | { kind: "message"; role: string; text: string; key: string }
  | { kind: "activity"; title: string; detail: string; key: string };

function itemLines(items: Item[]): ChatLine[] {
  const lines: ChatLine[] = [];
  for (const item of items) {
    if (item.type === "message") {
      const role = String(item.data.role ?? "assistant");
      const text = String(item.data.content ?? item.data.text ?? "");
      lines.push({ kind: "message", role, text, key: item.id });
      continue;
    }
    const name = String(item.data.name ?? item.type);
    lines.push({
      kind: "activity",
      title: item.type,
      detail: name,
      key: item.id,
    });
  }
  return lines;
}

function shortId(id: string): string {
  return id.slice(0, 8);
}

function eventLine(event: PublicEvent): ChatLine | null {
  if (event.type !== "agent.session.turn.item.added") {
    return null;
  }
  const itemType = String(event.data.item_type ?? "item");
  if (itemType === "message") {
    return null;
  }
  const name = String(event.data.name ?? itemType);
  return {
    kind: "activity",
    title: itemType,
    detail: name,
    key: `evt-${event.seq}`,
  };
}

export function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [agentId, setAgentId] = useState("");
  const [agentName, setAgentName] = useState("");
  const [agentModel, setAgentModel] = useState("");
  const [agentInstructions, setAgentInstructions] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState("idle");
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [streamText, setStreamText] = useState("");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const afterSeq = useRef(0);

  const refreshSessions = useCallback(async () => {
    setSessions(await listSessions());
  }, []);

  const loadSession = useCallback(async (sessionId: string) => {
    const [session, items, files, events] = await Promise.all([
      getSession(sessionId),
      listItems(sessionId),
      listArtifacts(sessionId),
      listEvents(sessionId),
    ]);
    setStatus(session.status);
    setLines(itemLines(items));
    setArtifacts(files);
    setStreamText("");
    afterSeq.current = events.reduce(
      (max, event) => Math.max(max, event.seq),
      0,
    );
  }, []);

  useEffect(() => {
    let ignore = false;
    void (async () => {
      try {
        const [saved, existing, ids] = await Promise.all([
          listAgents(),
          listSessions(),
          listModels(),
        ]);
        if (ignore) {
          return;
        }
        setAgents(saved);
        setSessions(existing);
        setModels(ids);
        if (saved.length > 0) {
          setAgentId(saved[0].id);
        }
        if (ids.length > 0) {
          setAgentModel(ids[0]);
        }
      } catch (err) {
        if (!ignore) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      return;
    }
    const sessionId = selectedId;
    const abort = new AbortController();
    void (async () => {
      try {
        await loadSession(sessionId);
        await readEventStream(
          sessionId,
          afterSeq.current,
          abort.signal,
          (event) => {
            afterSeq.current = Math.max(afterSeq.current, event.seq);
            if (event.type === "agent.session.in_progress") {
              setStatus("in_progress");
            }
            if (event.type === "agent.session.idle") {
              setStatus("idle");
              void listArtifacts(sessionId).then(setArtifacts);
            }
            if (event.type === "agent.session.failed") {
              setStatus("failed");
            }
            if (event.type === "agent.session.requires_action") {
              setStatus("requires_action");
            }
            if (event.type === "agent.session.turn.output_text.delta") {
              const delta = event.data.delta;
              if (typeof delta === "string") {
                setStreamText((text) => text + delta);
              }
            }
            if (event.type === "agent.session.turn.output_text.done") {
              const text = event.data.text;
              if (typeof text === "string") {
                setLines((current) => [
                  ...current,
                  {
                    kind: "message",
                    role: "assistant",
                    text,
                    key: `done-${event.seq}`,
                  },
                ]);
              }
              setStreamText("");
            }
            const extra = eventLine(event);
            if (extra !== null) {
              setLines((current) => [...current, extra]);
            }
          },
        );
      } catch (err) {
        if (!abort.signal.aborted) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => abort.abort();
  }, [selectedId, loadSession]);

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight);
  }, [lines, streamText]);

  async function onCreateAgent(): Promise<void> {
    if (agentModel === "") {
      setError("Pick a model before creating an agent.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const name = agentName.trim();
      const agent = await createAgent({
        name: name === "" ? undefined : name,
        model: agentModel,
        instructions: agentInstructions,
      });
      const saved = await listAgents();
      setAgents(saved);
      setAgentId(agent.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onNewSession(): Promise<void> {
    if (agentId === "") {
      setError("Create an agent before starting a session.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const session = await createSession(agentId);
      await refreshSessions();
      setSelectedId(session.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(): Promise<void> {
    if (selectedId === null) {
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const id = selectedId;
      await deleteSession(id);
      setSelectedId(null);
      setLines([]);
      setArtifacts([]);
      setStatus("idle");
      await refreshSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onSend(): Promise<void> {
    const text = draft.trim();
    if (selectedId === null || text === "" || status === "in_progress") {
      return;
    }
    setDraft("");
    setError(null);
    setBusy(true);
    setLines((current) => [
      ...current,
      { kind: "message", role: "user", text, key: `user-${Date.now()}` },
    ]);
    try {
      await sendMessage(selectedId, text);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="top">
        <h1>ApiPi playground</h1>
        <p className="hint">
          Local demo. The Vite proxy sends <code>API_KEY</code> to the gateway.
          The browser never sees it.
        </p>
      </header>
      {error !== null ? (
        <p className="banner" role="alert">
          {error}
        </p>
      ) : null}
      <div className="panes">
        <aside className="pane sessions">
          <div className="pane-head">
            <h2>Sessions</h2>
            <button
              type="button"
              data-testid="new-session"
              onClick={() => void onNewSession()}
              disabled={busy || agentId === ""}
            >
              New
            </button>
          </div>
          <form
            className="agent-form"
            onSubmit={(event) => {
              event.preventDefault();
              void onCreateAgent();
            }}
          >
            <label className="field">
              Name
              <input
                data-testid="agent-name"
                value={agentName}
                onChange={(event) => setAgentName(event.target.value)}
                placeholder="optional"
              />
            </label>
            <label className="field">
              Model
              <select
                data-testid="model-select"
                value={agentModel}
                onChange={(event) => setAgentModel(event.target.value)}
                disabled={models.length === 0}
              >
                {models.length === 0 ? (
                  <option value="">No models</option>
                ) : null}
                {models.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Instructions
              <textarea
                data-testid="agent-instructions"
                value={agentInstructions}
                onChange={(event) => setAgentInstructions(event.target.value)}
                rows={3}
                placeholder="How the agent should work"
              />
            </label>
            <div className="field">
              <button
                type="submit"
                data-testid="create-agent"
                disabled={busy || agentModel === ""}
              >
                Create agent
              </button>
            </div>
          </form>
          <label className="field">
            Agent
            <select
              data-testid="agent-select"
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
            >
              <option value="">Select an agent</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name ?? shortId(agent.id)}
                </option>
              ))}
            </select>
          </label>
          <ul data-testid="session-list">
            {sessions.map((session) => (
              <li key={session.id}>
                <button
                  type="button"
                  className={session.id === selectedId ? "active" : ""}
                  data-testid={`session-${session.id}`}
                  onClick={() => {
                    if (session.id === selectedId) {
                      void loadSession(session.id);
                      return;
                    }
                    setSelectedId(session.id);
                  }}
                >
                  <span className="id">{shortId(session.id)}</span>
                  <span className="meta">{session.status}</span>
                </button>
              </li>
            ))}
          </ul>
          {selectedId !== null ? (
            <button
              type="button"
              className="danger"
              data-testid="delete-session"
              onClick={() => void onDelete()}
              disabled={busy}
            >
              Delete session
            </button>
          ) : null}
        </aside>
        <section className="pane chat">
          <div className="pane-head">
            <h2>Turn</h2>
            <span className="status" data-testid="session-status">
              {selectedId === null ? "no session" : status}
            </span>
          </div>
          <div className="log" ref={logRef} data-testid="chat">
            {selectedId === null ? (
              <p className="empty">Create a session to take a turn.</p>
            ) : null}
            {lines.map((line) =>
              line.kind === "message" ? (
                <article
                  key={line.key}
                  className={`bubble ${line.role}`}
                >
                  <span className="role">{line.role}</span>
                  <pre>{line.text}</pre>
                </article>
              ) : (
                <article key={line.key} className="activity">
                  <span className="role">{line.title}</span>
                  <code>{line.detail}</code>
                </article>
              ),
            )}
            {streamText !== "" ? (
              <article className="bubble assistant streaming">
                <span className="role">assistant</span>
                <pre>{streamText}</pre>
              </article>
            ) : null}
          </div>
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void onSend();
            }}
          >
            <textarea
              data-testid="composer"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void onSend();
                }
              }}
              placeholder="Send a follow-up…"
              disabled={selectedId === null || status === "in_progress"}
              rows={3}
            />
            <button
              type="submit"
              data-testid="send"
              disabled={
                selectedId === null ||
                busy ||
                status === "in_progress" ||
                draft.trim() === ""
              }
            >
              Send
            </button>
          </form>
        </section>
        <aside className="pane files">
          <div className="pane-head">
            <h2>Artifacts</h2>
          </div>
          <p className="hint">
            Files under <code>artifacts/</code> on the computer show up after
            Pi stops (idle TTL).
          </p>
          <ul data-testid="artifact-list">
            {artifacts.map((file) => (
              <li key={file.id}>
                <a
                  href={artifactUrl(selectedId ?? "", file.id)}
                  download
                >
                  {file.path}
                </a>
              </li>
            ))}
          </ul>
          {selectedId !== null && artifacts.length === 0 ? (
            <p className="empty">None published yet.</p>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
