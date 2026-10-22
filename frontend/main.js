// CoVet Frontend — Núcleo Clínico SOAP con IA & Fidelización WhatsApp
// Monorepo Hetzner VPS + Vercel Edge

const API_BASE = import.meta.env.VITE_API_URL || 'https://vet-api.142.132.233.38.sslip.io';

// ==========================================
// 1. ESTADO GLOBAL DE LA APLICACIÓN
// ==========================================
let currentTaskId = null;
let mediaRecorder = null;
let audioChunks = [];
let recordingTimerInterval = null;
let recordingSeconds = 0;
let pollingInterval = null;
let currentMimeType = '';

// ==========================================
// 2. ELEMENTOS DEL DOM
// ==========================================
const statusDot = document.querySelector('.status-dot');
const statusText = document.getElementById('backend-status-text');
const dbStatusText = document.getElementById('db-status-text');

// Tabs
const tabBtns = document.querySelectorAll('.tab-btn');
const tabPanes = document.querySelectorAll('.tab-pane');
const tabCitasCounter = document.getElementById('tab-citas-counter');

// Estados del Flujo Vibe & Verify
const stateIdle = document.getElementById('state-idle');
const stateRecording = document.getElementById('state-recording');
const stateProcessing = document.getElementById('state-processing');
const stateReview = document.getElementById('state-review');

// Estado 1 (Idle)
const selectCitaContexto = document.getElementById('select-cita-contexto');
const btnStartRecord = document.getElementById('btn-start-record');

// Estado 2 (Recording)
const recordingTimer = document.getElementById('recording-timer');
const btnStopRecord = document.getElementById('btn-stop-record');
const btnCancelRecord = document.getElementById('btn-cancel-record');

// Estado 3 (Processing)
const processingStepTitle = document.getElementById('processing-step-title');
const processingStepDesc = document.getElementById('processing-step-desc');
const processingBarFill = document.getElementById('processing-bar-fill');
const processingErrorBanner = document.getElementById('processing-error-banner');
const processingErrorMessage = document.getElementById('processing-error-message');
const btnRetryRecord = document.getElementById('btn-retry-record');

// Pasos checklist
const stepUpload = document.getElementById('step-upload');
const stepWhisper = document.getElementById('step-whisper');
const stepClaude = document.getElementById('step-claude');
const stepReady = document.getElementById('step-ready');

// Estado 4 (Review SOAP)
const formSoapReview = document.getElementById('form-soap-review');
const soapFicheIdInput = document.getElementById('soap-fiche-id');
const reviewTutorNombre = document.getElementById('review-tutor-nombre');
const reviewTutorTelefono = document.getElementById('review-tutor-telefono');
const reviewPacienteNombre = document.getElementById('review-paciente-nombre');
const reviewEspecie = document.getElementById('review-especie');
const reviewRaza = document.getElementById('review-raza');
const reviewEdad = document.getElementById('review-edad');
const reviewPeso = document.getElementById('review-peso');
const reviewAnamnesis = document.getElementById('review-anamnesis');
const reviewExamenFisico = document.getElementById('review-examen-fisico');
const reviewDiagnostico = document.getElementById('review-diagnostico');
const reviewPlanTerapeutico = document.getElementById('review-plan-terapeutico');
const reviewProximosPasos = document.getElementById('review-proximos-pasos');
const reviewTranscriptRaw = document.getElementById('review-transcript-raw');
const btnConfirmSoap = document.getElementById('btn-confirm-soap');
const btnDiscardReview = document.getElementById('btn-discard-review');

// Modal Éxito
const modalSuccess = document.getElementById('modal-success');
const modalSuccessSummary = document.getElementById('modal-success-summary');
const n8nBadgeIndicator = document.getElementById('n8n-badge-indicator');
const n8nStatusText = document.getElementById('n8n-status-text');
const btnModalNewSoap = document.getElementById('btn-modal-new-soap');
const btnModalViewAgenda = document.getElementById('btn-modal-view-agenda');

