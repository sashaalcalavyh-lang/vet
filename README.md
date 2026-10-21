# VetSaaS 🐾

Micro-SaaS para gestión ágil, fichas clínicas y fidelización preventiva por WhatsApp para clínicas veterinarias.

## 🚀 Arquitectura Híbrida

- **Frontend (Vercel):** PWA moderna e intuitiva construida con Vite.
- **Backend (Hetzner VPS / Coolify):** API REST de alto rendimiento en FastAPI (`https://vet-api.142.132.233.38.sslip.io`).
- **Base de Datos (Hetzner VPS):** PostgreSQL 16 aislado en red Docker privada (sin exposición pública).
- **Notificaciones (n8n):** Flujos automatizados para confirmación de citas y recordatorios de vacunas por WhatsApp.

---

## 📂 Estructura del Proyecto

```
vet/
├── backend/            # API FastAPI y modelos SQLAlchemy
│   ├── main.py
│   └── requirements.txt
├── frontend/           # PWA Vite + Vanilla JS + CSS Glassmorphism
│   ├── index.html
│   ├── main.js
│   └── style.css
├── .agents/            # Reglas y sub-agentes especializados
├── docker-compose.yml  # Despliegue de Backend + PostgreSQL en Hetzner
├── Dockerfile          # Imagen de contenedor para Backend
└── vercel.json         # Configuración de build automático para Vercel
```

---

## 🛠️ Desarrollo Local

### 1. Backend
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

### 2. Frontend
```bash
cd frontend
npm install
npm run dev
```

---

## 🌐 Variables de Entorno

### Frontend (.env en Vercel)
```env
VITE_API_URL=https://vet-api.142.132.233.38.sslip.io
```

### Backend (.env en Hetzner)
```env
DATABASE_URL=postgresql+psycopg2://vet_admin:PASSWORD@vet-postgres:5432/vet_saas_db
APP_ENV=production
```
