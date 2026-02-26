export interface Hospital {
  hospital_id: string;
  hospital_name: string;
  lat: number;
  lng: number;
  max_capacity: number;
  current_occupancy: number;
}

export interface Patient {
  patient_id: number;
  lat: number;
  lng: number;
  severity: number;
}

export interface AttemptEvent {
  patient_id: number;
  hospital_id: string;
  hospital_name: string;
  hop_number: number;
  accepted: boolean;
  elapsed_time: number;
  lat: number;
  lng: number;
}

export interface PatientResult {
  patient_id: number;
  severity: number;
  assigned_hospital_id: string | null;
  assigned_hospital_name: string | null;
  hops: number;
  elapsed_time: number;
  attempts: AttemptEvent[];
}

export interface AggregateMetrics {
  avg_hops: number;
  min_hops: number;
  max_hops: number;
  avg_time: number;
  min_time: number;
  max_time: number;
  total_patients: number;
  accepted_patients: number;
}

export interface ScenarioResponse {
  scenario_id: string;
  hospitals: Hospital[];
  patients: Patient[];
}
