# VetSaaS — Micro-SaaS para Clínicas y Consultas Veterinarias

Sistema de gestión operativa, agendamiento ágil y fidelización preventiva automatizada por WhatsApp para clínicas veterinarias, consultas independientes y veterinarios a domicilio en Chile y Latinoamérica.

## Propósito y Filosofía de Negocio
- **Foco en el Flujo de Caja de la Veterinaria:** Resolver el mayor agujero financiero de las veterinarias: el ausentismo a citas y el olvido de controles periódicos (vacunas, desparasitaciones, revisiones).
- **Cero Fricción para el Tutor:** El cliente/tutor no necesita instalar apps complejas; recibe confirmaciones, recordatorios interactivos y recetas directamente por WhatsApp.
- **Ficha Clínica Ágil:** Diseñada para que el médico veterinario registre consultas en menos de 90 segundos desde su móvil o tablet durante la atención.
- **Micro-SaaS Multi-Tenant:** Arquitectura ligera, bajo consumo de recursos en VPS y preparada para onboarding automático de nuevas clínicas clientes.

---

## Arquitectura Multi-Agente (Sub-Agentes Especializados)

Para garantizar velocidad y modularidad en el desarrollo bajo Vibe Coding, el proyecto opera con sub-agentes con responsabilidades desacopladas:

1. **VetOrchestrator (Sebastián - Chief Architect):**
   - Supervisa la arquitectura general, dependencias y consistencia entre capas.
   - Valida el roadmap hacia el MVP comercializable y las integraciones de despliegue.

2. **VetClinicalAgent (Gestión Clínica & Pacientes):**
   - Diseña y mantiene los contratos de datos: tutores, pacientes (especie, raza, edad, peso), historial clínico, recetas y calendarios sanitarios.
   - Garantiza validación estricta de dosis y trazabilidad de tratamientos.

3. **VetCommAgent (Automatización & WhatsApp):**
   - Orquestador de plantillas, disparadores automáticos y confirmaciones bidireccionales vía WhatsApp API (Evolution API / Meta Cloud API).
   - Manejo de respuestas automáticas de confirmación/cancelación de citas.

4. **VetUIUXAgent (Frontend & PWA):**
   - Construcción de interfaces modernas, intuitivas y responsivas (mobile-first para veterinarios en terreno).
   - Panel de control de citas, calendario visual y vista rápida de ficha médica.

5. **VetBillingAgent (Suscripciones & Monetización):**
   - Integración de pasarelas de pago locales (Flow / MercadoPago / Stripe) para el cobro mensual del SaaS a las veterinarias.
   - Control de límites de plan (número de mensajes, médicos activos, etc.).

---

## Stack Tecnológico
- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy / SQLModel con SQLite (local) o PostgreSQL (producción Coolify).
- **Frontend:** PWA moderna con Vite + TypeScript / Vanilla modular con diseño accesible, rápido y limpio.
- **Notificaciones & Mensajería:** WhatsApp API (Webhooks asíncronos), templates aprobados y flujos en n8n opcionales.
- **Infraestructura & Despliegue:**
  - Servidor: VPS Hetzner Cloud (`142.132.233.38`).
  - Orquestador PaaS: **Coolify** con Dockerfile autónomo y red `coolify`.
  - Dominio & SSL: Subdominios autogestionados mediante Caddy / Coolify Traefik con Let's Encrypt TLS.
  - CI/CD: Git Webhook automático (`push` a rama `main` despliega automáticamente).

---

## Estructura del Repositorio (`/home/ashasasha/vet`)
- `backend/` — API REST en FastAPI, servicios de pacientes, agenda y WhatsApp.
- `frontend/` — Interfaz web PWA para recepción, médicos y tutores.
- `docker/` — Dockerfile y docker-compose optimizado para Coolify.
- `.agents/` — Protocolos, reglas y habilidades específicas del dominio veterinario.
- `docs/` — Especificaciones de producto, guías de onboarding y pitch comercial.

---

## Convenciones de Desarrollo
- **Patrón A.G.E.N.T.:** Aim (objetivo claro), Give Identity (rol al sub-agente), Execute (modular), Non-assumption (validar antes de asumir), Test & Evaluate.
- **Context7:** Obligatorio consultar la documentación activa de librerías externas antes de implementar código nuevo.
- **Cero Basura:** Prohibido crear scripts basura o duplicados (`_v2`, `_v3`, `.tmp`).
- **Seguridad:** Ningún token de WhatsApp, credencial de BD o secreto debe commitearse en Git. Uso exclusivo de variables de entorno (`.env`).
