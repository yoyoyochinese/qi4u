import { ScenarioResponse, AttemptEvent, PatientResult, AggregateMetrics } from "./types";

const API_BASE = "http://localhost:8000";

export async function startSimulation(
  numPatients: number,
  seed: number
): Promise<ScenarioResponse> {
  const res = await fetch(`${API_BASE}/api/simulations/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      num_patients: numPatients,
      random_seed: seed,
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Start failed: ${res.status}`);
  }
  return res.json();
}

export type SSEEvent =
  | { type: "phase"; phase: string; metrics?: AggregateMetrics }
  | { type: "hop"; mode: "baseline" | "optimized" } & AttemptEvent
  | { type: "patient_done"; mode: "baseline" | "optimized" } & PatientResult
  | {
      type: "done";
      baseline_metrics: AggregateMetrics;
      optimized_metrics: AggregateMetrics;
    };

/**
 * Stream simulation events via SSE.  Calls `onEvent` for each parsed event.
 * Returns a promise that resolves when the stream ends.
 */
export async function runSimulationStream(
  scenarioId: string,
  onEvent: (event: SSEEvent) => void
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/simulations/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario_id: scenarioId }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Run failed: ${res.status}`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Parse SSE lines:  "data: {...}\n\n"
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";

    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data: ")) continue;
      try {
        const json = JSON.parse(line.slice(6));
        onEvent(json as SSEEvent);
      } catch {
        // skip malformed lines
      }
    }
  }
}
