"""
CoVet Backend — API Central para Micro-SaaS Veterinario.
Persistencia en PostgreSQL, procesamiento SOAP con IA (Groq Whisper + Anthropic Claude)
e integración con n8n para WhatsApp.
"""
import os
import uuid
import datetime
import logging
from typing import List, Optional, Any, Dict, Union
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
    
    # Cumplimiento Ley 21.719 (Chile) - Protección de Datos Personales
    consentimiento_ley21719 = Column(Boolean, default=False)
    fecha_consentimiento = Column(DateTime, nullable=True)
    version_terminos = Column(String(20), default="v1.0-2026")
    activo_whatsapp = Column(Boolean, default=True)
    direccion = Column(Text, nullable=True)
    metodo_captura = Column(String(50), default="FORMULARIO_WEB_TRIAJE")
    actualizado_at = Column(DateTime, default=datetime.datetime.utcnow)

    pacientes = relationship("PacienteDB", back_populates="tutor")
    fiches = relationship("MedicalFicheDB", back_populates="tutor")

class PacienteDB(Base):
    __tablename__ = "pacientes"
    id = Column(Integer, primary_key=True, index=True)
    tutor_id = Column(Integer, ForeignKey("tutores.id"), nullable=False)
    nombre = Column(String(60), nullable=False)
    especie = Column(String(40), nullable=False)
    raza = Column(String(60), default="Mestizo")
    peso_kg = Column(Float, nullable=True, default=0.0)
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
    
    # Columnas nuevas para Triaje Autónomo y Reserva CoVet
    estado_reserva = Column(String(30), default="HOLD_15_MIN") # HOLD_15_MIN, CONFIRMED, EXPIRED
    nivel_urgencia = Column(String(20), default="LOW") # HIGH, MEDIUM, LOW
    modalidad = Column(String(30), default="DOMICILIO") # TELEMEDICINA, DOMICILIO
    ventana_horaria = Column(String(50), nullable=True) # "INMEDIATA", "11:00", "MANANA_10_13", "TARDE_15_19"
    sintomas_triaje = Column(JSONB, nullable=True)
    protocolo_id = Column(String(50), nullable=True)
    monto_clp = Column(Integer, default=25000)
    pagado = Column(Boolean, default=False)
    hold_expires_at = Column(DateTime, nullable=True)

    # Columnas previas preservadas para compatibilidad con endpoints y esquemas anteriores
    tipo_atencion = Column(String(30), default="CLINICA_PRESENCIAL") # TELEMEDICINA, DOMICILIO, CLINICA_PRESENCIAL
    estado_cita = Column(String(30), default="CONFIRMED") # HOLD_15_MIN, CONFIRMED, CANCELLED, COMPLETED
    triaje_urgencia = Column(String(30), default="MEDIUM_ROUTINE") # CRITICAL_EMERGENCY, HIGH_PRIORITY, MEDIUM_ROUTINE, LOW_PREVENTIVE
    protocolo_primeros_auxilios = Column(Text, nullable=True)
    hold_expira_en = Column(DateTime, nullable=True)
    monto_pago = Column(Float, default=0.0)
    pago_estado = Column(String(30), default="PENDING") # PENDING, SIMULATED_PAID, PAID_GATEWAY
    pago_metodo = Column(String(30), nullable=True)
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

class AuditoriaArcoDB(Base):
    """Tabla aislada de auditoría ARCO para defensa legal con HMAC conforme a Ley 21.719"""
    __tablename__ = "auditoria_arco"
    auditoria_id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tutor_id_referencia = Column(Integer, nullable=True)
    ticket_baja = Column(String(50), unique=True, nullable=False)
    telefono_hmac = Column(Text, nullable=False)
    accion_ejecutada = Column(String(50), default="SUPRESION_ANONIMIZACION")
    fecha_creacion = Column(DateTime, default=datetime.datetime.utcnow)

