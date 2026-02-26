from pydantic import BaseModel


class Patient(BaseModel):
    patient_id: int
    lat: float
    lng: float
    severity: int  # 1 (mild) to 5 (critical)