// Tab Agenda (Preservada)
const kpiCitas = document.getElementById('kpi-citas-count');
const kpiPacientes = document.getElementById('kpi-pacientes-count');
const kpiTutores = document.getElementById('kpi-tutores-count');
const appointmentsContainer = document.getElementById('appointments-list');
const appointmentForm = document.getElementById('appointment-form');
const btnSubmitAppointment = document.getElementById('btn-submit-appointment');
const btnRefresh = document.getElementById('btn-refresh');

// Tab Historial Fichas
const fichesHistoryList = document.getElementById('fiches-history-list');
const btnRefreshFiches = document.getElementById('btn-refresh-fiches');

// Toast Container
const toastContainer = document.getElementById('toast-container');

// ==========================================
// 3. NAVEGACIÓN ENTRE TABS
// ==========================================
tabBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    const targetTab = btn.getAttribute('data-tab');

    tabBtns.forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
    });
    tabPanes.forEach(p => p.classList.add('hidden'));

    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    const activePane = document.getElementById(targetTab);
    if (activePane) activePane.classList.remove('hidden');

    if (targetTab === 'tab-agenda') {
      loadAppointmentsData();
    } else if (targetTab === 'tab-history') {
      loadFichesHistory();
    }
  });
});

// ==========================================
// 4. TRANSICIONES DE ESTADO EN DICTADO
// ==========================================
function switchVibeState(stateName) {
  stateIdle.classList.add('hidden');
  stateRecording.classList.add('hidden');
  stateProcessing.classList.add('hidden');
  stateReview.classList.add('hidden');

  if (stateName === 'idle') {
    stateIdle.classList.remove('hidden');
  } else if (stateName === 'recording') {
    stateRecording.classList.remove('hidden');
  } else if (stateName === 'processing') {
    stateProcessing.classList.remove('hidden');
  } else if (stateName === 'review') {
    stateReview.classList.remove('hidden');
    // Auto-ajustar altura de textareas cargados
    document.querySelectorAll('.glass-textarea.auto-resize').forEach(autoResizeTextarea);
  }
}

// ==========================================
// 5. CAPTURA DE AUDIO MULTIPLATAFORMA
// ==========================================
function detectSupportedMimeType() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/aac',
    'audio/ogg;codecs=opus'
  ];

  if (!window.MediaRecorder) {
    return '';
  }

  for (const type of candidates) {
    if (MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return '';
}

async function startRecording() {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showToast('Tu navegador no soporta grabación de micrófono.', 'error');
      return;
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 48000,
        echoCancellation: true,
        noiseSuppression: true
      }
    });

    currentMimeType = detectSupportedMimeType();
    const options = currentMimeType ? { mimeType: currentMimeType } : {};

    try {
      mediaRecorder = new MediaRecorder(stream, options);
    } catch (e) {
      console.warn('Fallback a MediaRecorder básico:', e);
      mediaRecorder = new MediaRecorder(stream);
      currentMimeType = mediaRecorder.mimeType || '';
    }

    audioChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        audioChunks.push(event.data);
      }
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(track => track.stop());
    };

    mediaRecorder.start(250); // Emitir chunks cada 250ms
    startTimer();
    switchVibeState('recording');
    showToast('Grabando dictado clínico...', 'info');

  } catch (err) {
    console.error('Error al solicitar micrófono:', err);
    if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
      showToast('Permiso de micrófono denegado. Habilítalo en los ajustes del navegador.', 'error');
    } else {
      showToast(`No se pudo iniciar la grabación: ${err.message}`, 'error');
    }
  }
}

function startTimer() {
  clearInterval(recordingTimerInterval);
  recordingSeconds = 0;
  updateTimerDisplay();

  recordingTimerInterval = setInterval(() => {
    recordingSeconds++;
    updateTimerDisplay();

    // Notificación preventiva si llega a 90 segundos
    if (recordingSeconds === 90) {
      showToast('90s alcanzados (límite recomendado). Puedes detener y procesar.', 'info');
    }
  }, 1000);
}

function stopTimer() {
  clearInterval(recordingTimerInterval);
}

function updateTimerDisplay() {
  const mins = Math.floor(recordingSeconds / 60).toString().padStart(2, '0');
  const secs = (recordingSeconds % 60).toString().padStart(2, '0');
  recordingTimer.textContent = `${mins}:${secs}`;
}

