"use client";

import dynamic from "next/dynamic";
import { useState, useCallback, useRef } from "react";
import { startSimulation, runSimulationStream, SSEEvent } from "./api-client";
import MetricsCard from "./components/MetricsCard";
import type {
  Hospital,
  Patient,
  AttemptEvent,
  AggregateMetrics,
} from "./types";

const SimulationMap = dynamic(() => import("./components/SimulationMap"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[400px] bg-gray-100 dark:bg-gray-900 rounded-lg">
      Loading map...
    </div>
  ),
});

type Phase = "idle" | "loading" | "ready" | "running_baseline" | "running_optimized" | "done";

export default function Home() {
  const [numPatients, setNumPatients] = useState(50);
  const [seed, setSeed] = useState(-1); // -1 = auto (random each run)
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  const [hospitals, setHospitals] = useState<Hospital[]>([]);
  const [patients, setPatients] = useState<Patient[]>([]);
  const [scenarioId, setScenarioId] = useState<string>("");

  // Hop events accumulated from SSE, per mode
  const [baselineHops, setBaselineHops] = useState<AttemptEvent[]>([]);
  const [optimizedHops, setOptimizedHops] = useState<AttemptEvent[]>([]);

  // Live counters
  const [baselinePatientsAdmitted, setBaselinePatientsAdmitted] = useState(0);
  const [optimizedPatientsAdmitted, setOptimizedPatientsAdmitted] = useState(0);

  const [baselineMetrics, setBaselineMetrics] = useState<AggregateMetrics | null>(null);
  const [optimizedMetrics, setOptimizedMetrics] = useState<AggregateMetrics | null>(null);

  // Ref to track if we should abort
  const abortRef = useRef(false);

  const handleStart = useCallback(async () => {
    setError(null);
    setPhase("loading");
    setBaselineHops([]);
    setOptimizedHops([]);
    setBaselineMetrics(null);
    setOptimizedMetrics(null);
    setBaselinePatientsAdmitted(0);
    setOptimizedPatientsAdmitted(0);
    abortRef.current = false;

    try {
      const scenario = await startSimulation(numPatients, seed);
      setHospitals(scenario.hospitals);
      setPatients(scenario.patients);
      setScenarioId(scenario.scenario_id);
      setPhase("ready");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to start simulation");
      setPhase("idle");
    }
  }, [numPatients, seed]);

  const handleRun = useCallback(async () => {
    if (!scenarioId) return;
    setError(null);
    setPhase("running_baseline");
    setBaselineHops([]);
    setOptimizedHops([]);
    setBaselineMetrics(null);
    setOptimizedMetrics(null);
    setBaselinePatientsAdmitted(0);
    setOptimizedPatientsAdmitted(0);

    try {
      await runSimulationStream(scenarioId, (event: SSEEvent) => {
        if (abortRef.current) return;

        switch (event.type) {
          case "phase":
            if (event.phase === "baseline_start") {
              setPhase("running_baseline");
            } else if (event.phase === "baseline_done") {
              if (event.metrics) setBaselineMetrics(event.metrics);
            } else if (event.phase === "optimized_start") {
              setPhase("running_optimized");
            } else if (event.phase === "optimized_done") {
              if (event.metrics) setOptimizedMetrics(event.metrics);
            }
            break;

          case "hop":
            if (event.mode === "baseline") {
              setBaselineHops((prev) => [...prev, event as unknown as AttemptEvent]);
            } else {
              setOptimizedHops((prev) => [...prev, event as unknown as AttemptEvent]);
            }
            break;

          case "patient_done":
            if (event.mode === "baseline") {
              setBaselinePatientsAdmitted((n) => n + 1);
            } else {
              setOptimizedPatientsAdmitted((n) => n + 1);
            }
            break;

          case "done":
            setBaselineMetrics(event.baseline_metrics);
            setOptimizedMetrics(event.optimized_metrics);
            setPhase("done");
            break;
        }
      });
      setPhase("done");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Simulation failed");
      setPhase("ready");
    }
  }, [scenarioId]);

  const isRunning = phase === "running_baseline" || phase === "running_optimized";

  return (
    <main className="min-h-screen bg-gray-50 dark:bg-gray-950 p-4">
      <div className="max-w-[1600px] mx-auto">
        <h1 className="text-2xl font-bold text-center mb-1">
          Busan ER Allocation: Baseline vs Quantum-Inspired Optimization
        </h1>
        <p className="text-center text-gray-500 text-sm mb-4">
          Simulated annealing on a QUBO formulation to reduce ambulance refusal hops
        </p>

        {/* Controls */}
        <div className="flex flex-wrap items-center justify-center gap-4 mb-4">
          <label className="flex items-center gap-2 text-sm">
            Patients:
            <input
              type="number"
              min={5}
              max={200}
              value={numPatients}
              onChange={(e) => setNumPatients(Number(e.target.value))}
              className="w-20 border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700"
              disabled={phase === "loading" || isRunning}
            />
          </label>
          <label className="flex items-center gap-2 text-sm" title="Use -1 for random seed each run">
            Seed:
            <input
              type="number"
              value={seed}
              onChange={(e) => setSeed(Number(e.target.value))}
              className="w-24 border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700"
              disabled={phase === "loading" || isRunning}
            />
            <span className="text-xs text-gray-400">(-1 = random)</span>
          </label>
          <button
            onClick={handleStart}
            disabled={phase === "loading" || isRunning}
            className="bg-blue-600 text-white px-4 py-1.5 rounded hover:bg-blue-700 disabled:opacity-50 text-sm"
          >
            {phase === "loading" ? "Fetching data..." : "1. Load Scenario"}
          </button>
          <button
            onClick={handleRun}
            disabled={phase !== "ready" && phase !== "done"}
            className="bg-green-600 text-white px-4 py-1.5 rounded hover:bg-green-700 disabled:opacity-50 text-sm"
          >
            {isRunning ? "Simulating..." : "2. Run Simulation"}
          </button>
        </div>

        {error && (
          <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-2 rounded mb-4 text-center text-sm">
            {error}
          </div>
        )}

        {/* Status bar */}
        {hospitals.length > 0 && (
          <div className="text-center text-xs text-gray-500 mb-2">
            {hospitals.length} hospitals &middot; {patients.length} patients
            {scenarioId && <> &middot; Scenario: {scenarioId}</>}
            {isRunning && (
              <span className="ml-2 text-blue-500 font-medium">
                {phase === "running_baseline"
                  ? `Baseline: ${baselinePatientsAdmitted}/${patients.length} admitted (${baselineHops.length} hops)`
                  : `Optimized: ${optimizedPatientsAdmitted}/${patients.length} admitted (${optimizedHops.length} hops)`}
              </span>
            )}
          </div>
        )}

        {/* Side-by-side maps */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
          <div className="bg-white dark:bg-gray-900 rounded-lg shadow p-3 flex flex-col">
            <SimulationMap
              hospitals={hospitals}
              patients={patients}
              hops={baselineHops}
              label={`Baseline (Nearest Sequential)${baselineHops.length > 0 ? ` — ${baselineHops.length} hops` : ""}`}
            />
          </div>
          <div className="bg-white dark:bg-gray-900 rounded-lg shadow p-3 flex flex-col">
            <SimulationMap
              hospitals={hospitals}
              patients={patients}
              hops={optimizedHops}
              label={`Optimized (QUBO + SA)${optimizedHops.length > 0 ? ` — ${optimizedHops.length} hops` : ""}`}
            />
          </div>
        </div>

        {/* Metrics */}
        {(baselineMetrics || optimizedMetrics) && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {baselineMetrics && <MetricsCard metrics={baselineMetrics} label="Baseline Metrics" />}
            {optimizedMetrics && <MetricsCard metrics={optimizedMetrics} label="Optimized Metrics" />}
          </div>
        )}

        {/* Legend */}
        <div className="mt-4 text-xs text-gray-400 text-center flex flex-wrap justify-center gap-4">
          <span><span className="inline-block w-3 h-3 rounded-full bg-blue-500 mr-1" />Hospital</span>
          <span><span className="inline-block w-3 h-3 rounded-full bg-green-500 mr-1" style={{border: "1.5px solid white"}} />Severity 1</span>
          <span><span className="inline-block w-3 h-3 rounded-full bg-lime-500 mr-1" style={{border: "1.5px solid white"}} />Severity 2</span>
          <span><span className="inline-block w-3 h-3 rounded-full bg-yellow-500 mr-1" style={{border: "1.5px solid white"}} />Severity 3</span>
          <span><span className="inline-block w-3 h-3 rounded-full bg-orange-500 mr-1" style={{border: "1.5px solid white"}} />Severity 4</span>
          <span><span className="inline-block w-3 h-3 rounded-full bg-red-500 mr-1" style={{border: "1.5px solid white"}} />Severity 5</span>
          <span><span className="inline-block w-6 h-0.5 bg-green-500 mr-1 align-middle" />Accepted</span>
          <span><span className="inline-block w-6 h-0.5 bg-red-400 mr-1 align-middle" />Refused</span>
        </div>
      </div>
    </main>
  );
}
