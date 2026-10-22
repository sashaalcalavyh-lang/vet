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
    tipo_atencion = Column(String(30), default="CLINICA_PRESENCIAL") # TELEMEDICINA, DOMICILIO, CLINICA_PRESENCIAL
    estado_cita = Column(String(30), default="CONFIRMED") # HOLD_15_MIN, CONFIRMED, CANCELLED, COMPLETED
    triaje_urgencia = Column(String(30), default="MEDIUM_ROUTINE") # CRITICAL_EMERGENCY, HIGH_PRIORITY, MEDIUM_ROUTINE, LOW_PREVENTIVE
    protocolo_primeros_auxilios = Column(Text, nullable=True)
    hold_expira_en = Column(DateTime, nullable=True)
    monto_pago = Column(Float, default=0.0)
    pago_estado = Column(String(30), default="PENDING") # PENDING, SIMULATED_PAID, PAID_GATEWAY
    pago_metodo = Column(String(30), nullable=True)
    ventana_horaria = Column(String(30), nullable=True) # MAÑANA, TARDE, HORA_EXACTA
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
        # Migraciones automáticas seguras sin afectar datos previos
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS tipo_atencion VARCHAR(30) DEFAULT 'CLINICA_PRESENCIAL'"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS estado_cita VARCHAR(30) DEFAULT 'CONFIRMED'"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS triaje_urgencia VARCHAR(30) DEFAULT 'MEDIUM_ROUTINE'"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS protocolo_primeros_auxilios TEXT"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS hold_expira_en TIMESTAMP"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS monto_pago FLOAT DEFAULT 0.0"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS pago_estado VARCHAR(30) DEFAULT 'PENDING'"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS pago_metodo VARCHAR(30)"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS ventana_horaria VARCHAR(30)"))
            conn.commit()
        logger.info("Tablas y columnas de base de datos verificadas/creadas exitosamente.")
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
    tipo_atencion: Optional[str] = "CLINICA_PRESENCIAL"
    ventana_horaria: Optional[str] = "HORA_EXACTA"

class CitaOut(BaseModel):
    id: int
    paciente_id: int
    fecha_hora: datetime.datetime
    motivo: str
    confirmada: bool
    tipo_atencion: Optional[str] = "CLINICA_PRESENCIAL"
    estado_cita: Optional[str] = "CONFIRMED"
    triaje_urgencia: Optional[str] = "MEDIUM_ROUTINE"
    protocolo_primeros_auxilios: Optional[str] = None
    hold_expira_en: Optional[datetime.datetime] = None
    monto_pago: Optional[float] = 0.0
    pago_estado: Optional[str] = "PENDING"
    pago_metodo: Optional[str] = None
    ventana_horaria: Optional[str] = None
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class TriageRequest(BaseModel):
    tutor_nombre: str = Field(..., example="Camila Valenzuela")
    tutor_telefono: str = Field(..., example="+56987654321")
    paciente_nombre: str = Field(..., example="Milo")
    especie: str = Field(default="Canino", example="Canino")
    sintomas_o_motivo: str = Field(..., example="Mi perro vomitó espuma blanca y tiembla mucho hace 1 hora")
    preferencia_atencion: Optional[str] = "CUALQUIERA" # TELEMEDICINA, DOMICILIO, CLINICA_PRESENCIAL, CUALQUIERA
    preferencia_horario: Optional[str] = "LO_ANTES_POSIBLE" # LO_ANTES_POSIBLE, MAÑANA, TARDE