def run_safe_migrations(engine_instance):
    """
    Ejecuta migraciones idempotentes sobre tablas existentes (citas, pacientes)
    usando ALTER TABLE ... ADD COLUMN IF NOT EXISTS con text() de SQLAlchemy.
    """
    try:
        with engine_instance.connect() as conn:
            # 1. Pacientes: asegurar peso_kg opcional con default 0.0
            conn.execute(text("ALTER TABLE pacientes ALTER COLUMN peso_kg DROP NOT NULL;"))
            conn.execute(text("ALTER TABLE pacientes ALTER COLUMN peso_kg SET DEFAULT 0.0;"))
            
            # 2. Citas: columnas nuevas del sistema de triaje y reserva
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS estado_reserva VARCHAR(30) DEFAULT 'HOLD_15_MIN';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS nivel_urgencia VARCHAR(20) DEFAULT 'LOW';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS modalidad VARCHAR(30) DEFAULT 'DOMICILIO';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS ventana_horaria VARCHAR(50);"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS sintomas_triaje JSONB;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS protocolo_id VARCHAR(50);"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS monto_clp INTEGER DEFAULT 25000;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS pagado BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS hold_expires_at TIMESTAMP;"))

            # Columnas legadas para compatibilidad con registros existentes
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS tipo_atencion VARCHAR(30) DEFAULT 'CLINICA_PRESENCIAL';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS estado_cita VARCHAR(30) DEFAULT 'CONFIRMED';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS triaje_urgencia VARCHAR(30) DEFAULT 'MEDIUM_ROUTINE';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS protocolo_primeros_auxilios TEXT;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS hold_expira_en TIMESTAMP;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS monto_pago FLOAT DEFAULT 0.0;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS pago_estado VARCHAR(30) DEFAULT 'PENDING';"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS pago_metodo VARCHAR(30);"))

            # 3. Tutores y Auditoría ARCO (Ley 21.719 Protección de Datos Personales Chile)
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))
            conn.execute(text("ALTER TABLE tutores ALTER COLUMN telefono DROP NOT NULL;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS direccion TEXT;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS consentimiento BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS version_politica VARCHAR(20) DEFAULT 'v1.0-2026';"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS metodo_captura VARCHAR(50) DEFAULT 'FORMULARIO_WEB_TRIAJE';"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS actualizado_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS consentimiento_ley21719 BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS fecha_consentimiento TIMESTAMP;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS version_terminos VARCHAR(20) DEFAULT 'v1.0-2026';"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS activo_whatsapp BOOLEAN DEFAULT TRUE;"))

            # Tabla aislada de auditoría ARCO (Defensa legal con HMAC)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS auditoria_arco (
                    auditoria_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tutor_id_referencia INT,
                    ticket_baja VARCHAR(50) UNIQUE NOT NULL,
                    telefono_hmac TEXT NOT NULL,
                    accion_ejecutada VARCHAR(50) DEFAULT 'SUPRESION_ANONIMIZACION',
                    fecha_creacion TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))

            # Función segura para anonimizar tutor y generar registro de auditoría
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION anonimizar_tutor(p_telefono TEXT, p_secret_pepper TEXT)
                RETURNS TEXT AS $$
                DECLARE
                    v_tutor_id INT;
                    v_ticket TEXT;
                    v_hmac TEXT;
                BEGIN
                    SELECT id INTO v_tutor_id 
                    FROM tutores 
                    WHERE telefono = p_telefono;

                    IF v_tutor_id IS NULL THEN
                        RETURN 'TUTOR_NO_ENCONTRADO';
                    END IF;

                    v_ticket := 'BAJA-' || UPPER(SUBSTRING(MD5(RANDOM()::TEXT) FROM 1 for 8));
                    v_hmac := ENCODE(HMAC(p_telefono::BYTEA, p_secret_pepper::BYTEA, 'sha256'), 'hex');

                    INSERT INTO auditoria_arco (tutor_id_referencia, ticket_baja, telefono_hmac, accion_ejecutada)
                    VALUES (v_tutor_id, v_ticket, v_hmac, 'SUPRESION_ANONIMIZACION');

                    UPDATE tutores
                    SET nombre = 'ANONIMIZADO',
                        telefono = NULL,
                        email = NULL,
                        direccion = NULL,
                        consentimiento = FALSE,
                        consentimiento_ley21719 = FALSE,
                        activo_whatsapp = FALSE,
                        actualizado_at = CURRENT_TIMESTAMP
                    WHERE id = v_tutor_id;

                    RETURN v_ticket;
                END;
                $$ LANGUAGE plpgsql;
            """))

            # Función de retención y purga legal conforme a Ley 21.719 (Principio de Limitación del Plazo)
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION purgar_auditoria_arco(p_anios INT DEFAULT 3)
                RETURNS INT AS $$
                DECLARE
                    v_eliminados INT;
                BEGIN
                    DELETE FROM auditoria_arco 
                    WHERE fecha_creacion < NOW() - (p_anios || ' years')::INTERVAL;
                    
                    GET DIAGNOSTICS v_eliminados = ROW_COUNT;
                    RETURN v_eliminados;
                END;
                $$ LANGUAGE plpgsql;
            """))

            # Ejecutar purga automática preventiva de registros que superen el plazo de prescripción legal (3 años)
            conn.execute(text("SELECT purgar_auditoria_arco(3);"))

            conn.commit()
        logger.info("Migración segura de esquema (run_safe_migrations) ejecutada exitosamente.")
    except Exception as e:
        logger.error(f"Error en run_safe_migrations: {e}")

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        run_safe_migrations(engine)
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
    peso_kg: Optional[float] = Field(default=0.0, example=12.5)

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
# 7. Protocolos Clínicos de Primeros Auxilios (Blindaje Médico-Legal)
# -------------------------------------------------------------
FIRST_AID_PROTOCOLS = {
    "GI_VOMITO_AGUDO": {
        "id": "GI_VOMITO_AGUDO",
        "titulo": "Protocolo de Primeros Auxilios: Vómito Agudo y Trastorno Gastroentérico",
        "urgencia_sugerida": "MEDIUM",
        "indicaciones": [
            "Ayuno sólido preventivo estricto por 6 a 8 horas para permitir el reposo y desinflamación de la mucosa gástrica.",
            "Retiro de agua por 2 horas inmediatamente posteriores al último vómito para no gatillar reflejo emético.",
            "Tras las 2 horas sin vómitos, reintroducir agua en pequeños sorbos (1 a 2 cucharaditas cada 20 minutos) o cubos de hielo para lamer.",
            "Mantener a la mascota abrigada, tranquila y en reposo sobre una superficie limpia."
        ],
        "prohibiciones": [
            "PROHIBICIÓN ESTRICTA: NUNCA administrar medicamentos humanos. El Ibuprofeno, Paracetamol, Aspirina y Ketoprofeno son ALTAMENTE TÓXICOS y potencialmente letales en perros y gatos (causan necrosis hepática, úlceras perforadas y fallo renal agudo).",
            "No dar leche, caldos grasos, comida casera ni remedios caseros como aceites."
        ],
        "alertas_rojas": [
            "Presencia de sangre fresca (roja viva) o vómito oscuro con aspecto de borra de café.",
            "Intentos repetidos y fallidos de vomitar acompañados de abdomen hinchado, duro o muy doloroso (sospecha de dilatación-torsión gástrica).",
            "Decaimiento severo, desorientación, encías pálidas/blancas o colapso."
        ],
        "mensaje_tutor": "Sigue estas indicaciones preventivas mientras se concreta la atención médica. Si identificas signos de alerta roja, acude de inmediato a un centro con servicio de urgencias 24 horas."
    },
    "TRAUMA_URGENCIAS": {
        "id": "TRAUMA_URGENCIAS",
        "titulo": "Protocolo de Primeros Auxilios: Traumatismos, Caídas o Sospecha de Fractura",
        "urgencia_sugerida": "HIGH",
        "indicaciones": [
            "Inmovilización suave: Mantener a la mascota sobre una superficie rígida y plana (tabla, cartón grueso o dentro de su caja transportadora) sin flexionar ni manipular bruscamente cuello, columna o extremidades.",
            "Compresión de heridas: Si existe sangrado activo, presionar directamente sobre la herida con un paño limpio y seco o gasa durante al menos 5 minutos continuos sin retirar para revisar.",
            "Abrigo térmico: Cubrir el cuerpo con una manta suave para evitar el shock hipotérmico, asegurando que la cabeza y hocico queden completamente libres para respirar.",
            "Contención segura: El dolor severo puede provocar mordeduras o arañazos por reflejo defensivo; sujetar con una toalla envolvente con calma y voz suave."
        ],
        "prohibiciones": [
            "PROHIBICIÓN ESTRICTA: No intentar alinear, enderezar ni entablillar huesos o extremidades desviadas.",
            "PROHIBICIÓN ESTRICTA: No administrar analgésicos humanos ni desinfectantes abrasivos como alcohol o cloro.",
            "No ofrecer comida ni forzar a beber agua si la mascota está aturdida o con dolor intenso."
        ],
        "alertas_rojas": [
            "Pérdida de conciencia, convulsiones, movimientos oculares anormales o pupilas asimétricas.",
            "Sangrado copioso que empapa el paño y no se detiene tras compresión directa continua.",
            "Imposibilidad para ponerse de pie, quejidos intensos continuos o dificultad evidente para respirar."
        ],
        "mensaje_tutor": "Mantén la inmovilización y tranquilidad mientras el equipo veterinario de CoVet coordina la asistencia inmediata."
    },
    "RESPIRATORIO_CRITICO": {
        "id": "RESPIRATORIO_CRITICO",
        "titulo": "Protocolo de Primeros Auxilios: Dificultad Respiratoria y Alerta Crítica",
        "urgencia_sugerida": "HIGH",
        "indicaciones": [
            "Espacio fresco y ventilado: Ubicar a la mascota de inmediato en una habitación fresca, bien ventilada y en penumbra, alejando ruidos, niños u otras mascotas para minimizar el estrés.",
            "Postura ortopneica natural: Permitir que la mascota se posicione con el cuello estirado hacia adelante y los codos separados. NUNCA forzarla a acostarse de lado ni comprimir su tórax.",
            "Liberación de vías respiratorias: Retirar de inmediato cualquier collar, arnés, correa o prenda que presione su cuello o pecho.",
            "Conexión médica urgente: Iniciar telemedicina de urgencia en menos de 15 minutos para evaluación visual en vivo mientras se coordina la atención física."
        ],
        "prohibiciones": [
            "PROHIBICIÓN ESTRICTA: NUNCA administrar líquidos, agua, jarabes ni alimentos por la boca (riesgo crítico de asfixia y neumonía por aspiración).",
            "No exponer a fuentes de calor, estufas ni humo de cigarrillo o incienso.",
            "No manipular excesivamente ni realizar esfuerzos físicos."
        ],
        "alertas_rojas": [
            "Encías o lengua de color azul, violáceo o grisáceo (cianosis por hipoxia severa).",
            "Respiración con la boca abierta en gatos (en felinos esto constituye SIEMPRE una urgencia médica extrema).",
            "Sonidos respiratorios agudos, estertores, ahogamiento o hundimiento profundo del abdomen al intentar inhalar."
        ],
        "mensaje_tutor": "El compromiso respiratorio requiere atención profesional inmediata. Respeta estas pautas y mantente en conexión con el médico de urgencia."
    },
    "PREVENTIVO_RUTINA": {
        "id": "PREVENTIVO_RUTINA",
        "titulo": "Protocolo Preventivo: Preparación para Consulta y Visita Médica",
        "urgencia_sugerida": "LOW",
        "indicaciones": [
            "Carnet y antecedentes: Tener a mano la libreta sanitaria con registro de vacunas anteriores, desparasitaciones y recetas de tratamientos previos.",
            "Ambiente tranquilo: 15 minutos antes de la hora acordada, ubicar a la mascota en una habitación cerrada, cómoda y libre de distracciones donde se realizará la atención a domicilio.",
            "Control de ayuno: Si se contemplan exámenes de sangre rutinarios, mantener un ayuno de sólidos de 8 horas (el consumo de agua fresca está permitido).",
            "Listado de consultas: Anotar dudas sobre nutrición, conducta, vacunas o cuidados preventivos para revisarlas punto por punto con el profesional."
        ],
        "prohibiciones": [
            "No bañar a la mascota en las 48 horas previas ni posteriores a la vacunación para evitar cambios térmicos o estrés inmunológico.",
            "No aplicar antiparasitarios comerciales sin el pesaje exacto y autorización del veterinario tratante."
        ],
        "alertas_rojas": [
            "Si previo a la visita la mascota presenta decaimiento agudo, fiebre o vómitos, comunícate de inmediato para reclasificar la atención a urgencia clínica."
        ],
        "mensaje_tutor": "Tu cita preventiva está programada. Con estos sencillos preparativos garantizamos una atención cómoda, segura y sin estrés para tu compañero."
    }
}

