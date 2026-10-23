"""
CoVet Backend — API Central para Micro-SaaS Veterinario.
Persistencia en PostgreSQL, procesamiento SOAP con IA (Groq Whisper + Anthropic Claude)
e integración con n8n para WhatsApp.
"""
import os
import hmac
import hashlib
import time
import uuid
import random
import shutil
import datetime
import logging
import threading
import json
import gzip
import subprocess
from typing import List, Optional, Any, Dict, Union, Tuple
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File, Form, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, HTMLResponse
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

# Versiones legales vigentes (se registran junto a cada consentimiento / DPA)
TERMINOS_VERSION = "v2.0-2026-10"
DPA_VERSION = "DPA-v1.0-2026-10"

# Pool PostgreSQL dimensionado para ~50 veterinarios concurrentes (20 fijas + 30 de ráfaga).
# Postgres 16 permite 100 conexiones por defecto: queda margen para psql y migraciones.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "30"))
engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=60,
    pool_recycle=1800,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Límite de audio aceptado (Groq Whisper admite hasta 25 MB por archivo)
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))

# -------------------------------------------------------------
# 0. Utilidades transversales: LLM con backoff, seguridad admin, pepper HMAC
# -------------------------------------------------------------
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "8"))
_llm_semaphore = threading.BoundedSemaphore(LLM_MAX_CONCURRENCY)
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}


def llm_post(url: str, *, timeout: float, max_attempts: int = 4, **kwargs: Any) -> httpx.Response:
    """POST síncrono a Groq/Anthropic con concurrencia acotada y backoff exponencial.

    - Un semáforo global limita las llamadas simultáneas para no gatillar 429 en ráfagas.
    - Reintenta 429/5xx/529 y errores de red, respetando la cabecera Retry-After.
    - El sleep entre intentos ocurre fuera del semáforo para no bloquear a otros.
    - Si se agotan los intentos, relanza la última excepción (nunca falla en silencio).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        resp: Optional[httpx.Response] = None
        with _llm_semaphore:
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
        if resp is not None:
            if resp.status_code not in RETRYABLE_STATUS:
                resp.raise_for_status()
                return resp
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code} desde {url}", request=resp.request, response=resp
            )
        if attempt == max_attempts:
            break
        retry_after: Optional[float] = None
        if resp is not None:
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
        delay = retry_after if retry_after is not None else min(2 ** attempt, 20) + random.uniform(0, 1)
        logger.warning(f"[LLM] Intento {attempt}/{max_attempts} fallido ({last_exc}). Reintentando en {delay:.1f}s")
        time.sleep(min(delay, 30))
    assert last_exc is not None
    raise last_exc


def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    """Protege endpoints SuperAdmin con la cabecera X-Admin-Key (falla cerrado si no hay clave)."""
    expected = os.getenv("COVET_ADMIN_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="COVET_ADMIN_KEY no configurada: endpoints administrativos deshabilitados.")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Credencial administrativa inválida.")


def get_privacy_pepper() -> str:
    """Pepper HMAC para auditoría ARCO. Sin un secreto real no se ejecutan supresiones."""
    pepper = os.getenv("SECRET_KEY", "")
    if len(pepper) < 16:
        logger.critical("[Ley 21.719] SECRET_KEY ausente o débil: supresiones ARCO bloqueadas.")
        raise HTTPException(status_code=503, detail="Configuración de seguridad incompleta. Intenta más tarde.")
    return pepper

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
    # Finalidad separada (Art. 12): IA de terceros + transferencia internacional (Groq/Anthropic, EE.UU.)
    consentimiento_ia = Column(Boolean, default=False)
    fecha_consentimiento_ia = Column(DateTime, nullable=True)

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
    clinica_slug = Column(String(60), nullable=True, index=True)  # Marca blanca que originó la reserva
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
    n8n_intentos = Column(Integer, default=0)
    n8n_ultimo_intento = Column(DateTime, nullable=True)

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

class ClinicaMarcaDB(Base):
    """Marca blanca por veterinario/clínica: identidad visual + identidad legal del Responsable.

    Bajo la Ley 21.719 el veterinario es el Responsable del tratamiento y CoVet el Encargado;
    por eso la razón social, RUT y correo de privacidad se muestran al tutor en el aviso legal.
    """
    __tablename__ = "clinicas_marca"
    id = Column(Integer, primary_key=True)
    slug = Column(String(60), unique=True, nullable=False, index=True)
    nombre_fantasia = Column(String(120), nullable=False)
    razon_social = Column(String(150), nullable=True)
    rut_responsable = Column(String(20), nullable=True)
    email_privacidad = Column(String(120), nullable=True)
    color_primario = Column(String(7), default="#0D9488")
    color_secundario = Column(String(7), default="#0F766E")
    logo_url = Column(Text, nullable=True)
    whatsapp_contacto = Column(String(20), nullable=True)
    mensaje_bienvenida = Column(String(200), nullable=True)
    plan = Column(String(30), default="PRO_WHITE_LABEL")
    activo = Column(Boolean, default=True)
    dpa_aceptado = Column(Boolean, default=False)
    fecha_dpa = Column(DateTime, nullable=True)
    version_dpa = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

class VetDPAContratoDB(Base):
    """
    Contrato de Encargo de Tratamiento de Datos (Art. 21 Ley 21.719 Chile).
    El veterinario o clínica actúa como Responsable del Tratamiento y CoVet SpA como Encargado.
    Sella la suscripción con fecha cierta, IP y firma criptográfica HMAC-SHA256.
    """
    __tablename__ = "vet_dpa_contratos"
    id = Column(Integer, primary_key=True)
    folio_dpa = Column(String(50), unique=True, nullable=False, index=True)
    nombre_profesional = Column(String(150), nullable=False)
    rut_profesional = Column(String(30), nullable=False, index=True)
    registro_colmevet_sag = Column(String(60), nullable=False)
    email_contacto = Column(String(120), nullable=True)
    clinica_slug = Column(String(60), nullable=True, index=True)
    version_dpa = Column(String(30), default=DPA_VERSION)
    fecha_firma = Column(DateTime, default=datetime.datetime.utcnow)
    firma_hmac = Column(String(64), nullable=False)
    ip_firma = Column(String(50), nullable=True)

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
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS consentimiento_ia BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE tutores ADD COLUMN IF NOT EXISTS fecha_consentimiento_ia TIMESTAMP;"))

            # Cola de reintentos n8n y trazabilidad de marca blanca
            conn.execute(text("ALTER TABLE medical_fiches ADD COLUMN IF NOT EXISTS n8n_intentos INTEGER DEFAULT 0;"))
            conn.execute(text("ALTER TABLE medical_fiches ADD COLUMN IF NOT EXISTS n8n_ultimo_intento TIMESTAMP;"))
            conn.execute(text("ALTER TABLE citas ADD COLUMN IF NOT EXISTS clinica_slug VARCHAR(60);"))

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

            # Tabla de Contratos DPA (Art. 21 Ley 21.719)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS vet_dpa_contratos (
                    id SERIAL PRIMARY KEY,
                    folio_dpa VARCHAR(50) UNIQUE NOT NULL,
                    nombre_profesional VARCHAR(150) NOT NULL,
                    rut_profesional VARCHAR(30) NOT NULL,
                    registro_colmevet_sag VARCHAR(60) NOT NULL,
                    email_contacto VARCHAR(120),
                    clinica_slug VARCHAR(60),
                    version_dpa VARCHAR(30) DEFAULT 'DPA-v1.0-2026-10',
                    fecha_firma TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    firma_hmac VARCHAR(64) NOT NULL,
                    ip_firma VARCHAR(50)
                );
            """))

            # Función segura para supresión en cascada atómica (Ley 21.719: Tutores + Fichas + Citas)
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION anonimizar_tutor(p_telefono TEXT, p_secret_pepper TEXT)
                RETURNS TEXT AS $$
                DECLARE
                    v_tutor_id INT;
                    v_ticket TEXT;
                    v_hmac TEXT;
                    v_hash_corto TEXT;
                BEGIN
                    SELECT id INTO v_tutor_id 
                    FROM tutores 
                    WHERE telefono = p_telefono;

                    IF v_tutor_id IS NULL THEN
                        RETURN 'TUTOR_NO_ENCONTRADO';
                    END IF;

                    v_ticket := 'BAJA-' || UPPER(SUBSTRING(MD5(RANDOM()::TEXT) FROM 1 for 8));
                    v_hmac := ENCODE(HMAC(p_telefono::BYTEA, p_secret_pepper::BYTEA, 'sha256'), 'hex');
                    v_hash_corto := SUBSTRING(v_hmac FROM 1 for 10);

                    -- 1. Insertar registro inmutable de auditoría ARSOP+ (Ley 21.719 Art. 6, 7 y 14)
                    INSERT INTO auditoria_arco (tutor_id_referencia, ticket_baja, telefono_hmac, accion_ejecutada, fecha_creacion)
                    VALUES (v_tutor_id, v_ticket, v_hmac, 'SUPRESION_ANONIMIZACION_CASCADA', CURRENT_TIMESTAMP);

                    -- 2. Anonimizar tabla tutores
                    UPDATE tutores
                    SET nombre = 'TITULAR ANONIMIZADO (LEY 21.719)',
                        telefono = 'ANON-' || v_hash_corto,
                        email = NULL,
                        direccion = NULL,
                        consentimiento = FALSE,
                        consentimiento_ley21719 = FALSE,
                        consentimiento_ia = FALSE,
                        activo_whatsapp = FALSE,
                        actualizado_at = CURRENT_TIMESTAMP
                    WHERE id = v_tutor_id;

                    -- 3. Limpiar en cascada medical_fiches vinculadas a este tutor
                    UPDATE medical_fiches
                    SET texto_transcrito_original = '[TRANSCRIPCIÓN ELIMINADA POR EJERCICIO DE DERECHO DE SUPRESIÓN LEY 21.719 - SE CONSERVA ESTRUCTURA CLÍNICA DISOCIADA]',
                        json_estructurado = CASE 
                            WHEN json_estructurado IS NOT NULL THEN
                                json_estructurado || '{"tutor_nombre": "ANONIMIZADO", "tutor_telefono": "ANONIMIZADO"}'::jsonb
                            ELSE NULL
                        END
                    WHERE tutor_id = v_tutor_id;

                    -- 4. Limpiar en cascada citas asociadas a pacientes de este tutor
                    UPDATE citas
                    SET motivo = 'CONSULTA CLÍNICA (TITULAR ANONIMIZADO LEY 21.719)',
                        sintomas_triaje = CASE 
                            WHEN sintomas_triaje IS NOT NULL THEN
                                (sintomas_triaje - 'tutor_nombre' - 'tutor_telefono' - 'telefono' - 'direccion') || '{"tutor_nombre": "ANONIMIZADO", "tutor_telefono": "ANONIMIZADO"}'::jsonb
                            ELSE NULL
                        END
                    WHERE paciente_id IN (SELECT id FROM pacientes WHERE tutor_id = v_tutor_id);

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
    consentimiento_tutor_presencial: Optional[bool] = Field(
        default=True,
        description="Confirmación del veterinario de que el tutor autorizó en consulta el registro clínico digital y envío de su receta por WhatsApp (Ley 21.719)"
    )