class TriageEvaluationSchema(BaseModel):
    triaje_urgencia: str = Field(
        description="Nivel de urgencia clínica: CRITICAL_EMERGENCY, HIGH_PRIORITY, MEDIUM_ROUTINE o LOW_PREVENTIVE"
    )
    triaje_nivel_color: str = Field(
        description="Código de semáforo: ROJO (crítico/inmediato), AMARILLO (alta prioridad en horas), VERDE (rutina/preventivo)"
    )
    triaje_diagnostico_tentativo: str = Field(
        description="Hipótesis clínica tentativa en lenguaje empático y profesional veterinario"
    )
    protocolo_primeros_auxilios: str = Field(
        description="Pasos de primeros auxilios y advertencias de qué NO hacer inmediatamente mientras se recibe atención médica"
    )
    tipo_atencion_recomendada: str = Field(
        description="TELEMEDICINA (si requiere orientación inmediata), DOMICILIO o CLINICA_PRESENCIAL"
    )
    tiempo_maximo_espera_horas: float = Field(
        description="Tiempo máximo seguro en horas para recibir evaluación"
    )

class TriageResponse(BaseModel):
    cita_id: int
    triaje_urgencia: str
    triaje_nivel_color: str
    triaje_diagnostico_tentativo: str
    protocolo_primeros_auxilios: str
    tipo_atencion_recomendada: str
    bloque_asignado: datetime.datetime
    hold_expira_en: datetime.datetime
    monto_sugerido: float
    bloques_alternativos: List[datetime.datetime]
    estado_cita: str
    tutor_id: int
    paciente_id: int

class PaymentConfirmRequest(BaseModel):
    metodo_pago: str = Field(default="DEMO_SIMULATION", example="DEMO_SIMULATION") # MERCADOPAGO, FLOW, WEBPAY, DEMO_SIMULATION
    monto: Optional[float] = 15000.0

class PaymentConfirmResponse(BaseModel):
    cita_id: int
    estado_cita: str
    pago_estado: str
    pago_metodo: str
    mensaje: str
    n8n_dispatched: bool
    resumen_cita: dict

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