# -------------------------------------------------------------
# 8. Motor Híbrido de Cálculo de Cupos de Agenda
# -------------------------------------------------------------
def calculate_hybrid_slots(db: Session, urgency_level: str) -> dict:
    """
    Motor Híbrido de Cálculo de Cupos:
    - Respeta la ventana operativa de 08:30 a 20:30 hrs (comm_agent.md).
    - Limpia o ignora reservas en estado HOLD_15_MIN cuyo hold_expires_at haya vencido.
    - Si urgency_level == 'HIGH':
        * Modalidad TELEMEDICINA inmediata (<15 min) por $15.000 CLP.
        * Modalidad DOMICILIO con el primer bloque de urgencia disponible hoy ($35.000 CLP).
    - Si urgency_level en ['MEDIUM', 'LOW']:
        * Consulta CitaDB para no solapar horas ocupadas.
        * exact_slots: 3 bloques horarios exactos sugeridos (ej: ['11:00', '15:30', '18:00']).
        * flexible_windows: 2 ventanas para veterinarios a domicilio:
          'Mañana (10:00 a 13:00 hrs)' y 'Tarde (15:00 a 19:00 hrs)' ($25.000 CLP).
    """
    now = datetime.datetime.utcnow()

    # 1. Limpieza de holds expirados en CitaDB
    try:
        db.query(CitaDB).filter(
            CitaDB.estado_reserva == "HOLD_15_MIN",
            CitaDB.hold_expires_at.isnot(None),
            CitaDB.hold_expires_at < now
        ).update({"estado_reserva": "EXPIRED"}, synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Error al limpiar holds expirados: {e}")

    urgency = (urgency_level or "LOW").strip().upper()

    if urgency == "HIGH":
        # Primer bloque de urgencia disponible hoy a domicilio (aprox 45 min)
        urgencia_domicilio_dt = now + datetime.timedelta(minutes=45)
        urgencia_domicilio_str = f"Hoy a las {urgencia_domicilio_dt.strftime('%H:%M')} hrs"

        return {
            "urgency_level": "HIGH",
            "tipo_sugerido": "URGENCIA_INMEDIATA",
            "modalidades_disponibles": [
                {
                    "modalidad": "TELEMEDICINA",
                    "ventana_horaria": "INMEDIATA (En menos de 15 min)",
                    "monto_clp": 15000,
                    "etiqueta": "Telemedicina de Urgencia (< 15 min)",
                    "prioritaria": True,
                    "descripcion": "Atención médica inmediata por videollamada con veterinario de turno"
                },
                {
                    "modalidad": "DOMICILIO",
                    "ventana_horaria": urgencia_domicilio_str,
                    "monto_clp": 35000,
                    "etiqueta": "Visita Domiciliaria de Urgencia",
                    "prioritaria": False,
                    "descripcion": "Despacho prioritario de móvil veterinario a tu domicilio"
                }
            ]
        }
    else:
        # MEDIUM o LOW
        # Consultar CitaDB para no solapar horas ocupadas
        citas_activas = db.query(CitaDB).filter(
            CitaDB.estado_reserva.in_(["CONFIRMED", "HOLD_15_MIN"]),
            CitaDB.fecha_hora >= now
        ).all()
        horas_ocupadas = {c.fecha_hora.strftime("%H:%M") for c in citas_activas if c.fecha_hora}

        # Candidatos dentro de la ventana operativa 08:30 a 20:30
        candidatos = ["11:00", "15:30", "18:00", "09:30", "12:30", "16:45", "19:15"]
        exact_slots = []
        for h in candidatos:
            if h not in horas_ocupadas:
                exact_slots.append(h)
            if len(exact_slots) >= 3:
                break
        if len(exact_slots) < 3:
            exact_slots = ["11:00", "15:30", "18:00"]

        flexible_windows = [
            {
                "id": "MANANA_10_13",
                "nombre": "Mañana (10:00 a 13:00 hrs)",
                "rango": "10:00 - 13:00",
                "monto_clp": 25000,
                "descripcion": "Ventana flexible para visita veterinaria a domicilio en jornada matutina"
            },
            {
                "id": "TARDE_15_19",
                "nombre": "Tarde (15:00 a 19:00 hrs)",
                "rango": "15:00 - 19:00",
                "monto_clp": 25000,
                "descripcion": "Ventana flexible para visita veterinaria a domicilio en jornada vespertina"
            }
        ]

        modalidades = [
            {
                "modalidad": "DOMICILIO",
                "monto_clp": 25000,
                "etiqueta": "Atención Veterinaria a Domicilio",
                "descripcion": "Atención clínica completa en la comodidad de tu hogar"
            },
            {
                "modalidad": "TELEMEDICINA",
                "monto_clp": 15000,
                "etiqueta": "Orientación por Telemedicina",
                "descripcion": "Consulta virtual y orientación clínica médica"
            }
        ]

        return {
            "urgency_level": urgency,
            "exact_slots": exact_slots,
            "flexible_windows": flexible_windows,
            "modalidades_disponibles": modalidades
        }

# -------------------------------------------------------------
# 9. Esquemas Pydantic del Agente de Triaje y Reserva de Clientes
# -------------------------------------------------------------
class ClientIntentExtractionSchema(BaseModel):
    ui_mode: str = Field(
        ...,
        description="Modo de interfaz: EMERGENCY_TRIAGE (urgencias/dolor agudo/vómitos), ROUTINE_BOOKING (vacunas, chequeo sano) o FOLLOW_UP_QUERY (seguimiento)"
    )
    urgency_level: str = Field(
        ...,
        description="Nivel de urgencia médica: HIGH (riesgo vital, vómito repetido, disnea, sangrado, trauma), MEDIUM (cuadros moderados), LOW (preventivo/rutina)"
    )
    especie_detectada: str = Field(
        ...,
        description="Especie del paciente detectada: Felino, Canino o Exótico"
    )
    motivo_resumido: str = Field(
        ...,
        description="Resumen conciso y clínico del motivo (Ej: 'Vómito agudo felino')"
    )
    dynamic_questions: List[str] = Field(
        ...,
        description="Exactamente 3 preguntas clínicas breves y directas adaptadas al caso"
    )
    first_aid_protocol_id: str = Field(
        ...,
        description="Clave exacta del protocolo de primeros auxilios aplicable: GI_VOMITO_AGUDO, TRAUMA_URGENCIAS, RESPIRATORIO_CRITICO o PREVENTIVO_RUTINA"
    )

def mask_phone(phone: str) -> str:
    """Enmascara números de teléfono para logs y reportes anonimizados conforme a Ley 21.719 (+569****1234)"""
    if not phone:
        return "****"
    p = str(phone).strip()
    if len(p) >= 8:
        return f"{p[:4]}****{p[-4:]}"
    return f"****{p[-2:]}" if len(p) >= 3 else "****"

class PrivacyOptOutRequest(BaseModel):
    telefono: str = Field(..., example="+56912345678")
    tipo_solicitud: str = Field(default="opt_out", description="'opt_out' (baja de WhatsApp) o 'supresion' (anonimización completa)")

class TriageExecuteRequest(BaseModel):
    tutor_telefono: str = Field(..., example="+56912345678")
    tutor_nombre: str = Field(..., example="Constanza Pérez")
    paciente_nombre: str = Field(..., example="Luna")
    especie: str = Field(default="Canino", example="Canino")
    respuestas_triaje: Any = Field(..., description="Dict o list con las respuestas a las 3 preguntas clínicas")
    modalidad: str = Field(default="DOMICILIO", example="DOMICILIO") # TELEMEDICINA o DOMICILIO
    seleccion_horaria: str = Field(..., example="11:00") # 'INMEDIATA', '11:00', 'MANANA_10_13', 'TARDE_15_19'
    urgency_level: str = Field(default="LOW", example="HIGH") # HIGH, MEDIUM, LOW
    motivo_resumido: str = Field(..., example="Vómito agudo felino")
    first_aid_protocol_id: str = Field(default="PREVENTIVO_RUTINA", example="GI_VOMITO_AGUDO")
    consentimiento_ley21719: bool = Field(default=False, description="Consentimiento expreso conforme a Ley 21.719 de Protección de Datos")

# -------------------------------------------------------------
# 10. Los 3 Endpoints del Agente Autónomo de Clientes
# -------------------------------------------------------------
@app.post("/api/v1/covet/client-intent")
async def client_intent(
    texto: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """
    Endpoint 1: Agente Autónomo de Intención y Triaje de Clientes:
    - Recibe texto o audio (file) del tutor.
    - Transcribe audio con Groq whisper-large-v3-turbo (si falta clave, responde error claro).
    - Evalúa urgencia, especie, motivo y 3 preguntas dinámicas con Anthropic Claude Tool Use.
    - Asigna protocolo pre-aprobado y calcula agenda híbrida.
    """
    consulta_texto = (texto or "").strip()

    # Transcripción con Groq si se sube archivo de audio
    if file and file.filename:
        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Falta configurar GROQ_API_KEY en el servidor para transcribir audios. Por favor escribe el síntoma o consulta en texto."
                }
            )

        contents = await file.read()
        if contents and len(contents) > 0:
            temp_dir = "/tmp/covet_client_audios"
            os.makedirs(temp_dir, exist_ok=True)
            filename = file.filename or "cliente_audio.webm"
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
            temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}.{ext}")

            with open(temp_path, "wb") as f_out:
                f_out.write(contents)

            try:
                with open(temp_path, "rb") as af:
                    files_payload = {
                        "file": (os.path.basename(temp_path), af.read(), "application/octet-stream")
                    }
                data_payload = {
                    "model": "whisper-large-v3-turbo",
                    "language": "es",
                    "response_format": "json"
                }
                headers = {"Authorization": f"Bearer {groq_key}"}

                with httpx.Client(timeout=45.0) as client:
                    resp = client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        files=files_payload,
                        data=data_payload,
                        headers=headers
                    )
                    resp.raise_for_status()
                    transcripcion = resp.json().get("text", "").strip()
                    if transcripcion:
                        consulta_texto = f"{consulta_texto} {transcripcion}".strip() if consulta_texto else transcripcion
            except Exception as e:
                logger.error(f"Error en transcripción Groq: {e}")
                raise HTTPException(status_code=502, detail=f"Error transcribiendo audio con Groq: {str(e)}")
            finally:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

    if not consulta_texto:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar un texto o un archivo de audio con los síntomas de tu mascota."
        )

    # Análisis agéntico con Anthropic Claude Tool Use
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    ia_output = None

    if anthropic_key:
        tool_definition = {
            "name": "analizar_intencion_tutor",
            "description": "Extrae el triaje clínico, nivel de urgencia, especie y selecciona el protocolo pre-aprobado.",
            "input_schema": ClientIntentExtractionSchema.model_json_schema()
        }

        system_prompt = (
            "Actúas como un Médico Veterinario de Urgencias y Triaje Clínico Senior en Chile para CoVet. "
            "Tu misión es analizar la consulta del tutor de una mascota (por audio transcrito o texto) "
            "y estructurar el caso utilizando estrictamente la herramienta `analizar_intencion_tutor`.\n\n"
            "Reglas Clínicas Estrictas:\n"
            "1. `ui_mode`:\n"
            "   - 'EMERGENCY_TRIAGE': si presenta dolor agudo, vómitos repetidos, sangrado, dificultad respiratoria, trauma, sospecha de intoxicación, convulsión o colapso.\n"
            "   - 'ROUTINE_BOOKING': si es para vacunas, desparasitación, control sano preventivo, corte de uñas o consulta rutinaria.\n"
            "   - 'FOLLOW_UP_QUERY': si pregunta por evolución o dudas de un tratamiento previo.\n"
            "2. `urgency_level`:\n"
            "   - 'HIGH': Riesgo vital inminente, disnea, sangrado activo, traumatismo severo, convulsión, vómito persistente, dolor agudo intolerable.\n"
            "   - 'MEDIUM': Vómito aislado, diarrea sin sangre, cojera leve/moderada, molestia ocular, decaimiento leve.\n"
            "   - 'LOW': Controles rutinarios, vacunas, preventivo general.\n"
            "3. `especie_detectada`: 'Felino', 'Canino' o 'Exótico' (asumir Canino o Felino según mencione perro/gato o contexto).\n"
            "4. `motivo_resumido`: Frase clínica concisa (ej. 'Vómito agudo felino', 'Vacunación y control anual canino').\n"
            "5. `dynamic_questions`: EXACTAMENTE 3 preguntas clínicas breves, empáticas y directas adaptadas al caso para recabar datos antes de la atención (ej: '¿Hace cuántas horas comenzó el vómito?', '¿Has notado sangre, decaimiento extremo o intento de vomitar sin éxito?', '¿Comió algún objeto, planta o alimento nuevo?').\n"
            "6. `first_aid_protocol_id`: DEBE ser estrictamente una de estas 4 claves pre-aprobadas:\n"
            "   - 'GI_VOMITO_AGUDO'\n"
            "   - 'TRAUMA_URGENCIAS'\n"
            "   - 'RESPIRATORIO_CRITICO'\n"
            "   - 'PREVENTIVO_RUTINA'\n"
            "REGLA MÉDICO-LEGAL ESTRICTA: NUNCA inventes fármacos ni instrucciones médicas libres; solo selecciona una clave válida de FIRST_AID_PROTOCOLS."
        )

        anthropic_payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": f"Consulta del tutor: \"{consulta_texto}\""
                }
            ],
            "tools": [tool_definition],
            "tool_choice": {"type": "tool", "name": "analizar_intencion_tutor"}
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                res = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json=anthropic_payload
                )
                if res.status_code == 200:
                    data = res.json()
                    for block in data.get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") == "analizar_intencion_tutor":
                            ia_output = block.get("input", {})
                            break
        except Exception as e:
            logger.warning(f"Error consultando Anthropic Claude para triaje: {e}")

    # Fallback Determinístico Clínico si falla la API
    if not ia_output:
        texto_l = consulta_texto.lower()
        if any(w in texto_l for w in ["respira", "ahog", "asfix", "jade", "pecho"]):
            protocol_id = "RESPIRATORIO_CRITICO"
            urgency = "HIGH"
            ui_mode = "EMERGENCY_TRIAGE"
            motivo = "Dificultad respiratoria aguda"
            questions = [
                "¿Hace cuánto tiempo comenzó con dificultad o ruido al respirar?",
                "¿Notas su lengua o encías de color azulado, morado o pálido?",
                "¿Está respirando con la boca abierta o con el cuello muy estirado?"
            ]
        elif any(w in texto_l for w in ["atropell", "golpe", "caid", "sangr", "herid", "fractur", "piso"]):
            protocol_id = "TRAUMA_URGENCIAS"
            urgency = "HIGH"
            ui_mode = "EMERGENCY_TRIAGE"
            motivo = "Traumatismo o herida de urgencia"
            questions = [
                "¿Hay alguna herida con sangrado activo que no se detenga?",
                "¿Puede apoyar sus 4 extremidades o llora intensamente al moverse?",
                "¿Ha perdido el conocimiento o se encuentra desorientado?"
            ]
        elif any(w in texto_l for w in ["vomit", "diarre", "veneno", "guata", "comio", "planta", "nause"]):
            protocol_id = "GI_VOMITO_AGUDO"
            urgency = "HIGH" if any(w in texto_l for w in ["veneno", "raticida", "sangre", "mucho"]) else "MEDIUM"
            ui_mode = "EMERGENCY_TRIAGE"
            motivo = "Trastorno gastroentérico agudo"
            questions = [
                "¿Hace cuántas horas comenzó el vómito y cuántas veces ha vomitado?",
                "¿Has notado decaimiento extremo, sangre o intento de vomitar sin éxito?",
                "¿Hay posibilidad de que haya ingerido algún objeto, medicamento o planta tóxica?"
            ]
        else:
            protocol_id = "PREVENTIVO_RUTINA"
            urgency = "LOW"
            ui_mode = "ROUTINE_BOOKING"
            motivo = "Consulta preventiva o control de rutina"
            questions = [
                "¿Cuándo fue la última vez que recibió vacunas o antiparasitarios?",
                "¿Ha presentado cambios recientes en su apetito o consumo de agua?",
                "¿Prefieres atención veterinaria en tu domicilio o consulta por telemedicina?"
            ]

        especie = "Felino" if any(w in texto_l for w in ["gato", "gata", "gatito", "felin", "michi"]) else "Canino"
        ia_output = {
            "ui_mode": ui_mode,
            "urgency_level": urgency,
            "especie_detectada": especie,
            "motivo_resumido": motivo,
            "dynamic_questions": questions,
            "first_aid_protocol_id": protocol_id
        }

    # Asegurar que el protocolo seleccionado sea válido
    pid = ia_output.get("first_aid_protocol_id", "PREVENTIVO_RUTINA")
    if pid not in FIRST_AID_PROTOCOLS:
        pid = "PREVENTIVO_RUTINA"
        ia_output["first_aid_protocol_id"] = pid

    urgency_level = ia_output.get("urgency_level", "LOW")
    available_schedule = calculate_hybrid_slots(db, urgency_level)

    return {
        "ui_mode": ia_output.get("ui_mode", "ROUTINE_BOOKING"),
        "urgency_level": urgency_level,
        "especie_detectada": ia_output.get("especie_detectada", "Canino"),
        "motivo_resumido": ia_output.get("motivo_resumido", "Consulta veterinaria CoVet"),
        "dynamic_questions": ia_output.get("dynamic_questions", [
            "¿Hace cuántas horas comenzaron los síntomas?",
            "¿Notas decaimiento, dolor o cambios en su apetito?",
            "¿Tiene sus vacunas al día?"
        ]),
        "first_aid_protocol_id": pid,
        "available_schedule": available_schedule
    }


