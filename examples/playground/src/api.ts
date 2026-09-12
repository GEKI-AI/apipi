import OpenAI from "openai";

export type Agent = {
  id: string;
  name: string | null;
  model: string | null;
  instructions: string | null;
};

export type Session = {
  id: string;
  agent_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type Item = {
  id: string;
  type: string;
  data: Record<string, unknown>;
  created_at: string;
};

export type Artifact = {
  id: string;
  path: string;
  content_type: string;
  created_at: string;
};

export type PublicEvent = {
  id: string;
  type: string;
  seq: number;
  session_id: string;
  data: Record<string, unknown>;
};

const client = new OpenAI({
  apiKey: "proxy",
  baseURL: `${window.location.origin}/v1`,
  dangerouslyAllowBrowser: true,
  maxRetries: 0,
});

type List<T> = { data: T[] };

export const INLINE_AGENT = {
  model: "gpt-4.1",
  instructions: "Write clean code, run it, and report the actual output.",
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${window.location.origin}/v1${path}`, init);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function listAgents(): Promise<Agent[]> {
  const body = await api<List<Agent>>("/agents");
  return body.data ?? [];
}

export async function listSessions(): Promise<Session[]> {
  const body = await api<List<Session>>("/agents/sessions");
  return body.data ?? [];
}

export async function createSession(agentId: string | null): Promise<Session> {
  const body =
    agentId === null
      ? { agent: INLINE_AGENT, environment: { type: "openai_hosted" as const } }
      : {
          agent_id: agentId,
          environment: { type: "openai_hosted" as const },
        };
  return client.post("/agents/sessions", { body }) as unknown as Promise<Session>;
}

export async function deleteSession(sessionId: string): Promise<void> {
  await client.delete(`/agents/sessions/${sessionId}`);
}

export async function getSession(sessionId: string): Promise<Session> {
  return api<Session>(`/agents/sessions/${sessionId}`);
}

export async function listItems(sessionId: string): Promise<Item[]> {
  const body = await api<List<Item>>(`/agents/sessions/${sessionId}/items`);
  return body.data ?? [];
}

export async function listArtifacts(sessionId: string): Promise<Artifact[]> {
  const body = await api<List<Artifact>>(
    `/agents/sessions/${sessionId}/artifacts`,
  );
  return body.data ?? [];
}

export async function listEvents(sessionId: string): Promise<PublicEvent[]> {
  const body = await api<List<PublicEvent>>(
    `/agents/sessions/${sessionId}/events`,
  );
  return body.data ?? [];
}

export async function sendMessage(
  sessionId: string,
  text: string,
): Promise<Session> {
  return api<Session>(`/agents/sessions/${sessionId}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "agent.session.input.message", text }),
  });
}

export function artifactUrl(sessionId: string, artifactId: string): string {
  return `/v1/agents/sessions/${sessionId}/artifacts/${artifactId}/content`;
}

export async function readEventStream(
  sessionId: string,
  afterSeq: number | undefined,
  signal: AbortSignal,
  onEvent: (event: PublicEvent) => void,
): Promise<void> {
  const params = new URLSearchParams({ stream: "true" });
  if (afterSeq !== undefined) {
    params.set("after_seq", String(afterSeq));
  }
  const response = await fetch(
    `/v1/agents/sessions/${sessionId}/events?${params}`,
    { signal },
  );
  if (!response.ok || response.body === null) {
    throw new Error(`event stream failed: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data: ")) {
          continue;
        }
        onEvent(JSON.parse(line.slice(6)) as PublicEvent);
      }
    }
  }
}