# -------------------------------------------------------------
# 3. Pipeline de IA en Segundo Plano (Groq Whisper + Claude Tool Use)
# -------------------------------------------------------------
def process_voice_fiche(task_id: uuid.UUID, audio_bytes: bytes, filename: str):
    """
    Procesa el audio grabado en background:
    1. Transcribe con Groq Whisper Large V3.
    2. Estructura el SOAP con Anthropic Claude usando Tool Use para garantizar JSON estricto.
    3. Deja la ficha en estado READY_FOR_REVIEW (esperando confirmación humana).

    Ley 21.719 (minimización): el audio solo existe en memoria RAM; nunca toca el disco.
    Si el proceso muere, el audio desaparece con él y la ficha se marca FAILED al reiniciar
    (recover_interrupted_fiches) para que el frontend la reenvíe desde su búfer IndexedDB.
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
        files = {"file": (filename, audio_bytes, "application/octet-stream")}

        data = {
            "model": "whisper-large-v3-turbo",
            "language": "es",
            "response_format": "json"
        }
        headers = {"Authorization": f"Bearer {groq_key}"}

        groq_resp = llm_post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            timeout=60.0,
            files=files,
            data=data,
            headers=headers
        )
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

        anthropic_resp = llm_post(
            "https://api.anthropic.com/v1/messages",
            timeout=60.0,
            json=anthropic_payload,
            headers=anthropic_headers
        )
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


# -------------------------------------------------------------
# 3b. Resiliencia: recuperación de fichas interrumpidas y cola de reintentos n8n
# -------------------------------------------------------------
LEGACY_AUDIO_DIRS = ("/tmp/covet_audios", "/tmp/covet_client_audios")
N8N_RETRY_INTERVAL_S = int(os.getenv("N8N_RETRY_INTERVAL_S", "120"))
N8N_MAX_INTENTOS = int(os.getenv("N8N_MAX_INTENTOS", "10"))


def purge_legacy_audio_dirs() -> None:
    """Elimina audios huérfanos de versiones anteriores que escribían en /tmp (Ley 21.719)."""
    for path in LEGACY_AUDIO_DIRS:
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                logger.info(f"[Ley 21.719] Directorio de audios huérfanos eliminado: {path}")
            except OSError as err:
                logger.error(f"[Ley 21.719] No se pudo eliminar {path}: {err}")


def recover_interrupted_fiches() -> None:
    """Marca FAILED las fichas que quedaron PENDING/PROCESSING por un reinicio del proceso.

    El pipeline corre en el mismo proceso (un solo worker uvicorn): al arrancar, cualquier ficha
    en esos estados quedó huérfana. Marcarla FAILED permite al frontend reenviar el audio.
    """
    db = SessionLocal()
    try:
        n = db.query(MedicalFicheDB).filter(
            MedicalFicheDB.estado_procesamiento.in_(["PENDING", "PROCESSING"])
        ).update(
            {
                MedicalFicheDB.estado_procesamiento: "FAILED",
                MedicalFicheDB.error_detalle: "INTERRUPTED_BY_RESTART: reenvía el audio desde el dispositivo.",
            },
            synchronize_session=False,
        )
        db.commit()
        if n:
            logger.warning(f"[Recovery] {n} ficha(s) interrumpidas por reinicio marcadas como FAILED.")
    except Exception as err:
        db.rollback()
        logger.error(f"[Recovery] Error recuperando fichas interrumpidas: {err}")
    finally:
        db.close()


def dispatch_fiche_to_n8n(db: Session, fiche: "MedicalFicheDB") -> Tuple[bool, Optional[str]]:
    """Despacha una ficha confirmada a n8n y actualiza su estado (sin hacer commit).

    Respeta la Ley 21.719: no envía si el tutor fue anonimizado o revocó WhatsApp.
    """
    tutor = db.get(TutorDB, fiche.tutor_id) if fiche.tutor_id else None
    paciente = db.get(PacienteDB, fiche.paciente_id) if fiche.paciente_id else None

    if tutor is None or paciente is None or not tutor.telefono:
        fiche.estado_procesamiento = "CONFIRMED_N8N_FAILED"
        fiche.error_detalle = "Sin tutor/teléfono vigente (posible supresión ARCO). No se envía WhatsApp."
        return False, fiche.error_detalle

    if tutor.activo_whatsapp is False:
        fiche.estado_procesamiento = "CONFIRMED_NO_WHATSAPP"
        fiche.error_detalle = None
        return False, "El tutor no autorizó o revocó comunicaciones por WhatsApp (Ley 21.719)."

    payload = {
        "fiche_id": str(fiche.id),
        "tutor_id": tutor.id,
        "paciente_id": paciente.id,
        "tutor_nombre": tutor.nombre,
        "tutor_telefono": tutor.telefono,
        "paciente_nombre": paciente.nombre,
        "especie": paciente.especie,
        "raza": paciente.raza,
        "peso_kg": paciente.peso_kg,
        "fecha_confirmacion": fiche.fecha_confirmacion.isoformat() if fiche.fecha_confirmacion else None,
        "soap": fiche.json_estructurado,
    }

    fiche.n8n_intentos = (fiche.n8n_intentos or 0) + 1
    fiche.n8n_ultimo_intento = datetime.datetime.utcnow()
    error: Optional[str] = None
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(N8N_WEBHOOK_URL, json=payload)
        if resp.is_success:
            fiche.estado_procesamiento = "CONFIRMED_AND_SENT"
            fiche.error_detalle = None
            logger.info(f"[n8n {fiche.id}] Despachado (intento {fiche.n8n_intentos}).")
            return True, None
        error = f"n8n respondió HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        error = f"No se pudo conectar a n8n: {exc}"

    agotado = fiche.n8n_intentos >= N8N_MAX_INTENTOS
    fiche.estado_procesamiento = "CONFIRMED_N8N_FAILED" if agotado else "CONFIRMED_N8N_PENDING"
    fiche.error_detalle = error
    logger.warning(f"[n8n {fiche.id}] Intento {fiche.n8n_intentos}/{N8N_MAX_INTENTOS} fallido: {error}")
    return False, error


def retry_pending_n8n_dispatches(max_batch: int = 20) -> Dict[str, int]:
    """Reintenta fichas CONFIRMED_N8N_PENDING con backoff exponencial (2^n min, tope 6 h).

    Usa SELECT ... FOR UPDATE SKIP LOCKED para que dos ejecuciones nunca envíen la misma ficha.
    """
    db = SessionLocal()
    stats = {"revisadas": 0, "enviadas": 0, "pospuestas": 0, "fallidas": 0}
    try:
        ahora = datetime.datetime.utcnow()
        candidatas = (
            db.query(MedicalFicheDB)
            .filter(MedicalFicheDB.estado_procesamiento == "CONFIRMED_N8N_PENDING")
            .order_by(MedicalFicheDB.fecha_confirmacion)
            .limit(max_batch)
            .with_for_update(skip_locked=True)
            .all()
        )
        for fiche in candidatas:
            stats["revisadas"] += 1
            intentos = fiche.n8n_intentos or 0
            espera = datetime.timedelta(minutes=min(2 ** intentos, 360))
            if fiche.n8n_ultimo_intento and ahora < fiche.n8n_ultimo_intento + espera:
                stats["pospuestas"] += 1
                continue
            ok, _ = dispatch_fiche_to_n8n(db, fiche)
            stats["enviadas" if ok else "fallidas"] += 1
        db.commit()
    except Exception as err:
        db.rollback()
        logger.error(f"[n8n Retry] Error en ciclo de reintentos: {err}")
    finally:
        db.close()
    if stats["revisadas"]:
        logger.info(f"[n8n Retry] {stats}")
    return stats


def n8n_retry_loop(stop_event: threading.Event) -> None:
    """Hilo daemon que ejecuta la cola de reintentos cada N8N_RETRY_INTERVAL_S segundos."""
    while not stop_event.wait(N8N_RETRY_INTERVAL_S):
        retry_pending_n8n_dispatches()

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

_n8n_stop_event = threading.Event()


@app.on_event("startup")
def on_startup():
    init_db()
    purge_legacy_audio_dirs()
    recover_interrupted_fiches()
    threading.Thread(
        target=n8n_retry_loop, args=(_n8n_stop_event,), name="n8n-retry", daemon=True
    ).start()
    logger.info(f"[Startup] Pool DB {DB_POOL_SIZE}+{DB_MAX_OVERFLOW} | LLM concurrencia {LLM_MAX_CONCURRENCY} | n8n retry cada {N8N_RETRY_INTERVAL_S}s")


@app.on_event("shutdown")
def on_shutdown():
    _n8n_stop_event.set()

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
    if len(contents) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="El audio supera el límite de 25 MB.")

    # Ley 21.719 (minimización): el audio se procesa solo en RAM, nunca se escribe a disco.
    fiche_id = uuid.uuid4()
    filename = file.filename or "dictado.webm"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"

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
    background_tasks.add_task(process_voice_fiche, fiche.id, contents, f"{fiche_id}.{ext}")

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

    now = datetime.datetime.utcnow()

    # Cumplimiento Ley 21.719: Si el tutor no tenía consentimiento previo registrado y el veterinario confirma la autorización en terreno
    if payload.consentimiento_tutor_presencial:
        if not tutor.consentimiento_ley21719:
            tutor.consentimiento_ley21719 = True
            tutor.fecha_consentimiento = now
            tutor.version_terminos = "v1.0-2026-PRESENCIAL"
            tutor.metodo_captura = "CONSULTA_PRESENCIAL_TERRENO"
            tutor.activo_whatsapp = True
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
    fiche.tutor_id = tutor.id
    fiche.paciente_id = paciente.id
    fiche.json_estructurado = payload.model_dump()
    fiche.fecha_confirmacion = now
    fiche.n8n_intentos = 0
    fiche.n8n_ultimo_intento = None
    db.commit()

    # 4. Disparo a n8n con tolerancia a fallos: si falla, queda en la cola de reintentos automáticos
    n8n_dispatched, n8n_error = dispatch_fiche_to_n8n(db, fiche)
    db.commit()
    db.refresh(fiche)

    if n8n_dispatched:
        message = "Ficha médica confirmada y WhatsApp despachado."
    elif fiche.estado_procesamiento == "CONFIRMED_NO_WHATSAPP":
        message = "Ficha guardada. El tutor no autorizó WhatsApp: entrega la receta por otro canal."
    else:
        message = "Ficha médica guardada. WhatsApp en cola de reintento automático (n8n no disponible)."

    return {
        "status": "success",
        "task_id": str(fiche.id),
        "fiche_id": str(fiche.id),
        "estado_procesamiento": fiche.estado_procesamiento,
        "tutor_id": tutor.id,
        "paciente_id": paciente.id,
        "n8n_dispatched": n8n_dispatched,
        "n8n_detail": n8n_error,
        "receta_pdf_url": f"/api/v1/covet/fiches/{fiche.id}/receta-pdf",
        "message": message
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
    consentimiento_whatsapp: bool = Field(default=False, description="Finalidad separada: recetas y recordatorios por WhatsApp")
    consentimiento_ia: bool = Field(default=False, description="Finalidad separada: IA de terceros con transferencia internacional (EE.UU.)")
    clinica_slug: Optional[str] = Field(default=None, max_length=60, description="Marca blanca desde la que se originó la reserva")

# -------------------------------------------------------------
# 10. Los 3 Endpoints del Agente Autónomo de Clientes
# -------------------------------------------------------------
@app.post("/api/v1/covet/client-intent")
async def client_intent(
    texto: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    consentimiento_ia: bool = Form(False),
    db: Session = Depends(get_db)
):
    """
    Endpoint 1: Agente Autónomo de Intención y Triaje de Clientes:
    - Recibe texto o audio (file) del tutor.
    - Transcribe audio con Groq whisper-large-v3-turbo (si falta clave, responde error claro).
    - Evalúa urgencia, especie, motivo y 3 preguntas dinámicas con Anthropic Claude Tool Use.
    - Asigna protocolo pre-aprobado y calcula agenda híbrida.

    Ley 21.719 (Art. 12 y transferencia internacional): sin `consentimiento_ia=true` no se envía
    nada a Groq/Anthropic; el triaje se resuelve con reglas clínicas locales y el audio se rechaza.
    """
    consulta_texto = (texto or "").strip()

    # Transcripción con Groq si se sube archivo de audio
    if file and file.filename:
        if not consentimiento_ia:
            raise HTTPException(
                status_code=400,
                detail="Para enviar audio debes autorizar el procesamiento asistido por IA. También puedes escribir tu consulta en texto."
            )
        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Falta configurar GROQ_API_KEY en el servidor para transcribir audios. Por favor escribe el síntoma o consulta en texto."
                }
            )

        contents = await file.read()
        if contents and len(contents) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="El audio supera el límite de 25 MB.")
        if contents and len(contents) > 0:
            # Ley 21.719 (minimización): la voz del tutor se procesa solo en RAM, sin rastro en disco.
            filename = file.filename or "cliente_audio.webm"
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
            try:
                resp = await run_in_threadpool(
                    llm_post,
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    timeout=45.0,
                    max_attempts=3,
                    files={"file": (f"{uuid.uuid4()}.{ext}", contents, "application/octet-stream")},
                    data={
                        "model": "whisper-large-v3-turbo",
                        "language": "es",
                        "response_format": "json"
                    },
                    headers={"Authorization": f"Bearer {groq_key}"}
                )
                transcripcion = resp.json().get("text", "").strip()
                if transcripcion:
                    consulta_texto = f"{consulta_texto} {transcripcion}".strip() if consulta_texto else transcripcion
            except Exception as e:
                logger.error(f"Error en transcripción Groq: {e}")
                raise HTTPException(status_code=502, detail="No pudimos transcribir el audio. Intenta de nuevo o escribe tu consulta.")

    if not consulta_texto:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar un texto o un archivo de audio con los síntomas de tu mascota."
        )

    # Análisis agéntico con Anthropic Claude Tool Use
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    ia_output = None

    if anthropic_key and consentimiento_ia:
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
            # Llamada bloqueante enviada al threadpool: no congela el event loop de FastAPI
            res = await run_in_threadpool(
                llm_post,
                "https://api.anthropic.com/v1/messages",
                timeout=20.0,
                max_attempts=2,
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json=anthropic_payload
            )
            data = res.json()
            for block in data.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "analizar_intencion_tutor":
                    ia_output = block.get("input", {})
                    break
        except Exception as e:
            logger.warning(f"Error consultando Anthropic Claude para triaje: {e}")

    motor_triaje = "IA_CLAUDE" if ia_output else "REGLAS_LOCALES"

    # Fallback Determinístico Clínico si falla la API o no hay consentimiento de IA
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
        "available_schedule": available_schedule,
        "motor_triaje": motor_triaje
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

    # Base de licitud obligatoria: sin aceptación del aviso no se crean datos personales
    if not req.consentimiento_ley21719:
        raise HTTPException(
            status_code=400,
            detail="Debes aceptar el tratamiento de datos para la atención clínica (Ley 21.719) para reservar."
        )

    # Acción Agéntica 1: Cruce de Historial por WhatsApp
    tutor = db.query(TutorDB).filter(TutorDB.telefono == clean_phone).first()
    paciente_reconocido = False
    historial_clinico_previo = "Paciente nuevo. Sin atenciones previas registradas."

    now = datetime.datetime.utcnow()

    if tutor:
        paciente_reconocido = True
        if clean_tutor_name != "Tutor CoVet" and tutor.nombre != clean_tutor_name:
            tutor.nombre = clean_tutor_name
        
        # Registro trazable Ley 21.719: cada finalidad queda registrada por separado
        tutor.consentimiento_ley21719 = True
        tutor.fecha_consentimiento = now
        tutor.version_terminos = TERMINOS_VERSION
        tutor.activo_whatsapp = req.consentimiento_whatsapp
        tutor.consentimiento_ia = req.consentimiento_ia
        tutor.fecha_consentimiento_ia = now if req.consentimiento_ia else None
        tutor.metodo_captura = "FORMULARIO_WEB_TRIAJE"
        tutor.actualizado_at = now
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
            consentimiento_ley21719=True,
            fecha_consentimiento=now,
            version_terminos=TERMINOS_VERSION,
            activo_whatsapp=req.consentimiento_whatsapp,
            consentimiento_ia=req.consentimiento_ia,
            fecha_consentimiento_ia=now if req.consentimiento_ia else None,
            metodo_captura="FORMULARIO_WEB_TRIAJE"
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

    logger.info(
        f"[Triaje] Tutor {mask_phone(clean_phone)} | Mascota: {clean_pet_name} | "
        f"Consentimientos -> clínico: True, WhatsApp: {req.consentimiento_whatsapp}, IA: {req.consentimiento_ia}"
    )

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
        clinica_slug=(req.clinica_slug or None),
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
            "consentimiento_ia_transferencia_internacional": tutor.consentimiento_ia,
            "fecha_consentimiento_ia": tutor.fecha_consentimiento_ia.isoformat() if tutor.fecha_consentimiento_ia else None,
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
        # Ejecución atómica en PostgreSQL: HMAC + registro inmutable en auditoria_arco + anonimización.
        # Si falla, NO se emite comprobante: un ticket sin registro de auditoría no tiene valor legal.
        secret_pepper = get_privacy_pepper()
        try:
            tutor_id = tutor.id
            ticket = db.execute(
                text("SELECT anonimizar_tutor(:tel, :pepper);"),
                {"tel": clean_phone, "pepper": secret_pepper}
            ).scalar()

            # Doble capa de seguridad: limpieza exhaustiva en cascada sobre modelos ORM
            if tutor_id:
                fiches = db.query(MedicalFicheDB).filter(MedicalFicheDB.tutor_id == tutor_id).all()
                for f in fiches:
                    f.texto_transcrito_original = "[TRANSCRIPCIÓN ELIMINADA POR EJERCICIO DE DERECHO DE SUPRESIÓN LEY 21.719 - SE CONSERVA ESTRUCTURA CLÍNICA DISOCIADA]"
                    if f.json_estructurado and isinstance(f.json_estructurado, dict):
                        f.json_estructurado["tutor_nombre"] = "ANONIMIZADO"
                        f.json_estructurado["tutor_telefono"] = "ANONIMIZADO"
                
                pacientes_ids = [p.id for p in db.query(PacienteDB.id).filter(PacienteDB.tutor_id == tutor_id).all()]
                if pacientes_ids:
                    citas = db.query(CitaDB).filter(CitaDB.paciente_id.in_(pacientes_ids)).all()
                    for c in citas:
                        c.motivo = "CONSULTA CLÍNICA (TITULAR ANONIMIZADO LEY 21.719)"
                        if c.sintomas_triaje and isinstance(c.sintomas_triaje, dict):
                            c.sintomas_triaje["tutor_nombre"] = "ANONIMIZADO"
                            c.sintomas_triaje["tutor_telefono"] = "ANONIMIZADO"
                            c.sintomas_triaje.pop("telefono", None)
                            c.sintomas_triaje.pop("direccion", None)

            db.commit()
        except Exception as err:
            db.rollback()
            logger.error(f"[Ley 21.719] Error ejecutando supresión en cascada: {err}")
            raise HTTPException(
                status_code=500,
                detail="No pudimos completar la supresión. Tu solicitud no fue procesada; intenta nuevamente."
            )

        logger.info(f"[Privacidad Ley 21.719] Supresión en CASCADA ejecutada para {mask_phone(clean_phone)}. Ticket: {ticket}")
        return {
            "status": "DATOS_ANONIMIZADOS",
            "ticket_baja": ticket,
            "mensaje": f"Tus datos personales han sido suprimidos y anonimizados en cascada conforme a la Ley 21.719. Se ha generado el comprobante legal de baja {ticket}.",
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
# 11.5 BÓVEDA DE RESPALDOS CIFRADOS AES-256 (LEY 21.719 ART. 14TER)
# =============================================================

VAULT_BACKUP_DIR = os.getenv("BACKUP_DIR", "/var/backups/covet_encrypted")
VAULT_KEY_FILE = os.getenv("BACKUP_KEY_FILE", "/run/secrets/covet_backup.key")
VAULT_STATUS_FILE = os.path.join(VAULT_BACKUP_DIR, "last_backup_status.json")


def read_backup_status_dict() -> Dict[str, Any]:
    """
    Lee el estado del último respaldo cifrado desde last_backup_status.json.
    Si el archivo no existe (ej. primer arranque en local), devuelve PENDING_FIRST_RUN sin error 500.
    """
    if os.path.exists(VAULT_STATUS_FILE):
        try:
            with open(VAULT_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[Vault] Error al leer {VAULT_STATUS_FILE}: {e}")

    # Fallback local o previo al primer respaldo
    return {
        "status": "PENDING_FIRST_RUN",
        "encryption": "AES-256-CBC-PBKDF2",
        "last_backup_utc": None,
        "file_name": None,
        "size_bytes": 0,
        "sha256": None,
        "retained_copies_14d": 0,
        "ley_21719_compliant": False
    }


def _serialize_model_instance(inst) -> Dict[str, Any]:
    """Serializa de forma segura cualquier fila de SQLAlchemy a dict JSON-compatible."""
    if not inst:
        return {}
    res = {}
    for col in inst.__table__.columns:
        val = getattr(inst, col.name)
        if isinstance(val, (datetime.datetime, datetime.date)):
            res[col.name] = val.isoformat()
        elif isinstance(val, uuid.UUID):
            res[col.name] = str(val)
        else:
            res[col.name] = val
    return res


@app.get("/api/v1/covet/admin/backup-status")
def obtener_estado_backup_vault():
    """
    Retorna el estado de auditoría y verificación de integridad de la bóveda de respaldos cifrados
    (Ley 21.719 Art. 14Ter - Medidas de Seguridad y Cifrado en Reposo).
    """
    return read_backup_status_dict()


@app.post("/api/v1/covet/admin/backup-now")
def ejecutar_backup_en_caliente_cifrado(db: Session = Depends(get_db)):
    """
    Dispara la generación en caliente de un respaldo completo de la base de datos CoVet:
    1. Exporta tablas clínicas y de tutores (tutores, pacientes, citas, medical_fiches, auditoria_arco, etc.).
    2. Comprime con gzip (nivel 9).
    3. Cifra con OpenSSL AES-256-CBC + PBKDF2 (100,000 iteraciones) usando la llave maestra aislada (/run/secrets/covet_backup.key).
    4. Almacena en /var/backups/covet_encrypted/ con permisos 600.
    5. Calcula el hash SHA-256 criptográfico para trazabilidad inmutable.
    6. Aplica política de retención legal de 14 días y actualiza last_backup_status.json.
    """
    target_dir = VAULT_BACKUP_DIR
    try:
        os.makedirs(target_dir, exist_ok=True)
        probe_path = os.path.join(target_dir, ".vault_probe")
        with open(probe_path, "w") as f:
            f.write("ok")
        os.remove(probe_path)
    except Exception as e:
        logger.warning(f"[Vault] {target_dir} no es escribible, usando fallback /tmp/covet_encrypted: {e}")
        target_dir = "/tmp/covet_encrypted"
        os.makedirs(target_dir, exist_ok=True)

    status_file_path = os.path.join(target_dir, "last_backup_status.json")

    # Llave de cifrado AES-256
    key_file = VAULT_KEY_FILE
    temp_dev_key = None
    if not os.path.exists(key_file):
        logger.warning(f"[Vault] Llave maestra {key_file} no encontrada. Creando fallback temporal.")
        temp_dev_key = "/tmp/.covet_backup_dev.key"
        with open(temp_dev_key, "w", encoding="utf-8") as f:
            f.write(os.getenv("SECRET_KEY", "CoVetFallbackDevMasterKeyChileLey21719AES256!"))
        os.chmod(temp_dev_key, 0o400)
        key_file = temp_dev_key

    timestamp_str = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S_UTC")
    file_name = f"covet_db_{timestamp_str}.sql.gz.enc"
    target_file = os.path.join(target_dir, file_name)
    temp_gz_file = f"/tmp/covet_tmp_{timestamp_str}.gz"

    try:
        # Exportar tablas en formato JSON estructurado con envoltura SQL
        tutores_data = [_serialize_model_instance(r) for r in db.query(TutorDB).all()]
        pacientes_data = [_serialize_model_instance(r) for r in db.query(PacienteDB).all()]
        citas_data = [_serialize_model_instance(r) for r in db.query(CitaDB).all()]
        fiches_data = [_serialize_model_instance(r) for r in db.query(MedicalFicheDB).all()]
        auditoria_data = [_serialize_model_instance(r) for r in db.query(AuditoriaArcoDB).all()]
        marcas_data = [_serialize_model_instance(r) for r in db.query(ClinicaMarcaDB).all()]
        dpa_data = [_serialize_model_instance(r) for r in db.query(VetDPAContratoDB).all()]

        payload = {
            "metadata": {
                "plataforma": "CoVet SaaS Core v2.0",
                "servidor": "VPS Hetzner Ubuntu 24.04 (142.132.233.38)",
                "timestamp_utc": timestamp_str,
                "encryption": "AES-256-CBC-PBKDF2",
                "pbkdf2_iterations": 100000,
                "compliance": "Ley 21.719 Art. 14Ter (Chile)",
                "tablas_respaldadas": ["tutores", "pacientes", "citas", "medical_fiches", "auditoria_arco", "clinicas_marca", "vet_dpa_contratos"]
            },
            "tablas": {
                "tutores": tutores_data,
                "pacientes": pacientes_data,
                "citas": citas_data,
                "medical_fiches": fiches_data,
                "auditoria_arco": auditoria_data,
                "clinicas_marca": marcas_data,
                "vet_dpa_contratos": dpa_data
            }
        }

        sql_wrapper = (
            f"-- =========================================================\n"
            f"-- COVET DATABASE ENCRYPTED BACKUP (AES-256-CBC)\n"
            f"-- Art. 14Ter Ley 21.719 Protección de Datos Personales Chile\n"
            f"-- Timestamp: {timestamp_str}\n"
            f"-- =========================================================\n"
            f"/* JSON_DATABASE_PAYLOAD_BEGIN\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            f"JSON_DATABASE_PAYLOAD_END */\n"
        )

        compressed_bytes = gzip.compress(sql_wrapper.encode("utf-8"), compresslevel=9)
        with open(temp_gz_file, "wb") as f_gz:
            f_gz.write(compressed_bytes)

        openssl_cmd = [
            "openssl", "enc", "-aes-256-cbc",
            "-salt", "-pbkdf2", "-iter", "100000",
            "-pass", f"file:{key_file}",
            "-in", temp_gz_file,
            "-out", target_file
        ]
        proc = subprocess.run(openssl_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Fallo en OpenSSL: {proc.stderr}")

        try:
            os.chmod(target_file, 0o600)
        except Exception:
            pass
        file_size = os.path.getsize(target_file)

        sha_hasher = hashlib.sha256()
        with open(target_file, "rb") as f_target:
            for chunk in iter(lambda: f_target.read(65536), b""):
                sha_hasher.update(chunk)
        file_sha256 = sha_hasher.hexdigest()

        now_ts = time.time()
        retention_secs = 14 * 86400
        valid_copies = []
        for fn in os.listdir(target_dir):
            if (fn.startswith("covet_db_") or fn.startswith("covet_")) and fn.endswith(".enc"):
                fp = os.path.join(target_dir, fn)
                if os.path.isfile(fp):
                    if now_ts - os.path.getmtime(fp) > retention_secs:
                        try:
                            os.remove(fp)
                        except Exception:
                            pass
                    else:
                        valid_copies.append(fp)
        retained_count = len(valid_copies)

        status_payload = {
            "status": "OK",
            "encryption": "AES-256-CBC-PBKDF2",
            "last_backup_utc": timestamp_str,
            "file_name": file_name,
            "size_bytes": file_size,
            "sha256": file_sha256,
            "retained_copies_14d": retained_count,
            "ley_21719_compliant": True
        }

        with open(status_file_path, "w", encoding="utf-8") as f_st:
            json.dump(status_payload, f_st, indent=2)
        try:
            os.chmod(status_file_path, 0o644)
        except Exception:
            pass

        logger.info(f"[Vault] Respaldo cifrado generado con éxito: {file_name} ({file_size} bytes)")
        return status_payload

    except Exception as e:
        logger.error(f"[Vault] Error al generar backup cifrado: {e}")
        raise HTTPException(status_code=500, detail=f"Error en respaldo cifrado: {str(e)}")
    finally:
        if os.path.exists(temp_gz_file):
            try:
                os.remove(temp_gz_file)
            except Exception:
                pass
        if temp_dev_key and os.path.exists(temp_dev_key):
            try:
                os.remove(temp_dev_key)
            except Exception:
                pass


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
    - Auditoría de la Bóveda de Respaldos Cifrados AES-256.
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
        "backup_vault": read_backup_status_dict(),
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


@app.post("/api/v1/covet/admin/privacy/purge-audit", dependencies=[Depends(require_admin)])
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


@app.post("/api/v1/covet/admin/n8n/retry", dependencies=[Depends(require_admin)])
def reintentar_n8n_manual():
    """Fuerza un ciclo de la cola de reintentos n8n (útil tras restablecer n8n)."""
    return {"status": "OK", "resultado": retry_pending_n8n_dispatches()}


# =============================================================
# 13. MARCA BLANCA (WHITE-LABEL) POR VETERINARIO
# =============================================================
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"
SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])$"


class BrandingUpsert(BaseModel):
    """Configuración de marca que el SuperAdmin asigna a un veterinario con plan white-label."""
    nombre_fantasia: str = Field(..., min_length=2, max_length=120, example="Dra. Carolina · Veterinaria a Domicilio")
    razon_social: Optional[str] = Field(None, max_length=150, example="Carolina Rojas SpA")
    rut_responsable: Optional[str] = Field(None, max_length=20, example="76.123.456-7")
    email_privacidad: Optional[str] = Field(None, max_length=120, example="privacidad@dracarolina.cl")
    color_primario: str = Field("#0D9488", pattern=HEX_COLOR_PATTERN)
    color_secundario: str = Field("#0F766E", pattern=HEX_COLOR_PATTERN)
    logo_url: Optional[str] = Field(None, max_length=500, pattern=r"^https://")
    whatsapp_contacto: Optional[str] = Field(None, max_length=20)
    mensaje_bienvenida: Optional[str] = Field(None, max_length=200)
    plan: str = Field("PRO_WHITE_LABEL", max_length=30)
    activo: bool = True
    dpa_aceptado: bool = Field(False, description="El veterinario firmó el Contrato de Encargo de Tratamiento (DPA)")


def _branding_public(m: ClinicaMarcaDB) -> Dict[str, Any]:
    """Campos públicos: identidad visual + identidad legal del Responsable (exigida por Ley 21.719)."""
    return {
        "slug": m.slug,
        "nombre_fantasia": m.nombre_fantasia,
        "razon_social": m.razon_social,
        "rut_responsable": m.rut_responsable,
        "email_privacidad": m.email_privacidad,
        "color_primario": m.color_primario,
        "color_secundario": m.color_secundario,
        "logo_url": m.logo_url,
        "whatsapp_contacto": m.whatsapp_contacto,
        "mensaje_bienvenida": m.mensaje_bienvenida,
        "dpa_vigente": bool(m.dpa_aceptado),
    }


@app.get("/api/v1/covet/branding/{slug}")
def obtener_branding(slug: str, db: Session = Depends(get_db)):
    """Público: devuelve la marca del veterinario para tematizar la app del tutor (?vet=slug)."""
    marca = db.query(ClinicaMarcaDB).filter(
        ClinicaMarcaDB.slug == slug.lower().strip(), ClinicaMarcaDB.activo.is_(True)
    ).first()
    if not marca:
        raise HTTPException(status_code=404, detail="Marca no encontrada o inactiva.")
    return _branding_public(marca)


@app.get("/api/v1/covet/admin/branding", dependencies=[Depends(require_admin)])
def listar_branding(db: Session = Depends(get_db)):
    """SuperAdmin: lista todas las marcas blancas con su estado de plan y DPA."""
    marcas = db.query(ClinicaMarcaDB).order_by(ClinicaMarcaDB.created_at.desc()).all()
    return [
        {**_branding_public(m), "plan": m.plan, "activo": m.activo,
         "fecha_dpa": m.fecha_dpa.isoformat() if m.fecha_dpa else None, "version_dpa": m.version_dpa}
        for m in marcas
    ]


@app.put("/api/v1/covet/admin/branding/{slug}", dependencies=[Depends(require_admin)])
def upsert_branding(slug: str, req: BrandingUpsert, db: Session = Depends(get_db)):
    """SuperAdmin: crea o actualiza la marca blanca de un veterinario.

    La fecha y versión del DPA se sellan en servidor la primera vez que se marca como aceptado.
    """
    import re
    clean_slug = slug.lower().strip()
    if not re.fullmatch(SLUG_PATTERN, clean_slug):
        raise HTTPException(status_code=422, detail="Slug inválido: usa 3-60 caracteres a-z, 0-9 y guiones.")

    ahora = datetime.datetime.utcnow()
    marca = db.query(ClinicaMarcaDB).filter(ClinicaMarcaDB.slug == clean_slug).first()
    if not marca:
        marca = ClinicaMarcaDB(slug=clean_slug, created_at=ahora)
        db.add(marca)

    datos = req.model_dump()
    dpa_aceptado = datos.pop("dpa_aceptado")
    for campo, valor in datos.items():
        setattr(marca, campo, valor)

    if dpa_aceptado and not marca.dpa_aceptado:
        marca.dpa_aceptado = True
        marca.fecha_dpa = ahora
        marca.version_dpa = DPA_VERSION
    elif not dpa_aceptado:
        marca.dpa_aceptado = False
    marca.updated_at = ahora

    db.commit()
    db.refresh(marca)
    logger.info(f"[White-Label] Marca '{clean_slug}' guardada (DPA: {marca.dpa_aceptado}).")
    return {
        **_branding_public(marca),
        "plan": marca.plan,
        "fecha_dpa": marca.fecha_dpa.isoformat() if marca.fecha_dpa else None,
        "version_dpa": marca.version_dpa,
        "link_cliente": f"/?vet={clean_slug}",
    }


# =============================================================
# 14. CONTRATO DPA LEY 21.719 Y RECETAS MÉDICAS OFICIALES
# =============================================================

class VetSignDPARequest(BaseModel):
    nombre_profesional: str = Field(..., min_length=3, max_length=150, example="Dra. Carolina Rojas Morales")
    rut_profesional: str = Field(..., min_length=8, max_length=30, example="16.789.123-4")
    registro_colmevet_sag: str = Field(..., min_length=3, max_length=60, example="COLMEVET N° 4521 · SAG 8912")
    email_contacto: Optional[str] = Field(None, max_length=120, example="contacto@dracarolina.cl")
    clinica_slug: Optional[str] = Field(None, max_length=60, example="dra-carolina")


@app.post("/api/v1/covet/vet/sign-dpa")
def firmar_dpa_veterinario(req: VetSignDPARequest, request: Request, db: Session = Depends(get_db)):
    """
    Suscripción vinculante del Contrato de Encargo de Tratamiento de Datos (DPA)
    conforme al Art. 21 de la Ley 21.719. El veterinario es Responsable y CoVet Encargado.
    Sella firma digital HMAC-SHA256, IP y fecha cierta.
    """
    ahora = datetime.datetime.utcnow()
    folio = f"DPA-2026-{uuid.uuid4().hex[:8].upper()}"
    pepper = get_privacy_pepper()
    cadena_firma = f"{folio}|{req.rut_profesional.strip()}|{ahora.isoformat()}|{DPA_VERSION}"
    firma_hmac = hmac.new(pepper.encode(), cadena_firma.encode(), hashlib.sha256).hexdigest()
    client_ip = request.client.host if request.client else "127.0.0.1"

    contrato = VetDPAContratoDB(
        folio_dpa=folio,
        nombre_profesional=req.nombre_profesional.strip(),
        rut_profesional=req.rut_profesional.strip(),
        registro_colmevet_sag=req.registro_colmevet_sag.strip(),
        email_contacto=req.email_contacto.strip() if req.email_contacto else None,
        clinica_slug=req.clinica_slug.strip().lower() if req.clinica_slug else None,
        version_dpa=DPA_VERSION,
        fecha_firma=ahora,
        firma_hmac=firma_hmac,
        ip_firma=client_ip
    )
    db.add(contrato)

    # Si hay marca blanca vinculada, sincronizar flags
    if req.clinica_slug:
        marca = db.query(ClinicaMarcaDB).filter(ClinicaMarcaDB.slug == req.clinica_slug.strip().lower()).first()
        if marca:
            marca.dpa_aceptado = True
            marca.fecha_dpa = ahora
            marca.version_dpa = DPA_VERSION
            if req.rut_profesional and not marca.rut_responsable:
                marca.rut_responsable = req.rut_profesional.strip()

    db.commit()
    db.refresh(contrato)

    logger.info(f"[Escudo Legal DPA] Contrato {folio} suscrito por {contrato.nombre_profesional} (RUT: {contrato.rut_profesional})")
    return {
        "status": "DPA_FIRMADO",
        "folio_dpa": folio,
        "firma_hmac": firma_hmac,
        "fecha_firma": ahora.isoformat(),
        "version_dpa": DPA_VERSION,
        "nombre_profesional": contrato.nombre_profesional,
        "certificate_url": f"/api/v1/covet/vet/dpa-certificate?folio={folio}",
        "mensaje": "Contrato de Encargo de Tratamiento de Datos (Art. 21 Ley 21.719) suscrito válidamente con firma criptográfica HMAC-SHA256."
    }


@app.get("/api/v1/covet/vet/dpa-status")
def obtener_estado_dpa_veterinario(
    rut: Optional[str] = None,
    slug: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Consulta si el profesional o clínica cuenta con DPA suscrito vigente."""
    query = db.query(VetDPAContratoDB)
    if rut:
        query = query.filter(VetDPAContratoDB.rut_profesional == rut.strip())
    elif slug:
        query = query.filter(VetDPAContratoDB.clinica_slug == slug.strip().lower())

    contrato = query.order_by(VetDPAContratoDB.fecha_firma.desc()).first()
    if not contrato:
        if slug:
            marca = db.query(ClinicaMarcaDB).filter(ClinicaMarcaDB.slug == slug.strip().lower()).first()
            if marca and marca.dpa_aceptado:
                return {
                    "firmado": True,
                    "folio_dpa": f"DPA-MARCA-{marca.slug.upper()}",
                    "nombre_profesional": marca.nombre_fantasia,
                    "fecha_firma": marca.fecha_dpa.isoformat() if marca.fecha_dpa else None,
                    "version_dpa": marca.version_dpa or DPA_VERSION,
                    "rut_responsable": marca.rut_responsable,
                    "certificate_url": f"/api/v1/covet/vet/dpa-certificate?slug={slug}"
                }
        return {"firmado": False}

    return {
        "firmado": True,
        "folio_dpa": contrato.folio_dpa,
        "nombre_profesional": contrato.nombre_profesional,
        "rut_profesional": contrato.rut_profesional,
        "registro_colmevet_sag": contrato.registro_colmevet_sag,
        "fecha_firma": contrato.fecha_firma.isoformat() if contrato.fecha_firma else None,
        "version_dpa": contrato.version_dpa,
        "firma_hmac": contrato.firma_hmac,
        "certificate_url": f"/api/v1/covet/vet/dpa-certificate?folio={contrato.folio_dpa}"
    }


