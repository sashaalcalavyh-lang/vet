"""
CoVet Backend — API Central para Micro-SaaS Veterinario.
Persistencia en PostgreSQL, procesamiento SOAP con IA (Groq Whisper + Anthropic Claude)
e integración con n8n para WhatsApp.
"""
import os
import uuid
import datetime
import logging
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("covet_backend")

# Configuración de Entorno
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://vet_admin:VetSecurePostgres2026!@vet-postgres:5432/vet_saas_db"
)
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://n8n_app:5678/webhook/vet-cita-alerta")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# -------------------------------------------------------------
# 1. Modelos ORM (SQLAlchemy 2.0)
# -------------------------------------------------------------
class TutorDB(Base):
    __tablename__ = "tutores"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    telefono = Column(String(20), nullable=False, index=True)
    email = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    pacientes = relationship("PacienteDB", back_populates="tutor")
    fiches = relationship("MedicalFicheDB", back_populates="tutor")

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
    fiches = relationship("MedicalFicheDB", back_populates="paciente")

class CitaDB(Base):
    __tablename__ = "citas"
    id = Column(Integer, primary_key=True, index=True)
    paciente_id = Column(Integer, ForeignKey("pacientes.id"), nullable=False)
    fecha_hora = Column(DateTime, nullable=False)
    motivo = Column(String(200), nullable=False)
    confirmada = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    paciente = relationship("PacienteDB", back_populates="citas")
    fiches = relationship("MedicalFicheDB", back_populates="cita")

class MedicalFicheDB(Base):
    __tablename__ = "medical_fiches"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cita_id = Column(Integer, ForeignKey("citas.id"), nullable=True)
    tutor_id = Column(Integer, ForeignKey("tutores.id"), nullable=True)
    paciente_id = Column(Integer, ForeignKey("pacientes.id"), nullable=True)
    texto_transcrito_original = Column(Text, nullable=True)
    json_estructurado = Column(JSONB, nullable=True)
    estado_procesamiento = Column(String(30), default="PENDING")
    error_detalle = Column(Text, nullable=True)
    fecha_creacion = Column(DateTime, default=datetime.datetime.utcnow)
    fecha_confirmacion = Column(DateTime, nullable=True)

    tutor = relationship("TutorDB", back_populates="fiches")
    paciente = relationship("PacienteDB", back_populates="fiches")
    cita = relationship("CitaDB", back_populates="fiches")

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Tablas de base de datos verificadas/creadas exitosamente.")
    except Exception as e:
        logger.error(f"Error al inicializar tablas: {e}")

# -------------------------------------------------------------
# 2. Esquemas Pydantic
# -------------------------------------------------------------
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

class SOAPFicheSchema(BaseModel):
    tutor_nombre: str = Field(
        default="Por definir",
        description="Nombre del dueño/tutor mencionado o 'Por definir'"
    )
    tutor_telefono: str = Field(
        default="",
        description="Teléfono en formato +569XXXXXXXX si se menciona, o cadena vacía"
    )
    paciente_nombre: str = Field(
        ...,
        description="Nombre del paciente/mascota"
    )
    especie: str = Field(
        ...,
        description="Especie del animal (Ej: Canino, Felino, Exótico)"
    )
    raza: str = Field(
        default="Mestizo",
        description="Raza de la mascota"
    )
    edad_aproximada: str = Field(
        default="No especificada",
        description="Edad aproximada de la mascota"
    )
    peso_kg: Optional[float] = Field(
        default=None,
        description="Peso actual en kilogramos"
    )
    anamnesis: str = Field(
        ...,
        description="S - Subjetivo: motivo de consulta, historia clínica y síntomas reportados por el tutor"
    )
    examen_fisico: str = Field(
        ...,
        description="O - Objetivo: constantes vitales (temperatura, FC, FR, mucosas, TLLC) y hallazgos clínicos del examen físico"
    )
    diagnostico_presuntivo: str = Field(
        ...,
        description="A - Análisis: diagnóstico presuntivo y diagnósticos diferenciales"
    )
    plan_terapeutico: str = Field(
        ...,
        description="P - Plan: medicamentos aplicados en consulta, receta médica detallada con dosis/frecuencia/duración e indicaciones de cuidado"
    )
    proximos_pasos: str = Field(
        ...,
        description="Fecha o plazo de próximo control, vacunas o exámenes complementarios sugeridos"
    )

