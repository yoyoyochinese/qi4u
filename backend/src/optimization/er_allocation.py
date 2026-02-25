"""QUBO-based ER allocation using jijmodeling v2 + OpenJij SA.

Assigns multiple patients to hospitals by solving a QUBO with:
  1. One-hot constraint: each patient -> exactly one hospital
  2. Capacity avoidance: penalize hospitals near/at max capacity
  3. Distance: prefer closer hospitals, weighted by severity
  4. Congestion: penalize assigning many patients to the same hospital

Scalability: Each patient only considers top-K nearest candidate hospitals
that are not fully saturated. Default K=8, which keeps the binary variable
count manageable (num_patients * K) while covering enough options.
"""

import math

import jijmodeling as jm
import numpy as np
from ommx_openjij_adapter import OMMXOpenJijSAAdapter

from src.models.hospital import Hospital
from src.models.patient import Patient

# Top-K candidates per patient (balances runtime vs quality)
DEFAULT_K = 8

# QUBO weight constants (capacity >> distance to enforce priority)
W_CAPACITY = 50.0
W_DISTANCE = 5.0
W_CONGESTION = 20.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Approximate distance in km between two lat/lng points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _select_candidates(
    patient: Patient,
    hospitals: list[Hospital],
    k: int,
    occupancy: dict[str, int],
) -> list[int]:
    """Select top-k hospital indices by distance, excluding fully saturated ones."""
    scored: list[tuple[float, int]] = []
    for idx, h in enumerate(hospitals):
        occ = occupancy.get(h.hospital_id, h.current_occupancy)
        if occ >= h.max_capacity:
            continue
        dist = _haversine_km(patient.lat, patient.lng, h.lat, h.lng)
        scored.append((dist, idx))
    scored.sort()
    return [idx for _, idx in scored[:k]]


# jijmodeling v2 requires the @Problem.define decorator at module level
# (it uses inspect.getsource internally).
@jm.Problem.define("ER_Allocation")
def _er_problem(p: jm.DecoratedProblem):
    """Define the QUBO for ER patient-hospital allocation.

    Variables:
      x[i, j] = 1 if patient i assigned to candidate j

    Cost matrices cap_cost, dist_cost, cong_cost encode the per-assignment
    penalties; dimensions are inferred from cap_cost shape.
    """
    cap_cost = p.Float("cap_cost", ndim=2)
    P = cap_cost.len_at(0, latex="P")
    K = cap_cost.len_at(1, latex="K")

    dist_cost = p.Float("dist_cost", ndim=2)
    cong_cost = p.Float("cong_cost", ndim=2)

    x = p.BinaryVar("x", shape=(P, K))

    # Hard constraint: each patient assigned to exactly one candidate
    p += p.Constraint("one_hospital_per_patient", jm.sum(x, axis=1) == 1)

    # Objective: minimize total cost (capacity + distance + congestion)
    p += jm.sum(cap_cost * x) + jm.sum(dist_cost * x) + jm.sum(cong_cost * x)


def solve_allocation(
    patients: list[Patient],
    hospitals: list[Hospital],
    occupancy: dict[str, int],
    k: int = DEFAULT_K,
    num_reads: int = 50,
) -> dict[int, str]:
    """Solve the ER allocation problem using simulated annealing.

    Returns: {patient_id: hospital_id} mapping.
    """
    if not patients:
        return {}

    n_patients = len(patients)

    # Build candidate lists per patient
    candidates: list[list[int]] = []
    for pat in patients:
        cands = _select_candidates(pat, hospitals, k, occupancy)
        if not cands:
            dists = [
                (_haversine_km(pat.lat, pat.lng, h.lat, h.lng), idx)
                for idx, h in enumerate(hospitals)
            ]
            dists.sort()
            cands = [idx for _, idx in dists[:k]]
        candidates.append(cands)

    # Pad candidates to uniform length
    max_k = max(len(c) for c in candidates)
    actual_k = max(max_k, 1)
    for i, cands in enumerate(candidates):
        while len(cands) < actual_k:
            cands.append(cands[-1])
        candidates[i] = cands[:actual_k]

    # Build cost matrices
    cap_cost = np.zeros((n_patients, actual_k))
    dist_cost = np.zeros((n_patients, actual_k))
    cong_cost = np.zeros((n_patients, actual_k))

    # Count how many patients have each hospital as a candidate
    hospital_demand: dict[int, int] = {}
    for cands in candidates:
        for h_idx in cands:
            hospital_demand[h_idx] = hospital_demand.get(h_idx, 0) + 1

    for p_idx, pat in enumerate(patients):
        for k_idx, h_idx in enumerate(candidates[p_idx]):
            h = hospitals[h_idx]
            occ = occupancy.get(h.hospital_id, h.current_occupancy)
            ratio = occ / max(h.max_capacity, 1)

            # Capacity cost: sharp penalty as occupancy ratio -> 1
            if ratio >= 1.0:
                cap_cost[p_idx, k_idx] = W_CAPACITY * 10.0
            else:
                cap_cost[p_idx, k_idx] = W_CAPACITY * (ratio ** 4) * 5.0

            # Distance cost: scaled by severity (higher severity = prefer closer)
            dist_km = _haversine_km(pat.lat, pat.lng, h.lat, h.lng)
            severity_weight = 0.5 + 0.5 * (pat.severity / 5.0)
            dist_cost[p_idx, k_idx] = W_DISTANCE * dist_km * severity_weight

            # Congestion cost: demand for this hospital relative to remaining beds
            demand = hospital_demand.get(h_idx, 0)
            remaining = max(h.max_capacity - occ, 1)
            cong_cost[p_idx, k_idx] = W_CONGESTION * (demand / remaining) * ratio

    # Build instance and solve
    instance_data = {
        "cap_cost": cap_cost.tolist(),
        "dist_cost": dist_cost.tolist(),
        "cong_cost": cong_cost.tolist(),
    }

    instance = _er_problem.eval(instance_data)

    result = OMMXOpenJijSAAdapter.sample(
        instance, num_reads=num_reads, uniform_penalty_weight=100.0
    )

    # Extract assignment from the best feasible solution
    try:
        best_sol = result.best_feasible_unrelaxed
    except RuntimeError:
        try:
            best_sol = result.best_feasible
        except RuntimeError:
            # No feasible solution: fall back to nearest candidate
            return {
                patients[p_idx].patient_id: hospitals[candidates[p_idx][0]].hospital_id
                for p_idx in range(n_patients)
            }

    # Read assignments from decision variable subscripts
    assignment: dict[int, str] = {}
    for dv in best_sol.decision_variables:
        if dv.name == "x" and dv.value == 1.0:
            p_i, k_i = dv.subscripts[0], dv.subscripts[1]
            h_idx = candidates[p_i][k_i]
            assignment[patients[p_i].patient_id] = hospitals[h_idx].hospital_id

    # Fill unassigned patients with their nearest candidate
    for p_idx, pat in enumerate(patients):
        if pat.patient_id not in assignment:
            h_idx = candidates[p_idx][0]
            assignment[pat.patient_id] = hospitals[h_idx].hospital_id

    return assignment
