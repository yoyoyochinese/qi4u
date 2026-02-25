"""Simulation engine for baseline vs optimized ER allocation.

Admission probability model:
  P(accept) = (1 - ratio^3) * (0.6 + 0.4 * severity/5)
  where ratio = current_occupancy / max_capacity

  Capacity pressure dominates: even severity-5 patients get refused
  at high occupancy.

Time model:
  time_per_hop = BASE_TIME_MIN + DISTANCE_FACTOR * distance_km
  BASE_TIME_MIN = 3.0  (minutes: dispatch overhead, handoff)
  DISTANCE_FACTOR = 1.5  (min/km: ~40 km/h average ambulance speed)

Both baseline and optimized loops guarantee every patient is eventually
admitted.  Baseline retries nearest-to-current-location.  Optimized
re-anneals refused patients with updated occupancy each round.
"""

import random
import uuid
from collections.abc import Generator

from src.models.hospital import Hospital
from src.models.patient import Patient
from src.models.simulation import (
    AggregateMetrics,
    AttemptEvent,
    PatientResult,
)
from src.optimization.er_allocation import solve_allocation, _haversine_km

BASE_TIME_MIN = 3.0
DISTANCE_FACTOR = 1.5
MAX_HOPS_PER_PATIENT = 60  # safety valve

# Busan city center coordinates for patient generation
BUSAN_CENTER_LAT = 35.1796
BUSAN_CENTER_LNG = 129.0756
PATIENT_SPREAD_LAT = 0.06  # ~6.6 km spread
PATIENT_SPREAD_LNG = 0.07

# In-memory scenario store
_scenarios: dict[str, dict] = {}


def _admission_probability(
    occupancy: int, max_capacity: int, severity: int
) -> float:
    if max_capacity <= 0 or occupancy >= max_capacity:
        return 0.0
    ratio = occupancy / max_capacity
    capacity_factor = 1.0 - ratio ** 3
    severity_factor = 0.6 + 0.4 * (severity / 5.0)
    return max(0.0, min(1.0, capacity_factor * severity_factor))