function stopRecordingAndProcess() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;

  stopTimer();

  mediaRecorder.onstop = async () => {
    const finalMime = currentMimeType || 'audio/webm';
    const audioBlob = new Blob(audioChunks, { type: finalMime });

    if (audioBlob.size === 0) {
      showToast('El audio grabado está vacío.', 'error');
      switchVibeState('idle');
      return;
    }

    // Determinar extensión adecuada
    const ext = finalMime.includes('mp4') ? 'mp4' : finalMime.includes('ogg') ? 'ogg' : 'webm';
    const audioFile = new File([audioBlob], `recording.${ext}`, { type: finalMime });

    uploadAudioAndPoll(audioFile);
  };

  mediaRecorder.stop();
}

function cancelRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.onstop = null;
    mediaRecorder.stop();
    if (mediaRecorder.stream) {
      mediaRecorder.stream.getTracks().forEach(t => t.stop());
    }
  }
  stopTimer();
  audioChunks = [];
  switchVibeState('idle');
  showToast('Grabación cancelada y descartada.', 'info');
}

// ==========================================
// 6. SUBIDA Y POLLING CON ESTADOS DINÁMICOS
// ==========================================
async function uploadAudioAndPoll(audioFile) {
  switchVibeState('processing');
  resetProcessingUI();

  try {
    const formData = new FormData();
    formData.append('file', audioFile);

    const citaContexto = selectCitaContexto.value;
    if (citaContexto) {
      formData.append('cita_id', citaContexto);
    }

    setStepActive(stepUpload, 'Subiendo audio al servidor seguro Hetzner...', 25);

    const response = await fetch(`${API_BASE}/api/v1/covet/transcribe`, {
      method: 'POST',
      body: formData
    });

    if (!response.ok) {
      const errJson = await response.json().catch(() => ({}));
      throw new Error(errJson.detail || `Error HTTP ${response.status} al subir audio`);
    }

    const data = await response.json();
    currentTaskId = data.task_id;

    setStepDone(stepUpload);
    setStepActive(stepWhisper, 'Transcribiendo audio con Groq Whisper Large...', 50);

    // Iniciar Polling cada 2 segundos
    startPolling(currentTaskId);

  } catch (err) {
    console.error('Fallo en la subida:', err);
    showProcessingError(err.message);
  }
}

