"""QUBO-based ER allocation using jijmodeling + OpenJij SA.

Assigns multiple patients to hospitals by solving a QUBO with:
  1. One-hot constraint: each patient gets exactly one candidate
  2. Capacity avoidance: near/full hospitals are strongly penalized
  3. Distance: higher severity prefers closer hospitals
  4. Congestion: projected contention is penalized

Scalability: each patient considers top-K candidates (default K=10).
"""

import math
from collections import defaultdict

import jijmodeling as jm
import numpy as np
from ommx.v1 import Instance
from ommx_openjij_adapter import OMMXOpenJijSAAdapter

from src.models.hospital import Hospital
from src.models.patient import Patient

DEFAULT_K = 10

# Keep capacity pressure dominant over distance.
W_CAPACITY = 160.0
W_DISTANCE = 4.0
W_CONGESTION = 35.0
W_INVALID_SLOT = 5000.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Approximate distance in km between two lat/lng points."""
    earth_radius_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _select_candidates(
    patient: Patient,
    hospitals: list[Hospital],
    k: int,
    occupancy: dict[str, int],
    refused_hospital_ids: set[str],
) -> list[int]:
    """Pick top-K hospitals by capacity-aware score.

    Already-refused hospitals for this patient are heavily deprioritized,
    but can still be used if options are exhausted.
    """
    scored: list[tuple[float, int]] = []
    for idx, hosp in enumerate(hospitals):
        occ = occupancy.get(hosp.hospital_id, hosp.current_occupancy)
        ratio = occ / max(hosp.max_capacity, 1)
        dist = _haversine_km(patient.lat, patient.lng, hosp.lat, hosp.lng)

        refusal_bias = 100.0 if hosp.hospital_id in refused_hospital_ids else 0.0
        full_bias = 25.0 if ratio >= 1.0 else 0.0
        score = dist * (1.0 + 4.0 * (ratio**3)) + refusal_bias + full_bias
        scored.append((score, idx))

    scored.sort(key=lambda t: t[0])
    return [idx for _, idx in scored[: max(1, k)]]


def _capacity_penalty(occ: int, max_capacity: int) -> float:
    """Strongly penalize high occupancy and effectively forbid full hospitals."""
    ratio = occ / max(max_capacity, 1)
    if ratio >= 1.0:
        return W_CAPACITY * 50.0
    if ratio >= 0.95:
        return W_CAPACITY * 18.0
    if ratio >= 0.90:
        return W_CAPACITY * 10.0
    if ratio >= 0.80:
        return W_CAPACITY * 5.0
    return W_CAPACITY * (ratio**3)


@jm.Problem.define("ER_Allocation")
def _er_problem(p: jm.DecoratedProblem):
    """Define QUBO for patient-hospital assignment over candidate slots."""
    cap_cost = p.Float("cap_cost", ndim=2)
    dist_cost = p.Float("dist_cost", ndim=2)
    cong_cost = p.Float("cong_cost", ndim=2)
    valid_mask = p.Float("valid_mask", ndim=2)

    P = cap_cost.len_at(0)
    K = cap_cost.len_at(1)
    x = p.BinaryVar("x", shape=(P, K))

    p += p.Constraint("one_hospital_per_patient", jm.sum(x, axis=1) == 1)
    p += (
        jm.sum(cap_cost * x)
        + jm.sum(dist_cost * x)
        + jm.sum(cong_cost * x)
        + W_INVALID_SLOT * jm.sum((1.0 - valid_mask) * x)
    )


def _build_candidate_matrix(
    patients: list[Patient],
    hospitals: list[Hospital],
    occupancy: dict[str, int],
    k: int,
    refused_pairs: set[tuple[int, str]],
) -> list[list[int]]:
    """Build fixed-width candidate matrix using -1 for unused slots."""
    matrix: list[list[int]] = []
    for pat in patients:
        refused = {hid for pid, hid in refused_pairs if pid == pat.patient_id}
        cands = _select_candidates(pat, hospitals, k, occupancy, refused)
        row = cands[:k]
        while len(row) < k:
            row.append(-1)
        matrix.append(row)
    return matrix


def _repair_overflow_assignments(
    assignment_indices: dict[int, int],
    patients: list[Patient],
    hospitals: list[Hospital],
    candidates: list[list[int]],
    occupancy: dict[str, int],
) -> dict[int, int]:
    """Repair over-capacity assignments with greedy re-routing.

    This keeps SA fast (top-K candidates) while preventing obvious overload.
    """
    if not assignment_indices:
        return assignment_indices

    by_hospital: dict[int, list[int]] = defaultdict(list)
    for p_idx, h_idx in assignment_indices.items():
        by_hospital[h_idx].append(p_idx)

    # Dynamic load counts only for patients in this optimization batch.
    dynamic_load: dict[int, int] = {h_idx: len(p_idxs) for h_idx, p_idxs in by_hospital.items()}

    for h_idx, p_idxs in list(by_hospital.items()):
        hosp = hospitals[h_idx]
        occ = occupancy.get(hosp.hospital_id, hosp.current_occupancy)
        remaining = max(hosp.max_capacity - occ, 0)
        if len(p_idxs) <= remaining:
            continue

        # Keep most urgent + closest patients on overloaded hospital.
        keep_count = remaining
        ranked = sorted(
            p_idxs,
            key=lambda p_i: (
                -patients[p_i].severity,
                _haversine_km(
                    patients[p_i].lat,
                    patients[p_i].lng,
                    hosp.lat,
                    hosp.lng,
                ),
            ),
        )
        keepers = set(ranked[:keep_count])

        for p_i in ranked[keep_count:]:
            patient = patients[p_i]
            best_alt: int | None = None
            best_alt_score = float("inf")

            for alt_idx in candidates[p_i]:
                if alt_idx < 0 or alt_idx == h_idx:
                    continue

                alt_hosp = hospitals[alt_idx]
                alt_occ = occupancy.get(alt_hosp.hospital_id, alt_hosp.current_occupancy)
                alt_remaining = max(alt_hosp.max_capacity - alt_occ, 0)
                alt_planned = dynamic_load.get(alt_idx, 0)
                projected_ratio = (alt_occ + alt_planned + 1) / max(alt_hosp.max_capacity, 1)

                # Prefer alternatives with available room, then distance.
                cap_block = 200.0 if alt_planned >= alt_remaining else 0.0
                dist = _haversine_km(patient.lat, patient.lng, alt_hosp.lat, alt_hosp.lng)
                score = cap_block + dist + 30.0 * (projected_ratio**3)

                if score < best_alt_score:
                    best_alt = alt_idx
                    best_alt_score = score

            if best_alt is None:
                best_alt = min(
                    range(len(hospitals)),
                    key=lambda idx: (
                        (
                            occupancy.get(hospitals[idx].hospital_id, hospitals[idx].current_occupancy)
                            + dynamic_load.get(idx, 0)
                        )
                        / max(hospitals[idx].max_capacity, 1),
                        _haversine_km(
                            patient.lat,
                            patient.lng,
                            hospitals[idx].lat,
                            hospitals[idx].lng,
                        ),
                    ),
                )

            assignment_indices[p_i] = best_alt
            dynamic_load[h_idx] = max(0, dynamic_load.get(h_idx, 0) - 1)
            dynamic_load[best_alt] = dynamic_load.get(best_alt, 0) + 1

        by_hospital[h_idx] = [p_i for p_i in p_idxs if p_i in keepers]

    return assignment_indices


def solve_allocation(
    patients: list[Patient],
    hospitals: list[Hospital],
    occupancy: dict[str, int],
    k: int = DEFAULT_K,
    num_reads: int = 80,
    random_seed: int | None = None,
    refused_pairs: set[tuple[int, str]] | None = None,
) -> dict[int, str]:
    """Solve the ER allocation problem using simulated annealing.

    Returns: {patient_id: hospital_id}
    """
    if not patients or not hospitals:
        return {}

    refused_pairs = refused_pairs or set()
    k = max(1, min(k, len(hospitals)))
    n_patients = len(patients)

    candidates = _build_candidate_matrix(
        patients=patients,
        hospitals=hospitals,
        occupancy=occupancy,
        k=k,
        refused_pairs=refused_pairs,
    )

    cap_cost = np.zeros((n_patients, k), dtype=float)
    dist_cost = np.zeros((n_patients, k), dtype=float)
    cong_cost = np.zeros((n_patients, k), dtype=float)
    valid_mask = np.zeros((n_patients, k), dtype=float)

    projected_demand: dict[int, int] = defaultdict(int)
    for row in candidates:
        for h_idx in row:
            if h_idx >= 0:
                projected_demand[h_idx] += 1

    for p_idx, pat in enumerate(patients):
        for slot_idx, h_idx in enumerate(candidates[p_idx]):
            if h_idx < 0:
                continue

            valid_mask[p_idx, slot_idx] = 1.0
            hosp = hospitals[h_idx]
            occ = occupancy.get(hosp.hospital_id, hosp.current_occupancy)
            remaining = max(hosp.max_capacity - occ, 0)

            cap_cost[p_idx, slot_idx] = _capacity_penalty(occ, hosp.max_capacity)

            dist_km = _haversine_km(pat.lat, pat.lng, hosp.lat, hosp.lng)
            # Higher severity gets stronger preference for shorter distances.
            severity_weight = 0.45 + 0.55 * (pat.severity / 5.0)
            dist_cost[p_idx, slot_idx] = W_DISTANCE * severity_weight * dist_km

            pressure = projected_demand[h_idx] / max(remaining, 1)
            occ_ratio = occ / max(hosp.max_capacity, 1)
            cong_cost[p_idx, slot_idx] = W_CONGESTION * ((pressure**2) + (occ_ratio**2))

            if (pat.patient_id, hosp.hospital_id) in refused_pairs:
                # Strongly avoid hospitals that already refused this patient.
                cong_cost[p_idx, slot_idx] += 250.0

    instance_data = {
        "cap_cost": cap_cost.tolist(),
        "dist_cost": dist_cost.tolist(),
        "cong_cost": cong_cost.tolist(),
        "valid_mask": valid_mask.tolist(),
    }

    interpreter_cls = getattr(jm, "Interpreter", None)
    if interpreter_cls is not None:
        interpreter = interpreter_cls(instance_data)
        instance: Instance = interpreter.eval_problem(_er_problem)
    else:
        # jijmodeling>=2.0 path
        instance = _er_problem.eval(instance_data)

    result = OMMXOpenJijSAAdapter.sample(
        instance,
        num_reads=num_reads,
        seed=random_seed,
        uniform_penalty_weight=120.0,
    )

    try:
        best_sol = result.best_feasible_unrelaxed
    except RuntimeError:
        try:
            best_sol = result.best_feasible
        except RuntimeError:
            # Fallback: nearest candidate for each patient.
            return {
                pat.patient_id: hospitals[candidates[p_idx][0]].hospital_id
                for p_idx, pat in enumerate(patients)
            }

    assignment_indices: dict[int, int] = {}
    for dv in best_sol.decision_variables:
        if dv.name != "x" or dv.value != 1.0:
            continue
        p_i, slot_i = dv.subscripts[0], dv.subscripts[1]
        h_idx = candidates[p_i][slot_i]
        if h_idx >= 0:
            assignment_indices[p_i] = h_idx

    for p_idx, row in enumerate(candidates):
        if p_idx in assignment_indices:
            continue
        fallback = next((h_idx for h_idx in row if h_idx >= 0), 0)
        assignment_indices[p_idx] = fallback

    assignment_indices = _repair_overflow_assignments(
        assignment_indices=assignment_indices,
        patients=patients,
        hospitals=hospitals,
        candidates=candidates,
        occupancy=occupancy,
    )

    return {
        patients[p_idx].patient_id: hospitals[h_idx].hospital_id
        for p_idx, h_idx in assignment_indices.items()
    }