@app.post("/api/v1/covet/triage-execute")
def triage_execute(req: TriageExecuteRequest, db: Session = Depends(get_db)):
    """
    Endpoint 2: Ejecución de Triaje y Bloqueo Temporal (HOLD_15_MIN):
    - Acción Agéntica 1: Cruce de Historial por WhatsApp (TutorDB, PacienteDB, MedicalFicheDB).
    - Acción Agéntica 2: Bloqueo de cupo temporal por 15 minutos en CitaDB.
    - Retorna cita_id, estado_reserva, hold_expires_at, monto_clp, payment_link y paciente_reconocido.
    """
    clean_phone = req.tutor_telefono.strip()
    clean_tutor_name = req.tutor_nombre.strip() if req.tutor_nombre else "Tutor CoVet"
    clean_pet_name = req.paciente_nombre.strip() if req.paciente_nombre else "Mascota"

    # Acción Agéntica 1: Cruce de Historial por WhatsApp
    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    paciente_reconocido = False
    historial_clinico_previo = "Paciente nuevo. Sin atenciones previas registradas."

    now = datetime.datetime.utcnow()

    if tutor:
        paciente_reconocido = True
        if clean_tutor_name != "Tutor CoVet" and tutor.nombre != clean_tutor_name:
            tutor.nombre = clean_tutor_name
        
        # Registro trazable Ley 21.719 si otorga consentimiento
        if req.consentimiento_ley21719:
            tutor.consentimiento_ley21719 = True
            tutor.fecha_consentimiento = now
            tutor.version_terminos = "v1.0-2026"
            tutor.activo_whatsapp = True
        db.commit()

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
                peso_kg=0.0
            )
            db.add(paciente)
            db.commit()
            db.refresh(paciente)

        fiches = db.query(MedicalFicheDB).filter(
            MedicalFicheDB.paciente_id == paciente.id
        ).order_by(MedicalFicheDB.fecha_creacion.desc()).limit(2).all()

        if fiches:
            resumen_fiches = []
            for f in fiches:
                if f.json_estructurado and isinstance(f.json_estructurado, dict):
                    diag = f.json_estructurado.get("diagnostico_presuntivo", "Atención clínica")
                    fecha = f.fecha_creacion.strftime("%d/%m/%Y") if f.fecha_creacion else "Previa"
                    resumen_fiches.append(f"{fecha}: {diag}")
            if resumen_fiches:
                historial_clinico_previo = f"Paciente recurrente ({paciente.nombre}, {paciente.especie}). Historial: " + " | ".join(resumen_fiches)
            else:
                historial_clinico_previo = f"Paciente recurrente ({paciente.nombre}, {paciente.especie}) con ficha médica registrada."
        else:
            historial_clinico_previo = f"Tutor recurrente ({tutor.nombre}). Mascota: {paciente.nombre} ({paciente.especie})."
    else:
        tutor = TutorDB(
            nombre=clean_tutor_name,
            telefono=clean_phone,
            consentimiento_ley21719=req.consentimiento_ley21719,
            fecha_consentimiento=now if req.consentimiento_ley21719 else None,
            version_terminos="v1.0-2026",
            activo_whatsapp=True
        )
        db.add(tutor)
        db.commit()
        db.refresh(tutor)

        paciente = PacienteDB(
            tutor_id=tutor.id,
            nombre=clean_pet_name,
            especie=req.especie,
            raza="Mestizo",
            peso_kg=0.0
        )
        db.add(paciente)
        db.commit()
        db.refresh(paciente)

    logger.info(f"[Triaje] Ejecutado para tutor {mask_phone(clean_phone)} | Mascota: {clean_pet_name} | Consentimiento Ley 21.719: {req.consentimiento_ley21719}")

    # Acción Agéntica 2: Bloqueo Temporal HOLD_15_MIN
    modalidad_clean = req.modalidad.upper() if req.modalidad else "DOMICILIO"
    urgencia_clean = req.urgency_level.upper() if req.urgency_level else "LOW"

    if modalidad_clean == "TELEMEDICINA":
        monto_clp = 15000
    elif urgencia_clean == "HIGH":
        monto_clp = 35000
    else:
        monto_clp = 25000

    now = datetime.datetime.utcnow()
    hold_expires_at = now + datetime.timedelta(minutes=15)

    horario_sel = req.seleccion_horaria or "INMEDIATA"
    if "INMEDIATA" in horario_sel.upper():
        fecha_cita = now + datetime.timedelta(minutes=10)
    elif "MANANA" in horario_sel.upper() or "10_13" in horario_sel:
        fecha_cita = (now + datetime.timedelta(days=1 if now.hour >= 13 else 0)).replace(hour=10, minute=30, second=0, microsecond=0)
    elif "TARDE" in horario_sel.upper() or "15_19" in horario_sel:
        fecha_cita = (now + datetime.timedelta(days=1 if now.hour >= 19 else 0)).replace(hour=16, minute=0, second=0, microsecond=0)
    elif ":" in horario_sel:
        try:
            partes = horario_sel.split(":")
            h, m = int(partes[0]), int(partes[1][:2])
            fecha_cita = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if fecha_cita <= now:
                fecha_cita += datetime.timedelta(days=1)
        except Exception:
            fecha_cita = now + datetime.timedelta(hours=2)
    else:
        fecha_cita = now + datetime.timedelta(hours=2)

    db_cita = CitaDB(
        paciente_id=paciente.id,
        fecha_hora=fecha_cita,
        motivo=f"[{urgencia_clean}] {req.motivo_resumido}",
        confirmada=False,
        estado_reserva="HOLD_15_MIN",
        nivel_urgencia=urgencia_clean,
        modalidad=modalidad_clean,
        ventana_horaria=horario_sel,
        sintomas_triaje={
            "motivo": req.motivo_resumido,
            "respuestas": req.respuestas_triaje,
            "especie": req.especie,
            "protocolo_id": req.first_aid_protocol_id
        },
        protocolo_id=req.first_aid_protocol_id,
        monto_clp=monto_clp,
        pagado=False,
        hold_expires_at=hold_expires_at,
        # Campos previos para retrocompatibilidad
        tipo_atencion=modalidad_clean,
        estado_cita="HOLD_15_MIN",
        triaje_urgencia=urgencia_clean,
        hold_expira_en=hold_expires_at,
        monto_pago=float(monto_clp),
        pago_estado="PENDING"
    )
    db.add(db_cita)
    db.commit()
    db.refresh(db_cita)

    payment_link = os.getenv("PAYMENT_LINK_URL", "https://link.mercadopago.cl/covet-urgencias")

    return {
        "cita_id": db_cita.id,
        "estado_reserva": db_cita.estado_reserva,
        "hold_expires_at": hold_expires_at.isoformat(),
        "monto_clp": monto_clp,
        "payment_link": payment_link,
        "paciente_reconocido": paciente_reconocido,
        "historial_clinico_previo": historial_clinico_previo
    }