function startPolling(taskId) {
  clearInterval(pollingInterval);
  let pollAttempts = 0;

  pollingInterval = setInterval(async () => {
    pollAttempts++;
    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/fiches/${taskId}`);
      if (!res.ok) {
        throw new Error(`Error HTTP ${res.status} al consultar estado`);
      }

      const fiche = await res.json();
      const status = fiche.estado_procesamiento;

      if (status === 'PROCESSING') {
        if (pollAttempts > 2) {
          setStepDone(stepWhisper);
          setStepActive(stepClaude, 'Estructurando estándar clínico SOAP con Claude...', 75);
        }
      } else if (status === 'READY_FOR_REVIEW') {
        clearInterval(pollingInterval);
        setStepDone(stepClaude);
        setStepDone(stepReady);
        processingBarFill.style.width = '100%';
        processingStepTitle.textContent = '¡Estructuración Completada!';
        processingStepDesc.textContent = 'Cargando formulario de validación médica...';

        setTimeout(() => {
          populateReviewForm(fiche);
          switchVibeState('review');
        }, 700);

      } else if (status === 'FAILED') {
        clearInterval(pollingInterval);
        showProcessingError(fiche.error_detalle || 'El procesamiento de la ficha falló en el servidor.');
      }

      // Timeout preventivo tras 2 minutos (60 intentos)
      if (pollAttempts > 60) {
        clearInterval(pollingInterval);
        showProcessingError('El procesamiento tomó más tiempo del esperado. Por favor reintenta.');
      }

    } catch (err) {
      console.warn('Error en polling:', err);
    }
  }, 2000);
}

function resetProcessingUI() {
  processingErrorBanner.classList.add('hidden');
  processingBarFill.style.width = '15%';
  [stepUpload, stepWhisper, stepClaude, stepReady].forEach(step => {
    step.className = 'step-item';
  });
}

function setStepActive(stepElem, title, progressPercent) {
  stepElem.className = 'step-item active';
  processingStepTitle.textContent = title;
  processingBarFill.style.width = `${progressPercent}%`;
}

function setStepDone(stepElem) {
  stepElem.className = 'step-item done';
}

function showProcessingError(msg) {
  processingErrorBanner.classList.remove('hidden');
  processingErrorMessage.textContent = msg;
  processingStepTitle.textContent = 'Procesamiento Detenido';
  processingStepDesc.textContent = 'Revisa el detalle a continuación para reintentar.';
}

// ==========================================
// 7. FORMULARIO VIBE & VERIFY (ESTADO 4)
// ==========================================
function populateReviewForm(fiche) {
  const json = fiche.json_estructurado || {};

  soapFicheIdInput.value = fiche.id || '';
  reviewTutorNombre.value = json.tutor_nombre || 'Por definir';
  reviewTutorTelefono.value = json.tutor_telefono || '';
  reviewPacienteNombre.value = json.paciente_nombre || 'Paciente';
  reviewEspecie.value = json.especie || 'Canino';
  reviewRaza.value = json.raza || 'Mestizo';
  reviewEdad.value = json.edad_aproximada || '';
  reviewPeso.value = json.peso_kg !== null && json.peso_kg !== undefined ? json.peso_kg : '';

  reviewAnamnesis.value = json.anamnesis || '';
  reviewExamenFisico.value = json.examen_fisico || '';
  reviewDiagnostico.value = json.diagnostico_presuntivo || '';
  reviewPlanTerapeutico.value = json.plan_terapeutico || '';
  reviewProximosPasos.value = json.proximos_pasos || '';

  reviewTranscriptRaw.textContent = fiche.texto_transcrito_original || 'Sin transcripción literal.';

  // Auto-ajustar alturas
  setTimeout(() => {
    document.querySelectorAll('.glass-textarea.auto-resize').forEach(autoResizeTextarea);
  }, 50);
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = (textarea.scrollHeight + 4) + 'px';
}

document.querySelectorAll('.glass-textarea.auto-resize').forEach(ta => {
  ta.addEventListener('input', () => autoResizeTextarea(ta));
});

// Confirmación de Ficha (PUT)
formSoapReview.addEventListener('submit', async (e) => {
  e.preventDefault();

  const ficheId = soapFicheIdInput.value;
  if (!ficheId) {
    showToast('Falta el ID de la ficha a confirmar.', 'error');
    return;
  }

  // Validación y normalización de WhatsApp
  let rawTel = reviewTutorTelefono.value.trim().replace(/\s+/g, '');
  if (!rawTel) {
    showToast('El teléfono del tutor es obligatorio para el envío por WhatsApp.', 'error');
    reviewTutorTelefono.focus();
    return;
  }

  // Normalizar formato +569XXXXXXXX
  if (/^9\d{8}$/.test(rawTel)) {
    rawTel = `+56${rawTel}`;
  } else if (/^569\d{8}$/.test(rawTel)) {
    rawTel = `+${rawTel}`;
  }

  const payload = {
    tutor_nombre: reviewTutorNombre.value.trim(),
    tutor_telefono: rawTel,
    paciente_nombre: reviewPacienteNombre.value.trim(),
    especie: reviewEspecie.value,
    raza: reviewRaza.value.trim() || 'Mestizo',
    edad_aproximada: reviewEdad.value.trim() || 'No especificada',
    peso_kg: reviewPeso.value ? parseFloat(reviewPeso.value) : null,
    anamnesis: reviewAnamnesis.value.trim(),
    examen_fisico: reviewExamenFisico.value.trim(),
    diagnostico_presuntivo: reviewDiagnostico.value.trim(),
    plan_terapeutico: reviewPlanTerapeutico.value.trim(),
    proximos_pasos: reviewProximosPasos.value.trim()
  };

  btnConfirmSoap.disabled = true;
  btnConfirmSoap.innerHTML = `<span class="spinner" style="width:16px;height:16px;margin:0;display:inline-block;vertical-align:middle;"></span> Guardando y notificando...`;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/fiches/${ficheId}/confirm`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.detail || `Error HTTP ${res.status}`);
    }

    const resData = await res.json();
    showConfirmationSuccessModal(resData);
    loadAppointmentsData(); // Refrescar citas y tutores

  } catch (err) {
    console.error('Error al confirmar ficha:', err);
    showToast(`Error al confirmar: ${err.message}`, 'error');
  } finally {
    btnConfirmSoap.disabled = false;
    btnConfirmSoap.innerHTML = `<span class="btn-icon">✓</span> <span class="btn-text">[✓] Confirmar Ficha y Enviar por WhatsApp</span>`;
  }
});

