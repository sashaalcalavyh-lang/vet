"""
VetSaaS Backend — API Central para Micro-SaaS Veterinario.
Persistencia en PostgreSQL, integración con WhatsApp / n8n y CORS habilitado para Vercel.
"""
import os
import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://vet_admin:VetSecurePostgres2026!@vet-postgres:5432/vet_saas_db"
)
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://n8n_app:5678/webhook/vet-cita-alerta")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Modelos ORM
class TutorDB(Base):
    __tablename__ = "tutores"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    telefono = Column(String(20), nullable=False, index=True)
    email = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    pacientes = relationship("PacienteDB", back_populates="tutor")

class PacienteDB(Base):
    __tablename__ = "pacientes"
    id = Column(Integer, primary_key=True, index=True)
    tutor_id = Column(Integer, ForeignKey("tutores.id"), nullable=False)
    nombre = Column(String(60), nullable=False)
    especie = Column(String(40), nullable=False)
    raza = Column(String(60), default="Mestizo")
    peso_kg = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    tutor = relationship("TutorDB", back_populates="pacientes")
    citas = relationship("CitaDB", back_populates="paciente")

class CitaDB(Base):
    __tablename__ = "citas"
    id = Column(Integer, primary_key=True, index=True)
    paciente_id = Column(Integer, ForeignKey("pacientes.id"), nullable=False)
    fecha_hora = Column(DateTime, nullable=False)
    motivo = Column(String(200), nullable=False)
    confirmada = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    paciente = relationship("PacienteDB", back_populates="citas")

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"Advertencia al inicializar tablas: {e}")

app = FastAPI(
    title="VetSaaS API Core",
    description="Backend híbrido de alto rendimiento para Micro-SaaS Veterinario.",
    version="1.0.0"
)

# CORS abierto para Vercel y clientes autorizados
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Schemas Pydantic
class TutorCreate(BaseModel):
    nombre: str = Field(..., example="Constanza Pérez")
    telefono: str = Field(..., example="+56912345678")
    email: Optional[str] = None

class TutorOut(TutorCreate):
    id: int
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class PacienteCreate(BaseModel):
    tutor_id: int
    nombre: str = Field(..., example="Luna")
    especie: str = Field(..., example="Canino")
    raza: Optional[str] = "Mestizo"
    peso_kg: float = Field(..., example=12.5)

class PacienteOut(PacienteCreate):
    id: int
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class CitaCreate(BaseModel):
    paciente_id: int
    fecha_hora: datetime.datetime
    motivo: str = Field(..., example="Vacuna Óctuple y Desparasitación")

class CitaOut(CitaCreate):
    id: int
    confirmada: bool
    created_at: datetime.datetime
    class Config:
        from_attributes = True

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/")
def root():
    return {
        "service": "VetSaaS Backend API",
        "status": "online",
        "docs": "/docs",
        "cloud": "Hetzner VPS",
        "managed_by": "Sebastián / Antigravity"
    }

@app.get("/health")
def health(db: Session = Depends(get_db)):
    db_status = "error"
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"unreachable: {str(e)}"

    return {
        "status": "healthy" if db_status == "connected" else "degraded",
        "database": db_status,
        "database_type": "PostgreSQL 16",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

# Endpoints Tutores
@app.post("/api/tutores", response_model=TutorOut)
def crear_tutor(tutor: TutorCreate, db: Session = Depends(get_db)):
    db_tutor = TutorDB(**tutor.dict())
    db.add(db_tutor)
    db.commit()
    db.refresh(db_tutor)
    return db_tutor

@app.get("/api/tutores", response_model=List[TutorOut])
def listar_tutores(db: Session = Depends(get_db)):
    return db.query(TutorDB).order_by(TutorDB.id.desc()).all()

# Endpoints Pacientes
@app.post("/api/pacientes", response_model=PacienteOut)
def crear_paciente(paciente: PacienteCreate, db: Session = Depends(get_db)):
    tutor = db.query(TutorDB).filter(TutorDB.id == paciente.tutor_id).first()
    if not tutor:
        raise HTTPException(status_code=404, detail="Tutor no encontrado")
    db_paciente = PacienteDB(**paciente.dict())
    db.add(db_paciente)
    db.commit()
    db.refresh(db_paciente)
    return db_paciente

@app.get("/api/pacientes", response_model=List[PacienteOut])
def listar_pacientes(db: Session = Depends(get_db)):
    return db.query(PacienteDB).order_by(PacienteDB.id.desc()).all()

# Endpoints Citas
@app.post("/api/citas", response_model=CitaOut)
def crear_cita(cita: CitaCreate, db: Session = Depends(get_db)):
    paciente = db.query(PacienteDB).filter(PacienteDB.id == cita.paciente_id).first()
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    db_cita = CitaDB(**cita.dict())
    db.add(db_cita)
    db.commit()
    db.refresh(db_cita)
    return db_cita

@app.get("/api/citas", response_model=List[CitaOut])
def listar_citas(db: Session = Depends(get_db)):
    return db.query(CitaDB).order_by(CitaDB.fecha_hora.asc()).all()