# -------------------------------------------------------------
# 3. Pipeline de IA en Segundo Plano (Groq Whisper + Claude Tool Use)
# -------------------------------------------------------------
def process_voice_fiche(task_id: uuid.UUID, file_path: str):
    """
    Procesa el audio grabado en background:
    1. Transcribe con Groq Whisper Large V3.
    2. Estructura el SOAP con Anthropic Claude usando Tool Use para garantizar JSON estricto.
    3. Deja la ficha en estado READY_FOR_REVIEW (esperando confirmación humana).
    4. Elimina el archivo temporal en finally:.
    """
    db = SessionLocal()
    fiche = None
    try:
        fiche = db.query(MedicalFicheDB).filter(MedicalFicheDB.id == task_id).first()
        if not fiche:
            logger.error(f"[Pipeline] Ficha con ID {task_id} no encontrada.")
            return

        fiche.estado_procesamiento = "PROCESSING"
        db.commit()

        # PASO A: Transcripción con Groq Whisper
        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            raise ValueError("GROQ_API_KEY no configurada en las variables de entorno.")

        logger.info(f"[Pipeline {task_id}] Transcribiendo audio con Groq Whisper...")
        with open(file_path, "rb") as audio_file:
            files = {
                "file": (os.path.basename(file_path), audio_file.read(), "application/octet-stream")
            }
        
        data = {
            "model": "whisper-large-v3-turbo",
            "language": "es",
            "response_format": "json"
        }
        headers = {"Authorization": f"Bearer {groq_key}"}

        with httpx.Client(timeout=60.0) as client:
            groq_resp = client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                files=files,
                data=data,
                headers=headers
            )
            groq_resp.raise_for_status()
            transcription_data = groq_resp.json()
            transcript_text = transcription_data.get("text", "").strip()

        if not transcript_text:
            raise ValueError("Groq no detectó texto en el audio enviado.")

        fiche.texto_transcrito_original = transcript_text
        db.commit()
        logger.info(f"[Pipeline {task_id}] Transcripción completada: {transcript_text[:80]}...")

        # PASO B: Estructuración SOAP con Anthropic Claude (Tool Use)
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            raise ValueError("ANTHROPIC_API_KEY no configurada en las variables de entorno.")

        logger.info(f"[Pipeline {task_id}] Estructurando SOAP con Claude ({ANTHROPIC_MODEL})...")
        tool_definition = {
            "name": "registrar_ficha_soap",
            "description": "Registra la ficha clínica veterinaria bajo el estándar SOAP con datos de tutor, paciente y tratamiento.",
            "input_schema": SOAPFicheSchema.model_json_schema()
        }

        system_prompt = (
            "Actúas como un Médico Veterinario Clínico Senior en Chile. "
            "Transforma el dictado natural del veterinario en terreno en una ficha clínica estructurada bajo el estándar SOAP "
            "utilizando la herramienta `registrar_ficha_soap`. "
            "Respeta la terminología farmacológica y clínica veterinaria usada en Chile, "
            "corrige errores fonéticos obvios de transcripción y NUNCA inventes dosis ni constantes vitales que no hayan sido dictadas "
            "(si un dato no se mencionó, coloca 'No especificado en dictado')."
        )

        anthropic_payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": f"A continuación se presenta el dictado del veterinario en consulta:\n\n\"\"\"\n{transcript_text}\n\"\"\"\n\nPor favor, estructura la ficha clínica SOAP."
                }
            ],
            "tools": [tool_definition],
            "tool_choice": {"type": "tool", "name": "registrar_ficha_soap"}
        }

        anthropic_headers = {
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        with httpx.Client(timeout=60.0) as client:
            anthropic_resp = client.post(
                "https://api.anthropic.com/v1/messages",
                json=anthropic_payload,
                headers=anthropic_headers
            )
            anthropic_resp.raise_for_status()
            claude_result = anthropic_resp.json()

        # Extraer llamada a herramienta
        content_blocks = claude_result.get("content", [])
        tool_input = None
        for block in content_blocks:
            if block.get("type") == "tool_use" and block.get("name") == "registrar_ficha_soap":
                tool_input = block.get("input", {})
                break

        if not tool_input:
            raise ValueError(f"Claude no retornó el formato de herramienta esperado: {claude_result}")

        # Validar contra esquema Pydantic
        validated_soap = SOAPFicheSchema(**tool_input)
        fiche.json_estructurado = validated_soap.model_dump()
        fiche.estado_procesamiento = "READY_FOR_REVIEW"
        fiche.error_detalle = None
        db.commit()
        logger.info(f"[Pipeline {task_id}] Ficha estructurada en READY_FOR_REVIEW exitosamente.")

    except Exception as e:
        logger.error(f"[Pipeline {task_id}] Error en procesamiento: {e}")
        if fiche:
            fiche.estado_procesamiento = "FAILED"
            fiche.error_detalle = str(e)
            db.commit()
    finally:
        db.close()
        # Eliminar archivo temporal de audio para proteger almacenamiento SSD
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"[Pipeline {task_id}] Archivo temporal eliminado: {file_path}")
            except Exception as err:
                logger.warning(f"[Pipeline {task_id}] No se pudo borrar archivo temporal: {err}")