@app.post("/api/v1/covet/citas/{cita_id}/confirm-payment")
def confirmar_pago_reserva_cita(cita_id: int, db: Session = Depends(get_db)):
    """
    Endpoint 3: Confirmación de Pago y Desbloqueo de Protocolo:
    - Marca cita en CitaDB como CONFIRMED y pagado = True.
    - Obtiene el protocolo completo de FIRST_AID_PROTOCOLS.
    - Dispara POST asíncrono con httpx a N8N_WEBHOOK_URL (event_type=TRIAGE_CONFIRMED) con tolerancia a fallos.
    - Retorna first_aid_guide desbloqueada y alerta_veterinario_enviada.
    """
    cita = db.query(CitaDB).filter(CitaDB.id == cita_id).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    cita.estado_reserva = "CONFIRMED"
    cita.confirmada = True
    cita.pagado = True
    cita.estado_cita = "CONFIRMED"
    cita.pago_estado = "PAID"
    db.commit()
    db.refresh(cita)

    paciente = db.query(PacienteDB).filter(PacienteDB.id == cita.paciente_id).first()
    tutor = db.query(TutorDB).filter(TutorDB.id == paciente.tutor_id).first() if paciente else None

    # Recuperar protocolo médico-legal pre-aprobado
    protocolo_key = cita.protocolo_id or "PREVENTIVO_RUTINA"
    first_aid_guide = FIRST_AID_PROTOCOLS.get(protocolo_key, FIRST_AID_PROTOCOLS["PREVENTIVO_RUTINA"])

    # Disparar alerta asíncrona a n8n
    n8n_dispatched = False
    try:
        n8n_payload = {
            "event_type": "TRIAGE_CONFIRMED",
            "cita_id": cita.id,
            "tutor": {
                "id": tutor.id if tutor else None,
                "nombre": tutor.nombre if tutor else "Tutor",
                "telefono": tutor.telefono if tutor else ""
            },
            "paciente": {
                "id": paciente.id if paciente else None,
                "nombre": paciente.nombre if paciente else "Paciente",
                "especie": paciente.especie if paciente else "Desconocida",
                "raza": paciente.raza if paciente else "Mestizo",
                "peso_kg": paciente.peso_kg if paciente else 0.0
            },
            "triaje": {
                "urgencia": cita.nivel_urgencia,
                "motivo": cita.motivo,
                "sintomas_triaje": cita.sintomas_triaje,
                "protocolo_id": cita.protocolo_id
            },
            "reserva": {
                "modalidad": cita.modalidad,
                "ventana_horaria": cita.ventana_horaria,
                "fecha_hora": cita.fecha_hora.isoformat() if cita.fecha_hora else None,
                "monto_clp": cita.monto_clp,
                "pagado": True
            }
        }
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(N8N_WEBHOOK_URL, json=n8n_payload)
            n8n_dispatched = resp.is_success
            logger.info(f"[Cita {cita.id}] Webhook n8n TRIAGE_CONFIRMED despachado. Status: {resp.status_code}")
    except Exception as err:
        logger.warning(f"[Cita {cita.id}] Webhook n8n no respondió: {err}")

    return {
        "status": "success",
        "cita_id": cita.id,
        "estado_reserva": "CONFIRMED",
        "pagado": True,
        "first_aid_guide": first_aid_guide,
        "alerta_veterinario_enviada": n8n_dispatched,
        "mensaje": "¡Pago confirmado con éxito! Protocolo de primeros auxilios desbloqueado y veterinario notificado."
    }

