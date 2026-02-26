import json
import time
from collections.abc import Generator
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from src.config import get_service_key
from src.models.simulation import (
    AttemptEvent,
    PatientResult,
    SimulationStartRequest,
    SimulationStartResponse,
    SimulationRunRequest,
)
import httpx

from src.services.hospital_data import (
    fetch_busan_hospitals,
    generate_fallback_hospitals,
    reset_cache,
)
from src.services.simulation import (
    generate_patients,
    create_scenario,
    get_scenario,
    run_baseline_stream,
    run_optimized_stream,
    compute_metrics,
)

router = APIRouter(prefix="/api/simulations")


@router.post("/start", response_model=SimulationStartResponse)
async def start_simulation(
    req: SimulationStartRequest,
    x_data_go_kr_key: Optional[str] = Header(None),
):
    """Fetch hospital data and generate patients for a new simulation scenario.

    If random_seed <= 0, a time-based seed is used so each run is different.
    """
    reset_cache()

    try:
        service_key = get_service_key(x_data_go_kr_key)
        hospitals = await fetch_busan_hospitals(service_key)
        print(f"Fetched {len(hospitals)} hospitals from API.")
    except (ValueError, RuntimeError, httpx.HTTPError):
        # No API key or API call failed — use built-in Busan hospital data
        pring("Warning: Using fallback hospital data due to missing/invalid API key or fetch error.")
        hospitals = generate_fallback_hospitals()

    # Auto-generate seed when <= 0
    seed = req.random_seed if req.random_seed > 0 else int(time.time() * 1000) % (2**31)

    patients = generate_patients(
        num_patients=req.num_patients,
        seed=seed,
        severity_weights=req.severity_weights,
    )

    scenario_id = create_scenario(hospitals, patients, seed)
    scenario = get_scenario(scenario_id)

    return SimulationStartResponse(
        scenario_id=scenario_id,
        hospitals=scenario["hospitals"],
        patients=patients,
    )


def _sse_event(data: dict) -> str:
    """Format a single SSE event."""
    return f"data: {json.dumps(data)}\n\n"


def _stream_simulation(scenario_id: str) -> Generator[str, None, None]:
    """Generator that runs both simulations and yields SSE events."""
    scenario = get_scenario(scenario_id)
    hospitals = scenario["hospitals"]
    patients = scenario["patients"]
    seed = scenario["seed"]

    # --- Baseline ---
    yield _sse_event({"type": "phase", "phase": "baseline_start"})

    baseline_results: list[PatientResult] = []
    for item in run_baseline_stream(patients, hospitals, seed):
        if isinstance(item, AttemptEvent):
            yield _sse_event({"type": "hop", "mode": "baseline", **item.model_dump()})
        elif isinstance(item, PatientResult):
            baseline_results.append(item)
            yield _sse_event({"type": "patient_done", "mode": "baseline", **item.model_dump()})

    baseline_metrics = compute_metrics(baseline_results)
    yield _sse_event({
        "type": "phase",
        "phase": "baseline_done",
        "metrics": baseline_metrics.model_dump(),
    })

    # --- Optimized ---
    yield _sse_event({"type": "phase", "phase": "optimized_start"})

    optimized_results: list[PatientResult] = []
    for item in run_optimized_stream(patients, hospitals, seed):
        if isinstance(item, AttemptEvent):
            yield _sse_event({"type": "hop", "mode": "optimized", **item.model_dump()})
        elif isinstance(item, PatientResult):
            optimized_results.append(item)
            yield _sse_event({"type": "patient_done", "mode": "optimized", **item.model_dump()})

    optimized_metrics = compute_metrics(optimized_results)
    yield _sse_event({
        "type": "phase",
        "phase": "optimized_done",
        "metrics": optimized_metrics.model_dump(),
    })

    # Final done event
    yield _sse_event({
        "type": "done",
        "baseline_metrics": baseline_metrics.model_dump(),
        "optimized_metrics": optimized_metrics.model_dump(),
    })


@router.post("/run")
async def run_simulation(req: SimulationRunRequest):
    """Run both simulations, streaming hop events via SSE."""
    try:
        get_scenario(req.scenario_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return StreamingResponse(
        _stream_simulation(req.scenario_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