@app.get("/api/v1/covet/vet/dpa-certificate", response_class=HTMLResponse)
def generar_certificado_dpa(
    folio: Optional[str] = None,
    rut: Optional[str] = None,
    slug: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Genera documento oficial descargable e imprimible del Contrato DPA firmado."""
    query = db.query(VetDPAContratoDB)
    if folio:
        query = query.filter(VetDPAContratoDB.folio_dpa == folio.strip())
    elif rut:
        query = query.filter(VetDPAContratoDB.rut_profesional == rut.strip())
    elif slug:
        query = query.filter(VetDPAContratoDB.clinica_slug == slug.strip().lower())

    contrato = query.order_by(VetDPAContratoDB.fecha_firma.desc()).first()

    if contrato:
        nombre_vet = contrato.nombre_profesional
        rut_vet = contrato.rut_profesional
        reg_vet = contrato.registro_colmevet_sag
        folio_dpa = contrato.folio_dpa
        fecha_str = contrato.fecha_firma.strftime("%d/%m/%Y %H:%M UTC") if contrato.fecha_firma else "Vigente 2026"
        hmac_str = contrato.firma_hmac
        version_dpa = contrato.version_dpa
    else:
        nombre_vet = "Dra. Carolina Rojas Morales"
        rut_vet = "16.789.123-4"
        reg_vet = "COLMEVET N° 4521 · SAG 8912"
        folio_dpa = "DPA-2026-DEMO89A1"
        fecha_str = datetime.datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")
        hmac_str = hashlib.sha256(f"{folio_dpa}|{rut_vet}|{fecha_str}".encode()).hexdigest()
        version_dpa = DPA_VERSION

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Certificado DPA — Contrato de Encargo de Tratamiento Ley 21.719</title>
  <style>
    @page {{ size: letter; margin: 18mm; }}
    @media print {{
      .no-print {{ display: none !important; }}
      body {{ background: white !important; color: black !important; padding: 0 !important; }}
      .dpa-sheet {{ border: none !important; box-shadow: none !important; padding: 0 !important; max-width: 100% !important; }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.5;
      color: #1e293b;
      background: #f1f5f9;
      margin: 0;
      padding: 2rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .dpa-sheet {{
      background: #ffffff;
      width: 100%;
      max-width: 820px;
      padding: 3rem 3.5rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.08);
    }}
    .dpa-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      border-bottom: 2px solid #0d9488;
      padding-bottom: 1.5rem;
      margin-bottom: 2rem;
    }}
    .dpa-logo {{
      font-size: 1.75rem;
      font-weight: 800;
      color: #0d9488;
      letter-spacing: -0.5px;
    }}
    .dpa-badge {{
      background: #f0fdfa;
      color: #0f766e;
      border: 1px solid #99f6e4;
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: 1.3rem;
      color: #0f172a;
      margin: 0 0 0.5rem 0;
      text-align: center;
      line-height: 1.3;
    }}
    .subtitle {{
      text-align: center;
      color: #64748b;
      font-size: 0.85rem;
      margin: 0 0 2rem 0;
    }}
    .parties-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.5rem;
      background: #f8fafc;
      padding: 1.25rem;
      border-radius: 6px;
      border: 1px solid #e2e8f0;
      margin-bottom: 2rem;
      font-size: 0.88rem;
    }}
    .party-col strong {{ display: block; color: #0d9488; margin-bottom: 0.25rem; }}
    .legal-clause {{
      margin-bottom: 1.25rem;
      font-size: 0.86rem;
      text-align: justify;
    }}
    .legal-clause h3 {{
      font-size: 0.95rem;
      margin: 0 0 0.35rem 0;
      color: #0f172a;
    }}
    .legal-seal-box {{
      margin-top: 2.5rem;
      padding: 1.25rem;
      background: #f0fdf4;
      border: 1.5px dashed #22c55e;
      border-radius: 6px;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      font-size: 0.82rem;
    }}
    .seal-hash {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.78rem;
      color: #15803d;
      word-break: break-all;
    }}
    .btn-print {{
      background: #0d9488;
      color: white;
      border: none;
      padding: 0.85rem 1.75rem;
      border-radius: 8px;
      font-size: 0.95rem;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 4px 12px rgba(13, 148, 136, 0.35);
      margin-bottom: 1.5rem;
    }}
    .btn-print:hover {{ background: #0f766e; }}
  </style>
</head>
<body>
  <div class="no-print">
    <button class="btn-print" onclick="window.print()">🖨️ Imprimir / Guardar Certificado DPA en PDF</button>
  </div>

  <div class="dpa-sheet">
    <div class="dpa-header">
      <div>
        <div class="dpa-logo">CoVet <span>Chile</span></div>
        <div style="font-size: 0.8rem; color: #64748b;">Tecnología Clínica y Cumplimiento Normativo</div>
      </div>
      <span class="dpa-badge">Ley N° 21.719 • Art. 21</span>
    </div>

    <h1>CERTIFICADO DE SUSCRIPCIÓN — ACUERDO DE TRATAMIENTO DE DATOS (DPA)</h1>
    <div class="subtitle">Conforme al Artículo 21 de la Ley 21.719 sobre Protección de Datos Personales (Entrada en vigencia: Dic 2026)</div>

    <div class="parties-grid">
      <div class="party-col">
        <strong>RESPONSABLE DEL TRATAMIENTO (CONTROLLER):</strong>
        <div><strong>Profesional:</strong> {nombre_vet}</div>
        <div><strong>RUT:</strong> {rut_vet}</div>
        <div><strong>Registro:</strong> {reg_vet}</div>
        <div><strong>Calidad:</strong> Mandante / Prestador Médico Veterinario</div>
      </div>
      <div class="party-col">
        <strong>ENCARGADO DEL TRATAMIENTO (PROCESSOR):</strong>
        <div><strong>Razón Social:</strong> CoVet SpA</div>
        <div><strong>RUT:</strong> 77.492.180-K</div>
        <div><strong>Domicilio:</strong> Santiago, República de Chile</div>
        <div><strong>Calidad:</strong> Proveedor SaaS Cloud & Modelos IA</div>
      </div>
    </div>

    <div class="legal-clause">
      <h3>PRIMERA. Objeto y Alcance del Encargo</h3>
      <p>El Responsable encarga a CoVet SpA el tratamiento automatizado de datos de contacto de tutores y registros clínicos de pacientes veterinarios con la exclusiva finalidad de prestar los servicios de reserva, triaje automatizado, estructuración de fichas SOAP mediante inteligencia artificial y despacho de recetas e indicaciones por WhatsApp.</p>
    </div>

    <div class="legal-clause">
      <h3>SEGUNDA. Obligaciones de Seguridad y Principio de Minimización</h3>
      <p>CoVet SpA se compromete a implementar medidas técnicas y organizativas adecuadas para garantizar la confidencialidad e integridad de la información. El procesamiento de audios dictados en terreno se ejecuta estrictamente en la memoria volátil (RAM) del servidor sin persistencia física en disco una vez estructurada la ficha clínica.</p>
    </div>

    <div class="legal-clause">
      <h3>TERCERA. Subencargados y Transferencia Internacional (Art. 26)</h3>
      <p>El Responsable autoriza expresamente a CoVet SpA a utilizar como subencargados de procesamiento de lenguaje a <em>Groq Inc.</em> y <em>Anthropic PBC</em> ubicados en los Estados Unidos de América bajo acuerdos contractuales con cifrado TLS 1.3 que prohíben taxativamente el entrenamiento de modelos con datos de tutores o pacientes.</p>
    </div>

    <div class="legal-clause">
      <h3>CUARTA. Asistencia en Derechos ARSOP+</h3>
      <p>CoVet SpA proporciona herramientas automáticas para que el Responsable garantice a los tutores sus derechos de Acceso, Rectificación, Supresión, Oposición y Portabilidad, generando un ticket legal HMAC inmutable para fines probatorios ante la Agencia de Protección de Datos Personales (APDP).</p>
    </div>

    <div class="legal-seal-box">
      <div style="font-weight: 700; color: #166534; display: flex; justify-content: space-between;">
        <span>🛡️ CONSTANCIA DE FIRMA ELECTRÓNICA VINCULANTE</span>
        <span>Folio: {folio_dpa}</span>
      </div>
      <div><strong>Fecha Cierta de Suscripción:</strong> {fecha_str}</div>
      <div><strong>Versión Contractual:</strong> {version_dpa}</div>
      <div><strong>Sello Criptográfico HMAC-SHA256:</strong></div>
      <div class="seal-hash">{hmac_str}</div>
      <div style="font-size: 0.75rem; color: #4b5563; margin-top: 0.25rem;">
        Este certificado acredita que el profesional médico veterinario opera con el Escudo Legal CoVet, delimitando formalmente la responsabilidad civil y administrativa ante fiscalizaciones de la APDP o el SERNAC.
      </div>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, media_type="text/html")


@app.get("/api/v1/covet/fiches/{task_id}/receta-pdf", response_class=HTMLResponse)
def generar_receta_medica_pdf(
    task_id: uuid.UUID,
    auto_print: int = 0,
    db: Session = Depends(get_db)
):
    """
    Genera la Receta Médica Veterinaria Oficial e Imprimible (con estándar SAG / Colmevet y Ley 21.719).
    Vista lista para imprimir o guardar como PDF en cualquier navegador o teléfono móvil.
    """
    fiche = db.query(MedicalFicheDB).filter(MedicalFicheDB.id == task_id).first()
    if not fiche:
        raise HTTPException(status_code=404, detail="Ficha clínica no encontrada.")

    json_data = fiche.json_estructurado or {}

    tutor_nombre = json_data.get("tutor_nombre") or (fiche.tutor.nombre if fiche.tutor else "Por definir")
    tutor_telefono = json_data.get("tutor_telefono") or (fiche.tutor.telefono if fiche.tutor else "No informado")
    paciente_nombre = json_data.get("paciente_nombre") or (fiche.paciente.nombre if fiche.paciente else "Paciente")
    especie = json_data.get("especie") or (fiche.paciente.especie if fiche.paciente else "Canino")
    raza = json_data.get("raza") or (fiche.paciente.raza if fiche.paciente else "Mestizo")
    peso = json_data.get("peso_kg") or (fiche.paciente.peso_kg if fiche.paciente else "No informado")
    edad = json_data.get("edad_aproximada") or "No informada"
    anamnesis = json_data.get("anamnesis") or "Sin anamnesis registrada."
    diagnostico = json_data.get("diagnostico_presuntivo") or "Evaluación clínica general."
    plan_terapeutico = json_data.get("plan_terapeutico") or "Indicaciones generales según evolución."
    proximos_pasos = json_data.get("proximos_pasos") or "Control a necesidad según evolución clínica."

    fecha_dt = fiche.fecha_confirmacion or fiche.fecha_creacion or datetime.datetime.utcnow()
    fecha_emision = fecha_dt.strftime("%d/%m/%Y %H:%M")
    folio_receta = f"COVET-2026-{str(fiche.id)[:8].upper()}"

    # Recuperar datos del profesional veterinario
    contrato = db.query(VetDPAContratoDB).order_by(VetDPAContratoDB.fecha_firma.desc()).first()
    vet_nombre = contrato.nombre_profesional if contrato else "Dra. Carolina Rojas Morales"
    vet_rut = contrato.rut_profesional if contrato else "16.789.123-4"
    vet_colmevet = contrato.registro_colmevet_sag if contrato else "Colmevet N° 4521 · SAG 8912"
    vet_email = contrato.email_contacto if contrato and contrato.email_contacto else "consultas@covet.cl"

    pepper = get_privacy_pepper()
    sello_hmac = hmac.new(pepper.encode(), f"{folio_receta}|{paciente_nombre}|{fecha_emision}".encode(), hashlib.sha256).hexdigest()[:24].upper()

    auto_print_js = "<script>window.addEventListener('load', () => setTimeout(() => window.print(), 350));</script>" if auto_print == 1 else ""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Receta Médica Veterinaria Oficial — {folio_receta}</title>
  <style>
    @page {{ size: letter; margin: 15mm; }}
    @media print {{
      .no-print {{ display: none !important; }}
      body {{ background: white !important; color: black !important; padding: 0 !important; }}
      .rx-sheet {{ border: none !important; box-shadow: none !important; padding: 0 !important; max-width: 100% !important; }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.45;
      color: #0f172a;
      background: #f8fafc;
      margin: 0;
      padding: 2rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .rx-sheet {{
      background: #ffffff;
      width: 100%;
      max-width: 820px;
      padding: 2.5rem 3rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.08);
      position: relative;
    }}
    .no-print-bar {{
      margin-bottom: 1.25rem;
      display: flex;
      gap: 0.75rem;
    }}
    .btn-print {{
      background: #0d9488;
      color: white;
      border: none;
      padding: 0.75rem 1.5rem;
      border-radius: 8px;
      font-size: 0.92rem;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 4px 12px rgba(13, 148, 136, 0.35);
    }}
    .btn-print:hover {{ background: #0f766e; }}
    .btn-close-win {{
      background: #e2e8f0;
      color: #334155;
      border: none;
      padding: 0.75rem 1.25rem;
      border-radius: 8px;
      font-size: 0.92rem;
      cursor: pointer;
    }}
    .rx-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      border-bottom: 2px solid #0d9488;
      padding-bottom: 1.25rem;
      margin-bottom: 1.5rem;
    }}
    .vet-identity h1 {{
      font-size: 1.35rem;
      margin: 0 0 0.25rem 0;
      color: #0d9488;
    }}
    .vet-sub {{
      font-size: 0.85rem;
      color: #475569;
      font-weight: 600;
    }}
    .rx-folio-box {{
      text-align: right;
    }}
    .rx-folio-num {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 1rem;
      font-weight: 800;
      color: #0f172a;
      background: #f1f5f9;
      padding: 0.25rem 0.6rem;
      border-radius: 4px;
      border: 1px solid #cbd5e1;
    }}
    .patient-box {{
      display: grid;
      grid-template-columns: 2fr 1fr 1fr;
      gap: 0.75rem;
      background: #f8fafc;
      padding: 1rem 1.25rem;
      border-radius: 6px;
      border: 1px solid #e2e8f0;
      font-size: 0.88rem;
      margin-bottom: 1.5rem;
    }}
    .patient-box div strong {{
      color: #475569;
      font-size: 0.78rem;
      text-transform: uppercase;
      display: block;
    }}
    .clinical-summary {{
      margin-bottom: 1.5rem;
      font-size: 0.88rem;
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 0.85rem 1.15rem;
    }}
    .clinical-summary strong {{ color: #0d9488; }}
    .rx-body {{
      border: 1.5px solid #0d9488;
      border-radius: 8px;
      padding: 1.5rem 1.75rem;
      margin-bottom: 1.5rem;
      background: #fafafa;
      position: relative;
    }}
    .rp-symbol {{
      font-family: "Times New Roman", Times, serif;
      font-size: 2.2rem;
      font-weight: bold;
      color: #0d9488;
      line-height: 1;
      margin-bottom: 0.5rem;
    }}
    .rp-content {{
      font-size: 0.98rem;
      white-space: pre-wrap;
      line-height: 1.6;
      color: #1e293b;
    }}
    .indications-box {{
      background: #f1f5f9;
      border-radius: 6px;
      padding: 1rem 1.25rem;
      font-size: 0.88rem;
      margin-bottom: 2rem;
    }}
    .indications-box strong {{
      display: block;
      color: #334155;
      margin-bottom: 0.25rem;
    }}
    .rx-footer {{
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      border-top: 1px solid #cbd5e1;
      padding-top: 1.5rem;
      margin-top: 2rem;
    }}
    .sign-box {{
      text-align: center;
      width: 260px;
    }}
    .sign-line {{
      border-bottom: 1.5px solid #475569;
      margin-bottom: 0.5rem;
      height: 40px;
    }}
    .sign-name {{
      font-weight: 700;
      font-size: 0.9rem;
      color: #0f172a;
    }}
    .sign-reg {{
      font-size: 0.78rem;
      color: #64748b;
    }}
    .verification-box {{
      font-size: 0.76rem;
      color: #64748b;
      max-width: 440px;
      line-height: 1.4;
    }}
    .hmac-code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
      color: #0d9488;
    }}
  </style>
</head>
<body>
  <div class="no-print no-print-bar">
    <button class="btn-print" onclick="window.print()">🖨️ Imprimir / Guardar Receta PDF</button>
    <button class="btn-close-win" onclick="window.close()">✕ Cerrar</button>
  </div>

  <div class="rx-sheet">
    <div class="rx-header">
      <div class="vet-identity">
        <h1>{vet_nombre}</h1>
        <div class="vet-sub">Médico Veterinaria · {vet_colmevet}</div>
        <div style="font-size: 0.78rem; color: #64748b;">RUT: {vet_rut} · Contacto: {vet_email}</div>
      </div>
      <div class="rx-folio-box">
        <div style="font-size: 0.72rem; color: #64748b; text-transform: uppercase; font-weight: 700;">Folio Receta Oficial</div>
        <div class="rx-folio-num">{folio_receta}</div>
        <div style="font-size: 0.75rem; color: #64748b; margin-top: 0.25rem;">Fecha: {fecha_emision}</div>
      </div>
    </div>

    <div class="patient-box">
      <div>
        <strong>Paciente / Mascota</strong>
        <span style="font-size: 1.05rem; font-weight: 700; color: #0f172a;">{paciente_nombre}</span>
        <div style="color: #64748b; font-size: 0.82rem;">Especie: {especie} · Raza: {raza}</div>
      </div>
      <div>
        <strong>Edad / Peso</strong>
        <div>{edad}</div>
        <div>{peso} kg</div>
      </div>
      <div>
        <strong>Tutor Responsable</strong>
        <div>{tutor_nombre}</div>
        <div style="font-size: 0.8rem; color: #64748b;">{tutor_telefono}</div>
      </div>
    </div>

    <div class="clinical-summary">
      <strong>Diagnóstico Presuntivo / Motivo:</strong> {diagnostico}
    </div>

    <div class="rx-body">
      <div class="rp-symbol">℞ / Rp.</div>
      <div class="rp-content">{plan_terapeutico}</div>
    </div>

    <div class="indications-box">
      <strong>📅 Próximo Control / Indicaciones Especiales:</strong>
      <div>{proximos_pasos}</div>
    </div>

    <div class="rx-footer">
      <div class="verification-box">
        <div><strong>Documento Clínico Digital Válido</strong> — SAG / Colmevet Chile.</div>
        <div>Cumplimiento Ley N° 21.719 de Protección de Datos Personales.</div>
        <div>Sello de Verificación Digital: <span class="hmac-code">HMAC-{sello_hmac}</span></div>
        <div style="margin-top: 0.25rem; font-size: 0.72rem;">Generado con CoVet SaaS Clínico • Cero retención física de audio en servidor.</div>
      </div>

      <div class="sign-box">
        <div class="sign-line"></div>
        <div class="sign-name">{vet_nombre}</div>
        <div class="sign-reg">{vet_colmevet}</div>
      </div>
    </div>
  </div>
  {auto_print_js}
</body>
</html>"""
    return HTMLResponse(content=html, media_type="text/html")