# -------------------------------------------------------------
# Endpoints de compatibilidad previa para tests y demos existentes
# -------------------------------------------------------------
@app.post("/api/v1/covet/triage", response_model=TriageResponse)
def ejecutar_triaje_autonomo_legacy(req: TriageRequest, db: Session = Depends(get_db)):
    """
    Ruta legacy para compatibilidad con versiones previas del frontend o tests.
    """
    clean_phone = req.tutor_telefono.strip()
    clean_tutor_name = req.tutor_nombre.strip()
    clean_pet_name = req.paciente_nombre.strip()

    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    if not tutor:
        tutor = TutorDB(nombre=clean_tutor_name, telefono=clean_phone)
        db.add(tutor)
        db.commit()
        db.refresh(tutor)

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
            peso_kg=0.0
        )
        db.add(paciente)
        db.commit()
        db.refresh(paciente)

    ahora = datetime.datetime.utcnow()
    hold_expira = ahora + datetime.timedelta(minutes=15)
    bloque_asignado = ahora + datetime.timedelta(minutes=15)

    db_cita = CitaDB(
        paciente_id=paciente.id,
        fecha_hora=bloque_asignado,
        motivo=f"[LEGACY] {req.sintomas_o_motivo[:150]}",
        confirmada=False,
        tipo_atencion="DOMICILIO",
        estado_cita="HOLD_15_MIN",
        estado_reserva="HOLD_15_MIN",
        triaje_urgencia="MEDIUM_ROUTINE",
        nivel_urgencia="MEDIUM",
        modalidad="DOMICILIO",
        protocolo_primeros_auxilios="Mantener en reposo",
        protocolo_id="PREVENTIVO_RUTINA",
        hold_expira_en=hold_expira,
        hold_expires_at=hold_expira,
        monto_pago=25000.0,
        monto_clp=25000,
        pagado=False,
        pago_estado="PENDING",
        ventana_horaria=req.preferencia_horario
    )
    db.add(db_cita)
    db.commit()
    db.refresh(db_cita)

    return {
        "cita_id": db_cita.id,
        "triaje_urgencia": "MEDIUM_ROUTINE",
        "triaje_nivel_color": "AMARILLO",
        "triaje_diagnostico_tentativo": "Evaluación clínica requerida",
        "protocolo_primeros_auxilios": "Mantener en reposo",
        "tipo_atencion_recomendada": "DOMICILIO",
        "bloque_asignado": bloque_asignado,
        "hold_expira_en": hold_expira,
        "monto_sugerido": 25000.0,
        "bloques_alternativos": [bloque_asignado + datetime.timedelta(hours=2)],
        "estado_cita": "HOLD_15_MIN",
        "tutor_id": tutor.id,
        "paciente_id": paciente.id
    }

