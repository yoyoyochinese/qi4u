from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router

app = FastAPI(title="Busan ER Optimization Demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok"}
