// VetSaaS Frontend — Bridge to Hetzner VPS (FastAPI + PostgreSQL)

const API_BASE = import.meta.env.VITE_API_URL || 'https://vet-api.142.132.233.38.sslip.io';

const statusDot = document.querySelector('.status-dot');
const statusText = document.getElementById('backend-status-text');
const dbStatusText = document.getElementById('db-status-text');

const kpiCitas = document.getElementById('kpi-citas-count');
const kpiPacientes = document.getElementById('kpi-pacientes-count');
const kpiTutores = document.getElementById('kpi-tutores-count');

const appointmentsContainer = document.getElementById('appointments-list');
const appointmentForm = document.getElementById('appointment-form');
const btnSubmit = document.getElementById('btn-submit-appointment');
const btnRefresh = document.getElementById('btn-refresh');

// Set default datetime to tomorrow at 10:00 AM
const tomorrow = new Date();
tomorrow.setDate(tomorrow.getDate() + 1);
tomorrow.setHours(10, 0, 0, 0);
const dateInput = document.getElementById('cita-fecha');
if (dateInput) {
  dateInput.value = tomorrow.toISOString().slice(0, 16);
}

// Check Backend & DB Health
async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.status === 'healthy') {
      statusDot.className = 'status-dot active';
      statusText.textContent = 'Hetzner FastAPI Online';
      dbStatusText.textContent = `PostgreSQL Conectado`;
    } else {
      statusDot.className = 'status-dot';
      statusText.textContent = 'Backend Degradado';
      dbStatusText.textContent = data.database || 'DB Error';
    }
  } catch (err) {
    statusDot.className = 'status-dot';
    statusText.textContent = 'Backend Desconectado';
    dbStatusText.textContent = 'Sin Conexión';
    console.warn('Health check failed:', err);
  }
}

// Load Appointments & Stats
async function loadData() {
  try {
    const [citasRes, pacientesRes, tutoresRes] = await Promise.all([
      fetch(`${API_BASE}/api/citas`).catch(() => null),
      fetch(`${API_BASE}/api/pacientes`).catch(() => null),
      fetch(`${API_BASE}/api/tutores`).catch(() => null)
    ]);

    const citas = citasRes && citasRes.ok ? await citasRes.json() : [];
    const pacientes = pacientesRes && pacientesRes.ok ? await pacientesRes.json() : [];
    const tutores = tutoresRes && tutoresRes.ok ? await tutoresRes.json() : [];

    // Update KPIs
    kpiCitas.textContent = citas.length;
    kpiPacientes.textContent = pacientes.length;
    kpiTutores.textContent = tutores.length;

    renderAppointments(citas, pacientes);
  } catch (err) {
    console.error('Error loading data:', err);
    appointmentsContainer.innerHTML = `
      <div class="empty-state">
        <p>No se pudieron cargar las citas. Verifica la conexión con el servidor.</p>
      </div>
    `;
  }
}

function renderAppointments(citas, pacientes) {
  if (!citas || citas.length === 0) {
    appointmentsContainer.innerHTML = `
      <div class="empty-state">
        <p>No hay citas programadas aún. Registra la primera usando el formulario.</p>
      </div>
    `;
    return;
  }

  const pacientesMap = new Map();
  pacientes.forEach(p => pacientesMap.set(p.id, p));

  appointmentsContainer.innerHTML = citas.map(cita => {
    const paciente = pacientesMap.get(cita.paciente_id) || { nombre: 'Paciente # ' + cita.paciente_id, especie: 'Mascota' };
    const dateFormatted = new Date(cita.fecha_hora).toLocaleString('es-CL', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    });
    const emoji = paciente.especie === 'Felino' ? '🐱' : paciente.especie === 'Canino' ? '🐶' : '🐾';

    return `
      <div class="appointment-card">
        <div class="cita-main">
          <div class="cita-pet">
            <span>${emoji}</span>
            <span>${escapeHtml(paciente.nombre)}</span>
            <span class="cita-status-tag">WhatsApp Activo</span>
          </div>
          <div class="cita-motivo">${escapeHtml(cita.motivo)}</div>
          <div class="cita-time">📅 ${dateFormatted}</div>
        </div>
      </div>
    `;
  }).join('');
}

function escapeHtml(text) {
  if (!text) return '';
  return text.replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[m]);
}

// Handle Form Submission
appointmentForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  btnSubmit.disabled = true;
  btnSubmit.querySelector('span').textContent = 'Guardando en PostgreSQL...';

  try {
    // 1. Crear Tutor
    const tutorData = {
      nombre: document.getElementById('tutor-nombre').value.trim(),
      telefono: document.getElementById('tutor-telefono').value.trim(),
      email: null
    };

    const tutorRes = await fetch(`${API_BASE}/api/tutores`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(tutorData)
    });
    if (!tutorRes.ok) throw new Error('Error al registrar tutor');
    const tutor = await tutorRes.json();

    // 2. Crear Paciente
    const pacienteData = {
      tutor_id: tutor.id,
      nombre: document.getElementById('paciente-nombre').value.trim(),
      especie: document.getElementById('paciente-especie').value,
      raza: document.getElementById('paciente-raza').value.trim() || 'Mestizo',
      peso_kg: parseFloat(document.getElementById('paciente-peso').value) || 10.0
    };

    const pacienteRes = await fetch(`${API_BASE}/api/pacientes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pacienteData)
    });
    if (!pacienteRes.ok) throw new Error('Error al registrar paciente');
    const paciente = await pacienteRes.json();

    // 3. Crear Cita
    const citaData = {
      paciente_id: paciente.id,
      fecha_hora: new Date(document.getElementById('cita-fecha').value).toISOString(),
      motivo: document.getElementById('cita-motivo').value.trim()
    };

    const citaRes = await fetch(`${API_BASE}/api/citas`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(citaData)
    });
    if (!citaRes.ok) throw new Error('Error al agendar cita');

    // Reset Form
    appointmentForm.reset();
    if (dateInput) dateInput.value = tomorrow.toISOString().slice(0, 16);

    btnSubmit.querySelector('span').textContent = '✅ Cita Guardada con Éxito!';
    setTimeout(() => {
      btnSubmit.disabled = false;
      btnSubmit.querySelector('span').textContent = 'Guardar Cita & Programar Recordatorio';
    }, 2000);

    // Refresh Data
    await loadData();
  } catch (err) {
    alert(`Error: ${err.message}`);
    btnSubmit.disabled = false;
    btnSubmit.querySelector('span').textContent = 'Guardar Cita & Programar Recordatorio';
  }
});

btnRefresh.addEventListener('click', () => {
  loadData();
  checkHealth();
});

// Initial run & Interval polling
checkHealth();
loadData();
setInterval(checkHealth, 15000);