function showConfirmationSuccessModal(data) {
  modalSuccess.classList.remove('hidden');

  const tutorName = reviewTutorNombre.value || 'Tutor';
  const petName = reviewPacienteNombre.value || 'Paciente';
  modalSuccessSummary.textContent = `La ficha clínica de ${petName} (${tutorName}) ha sido sincronizada en PostgreSQL.`;

  if (data.n8n_dispatched === true) {
    n8nBadgeIndicator.className = 'n8n-badge success';
    n8nBadgeIndicator.textContent = '✅ WhatsApp Despachado';
    n8nStatusText.textContent = 'El webhook de n8n_app recibió la ficha y la receta médica para envío inmediato.';
  } else {
    n8nBadgeIndicator.className = 'n8n-badge pending';
    n8nBadgeIndicator.textContent = '⚠️ Ficha en PostgreSQL (n8n Pendiente)';
    n8nStatusText.textContent = data.detalle || 'La ficha quedó registrada en la base de datos. Se despachará cuando n8n esté activo.';
  }
}

// Acciones del Modal
btnModalNewSoap.addEventListener('click', () => {
  modalSuccess.classList.add('hidden');
  switchVibeState('idle');
});

btnModalViewAgenda.addEventListener('click', () => {
  modalSuccess.classList.add('hidden');
  switchVibeState('idle');
  document.getElementById('tab-btn-agenda').click();
});

btnDiscardReview.addEventListener('click', () => {
  if (confirm('¿Seguro que deseas descartar esta ficha y volver al inicio?')) {
    switchVibeState('idle');
  }
});

btnRetryRecord.addEventListener('click', () => {
  switchVibeState('idle');
});

// Event listeners de botones de grabación
btnStartRecord.addEventListener('click', startRecording);
btnStopRecord.addEventListener('click', stopRecordingAndProcess);
btnCancelRecord.addEventListener('click', cancelRecording);

// ==========================================
// 8. AGENDA & CITAS (TAB 2)
// ==========================================
async function loadAppointmentsData() {
  try {
    const [citasRes, pacientesRes, tutoresRes] = await Promise.all([
      fetch(`${API_BASE}/api/citas`).catch(() => null),
      fetch(`${API_BASE}/api/pacientes`).catch(() => null),
      fetch(`${API_BASE}/api/tutores`).catch(() => null)
    ]);

    const citas = citasRes && citasRes.ok ? await citasRes.json() : [];
    const pacientes = pacientesRes && pacientesRes.ok ? await pacientesRes.json() : [];
    const tutores = tutoresRes && tutoresRes.ok ? await tutoresRes.json() : [];

    // Actualizar Contadores
    kpiCitas.textContent = citas.length;
    kpiPacientes.textContent = pacientes.length;
    kpiTutores.textContent = tutores.length;
    tabCitasCounter.textContent = citas.length;

    renderAppointmentsList(citas, pacientes);
    populateAppointmentContextSelector(citas, pacientes);
  } catch (err) {
    console.error('Error cargando citas:', err);
    appointmentsContainer.innerHTML = `
      <div class="empty-state">
        <p>No se pudieron cargar las citas. Verifica la conexión con Hetzner.</p>
      </div>
    `;
  }
}