# -------------------------------------------------------------
# 4. Inicialización de la Aplicación FastAPI
# -------------------------------------------------------------
app = FastAPI(
    title="CoVet API Core",
    description="Micro-SaaS para clínicas veterinarias: transcripción por voz, SOAP estructurado y automatización WhatsApp.",
    version="2.0.0"
)

# CORS permisivo para Vercel y clientes autorizados
cors_env = os.getenv("CORS_ORIGINS", "*")
origins = [o.strip() for o in cors_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
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

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/")
def root():
    return {
        "service": "CoVet API Core",
        "status": "online",
        "docs": "/docs",
        "cloud": "Hetzner VPS",
        "version": "2.0.0"
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
        "groq_configured": bool(os.getenv("GROQ_API_KEY")),
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

# -------------------------------------------------------------
# 5. Endpoints del Ciclo Clínico CoVet
# -------------------------------------------------------------

@app.post("/api/v1/covet/transcribe", status_code=202)
async def transcribir_audio_consulta(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    cita_id: Optional[int] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Recibe un audio de dictado médico (móvil o desktop), crea la ficha en estado PENDING,
    agenda la tarea asíncrona de transcripción/estructuración y responde de inmediato con 202 Accepted.
    """
    contents = await file.read()
    if not contents or len(contents) == 0:
        raise HTTPException(status_code=400, detail="El archivo de audio está vacío.")

    # Guardado temporal seguro
    temp_dir = "/tmp/covet_audios"
    os.makedirs(temp_dir, exist_ok=True)
    fiche_id = uuid.uuid4()

    filename = file.filename or "dictado.webm"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
    temp_file_path = os.path.join(temp_dir, f"{fiche_id}.{ext}")

    with open(temp_file_path, "wb") as f:
        f.write(contents)

    # Crear registro inicial en PostgreSQL
    fiche = MedicalFicheDB(
        id=fiche_id,
        cita_id=cita_id,
        estado_procesamiento="PENDING"
    )
    db.add(fiche)
    db.commit()
    db.refresh(fiche)

    # Agendar proceso en background sin bloquear hilo principal
    background_tasks.add_task(process_voice_fiche, fiche.id, temp_file_path)

    return {
        "task_id": str(fiche.id),
        "status": "PENDING",
        "message": "Audio recibido. Procesamiento iniciado en segundo plano."
    }

@app.get("/api/v1/covet/fiches/{task_id}")
def obtener_estado_ficha(task_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Retorna el estado de procesamiento y contenido de la ficha para alimentar el polling del Frontend.
    """
    fiche = db.query(MedicalFicheDB).filter(MedicalFicheDB.id == task_id).first()
    if not fiche:
        raise HTTPException(status_code=404, detail="Ficha clínica no encontrada")

    return {
        "id": str(fiche.id),
        "cita_id": fiche.cita_id,
        "tutor_id": fiche.tutor_id,
        "paciente_id": fiche.paciente_id,
        "estado_procesamiento": fiche.estado_procesamiento,
        "texto_transcrito_original": fiche.texto_transcrito_original,
        "json_estructurado": fiche.json_estructurado,
        "error_detalle": fiche.error_detalle,
        "fecha_creacion": fiche.fecha_creacion.isoformat() if fiche.fecha_creacion else None,
        "fecha_confirmacion": fiche.fecha_confirmacion.isoformat() if fiche.fecha_confirmacion else None
    }

@app.get("/api/v1/covet/fiches")
def listar_fichas_clinicas(limit: int = 25, db: Session = Depends(get_db)):
    """
    Retorna las últimas fichas clínicas registradas para alimentar el historial del Frontend.
    """
    fiches = db.query(MedicalFicheDB).order_by(MedicalFicheDB.fecha_creacion.desc()).limit(limit).all()
    return [
        {
            "id": str(f.id),
            "cita_id": f.cita_id,
            "tutor_id": f.tutor_id,
            "paciente_id": f.paciente_id,
            "estado_procesamiento": f.estado_procesamiento,
            "json_estructurado": f.json_estructurado,
            "fecha_creacion": f.fecha_creacion.isoformat() if f.fecha_creacion else None,
            "fecha_confirmacion": f.fecha_confirmacion.isoformat() if f.fecha_confirmacion else None
        }
        for f in fiches
    ]

@app.put("/api/v1/covet/fiches/{task_id}/confirm")
def confirmar_y_enviar_ficha(
    task_id: uuid.UUID,
    payload: SOAPFicheSchema,
    db: Session = Depends(get_db)
):
    """
    Valida y guarda la ficha editada por el veterinario:
    1. Ejecuta upsert en tutores (por teléfono) y pacientes (por nombre y tutor).
    2. Actualiza medical_fiches vinculando las llaves foráneas.
    3. Dispara webhook a n8n para envío de WhatsApp (con tolerancia a fallos).
    """
    if not payload.tutor_telefono or not payload.tutor_telefono.strip():
        raise HTTPException(
            status_code=400,
            detail="El teléfono del tutor es obligatorio para confirmar la ficha y activar los recordatorios por WhatsApp."
        )

    fiche = db.query(MedicalFicheDB).filter(MedicalFicheDB.id == task_id).first()
    if not fiche:
        raise HTTPException(status_code=404, detail="Ficha clínica no encontrada")

    # 1. Sincronización Relacional: Tutor (Upsert)
    clean_phone = payload.tutor_telefono.strip()
    clean_tutor_name = payload.tutor_nombre.strip() if payload.tutor_nombre else "Por definir"

    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    if not tutor:
        tutor = TutorDB(nombre=clean_tutor_name, telefono=clean_phone)
        db.add(tutor)
        db.commit()
        db.refresh(tutor)
    else:
        if clean_tutor_name != "Por definir" and tutor.nombre != clean_tutor_name:
            tutor.nombre = clean_tutor_name
            db.commit()

    # 2. Sincronización Relacional: Paciente (Upsert por nombre y tutor)
    clean_pet_name = payload.paciente_nombre.strip()
    paciente = db.query(PacienteDB).filter(
        PacienteDB.tutor_id == tutor.id,
        PacienteDB.nombre.ilike(clean_pet_name)
    ).first()

    if not paciente:
        paciente = PacienteDB(
            tutor_id=tutor.id,
            nombre=clean_pet_name,
            especie=payload.especie,
            raza=payload.raza,
            peso_kg=payload.peso_kg or 0.0
        )
        db.add(paciente)
        db.commit()
        db.refresh(paciente)
    else:
        if payload.peso_kg:
            paciente.peso_kg = payload.peso_kg
        if payload.raza:
            paciente.raza = payload.raza
        db.commit()

    # 3. Actualizar la Ficha Médica
    now = datetime.datetime.utcnow()
    fiche.tutor_id = tutor.id
    fiche.paciente_id = paciente.id
    fiche.json_estructurado = payload.model_dump()
    fiche.fecha_confirmacion = now

    # 4. Disparo a n8n con Tolerancia a Fallos
    n8n_dispatched = False
    n8n_error = None

    n8n_payload = {
        "fiche_id": str(fiche.id),
        "tutor_id": tutor.id,
        "paciente_id": paciente.id,
        "tutor_nombre": tutor.nombre,
        "tutor_telefono": tutor.telefono,
        "paciente_nombre": paciente.nombre,
        "especie": paciente.especie,
        "raza": paciente.raza,
        "peso_kg": paciente.peso_kg,
        "fecha_confirmacion": now.isoformat(),
        "soap": payload.model_dump()
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(N8N_WEBHOOK_URL, json=n8n_payload)
            if 200 <= resp.status_code < 300:
                fiche.estado_procesamiento = "CONFIRMED_AND_SENT"
                n8n_dispatched = True
                logger.info(f"[Confirm {task_id}] Webhook n8n despachado exitosamente (HTTP {resp.status_code}).")
            else:
                fiche.estado_procesamiento = "CONFIRMED_N8N_PENDING"
                n8n_error = f"n8n respondió con código HTTP {resp.status_code}: {resp.text}"
                logger.warning(f"[Confirm {task_id}] n8n respondió con error: {n8n_error}")
    except Exception as e:
        fiche.estado_procesamiento = "CONFIRMED_N8N_PENDING"
        n8n_error = f"No se pudo conectar a n8n: {str(e)}"
        logger.warning(f"[Confirm {task_id}] Excepción al contactar n8n: {n8n_error}")

    db.commit()
    db.refresh(fiche)

    return {
        "status": "success",
        "fiche_id": str(fiche.id),
        "estado_procesamiento": fiche.estado_procesamiento,
        "tutor_id": tutor.id,
        "paciente_id": paciente.id,
        "n8n_dispatched": n8n_dispatched,
        "n8n_detail": n8n_error,
        "message": (
            "Ficha médica confirmada y WhatsApp despachado."
            if n8n_dispatched
            else "Ficha médica guardada en PostgreSQL. Envío de WhatsApp en cola (n8n no disponible)."
        )
    }

# -------------------------------------------------------------
# 6. Endpoints CRUD Existentes (Mantenidos intactos)
# -------------------------------------------------------------
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