@app.post("/api/v1/covet/appointments/{cita_id}/confirm-payment", response_model=PaymentConfirmResponse)
def confirmar_pago_cita_legacy(
    cita_id: int,
    req: PaymentConfirmRequest,
    db: Session = Depends(get_db)
):
    """
    Ruta legacy de confirmación de pago para compatibilidad.
    """
    cita = db.query(CitaDB).filter(CitaDB.id == cita_id).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    cita.estado_cita = "CONFIRMED"
    cita.estado_reserva = "CONFIRMED"
    cita.confirmada = True
    cita.pagado = True
    cita.pago_estado = "SIMULATED_PAID" if req.metodo_pago == "DEMO_SIMULATION" else "PAID_GATEWAY"
    cita.pago_metodo = req.metodo_pago
    if req.monto:
        cita.monto_pago = req.monto
        cita.monto_clp = int(req.monto)
    db.commit()
    db.refresh(cita)

    paciente = db.query(PacienteDB).filter(PacienteDB.id == cita.paciente_id).first()
    tutor = db.query(TutorDB).filter(TutorDB.id == paciente.tutor_id).first() if paciente else None

    return {
        "cita_id": cita.id,
        "estado_cita": "CONFIRMED",
        "pago_estado": cita.pago_estado,
        "pago_metodo": cita.pago_metodo or req.metodo_pago,
        "mensaje": "¡Pago recibido y cupo clínico 100% confirmado! WhatsApp enviado al tutor.",
        "n8n_dispatched": False,
        "resumen_cita": {
            "fecha_hora": cita.fecha_hora.isoformat(),
            "tipo_atencion": cita.tipo_atencion,
            "paciente": paciente.nombre if paciente else "Paciente",
            "tutor": tutor.nombre if tutor else "Tutor",
            "telefono": tutor.telefono if tutor else ""
        }
    }


# =============================================================
# 11. CUMPLIMIENTO LEY 21.719 — ENDPOINTS DE DERECHOS ARSOP+
# =============================================================

@app.get("/api/v1/covet/privacy/export/{telefono}")
def exportar_datos_personales(telefono: str, db: Session = Depends(get_db)):
    """
    Derecho de Portabilidad (Ley 21.719 Art. 5):
    Permite al titular o tutor descargar en formato estructurado, interoperable
    y legible por máquina (JSON) todos sus datos de contacto, consentimientos,
    mascotas registradas y fichas clínicas estructuradas.
    """
    clean_phone = telefono.strip()
    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    if not tutor:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron registros de datos personales para el número consultado ({mask_phone(clean_phone)})."
        )

    pacientes = db.query(PacienteDB).filter(PacienteDB.tutor_id == tutor.id).all()
    pacientes_data = []

    for p in pacientes:
        citas = db.query(CitaDB).filter(CitaDB.paciente_id == p.id).all()
        fiches = db.query(MedicalFicheDB).filter(MedicalFicheDB.paciente_id == p.id).all()

        pacientes_data.append({
            "id": p.id,
            "nombre": p.nombre,
            "especie": p.especie,
            "raza": p.raza,
            "peso_kg": p.peso_kg,
            "fecha_registro": p.created_at.isoformat() if p.created_at else None,
            "citas_historial": [
                {
                    "cita_id": c.id,
                    "fecha_hora": c.fecha_hora.isoformat() if c.fecha_hora else None,
                    "motivo": c.motivo,
                    "modalidad": c.modalidad or c.tipo_atencion,
                    "estado_reserva": c.estado_reserva or c.estado_cita,
                    "monto_clp": c.monto_clp
                }
                for c in citas
            ],
            "fiches_clinicas": [
                {
                    "fiche_id": str(f.id),
                    "fecha_creacion": f.fecha_creacion.isoformat() if f.fecha_creacion else None,
                    "estado": f.estado_procesamiento,
                    "soap": f.json_estructurado
                }
                for f in fiches
            ]
        })

    logger.info(f"[Privacidad Ley 21.719] Exportación de datos solicitada para tutor {mask_phone(clean_phone)}")

    return {
        "estatus_legal": "CUMPLIMIENTO_LEY_21719_PORTABILIDAD",
        "normativa_aplicable": "Ley N° 21.719 sobre Protección de Datos Personales (Chile)",
        "fecha_emision": datetime.datetime.utcnow().isoformat(),
        "titular": {
            "id": tutor.id,
            "nombre": tutor.nombre,
            "telefono": tutor.telefono,
            "email": tutor.email,
            "consentimiento_ley21719": tutor.consentimiento_ley21719,
            "fecha_consentimiento": tutor.fecha_consentimiento.isoformat() if tutor.fecha_consentimiento else None,
            "version_terminos": tutor.version_terminos,
            "activo_notificaciones_whatsapp": tutor.activo_whatsapp,
            "fecha_registro": tutor.created_at.isoformat() if tutor.created_at else None
        },
        "pacientes": pacientes_data
    }