function renderAppointmentsList(citas, pacientes) {
  if (!citas || citas.length === 0) {
    appointmentsContainer.innerHTML = `
      <div class="empty-state">
        <p>No hay citas programadas aún. Registra la primera o dicta una ficha en terreno.</p>
      </div>
    `;
    return;
  }

  const pacientesMap = new Map();
  pacientes.forEach(p => pacientesMap.set(p.id, p));

  appointmentsContainer.innerHTML = citas.map(cita => {
    const paciente = pacientesMap.get(cita.paciente_id) || { nombre: 'Paciente #' + cita.paciente_id, especie: 'Canino' };
    const dateFormatted = new Date(cita.fecha_hora).toLocaleString('es-CL', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    });
    const emoji = paciente.especie === 'Felino' ? '🐱' : paciente.especie === 'Canino' ? '🐶' : '🐾';

    return `
      <div class="appointment-card">
        <div class="cita-pet">
          <span>${emoji}</span>
          <span>${escapeHtml(paciente.nombre)}</span>
          <span class="cita-status-tag">Agendada</span>
        </div>
        <div class="cita-motivo">${escapeHtml(cita.motivo)}</div>
        <div class="cita-time">📅 ${dateFormatted}</div>
      </div>
    `;
  }).join('');
}

function populateAppointmentContextSelector(citas, pacientes) {
  const pacientesMap = new Map();
  pacientes.forEach(p => pacientesMap.set(p.id, p));

  selectCitaContexto.innerHTML = '<option value="">-- Consulta espontánea / Sin cita previa --</option>';

  citas.forEach(cita => {
    const paciente = pacientesMap.get(cita.paciente_id);
    const petName = paciente ? paciente.nombre : `Paciente #${cita.paciente_id}`;
    const dateStr = new Date(cita.fecha_hora).toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
    const opt = document.createElement('option');
    opt.value = cita.id;
    opt.textContent = `Cita #${cita.id} — ${petName} (${dateStr}: ${cita.motivo.slice(0, 30)})`;
    selectCitaContexto.appendChild(opt);
  });
}