# -------------------------------------------------------------
# 7. Núcleo de Triaje Autónomo y Protocolos de Primeros Auxilios (IA)
# -------------------------------------------------------------
def evaluar_triaje_con_ia(sintomas: str, especie: str, paciente: str) -> dict:
    """
    Evalúa la urgencia médica usando Anthropic Claude con salida estructurada Tool Use.
    Fallback determinístico basado en reglas clínicas chilenas si no hay API o hay timeout.
    """
    if ANTHROPIC_API_KEY:
        try:
            anthropic_payload = {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1024,
                "system": (
                    "Actúas como un Médico Veterinario de Urgencias y Triaje Clínico en Chile (CoVet). "
                    "Tu misión es evaluar los síntomas reportados por el tutor, categorizar el riesgo vital "
                    "bajo el estándar de triaje veterinario (ROJO: Riesgo vital inminente / Shock / Disnea severa / Intoxicación aguda / Torsión gástrica / Obstrucción urinaria; "
                    "AMARILLO: Vómitos repetidos / Sangrado / Fiebre / Cojera severa / Diarrea profusa; "
                    "VERDE: Control preventivo, vacunas, decaimiento leve sin dolor agudo) "
                    "y redactar un protocolo de primeros auxilios claro, seguro y pre-aprobado (indicando qué hacer y qué NO hacer antes de la consulta). "
                    "Utiliza la herramienta registrar_evaluacion_triaje con la información exacta. "
                    "NUNCA recetes fármacos específicos ni antibióticos; prioriza soporte vital, vía aérea, contención y confort térmico."
                ),
                "messages": [
                    {
                        "role": "user",
                        "content": f"Paciente: {paciente} (Especie: {especie}).\nSíntomas/Motivo reportado: \"{sintomas}\""
                    }
                ],
                "tools": [
                    {
                        "name": "registrar_evaluacion_triaje",
                        "description": "Registra el triaje veterinario y protocolo de primeros auxilios.",
                        "input_schema": TriageEvaluationSchema.model_json_schema()
                    }
                ],
                "tool_choice": {"type": "tool", "name": "registrar_evaluacion_triaje"}
            }

            with httpx.Client(timeout=15.0) as client:
                res = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json=anthropic_payload
                )
                if res.status_code == 200:
                    data = res.json()
                    for block in data.get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") == "registrar_evaluacion_triaje":
                            return block.get("input", {})
        except Exception as e:
            logger.warning(f"Error evaluando triaje con Claude, aplicando fallback: {e}")

    # Fallback Determinístico de Reglas Clínicas Chilenas
    sintomas_lower = sintomas.lower()
    critical_keywords = ["veneno", "raticida", "convuls", "no respira", "ahogo", "asfixia", "atropell", "cianosis", "morad", "hinchado", "torsion", "inconsciente"]
    high_keywords = ["vomit", "diarrea", "sangre", "cojera", "ojo", "fiebre", "herida", "dolor", "quej"]

    if any(k in sintomas_lower for k in critical_keywords):
        return {
            "triaje_urgencia": "CRITICAL_EMERGENCY",
            "triaje_nivel_color": "ROJO",
            "triaje_diagnostico_tentativo": "Alerta de Emergencia Vital Inmediata. Sospecha de intoxicación aguda, compromiso respiratorio o shock.",
            "protocolo_primeros_auxilios": (
                "1. Mantén la calma y mantén a la mascota abrigada y en reposo absoluto.\n"
                "2. NO induzcas el vómito si sospechas sustancias cáusticas o si la mascota está desorientada.\n"
                "3. Asegura que las vías respiratorias estén despejadas (estira suavemente su cuello).\n"
                "4. Si hay sangrado externo activo, aplica presión firme con un paño limpio y seco.\n"
                "5. Conéctate a Telemedicina de urgencia ahora mismo o acude a un centro 24h."
            ),
            "tipo_atencion_recomendada": "TELEMEDICINA",
            "tiempo_maximo_espera_horas": 0.5
        }
    elif any(k in sintomas_lower for k in high_keywords):
        return {
            "triaje_urgencia": "HIGH_PRIORITY",
            "triaje_nivel_color": "AMARILLO",
            "triaje_diagnostico_tentativo": "Cuadro agudo de prioridad alta (gastroentérico, trauma o dolor agudo).",
            "protocolo_primeros_auxilios": (
                "1. Suspende comida sólida por 4 a 6 horas para evitar sobrecarga gástrica.\n"
                "2. Ofrece pequeñas cantidades de agua fresca (a sorbos cada 15 min).\n"
                "3. NUNCA administres fármacos humanos (Paracetamol e Ibuprofeno son letales para mascotas).\n"
                "4. Observa el color de sus encías (deben ser rosadas, no blancas ni azuladas)."
            ),
            "tipo_atencion_recomendada": "DOMICILIO",
            "tiempo_maximo_espera_horas": 4.0
        }
    else:
        return {
            "triaje_urgencia": "LOW_PREVENTIVE",
            "triaje_nivel_color": "VERDE",
            "triaje_diagnostico_tentativo": "Consulta de control, chequeo preventivo o síntoma leve sin compromiso vital.",
            "protocolo_primeros_auxilios": (
                "1. Mantén el registro de los síntomas observados y de su alimentación.\n"
                "2. Ten a mano la cartilla sanitaria con sus vacunas al día.\n"
                "3. Asegura que descanse en un espacio confortable hasta su atención."
            ),
            "tipo_atencion_recomendada": "CLINICA_PRESENCIAL",
            "tiempo_maximo_espera_horas": 24.0
        }