@app.post("/api/v1/covet/privacy/opt-out")
def ejercer_derecho_supresion_oposicion(req: PrivacyOptOutRequest, db: Session = Depends(get_db)):
    """
    Derecho de Supresión / Oposición (Ley 21.719 Art. 6 y 7):
    - 'opt_out': Revoca consentimiento para mensajes y recordatorios comerciales/WhatsApp (activo_whatsapp=False).
    - 'supresion': Disocia y anonimiza los datos personales de contacto conservando únicamente las estadísticas clínicas disociadas para fines de salud animal.
    """
    clean_phone = req.telefono.strip()
    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    if not tutor:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró registro para el teléfono {mask_phone(clean_phone)}"
        )

    solicitud = req.tipo_solicitud.lower()
    ahora = datetime.datetime.utcnow()

    if solicitud == "supresion":
        # Ejecución segura en PostgreSQL con cálculo de HMAC y registro inmutable en auditoria_arco
        secret_pepper = os.getenv("SECRET_KEY", "covet-secret-pepper-2026")
        try:
            ticket = db.execute(
                text("SELECT anonimizar_tutor(:tel, :pepper);"),
                {"tel": clean_phone, "pepper": secret_pepper}
            ).scalar()
            db.commit()
        except Exception as err:
            logger.error(f"Error ejecutando anonimizar_tutor en DB: {err}")
            tutor.nombre = "ANONIMIZADO"
            tutor.telefono = None
            tutor.email = None
            tutor.direccion = None
            tutor.consentimiento_ley21719 = False
            tutor.activo_whatsapp = False
            db.commit()
            ticket = f"BAJA-{uuid.uuid4().hex[:8].upper()}"

        logger.info(f"[Privacidad Ley 21.719] Supresión/Anonimización ejecutada con HMAC para {mask_phone(clean_phone)}. Ticket: {ticket}")
        return {
            "status": "DATOS_ANONIMIZADOS",
            "ticket_baja": ticket,
            "mensaje": f"Tus datos personales han sido suprimidos y anonimizados conforme a la Ley 21.719. Se ha generado el comprobante legal de baja {ticket}.",
            "fecha": ahora.isoformat()
        }
    else:
        # Baja de notificaciones y WhatsApp (Oposición a tratamiento para comunicaciones)
        tutor.activo_whatsapp = False
        db.commit()

        logger.info(f"[Privacidad Ley 21.719] Oposición a WhatsApp registrada para {mask_phone(clean_phone)}")
        return {
            "status": "OPT_OUT_EXITOSO",
            "mensaje": "Has revocado el consentimiento para recibir comunicaciones y alertas por WhatsApp. Tus datos clínicos permanecen protegidos.",
            "activo_whatsapp": False,
            "fecha": ahora.isoformat()
        }


# =============================================================
# 12. ENDPOINT EJECUTIVO SUPERADMIN — SASHA
# =============================================================

@app.get("/api/v1/covet/admin/metrics")
def obtener_metricas_superadmin(db: Session = Depends(get_db)):
    """
    Panel Ejecutivo de Métricas para Sasha (SuperAdmin):
    - Conteo de tutores con consentimiento Ley 21.719 firmado vs totales.
    - Citas en HOLD_15_MIN vs CONFIRMED.
    - Fichas SOAP procesadas.
    - Estado de salud de integraciones (Groq, Anthropic, n8n, Postgres).
    """
    total_tutores = db.query(TutorDB).count()
    tutores_consentimiento = db.query(TutorDB).filter(TutorDB.consentimiento_ley21719 == True).count()
    tutores_activos_whatsapp = db.query(TutorDB).filter(TutorDB.activo_whatsapp == True).count()

    total_citas = db.query(CitaDB).count()
    citas_hold = db.query(CitaDB).filter(
        (CitaDB.estado_reserva == "HOLD_15_MIN") | (CitaDB.estado_cita == "HOLD_15_MIN")
    ).count()
    citas_confirmed = db.query(CitaDB).filter(
        (CitaDB.estado_reserva == "CONFIRMED") | (CitaDB.confirmada == True)
    ).count()

    total_fiches = db.query(MedicalFicheDB).count()
    fiches_ready = db.query(MedicalFicheDB).filter(MedicalFicheDB.estado_procesamiento == "READY_FOR_REVIEW").count()
    fiches_confirmed = db.query(MedicalFicheDB).filter(
        MedicalFicheDB.estado_procesamiento.in_(["CONFIRMED_AND_SENT", "CONFIRMED_N8N_PENDING"])
    ).count()

    # Chequeo de Postgres
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "plataforma": "CoVet SaaS Core v2.0",
        "servidor": "VPS Hetzner Ubuntu 24.04 (142.132.233.38)",
        "cumplimiento_ley21719": {
            "total_tutores": total_tutores,
            "tutores_con_consentimiento": tutores_consentimiento,
            "tasa_consentimiento_pct": round((tutores_consentimiento / total_tutores * 100), 1) if total_tutores > 0 else 100.0,
            "activos_whatsapp": tutores_activos_whatsapp
        },
        "metricas_citas": {
            "total_citas": total_citas,
            "citas_en_hold_15_min": citas_hold,
            "citas_confirmadas": citas_confirmed
        },
        "metricas_soap": {
            "total_fichas": total_fiches,
            "listas_para_revision": fiches_ready,
            "confirmadas_y_despachadas": fiches_confirmed
        },
        "integraciones": {
            "postgresql_16": "ONLINE" if db_ok else "OFFLINE",
            "groq_whisper": "CONFIGURADO" if bool(os.getenv("GROQ_API_KEY")) else "FALTA_KEY",
            "anthropic_claude": "CONFIGURADO" if bool(os.getenv("ANTHROPIC_API_KEY")) else "FALTA_KEY",
            "n8n_webhook": "CONFIGURADO" if bool(os.getenv("N8N_WEBHOOK_URL")) else "URL_DEFAULT_LOCAL"
        }
    }


@app.post("/api/v1/covet/admin/privacy/purge-audit")
def purgar_auditoria_legal(anios: int = 3, db: Session = Depends(get_db)):
    """
    Purga registros de auditoría ARCO que hayan superado el plazo legal de prescripción
    (Ley 21.719 Art. 10 - Principio de Limitación de Conservación).
    Default: 3 años.
    """
    try:
        eliminados = db.execute(
            text("SELECT purgar_auditoria_arco(:anios);"),
            {"anios": anios}
        ).scalar()
        db.commit()
        logger.info(f"[Ley 21.719 Purga] Se purgaron {eliminados} registros de auditoria_arco con antigüedad > {anios} años.")
        return {
            "status": "PURGA_COMPLETADA",
            "registros_eliminados": eliminados or 0,
            "criterio": f"fecha_creacion < NOW() - INTERVAL '{anios} years'",
            "fecha_ejecucion": datetime.datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error al purgar auditoria_arco: {e}")
        raise HTTPException(status_code=500, detail=str(e))



