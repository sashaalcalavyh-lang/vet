"""
VetSaaS Backend — API Central para Micro-SaaS Veterinario.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
import datetime

app = FastAPI(
    title="VetSaaS API",
    description="Micro-SaaS para gestión de citas, ficha clínica y recordatorios por WhatsApp.",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Tutor(BaseModel):
    id: Optional[int] = None
    nombre: str = Field(..., example="Constanza Pérez")
    telefono: str = Field(..., example="+56912345678")
    email: Optional[str] = None

class Paciente(BaseModel):
    id: Optional[int] = None
    tutor_id: int
    nombre: str = Field(..., example="Luna")
    especie: str = Field(..., example="Canino")
    raza: Optional[str] = "Mestizo"
    peso_kg: float = Field(..., example=12.5)

class Cita(BaseModel):
    id: Optional[int] = None
    paciente_id: int
    fecha_hora: datetime.datetime
    motivo: str = Field(..., example="Control vacunas anuales")
    confirmada: bool = False

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "VetSaaS Core",
        "version": "0.1.0",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

@app.get("/")
def root():
    return {
        "message": "Bienvenido a VetSaaS API — Potenciando Clínicas Veterinarias con IA",
        "docs": "/docs"
    }
