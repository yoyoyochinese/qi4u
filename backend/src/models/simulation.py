from typing import Optional
from pydantic import BaseModel

from src.models.hospital import Hospital
from src.models.patient import Patient


class SimulationStartRequest(BaseModel):
    num_patients: int = 50
    random_seed: int = -1  # <=0 means auto-generate from timestamp
    severity_weights: list[float] = [0.15, 0.25, 0.30, 0.20, 0.10]


class SimulationStartResponse(BaseModel):
    scenario_id: str
    hospitals: list[Hospital]
    patients: list[Patient]


class AttemptEvent(BaseModel):
    patient_id: int
    hospital_id: str
    hospital_name: str
    hop_number: int
    accepted: bool
    elapsed_time: float  # cumulative time for this patient so far
    lat: float
    lng: float


class PatientResult(BaseModel):
    patient_id: int
    severity: int
    assigned_hospital_id: Optional[str]
    assigned_hospital_name: Optional[str]
    hops: int
    elapsed_time: float
    attempts: list[AttemptEvent]


class AggregateMetrics(BaseModel):
    avg_hops: float
    min_hops: int
    max_hops: int
    avg_time: float
    min_time: float
    max_time: float
    total_patients: int
    accepted_patients: int


class SimulationRunRequest(BaseModel):
    scenario_id: str