@app.post("/api/v1/covet/triage", response_model=TriageResponse)
def ejecutar_triaje_autonomo(req: TriageRequest, db: Session = Depends(get_db)):
    """
    Agente Autónomo de Triaje Clínico:
    1. Sincroniza Tutor y Paciente en PostgreSQL.
    2. Evalúa riesgo vital con IA y asigna protocolo de primeros auxilios.
    3. Bloquea cupo temporal en agenda (HOLD_15_MIN).
    4. Notifica asíncronamente a n8n para alerta WhatsApp al veterinario.
    """
    clean_phone = req.tutor_telefono.strip()
    clean_tutor_name = req.tutor_nombre.strip()
    clean_pet_name = req.paciente_nombre.strip()

    # 1. Upsert Tutor
    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    if not tutor:
        tutor = TutorDB(nombre=clean_tutor_name, telefono=clean_phone)
        db.add(tutor)
        db.commit()
        db.refresh(tutor)
    else:
        if tutor.nombre != clean_tutor_name:
            tutor.nombre = clean_tutor_name
            db.commit()

    # 2. Upsert Paciente
    paciente = db.query(PacienteDB).filter(
        PacienteDB.tutor_id == tutor.id,
        PacienteDB.nombre.ilike(clean_pet_name)
    ).first()
    if not paciente:
        paciente = PacienteDB(
            tutor_id=tutor.id,
            nombre=clean_pet_name,
            especie=req.especie,
            raza="Mestizo",
            peso_kg=10.0
        )
        db.add(paciente)
        db.commit()
        db.refresh(paciente)

    # 3. Evaluación con IA
    evaluacion = evaluar_triaje_con_ia(req.sintomas_o_motivo, req.especie, clean_pet_name)
    nivel_urgencia = evaluacion.get("triaje_urgencia", "MEDIUM_ROUTINE")
    nivel_color = evaluacion.get("triaje_nivel_color", "AMARILLO")
    diag_tentativo = evaluacion.get("triaje_diagnostico_tentativo", "Evaluación clínica requerida")
    primeros_auxilios = evaluacion.get("protocolo_primeros_auxilios", "Mantener en reposo")
    tipo_atencion = req.preferencia_atencion if req.preferencia_atencion != "CUALQUIERA" else evaluacion.get("tipo_atencion_recomendada", "TELEMEDICINA")

    # 4. Cálculo de Bloques de Agenda (Opción C Híbrida / Adaptable)
    ahora = datetime.datetime.utcnow()
    bloque_asignado = ahora + datetime.timedelta(minutes=10) # Telemedicina inmediata por defecto si es crítico

    if nivel_urgencia == "CRITICAL_EMERGENCY":
        bloque_asignado = ahora + datetime.timedelta(minutes=10)
        tipo_atencion = "TELEMEDICINA"
        monto_sugerido = 15000.0
    elif nivel_urgencia == "HIGH_PRIORITY":
        bloque_asignado = ahora + datetime.timedelta(hours=2)
        monto_sugerido = 25000.0 if tipo_atencion == "DOMICILIO" else 18000.0
    else:
        # Rutina o Preventivo
        monto_sugerido = 20000.0
        # Próximo día hábil a las 10:00 o 15:30 según preferencia
        mañana = ahora + datetime.timedelta(days=1)
        if req.preferencia_horario == "TARDE":
            bloque_asignado = mañana.replace(hour=16, minute=0, second=0, microsecond=0)
        else:
            bloque_asignado = mañana.replace(hour=10, minute=30, second=0, microsecond=0)

    # Alternativas horarias sugeridas
    bloques_alt = [
        bloque_asignado + datetime.timedelta(hours=1),
        bloque_asignado + datetime.timedelta(hours=3),
        bloque_asignado + datetime.timedelta(days=1)
    ]

    hold_expira = ahora + datetime.timedelta(minutes=15)

    # 5. Crear Cita en Estado HOLD_15_MIN
    db_cita = CitaDB(
        paciente_id=paciente.id,
        fecha_hora=bloque_asignado,
        motivo=f"[{nivel_color}] {req.sintomas_o_motivo[:150]}",
        confirmada=False,
        tipo_atencion=tipo_atencion,
        estado_cita="HOLD_15_MIN",
        triaje_urgencia=nivel_urgencia,
        protocolo_primeros_auxilios=primeros_auxilios,
        hold_expira_en=hold_expira,
        monto_pago=monto_sugerido,
        pago_estado="PENDING",
        ventana_horaria=req.preferencia_horario
    )
    db.add(db_cita)
    db.commit()
    db.refresh(db_cita)

    # 6. Notificación asíncrona a n8n
    try:
        n8n_payload = {
            "evento": "nuevo_triaje_urgencia",
            "cita_id": db_cita.id,
            "tutor_nombre": clean_tutor_name,
            "tutor_telefono": clean_phone,
            "paciente_nombre": clean_pet_name,
            "especie": req.especie,
            "sintomas": req.sintomas_o_motivo,
            "triaje_urgencia": nivel_urgencia,
            "triaje_nivel_color": nivel_color,
            "diagnostico_tentativo": diag_tentativo,
            "tipo_atencion": tipo_atencion,
            "bloque_asignado": bloque_asignado.isoformat(),
            "estado_cita": "HOLD_15_MIN"
        }
        with httpx.Client(timeout=4.0) as client:
            client.post(N8N_WEBHOOK_URL, json=n8n_payload)
    except Exception as err:
        logger.warning(f"Aviso a n8n no despachado (n8n no activo): {err}")

    return {
        "cita_id": db_cita.id,
        "triaje_urgencia": nivel_urgencia,
        "triaje_nivel_color": nivel_color,
        "triaje_diagnostico_tentativo": diag_tentativo,
        "protocolo_primeros_auxilios": primeros_auxilios,
        "tipo_atencion_recomendada": tipo_atencion,
        "bloque_asignado": bloque_asignado,
        "hold_expira_en": hold_expira,
        "monto_sugerido": monto_sugerido,
        "bloques_alternativos": bloques_alt,
        "estado_cita": "HOLD_15_MIN",
        "tutor_id": tutor.id,
        "paciente_id": paciente.id
    }