// Envío del formulario de agendamiento rápido manual
if (appointmentForm) {
  appointmentForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    btnSubmitAppointment.disabled = true;
    btnSubmitAppointment.innerHTML = '<span>Guardando en PostgreSQL...</span>';

    try {
      // 1. Crear Tutor
      const tutorRes = await fetch(`${API_BASE}/api/tutores`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          nombre: document.getElementById('tutor-nombre').value.trim(),
          telefono: document.getElementById('tutor-telefono').value.trim(),
          email: null
        })
      });
      if (!tutorRes.ok) throw new Error('Error al registrar tutor');
      const tutor = await tutorRes.json();

      // 2. Crear Paciente
      const pacienteRes = await fetch(`${API_BASE}/api/pacientes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tutor_id: tutor.id,
          nombre: document.getElementById('paciente-nombre').value.trim(),
          especie: document.getElementById('paciente-especie').value,
          raza: document.getElementById('paciente-raza').value.trim() || 'Mestizo',
          peso_kg: parseFloat(document.getElementById('paciente-peso').value) || 10.0
        })
      });
      if (!pacienteRes.ok) throw new Error('Error al registrar paciente');
      const paciente = await pacienteRes.json();

      // 3. Crear Cita
      const citaRes = await fetch(`${API_BASE}/api/citas`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          paciente_id: paciente.id,
          fecha_hora: new Date(document.getElementById('cita-fecha').value).toISOString(),
          motivo: document.getElementById('cita-motivo').value.trim()
        })
      });
      if (!citaRes.ok) throw new Error('Error al agendar cita');

      showToast('Cita guardada con éxito en PostgreSQL', 'success');
      appointmentForm.reset();
      loadAppointmentsData();

    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      btnSubmitAppointment.disabled = false;
      btnSubmitAppointment.innerHTML = '<span>Guardar Cita en PostgreSQL</span>';
    }
  });
}

btnRefresh.addEventListener('click', () => {
  loadAppointmentsData();
  checkHealth();
  showToast('Datos de agenda actualizados', 'info');
});

// ==========================================
// 9. HISTORIAL DE FICHAS (TAB 3)
// ==========================================
async function loadFichesHistory() {
  fichesHistoryList.innerHTML = `
    <div class="loading-state">
      <span class="spinner"></span>
      <p>Consultando fichas clínicas en PostgreSQL...</p>
    </div>
  `;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/fiches?limit=25`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fiches = await res.json();

    if (!fiches || fiches.length === 0) {
      fichesHistoryList.innerHTML = `
        <div class="empty-state">
          <p>No hay fichas clínicas registradas aún. Toca en "Dictado SOAP" para crear la primera.</p>
        </div>
      `;
      return;
    }

    fichesHistoryList.innerHTML = fiches.map(fiche => {
      const json = fiche.json_estructurado || {};
      const petName = json.paciente_nombre || 'Paciente';
      const tutorName = json.tutor_nombre || 'Tutor';
      const especie = json.especie || 'Mascota';
      const diag = json.diagnostico_presuntivo || 'Sin diagnóstico registrado';
      const emoji = especie === 'Felino' ? '🐱' : especie === 'Canino' ? '🐶' : '🐾';
      
      const createdDate = fiche.fecha_creacion 
        ? new Date(fiche.fecha_creacion).toLocaleString('es-CL', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
        : 'Reciente';

      let statusBadge = '<span class="cita-status-tag">PENDIENTE</span>';
      if (fiche.estado_procesamiento === 'CONFIRMED_AND_SENT') {
        statusBadge = '<span class="cita-status-tag" style="background:rgba(16,185,129,0.2);color:#34d399;">✅ WhatsApp Enviado</span>';
      } else if (fiche.estado_procesamiento === 'CONFIRMED_N8N_PENDING') {
        statusBadge = '<span class="cita-status-tag" style="background:rgba(245,158,11,0.2);color:#fbbf24;">⚠️ n8n Pendiente</span>';
      } else if (fiche.estado_procesamiento === 'READY_FOR_REVIEW') {
        statusBadge = '<span class="cita-status-tag" style="background:rgba(56,189,248,0.2);color:#38bdf8;">📝 Listo Revisar</span>';
      } else if (fiche.estado_procesamiento === 'FAILED') {
        statusBadge = '<span class="cita-status-tag" style="background:rgba(244,63,94,0.2);color:#f43f5e;">❌ Fallido</span>';
      }

      return `
        <div class="fiche-history-card">
          <div class="cita-pet">
            <span>${emoji}</span>
            <span>${escapeHtml(petName)}</span>
            <small style="color:var(--text-muted);font-weight:normal;">(${escapeHtml(tutorName)})</small>
            ${statusBadge}
          </div>
          <div class="cita-motivo"><strong>Dx:</strong> ${escapeHtml(diag)}</div>
          <div class="cita-time">📅 ${createdDate} • ID: ${fiche.id.slice(0, 8)}...</div>
        </div>
      `;
    }).join('');

  } catch (err) {
    fichesHistoryList.innerHTML = `
      <div class="empty-state">
        <p>No se pudo consultar el historial: ${escapeHtml(err.message)}</p>
      </div>
    `;
  }
}

if (btnRefreshFiches) {
  btnRefreshFiches.addEventListener('click', loadFichesHistory);
}

// ==========================================
// 10. HEALTH CHECK & MONITOR
// ==========================================
async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.status === 'healthy') {
      statusDot.className = 'status-dot active';
      statusText.textContent = 'Hetzner Online';
      dbStatusText.textContent = `PostgreSQL 16 OK`;
    } else {
      statusDot.className = 'status-dot';
      statusText.textContent = 'Backend Degradado';
      dbStatusText.textContent = data.database || 'DB Error';
    }
  } catch (err) {
    statusDot.className = 'status-dot';
    statusText.textContent = 'Backend Desconectado';
    dbStatusText.textContent = 'Sin Conexión';
  }
}

// ==========================================
// 11. UTILITARIOS & TOASTS
// ==========================================
function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;

  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(text) {
  if (!text) return '';
  return text.replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[m]);
}

// Configurar fecha por defecto en formulario de citas (+1 día a las 10:00)
const tomorrow = new Date();
tomorrow.setDate(tomorrow.getDate() + 1);
tomorrow.setHours(10, 0, 0, 0);
const dateInput = document.getElementById('cita-fecha');
if (dateInput) {
  dateInput.value = tomorrow.toISOString().slice(0, 16);
}

// Inicialización
checkHealth();
loadAppointmentsData();
setInterval(checkHealth, 20000);
