from pydantic import BaseModel


class Hospital(BaseModel):
    hospital_id: str
    hospital_name: str
    lat: float
    lng: float
    max_capacity: int
    current_occupancy: int  # initial occupancy at simulation start