@app.post("/api/v1/covet/appointments/{cita_id}/confirm-payment", response_model=PaymentConfirmResponse)
def confirmar_pago_cita(
    cita_id: int,
    req: PaymentConfirmRequest,
    db: Session = Depends(get_db)
):
    """
    Confirma el pago de la telemedicina o visita:
    Pasa la cita de HOLD_15_MIN a CONFIRMED y notifica a n8n para WhatsApp inmediato.
    """
    cita = db.query(CitaDB).filter(CitaDB.id == cita_id).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    cita.estado_cita = "CONFIRMED"
    cita.confirmada = True
    cita.pago_estado = "SIMULATED_PAID" if req.metodo_pago == "DEMO_SIMULATION" else "PAID_GATEWAY"
    cita.pago_metodo = req.metodo_pago
    if req.monto:
        cita.monto_pago = req.monto
    db.commit()
    db.refresh(cita)

    paciente = db.query(PacienteDB).filter(PacienteDB.id == cita.paciente_id).first()
    tutor = db.query(TutorDB).filter(TutorDB.id == paciente.tutor_id).first() if paciente else None

    # Notificar a n8n el pago exitoso
    n8n_dispatched = False
    try:
        n8n_payload = {
            "evento": "cita_pagada_y_confirmada",
            "cita_id": cita.id,
            "tutor_nombre": tutor.nombre if tutor else "Tutor",
            "tutor_telefono": tutor.telefono if tutor else "",
            "paciente_nombre": paciente.nombre if paciente else "Paciente",
            "tipo_atencion": cita.tipo_atencion,
            "fecha_hora": cita.fecha_hora.isoformat(),
            "monto_pago": cita.monto_pago,
            "pago_metodo": cita.pago_metodo,
            "estado_cita": "CONFIRMED"
        }
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(N8N_WEBHOOK_URL, json=n8n_payload)
            n8n_dispatched = resp.is_success
    except Exception as err:
        logger.warning(f"n8n webhook error en confirmación de pago: {err}")

    return {
        "cita_id": cita.id,
        "estado_cita": "CONFIRMED",
        "pago_estado": cita.pago_estado,
        "pago_metodo": cita.pago_metodo or req.metodo_pago,
        "mensaje": "¡Pago recibido y cupo clínico 100% confirmado! WhatsApp enviado al tutor.",
        "n8n_dispatched": n8n_dispatched,
        "resumen_cita": {
            "fecha_hora": cita.fecha_hora.isoformat(),
            "tipo_atencion": cita.tipo_atencion,
            "paciente": paciente.nombre if paciente else "Paciente",
            "tutor": tutor.nombre if tutor else "Tutor",
            "telefono": tutor.telefono if tutor else ""
        }
    }