def _hop_time(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dist = _haversine_km(lat1, lng1, lat2, lng2)
    return BASE_TIME_MIN + DISTANCE_FACTOR * dist


def generate_patients(
    num_patients: int,
    seed: int,
    severity_weights: list[float],
) -> list[Patient]:
    """Generate patients with positions biased toward Busan city center."""
    rng = random.Random(seed)
    patients: list[Patient] = []
    for i in range(num_patients):
        lat = rng.gauss(BUSAN_CENTER_LAT, PATIENT_SPREAD_LAT)
        lng = rng.gauss(BUSAN_CENTER_LNG, PATIENT_SPREAD_LNG)
        severity = rng.choices([1, 2, 3, 4, 5], weights=severity_weights, k=1)[0]
        patients.append(Patient(patient_id=i, lat=lat, lng=lng, severity=severity))
    return patients


def _apply_proximity_congestion(
    hospitals: list[Hospital],
    patients: list[Patient],
) -> list[Hospital]:
    """Inflate occupancy for hospitals near the patient cluster center.

    Near zone (<=5 km): 95-100% occupancy.
    Mid zone (5-10 km): 85-95% occupancy.
    Far zone (>10 km): keep original occupancy.
    """
    if not patients:
        return hospitals

    c_lat = sum(p.lat for p in patients) / len(patients)
    c_lng = sum(p.lng for p in patients) / len(patients)

    NEAR_KM = 5.0
    MID_KM = 10.0

    adjusted: list[Hospital] = []
    for h in hospitals:
        dist = _haversine_km(c_lat, c_lng, h.lat, h.lng)

        if dist <= NEAR_KM:
            target_ratio = 0.95 + 0.05 * (1.0 - dist / NEAR_KM)
        elif dist <= MID_KM:
            t = (dist - NEAR_KM) / (MID_KM - NEAR_KM)
            target_ratio = 0.95 - 0.10 * t
        else:
            adjusted.append(h)
            continue

        new_occ = min(
            h.max_capacity,
            max(h.current_occupancy, round(h.max_capacity * target_ratio)),
        )
        adjusted.append(h.model_copy(update={"current_occupancy": new_occ}))

    return adjusted


def create_scenario(
    hospitals: list[Hospital],
    patients: list[Patient],
    seed: int,
) -> str:
    """Store a scenario and return its ID.

    Applies proximity-based congestion: hospitals near the patient cluster
    are inflated to near-100% occupancy.
    """
    hospitals = _apply_proximity_congestion(hospitals, patients)

    scenario_id = str(uuid.uuid4())[:8]
    _scenarios[scenario_id] = {
        "hospitals": hospitals,
        "patients": patients,
        "seed": seed,
    }
    return scenario_id


def get_scenario(scenario_id: str) -> dict:
    if scenario_id not in _scenarios:
        raise ValueError(f"Scenario {scenario_id} not found")
    return _scenarios[scenario_id]


# ---------------------------------------------------------------------------
# Generators that yield one hop at a time for SSE streaming
# ---------------------------------------------------------------------------

def run_baseline_stream(
    patients: list[Patient],
    hospitals: list[Hospital],
    seed: int,
) -> Generator[AttemptEvent | PatientResult, None, None]:
    """Baseline: each patient tries the nearest hospital to their CURRENT
    location.  If refused, they move there and try the next nearest.
    Repeats until admitted (probabilistic, each try is independent).

    Yields AttemptEvent for each hop, then PatientResult when patient is done.
    """
    rng = random.Random(seed + 1000)
    occupancy: dict[str, int] = {h.hospital_id: h.current_occupancy for h in hospitals}

    for pat in patients:
        cur_lat, cur_lng = pat.lat, pat.lng
        attempts: list[AttemptEvent] = []
        elapsed = 0.0
        accepted_hospital: Hospital | None = None
        # Track recently refused hospitals to avoid bouncing between two
        recent_refused: list[str] = []

        while len(attempts) < MAX_HOPS_PER_PATIENT:
            # Sort hospitals by distance from current location
            sorted_hosps = sorted(
                hospitals,
                key=lambda h: _haversine_km(cur_lat, cur_lng, h.lat, h.lng),
            )

            # Pick the nearest hospital not recently refused.
            # After trying all, allow revisiting (random acceptance can succeed).
            recent_set = set(recent_refused[-len(hospitals):])
            target = None
            for h in sorted_hosps:
                if h.hospital_id not in recent_set:
                    target = h
                    break
            if target is None:
                # All hospitals tried recently — clear history and try nearest
                recent_refused.clear()
                target = sorted_hosps[0]

            travel = _hop_time(cur_lat, cur_lng, target.lat, target.lng)
            elapsed += travel

            occ = occupancy.get(target.hospital_id, 0)
            prob = _admission_probability(occ, target.max_capacity, pat.severity)
            accepted = rng.random() < prob

            event = AttemptEvent(
                patient_id=pat.patient_id,
                hospital_id=target.hospital_id,
                hospital_name=target.hospital_name,
                hop_number=len(attempts) + 1,
                accepted=accepted,
                elapsed_time=round(elapsed, 2),
                lat=target.lat,
                lng=target.lng,
            )
            attempts.append(event)
            yield event  # stream this hop immediately

            if accepted:
                occupancy[target.hospital_id] = occ + 1
                accepted_hospital = target
                break

            # Move to the hospital that refused us
            cur_lat, cur_lng = target.lat, target.lng
            recent_refused.append(target.hospital_id)

        # Safety: force-admit to least-full hospital if max hops exceeded
        if accepted_hospital is None:
            least_full = min(
                hospitals,
                key=lambda h: occupancy.get(h.hospital_id, 0) / max(h.max_capacity, 1),
            )
            occupancy[least_full.hospital_id] = occupancy.get(least_full.hospital_id, 0) + 1
            accepted_hospital = least_full

        yield PatientResult(
            patient_id=pat.patient_id,
            severity=pat.severity,
            assigned_hospital_id=accepted_hospital.hospital_id,
            assigned_hospital_name=accepted_hospital.hospital_name,
            hops=len(attempts),
            elapsed_time=round(elapsed, 2),
            attempts=attempts,
        )


def run_optimized_stream(
    patients: list[Patient],
    hospitals: list[Hospital],
    seed: int,
) -> Generator[AttemptEvent | PatientResult, None, None]:
    """Optimized: run SA on all unplaced patients, simulate admissions,
    re-anneal refused patients with updated occupancy.  Repeat until all
    patients are admitted.

    Yields AttemptEvent for each hop, then PatientResult when patient is done.
    """
    rng = random.Random(seed + 2000)
    occupancy: dict[str, int] = {h.hospital_id: h.current_occupancy for h in hospitals}
    hosp_map: dict[str, Hospital] = {h.hospital_id: h for h in hospitals}

    # Track per-patient state across rounds
    pending = list(patients)  # patients not yet admitted
    patient_state: dict[int, dict] = {
        p.patient_id: {"attempts": [], "elapsed": 0.0, "lat": p.lat, "lng": p.lng}
        for p in patients
    }
    results: list[PatientResult] = []

    round_num = 0
    max_rounds = 10

    while pending and round_num < max_rounds:
        round_num += 1

        # Run SA for this batch
        assignment = solve_allocation(pending, hospitals, occupancy)

        still_pending: list[Patient] = []

        for pat in pending:
            st = patient_state[pat.patient_id]
            target_hid = assignment.get(pat.patient_id)

            if target_hid is None or target_hid not in hosp_map:
                still_pending.append(pat)
                continue

            h = hosp_map[target_hid]
            travel = _hop_time(st["lat"], st["lng"], h.lat, h.lng)
            st["elapsed"] += travel

            occ = occupancy.get(h.hospital_id, 0)
            prob = _admission_probability(occ, h.max_capacity, pat.severity)
            accepted = rng.random() < prob

            event = AttemptEvent(
                patient_id=pat.patient_id,
                hospital_id=h.hospital_id,
                hospital_name=h.hospital_name,
                hop_number=len(st["attempts"]) + 1,
                accepted=accepted,
                elapsed_time=round(st["elapsed"], 2),
                lat=h.lat,
                lng=h.lng,
            )
            st["attempts"].append(event)
            yield event

            if accepted:
                occupancy[h.hospital_id] = occ + 1
                results.append(PatientResult(
                    patient_id=pat.patient_id,
                    severity=pat.severity,
                    assigned_hospital_id=h.hospital_id,
                    assigned_hospital_name=h.hospital_name,
                    hops=len(st["attempts"]),
                    elapsed_time=round(st["elapsed"], 2),
                    attempts=list(st["attempts"]),
                ))
                yield results[-1]
            else:
                st["lat"], st["lng"] = h.lat, h.lng
                still_pending.append(pat)

        pending = still_pending

    # Force-admit any remaining patients to least-full hospital
    for pat in pending:
        st = patient_state[pat.patient_id]
        least_full = min(
            hospitals,
            key=lambda h: occupancy.get(h.hospital_id, 0) / max(h.max_capacity, 1),
        )
        travel = _hop_time(st["lat"], st["lng"], least_full.lat, least_full.lng)
        st["elapsed"] += travel
        occupancy[least_full.hospital_id] = occupancy.get(least_full.hospital_id, 0) + 1

        event = AttemptEvent(
            patient_id=pat.patient_id,
            hospital_id=least_full.hospital_id,
            hospital_name=least_full.hospital_name,
            hop_number=len(st["attempts"]) + 1,
            accepted=True,
            elapsed_time=round(st["elapsed"], 2),
            lat=least_full.lat,
            lng=least_full.lng,
        )
        st["attempts"].append(event)
        yield event

        results.append(PatientResult(
            patient_id=pat.patient_id,
            severity=pat.severity,
            assigned_hospital_id=least_full.hospital_id,
            assigned_hospital_name=least_full.hospital_name,
            hops=len(st["attempts"]),
            elapsed_time=round(st["elapsed"], 2),
            attempts=list(st["attempts"]),
        ))
        yield results[-1]

    # Yield results sorted by patient_id (already emitted individually above)


def compute_metrics(results: list[PatientResult]) -> AggregateMetrics:
    if not results:
        return AggregateMetrics(
            avg_hops=0, min_hops=0, max_hops=0,
            avg_time=0, min_time=0, max_time=0,
            total_patients=0, accepted_patients=0,
        )
    hops = [r.hops for r in results]
    times = [r.elapsed_time for r in results]
    accepted = sum(1 for r in results if r.assigned_hospital_id is not None)
    return AggregateMetrics(
        avg_hops=round(sum(hops) / len(hops), 2),
        min_hops=min(hops),
        max_hops=max(hops),
        avg_time=round(sum(times) / len(times), 2),
        min_time=round(min(times), 2),
        max_time=round(max(times), 2),
        total_patients=len(results),
        accepted_patients=accepted,
    )
