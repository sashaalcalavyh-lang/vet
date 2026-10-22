// CoVet Frontend — Núcleo Clínico SOAP, Triaje con IA & Fidelización WhatsApp
// Monorepo Hetzner VPS + Vercel Edge

const API_BASE = import.meta.env.VITE_API_URL || 'https://vet-api.142.132.233.38.sslip.io';

// ==========================================
// 1. ESTADO GLOBAL DE LA APLICACIÓN
// ==========================================
let currentTaskId = null;
let activeTriageCitaId = null;
let holdTimerInterval = null;
let holdSecondsLeft = 15 * 60; // 15 minutos
let mediaRecorder = null;
let audioChunks = [];
let recordingTimerInterval = null;
let recordingSeconds = 0;
let pollingInterval = null;
let currentMimeType = '';

// ==========================================
// 2. SWITCH DUAL DE VENTA & TEMA CLÍNICO
// ==========================================
const btnModeTutor = document.getElementById('btn-mode-tutor');
const btnModeVet = document.getElementById('btn-mode-vet');
const viewTutor = document.getElementById('view-tutor');
const viewVet = document.getElementById('view-vet');
const btnThemeToggle = document.getElementById('btn-theme-toggle');
const themeToggleIcon = document.getElementById('theme-toggle-icon');

// Configuración inicial de Tema (Clinical Light por defecto)
const savedTheme = localStorage.getItem('covet-theme') || 'theme-light';
document.body.className = savedTheme;
updateThemeIcon(savedTheme);

btnThemeToggle.addEventListener('click', () => {
  const isLight = document.body.classList.contains('theme-light');
  const newTheme = isLight ? 'theme-dark' : 'theme-light';
  document.body.className = newTheme;
  localStorage.setItem('covet-theme', newTheme);
  updateThemeIcon(newTheme);
});

function updateThemeIcon(theme) {
  themeToggleIcon.textContent = theme === 'theme-light' ? '🌙' : '☀️';
}

// Switch Dual (Cliente vs Veterinario)
btnModeTutor.addEventListener('click', () => switchMainMode('tutor'));
btnModeVet.addEventListener('click', () => switchMainMode('vet'));

function switchMainMode(mode) {
  if (mode === 'tutor') {
    btnModeTutor.classList.add('active');
    btnModeTutor.setAttribute('aria-selected', 'true');
    btnModeVet.classList.remove('active');
    btnModeVet.setAttribute('aria-selected', 'false');
    viewTutor.classList.remove('hidden');
    viewVet.classList.add('hidden');
  } else {
    btnModeVet.classList.add('active');
    btnModeVet.setAttribute('aria-selected', 'true');
    btnModeTutor.classList.remove('active');
    btnModeTutor.setAttribute('aria-selected', 'false');
    viewVet.classList.remove('hidden');
    viewTutor.classList.add('hidden');
    // Al entrar al modo veterinario, refrescar la agenda con los datos más recientes
    loadAppointmentsData();
  }
}

// ==========================================
// 3. VISTA TUTOR / CLIENTE: TRIAJE & PRIMEROS AUXILIOS
// ==========================================
const tutorSymptomsInput = document.getElementById('tutor-symptoms-input');
const btnVoiceIntent = document.getElementById('btn-voice-intent');
const voiceIntentLabel = document.getElementById('voice-intent-label');
const symptomChips = document.querySelectorAll('.symptom-chip');
const tutorTriageForm = document.getElementById('tutor-triage-form');
const tutorPetName = document.getElementById('tutor-pet-name');
const tutorSpecies = document.getElementById('tutor-species');
const tutorPhone = document.getElementById('tutor-phone');
const tutorName = document.getElementById('tutor-name');
const tutorServicePref = document.getElementById('tutor-service-pref');

const tutorIntakeCard = document.getElementById('tutor-intake-card');
const tutorTriageLoading = document.getElementById('tutor-triage-loading');
const tutorTriageResult = document.getElementById('tutor-triage-result');

const triageStatusBanner = document.getElementById('triage-status-banner');
const triageLevelBadge = document.getElementById('triage-level-badge');
const triageDiagTitle = document.getElementById('triage-diag-title');
const triageServiceRecommendation = document.getElementById('triage-service-recommendation');
const triageFirstAidBody = document.getElementById('triage-first-aid-body');

const holdTimerText = document.getElementById('hold-timer-text');
const holdPetName = document.getElementById('hold-pet-name');
const holdModality = document.getElementById('hold-modality');
const holdTimeSlot = document.getElementById('hold-time-slot');
const holdPrice = document.getElementById('hold-price');
const btnConfirmPayment = document.getElementById('btn-confirm-payment');
const btnPayLabel = document.getElementById('btn-pay-label');

const btnSlotAssigned = document.getElementById('btn-slot-assigned');
const btnSlotMorning = document.getElementById('btn-slot-morning');
const btnSlotAfternoon = document.getElementById('btn-slot-afternoon');

const modalPaymentSuccess = document.getElementById('modal-payment-success');
const btnModalSwitchToVet = document.getElementById('btn-modal-switch-to-vet');
const btnModalResetTriage = document.getElementById('btn-modal-reset-triage');

// Chips de Síntomas Frecuentes
symptomChips.forEach(chip => {
  chip.addEventListener('click', () => {
    tutorSymptomsInput.value = chip.getAttribute('data-text');
    tutorSymptomsInput.focus();
  });
});

// Grabación de voz rápida para síntomas del tutor (Web Speech API o MediaRecorder)
let isIntentRecording = false;
let intentSpeechRec = null;

if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  intentSpeechRec = new SpeechRec();
  intentSpeechRec.lang = 'es-CL';
  intentSpeechRec.continuous = false;
  intentSpeechRec.interimResults = false;

  intentSpeechRec.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    tutorSymptomsInput.value = (tutorSymptomsInput.value ? tutorSymptomsInput.value + ' ' : '') + transcript;
  };

  intentSpeechRec.onend = () => {
    isIntentRecording = false;
    btnVoiceIntent.classList.remove('recording');
    voiceIntentLabel.textContent = 'Hablar';
  };

  intentSpeechRec.onerror = () => {
    isIntentRecording = false;
    btnVoiceIntent.classList.remove('recording');
    voiceIntentLabel.textContent = 'Hablar';
  };
}

btnVoiceIntent.addEventListener('click', (e) => {
  e.preventDefault();
  if (!intentSpeechRec) {
    showToast('Dictado por voz directo no disponible en este navegador. Escribe en el cuadro de texto.', 'info');
    return;
  }

  if (!isIntentRecording) {
    try {
      intentSpeechRec.start();
      isIntentRecording = true;
      btnVoiceIntent.classList.add('recording');
      voiceIntentLabel.textContent = 'Escuchando...';
    } catch (err) {
      console.warn('Error iniciando SpeechRecognition:', err);
    }
  } else {
    intentSpeechRec.stop();
  }
});

// Envío del Formulario de Triaje
tutorTriageForm.addEventListener('submit', async (e) => {
  e.preventDefault();

  const sintomas = tutorSymptomsInput.value.trim();
  if (!sintomas) {
    showToast('Por favor describe los síntomas de tu mascota para evaluar el triaje.', 'error');
    tutorSymptomsInput.focus();
    return;
  }

  // Normalización WhatsApp (+569...)
  let rawPhone = tutorPhone.value.trim().replace(/\s+/g, '');
  if (/^9\d{8}$/.test(rawPhone)) rawPhone = `+56${rawPhone}`;
  else if (/^569\d{8}$/.test(rawPhone)) rawPhone = `+${rawPhone}`;

  const triagePayload = {
    tutor_nombre: tutorName.value.trim(),
    tutor_telefono: rawPhone,
    paciente_nombre: tutorPetName.value.trim(),
    especie: tutorSpecies.value,
    sintomas_o_motivo: sintomas,
    preferencia_atencion: tutorServicePref.value,
    preferencia_horario: 'LO_ANTES_POSIBLE'
  };

  // Mostrar Loading
  tutorIntakeCard.classList.add('hidden');
  tutorTriageLoading.classList.remove('hidden');

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/triage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(triagePayload)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Error HTTP ${res.status}`);
    }

    const data = await res.json();
    activeTriageCitaId = data.cita_id;

    // Renderizar Resultados
    renderTriageResult(data);
    tutorTriageLoading.classList.add('hidden');
    tutorTriageResult.classList.remove('hidden');

  } catch (err) {
    console.error('Error al evaluar triaje:', err);
    showToast(`Error al evaluar triaje: ${err.message}`, 'error');
    tutorTriageLoading.classList.add('hidden');
    tutorIntakeCard.classList.remove('hidden');
  }
});

function renderTriageResult(data) {
  // 1. Semáforo
  triageStatusBanner.className = 'triage-status-banner';
  if (data.triaje_nivel_color === 'ROJO') {
    triageStatusBanner.classList.add('red');
    triageLevelBadge.textContent = '🚨 URGENCIA CRÍTICA (RIESGO VITAL)';
  } else if (data.triaje_nivel_color === 'AMARILLO') {
    triageStatusBanner.classList.add('yellow');
    triageLevelBadge.textContent = '⚠️ ALTA PRIORIDAD CLÍNICA';
  } else {
    triageStatusBanner.classList.add('green');
    triageLevelBadge.textContent = '✅ PREVENTIVO / CONTROL DE RUTINA';
  }

  triageDiagTitle.textContent = data.triaje_diagnostico_tentativo;
  triageServiceRecommendation.textContent = `Modalidad recomendada: ${data.tipo_atencion_recomendada} (Atención sugerida en menos de 2 horas)`;

  // 2. Primeros Auxilios
  triageFirstAidBody.textContent = data.protocolo_primeros_auxilios;

  // 3. Reserva y Datos del Bloque
  holdPetName.textContent = tutorPetName.value || 'Tu mascota';
  holdModality.textContent = data.tipo_atencion_recomendada;

  const dateObj = new Date(data.bloque_asignado);
  holdTimeSlot.textContent = dateObj.toLocaleString('es-CL', {
    weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
  });

  const formattedPrice = Number(data.monto_sugerido || 15000).toLocaleString('es-CL');
  holdPrice.textContent = `$${formattedPrice} CLP`;
  btnPayLabel.textContent = `Pagar y Confirmar Cupo ($${formattedPrice} CLP)`;

  // Iniciar temporizador de 15 minutos
  startHoldCountdown();
}

function startHoldCountdown() {
  clearInterval(holdTimerInterval);
  holdSecondsLeft = 15 * 60;
  updateHoldTimerUI();

  holdTimerInterval = setInterval(() => {
    holdSecondsLeft--;
    if (holdSecondsLeft <= 0) {
      clearInterval(holdTimerInterval);
      holdTimerText.textContent = '00:00 (Expirado)';
      btnConfirmPayment.disabled = true;
      btnPayLabel.textContent = 'Cupo Expirado — Solicita Nuevo Triaje';
      return;
    }
    updateHoldTimerUI();
  }, 1000);
}

function updateHoldTimerUI() {
  const m = Math.floor(holdSecondsLeft / 60).toString().padStart(2, '0');
  const s = (holdSecondsLeft % 60).toString().padStart(2, '0');
  holdTimerText.textContent = `${m}:${s}`;
}

// Opciones Híbridas de Bloque
[btnSlotAssigned, btnSlotMorning, btnSlotAfternoon].forEach(btn => {
  btn.addEventListener('click', () => {
    [btnSlotAssigned, btnSlotMorning, btnSlotAfternoon].forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (btn === btnSlotMorning) {
      holdTimeSlot.textContent = 'Mañana (Ventana 10:00 - 13:00 hrs)';
    } else if (btn === btnSlotAfternoon) {
      holdTimeSlot.textContent = 'Tarde (Ventana 15:00 - 19:00 hrs)';
    }
  });
});

// Botón de Pago / Confirmación 1-Clic
btnConfirmPayment.addEventListener('click', async () => {
  if (!activeTriageCitaId) return;

  btnConfirmPayment.disabled = true;
  btnConfirmPayment.innerHTML = `<span class="spinner" style="width:16px;height:16px;margin:0;display:inline-block;vertical-align:middle;"></span> Procesando confirmación...`;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/appointments/${activeTriageCitaId}/confirm-payment`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        metodo_pago: 'DEMO_SIMULATION',
        monto: 15000.0
      })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    clearInterval(holdTimerInterval);
    modalPaymentSuccess.classList.remove('hidden');

  } catch (err) {
    console.error('Error al confirmar pago:', err);
    showToast(`Error al confirmar cita: ${err.message}`, 'error');
  } finally {
    btnConfirmPayment.disabled = false;
    btnConfirmPayment.innerHTML = `<span class="pay-icon">💳</span> <span id="btn-pay-label">Pagar y Confirmar Cupo</span>`;
  }
});

// Acciones del Modal de Pago
btnModalSwitchToVet.addEventListener('click', () => {
  modalPaymentSuccess.classList.add('hidden');
  switchMainMode('vet');
  document.getElementById('tab-btn-agenda').click();
  showToast('¡Observa cómo la cita triada ya está en la agenda en tiempo real!', 'success');
});

btnModalResetTriage.addEventListener('click', () => {
  modalPaymentSuccess.classList.add('hidden');
  tutorTriageResult.classList.add('hidden');
  tutorIntakeCard.classList.remove('hidden');
  tutorSymptomsInput.value = '';
});


// ==========================================
// 4. MODO VETERINARIO (PANEL CLÍNICO COMPLETO)
// ==========================================
const statusDot = document.querySelector('.status-dot');
const statusText = document.getElementById('backend-status-text');
const dbStatusText = document.getElementById('db-status-text');

const tabBtns = document.querySelectorAll('.tab-btn');
const tabPanes = document.querySelectorAll('.tab-pane');
const tabCitasCounter = document.getElementById('tab-citas-counter');

// Estados del Flujo Vibe & Verify
const stateIdle = document.getElementById('state-idle');
const stateRecording = document.getElementById('state-recording');
const stateProcessing = document.getElementById('state-processing');
const stateReview = document.getElementById('state-review');

const selectCitaContexto = document.getElementById('select-cita-contexto');
const btnStartRecord = document.getElementById('btn-start-record');
const recordingTimer = document.getElementById('recording-timer');
const btnStopRecord = document.getElementById('btn-stop-record');
const btnCancelRecord = document.getElementById('btn-cancel-record');

const processingStepTitle = document.getElementById('processing-step-title');
const processingStepDesc = document.getElementById('processing-step-desc');
const processingBarFill = document.getElementById('processing-bar-fill');
const processingErrorBanner = document.getElementById('processing-error-banner');
const processingErrorMessage = document.getElementById('processing-error-message');
const btnRetryRecord = document.getElementById('btn-retry-record');

const stepUpload = document.getElementById('step-upload');
const stepWhisper = document.getElementById('step-whisper');
const stepClaude = document.getElementById('step-claude');
const stepReady = document.getElementById('step-ready');

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

// Tab Agenda
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

const toastContainer = document.getElementById('toast-container');

// Tabs en Vet Mode
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

    if (targetTab === 'tab-agenda') loadAppointmentsData();
    else if (targetTab === 'tab-history') loadFichesHistory();
  });
});

function switchVibeState(stateName) {
  stateIdle.classList.add('hidden');
  stateRecording.classList.add('hidden');
  stateProcessing.classList.add('hidden');
  stateReview.classList.add('hidden');

  if (stateName === 'idle') stateIdle.classList.remove('hidden');
  else if (stateName === 'recording') stateRecording.classList.remove('hidden');
  else if (stateName === 'processing') stateProcessing.classList.remove('hidden');
  else if (stateName === 'review') {
    stateReview.classList.remove('hidden');
    document.querySelectorAll('.glass-textarea.auto-resize').forEach(autoResizeTextarea);
  }
}

// Grabación de Audio con MediaRecorder API
function detectSupportedMimeType() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/aac',
    'audio/ogg;codecs=opus'
  ];
  if (!window.MediaRecorder) return '';
  for (const type of candidates) {
    if (MediaRecorder.isTypeSupported(type)) return type;
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
      audio: { channelCount: 1, sampleRate: 48000, echoCancellation: true, noiseSuppression: true }
    });

    currentMimeType = detectSupportedMimeType();
    const options = currentMimeType ? { mimeType: currentMimeType } : {};

    try {
      mediaRecorder = new MediaRecorder(stream, options);
    } catch (e) {
      mediaRecorder = new MediaRecorder(stream);
      currentMimeType = mediaRecorder.mimeType || '';
    }

    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) audioChunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
    };

    mediaRecorder.start(250);
    startTimer();
    switchVibeState('recording');
    showToast('Grabando dictado clínico...', 'info');

  } catch (err) {
    showToast(`Error al activar micrófono: ${err.message}`, 'error');
  }
}

function startTimer() {
  clearInterval(recordingTimerInterval);
  recordingSeconds = 0;
  updateTimerDisplay();
  recordingTimerInterval = setInterval(() => {
    recordingSeconds++;
    updateTimerDisplay();
  }, 1000);
}

function stopTimer() {
  clearInterval(recordingTimerInterval);
}

function updateTimerDisplay() {
  const m = Math.floor(recordingSeconds / 60).toString().padStart(2, '0');
  const s = (recordingSeconds % 60).toString().padStart(2, '0');
  recordingTimer.textContent = `${m}:${s}`;
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
    if (mediaRecorder.stream) mediaRecorder.stream.getTracks().forEach(t => t.stop());
  }
  stopTimer();
  audioChunks = [];
  switchVibeState('idle');
  showToast('Grabación cancelada.', 'info');
}

// Subida y Polling de Ficha SOAP
async function uploadAudioAndPoll(audioFile) {
  switchVibeState('processing');
  resetProcessingUI();

  try {
    const formData = new FormData();
    formData.append('file', audioFile);
    if (selectCitaContexto.value) {
      formData.append('cita_id', selectCitaContexto.value);
    }

    setStepActive(stepUpload, 'Subiendo audio al servidor Hetzner...', 25);

    const response = await fetch(`${API_BASE}/api/v1/covet/transcribe`, {
      method: 'POST',
      body: formData
    });

    if (!response.ok) throw new Error(`HTTP ${response.status} en transcripción`);
    const data = await response.json();
    currentTaskId = data.task_id;

    setStepDone(stepUpload);
    setStepActive(stepWhisper, 'Transcribiendo audio con Groq Whisper Large Turbo...', 50);

    startPolling(currentTaskId);
  } catch (err) {
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
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

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

        setTimeout(() => {
          populateReviewForm(fiche);
          switchVibeState('review');
        }, 700);
      } else if (status === 'FAILED') {
        clearInterval(pollingInterval);
        showProcessingError(fiche.error_detalle || 'Error durante el procesamiento.');
      }

      if (pollAttempts > 60) {
        clearInterval(pollingInterval);
        showProcessingError('Tiempo de espera agotado. Reintenta.');
      }
    } catch (err) {
      console.warn('Error en polling:', err);
    }
  }, 2000);
}

function resetProcessingUI() {
  processingErrorBanner.classList.add('hidden');
  processingBarFill.style.width = '15%';
  [stepUpload, stepWhisper, stepClaude, stepReady].forEach(s => s.className = 'step-item');
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
}

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

// Confirmación de Ficha SOAP (PUT)
formSoapReview.addEventListener('submit', async (e) => {
  e.preventDefault();
  const ficheId = soapFicheIdInput.value;
  if (!ficheId) return;

  let rawTel = reviewTutorTelefono.value.trim().replace(/\s+/g, '');
  if (/^9\d{8}$/.test(rawTel)) rawTel = `+56${rawTel}`;
  else if (/^569\d{8}$/.test(rawTel)) rawTel = `+${rawTel}`;

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
  btnConfirmSoap.innerHTML = `<span class="spinner" style="width:16px;height:16px;margin:0;display:inline-block;vertical-align:middle;"></span> Guardando...`;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/fiches/${ficheId}/confirm`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const resData = await res.json();
    showToast('Ficha clínica confirmada y sincronizada en PostgreSQL.', 'success');
    switchVibeState('idle');
    loadAppointmentsData();
  } catch (err) {
    showToast(`Error al confirmar: ${err.message}`, 'error');
  } finally {
    btnConfirmSoap.disabled = false;
    btnConfirmSoap.innerHTML = `<span class="btn-icon">✓</span> <span class="btn-text">[✓] Confirmar Ficha y Enviar por WhatsApp</span>`;
  }
});

btnDiscardReview.addEventListener('click', () => {
  if (confirm('¿Descartar esta ficha y volver al inicio?')) switchVibeState('idle');
});

btnRetryRecord.addEventListener('click', () => switchVibeState('idle'));

btnStartRecord.addEventListener('click', startRecording);
btnStopRecord.addEventListener('click', stopRecordingAndProcess);
btnCancelRecord.addEventListener('click', cancelRecording);

// ==========================================
// 5. AGENDA & CITAS (VISTA VETERINARIO)
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

    kpiCitas.textContent = citas.length;
    kpiPacientes.textContent = pacientes.length;
    kpiTutores.textContent = tutores.length;
    tabCitasCounter.textContent = citas.length;

    renderAppointmentsList(citas, pacientes);
    populateAppointmentContextSelector(citas, pacientes);
  } catch (err) {
    console.error('Error cargando citas:', err);
  }
}

function renderAppointmentsList(citas, pacientes) {
  if (!citas || citas.length === 0) {
    appointmentsContainer.innerHTML = `
      <div class="empty-state">
        <p>No hay citas programadas aún.</p>
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

    const isHold = cita.estado_cita === 'HOLD_15_MIN';
    const statusClass = isHold ? 'hold' : 'confirmed';
    const statusText = isHold ? '⏳ HOLD 15 MIN' : (cita.pago_estado === 'SIMULATED_PAID' ? '✅ PAGADO & CONFIRMADO' : '✓ CONFIRMADA');
    const modality = cita.tipo_atencion || 'CLINICA';

    return `
      <div class="appointment-card ${statusClass}">
        <div class="cita-pet">
          <span>${emoji}</span>
          <span>${escapeHtml(paciente.nombre)}</span>
          <small style="color:var(--text-dim);font-weight:600;">[${modality}]</small>
          <span class="cita-status-tag ${statusClass}">${statusText}</span>
        </div>
        <div class="cita-motivo">${escapeHtml(cita.motivo)}</div>
        <div class="cita-time">📅 ${dateFormatted} • Valor: $${Number(cita.monto_pago || 0).toLocaleString('es-CL')} CLP</div>
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

// Agendamiento manual rápido en agenda
if (appointmentForm) {
  appointmentForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    btnSubmitAppointment.disabled = true;

    try {
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

      const pacienteRes = await fetch(`${API_BASE}/api/pacientes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tutor_id: tutor.id,
          nombre: document.getElementById('paciente-nombre').value.trim(),
          especie: document.getElementById('paciente-especie').value,
          raza: document.getElementById('paciente-raza').value.trim() || 'Mestizo',
          peso_kg: 10.0
        })
      });
      if (!pacienteRes.ok) throw new Error('Error al registrar paciente');
      const paciente = await pacienteRes.json();

      const citaRes = await fetch(`${API_BASE}/api/citas`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          paciente_id: paciente.id,
          fecha_hora: new Date(document.getElementById('cita-fecha').value).toISOString(),
          motivo: document.getElementById('cita-motivo').value.trim(),
          tipo_atencion: document.getElementById('cita-tipo-atencion').value,
          ventana_horaria: 'HORA_EXACTA'
        })
      });
      if (!citaRes.ok) throw new Error('Error al agendar cita');

      showToast('Cita guardada en PostgreSQL', 'success');
      appointmentForm.reset();
      loadAppointmentsData();
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      btnSubmitAppointment.disabled = false;
    }
  });
}

btnRefresh.addEventListener('click', () => {
  loadAppointmentsData();
  checkHealth();
  showToast('Agenda sincronizada con PostgreSQL', 'info');
});

// ==========================================
// 6. HISTORIAL DE FICHAS (TAB 3)
// ==========================================
async function loadFichesHistory() {
  fichesHistoryList.innerHTML = `
    <div class="loading-state">
      <span class="spinner"></span>
      <p>Consultando fichas en PostgreSQL...</p>
    </div>
  `;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/fiches?limit=25`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fiches = await res.json();

    if (!fiches || fiches.length === 0) {
      fichesHistoryList.innerHTML = `<div class="empty-state"><p>No hay fichas clínicas registradas aún.</p></div>`;
      return;
    }

    fichesHistoryList.innerHTML = fiches.map(fiche => {
      const json = fiche.json_estructurado || {};
      const petName = json.paciente_nombre || 'Paciente';
      const tutorName = json.tutor_nombre || 'Tutor';
      const diag = json.diagnostico_presuntivo || 'Sin diagnóstico registrado';
      const createdDate = fiche.fecha_creacion ? new Date(fiche.fecha_creacion).toLocaleString('es-CL') : 'Reciente';

      let statusBadge = '<span class="cita-status-tag confirmed">✓ CONFIRMADA</span>';
      if (fiche.estado_procesamiento === 'CONFIRMED_AND_SENT') {
        statusBadge = '<span class="cita-status-tag confirmed">✅ WhatsApp Despachado</span>';
      } else if (fiche.estado_procesamiento === 'CONFIRMED_N8N_PENDING') {
        statusBadge = '<span class="cita-status-tag hold">⚠️ n8n Pendiente</span>';
      }

      return `
        <div class="fiche-history-card">
          <div class="cita-pet">
            <span>🐾</span>
            <span>${escapeHtml(petName)}</span>
            <small style="color:var(--text-dim);">(${escapeHtml(tutorName)})</small>
            ${statusBadge}
          </div>
          <div class="cita-motivo"><strong>Dx:</strong> ${escapeHtml(diag)}</div>
          <div class="cita-time">📅 ${createdDate} • ID: ${fiche.id.slice(0, 8)}...</div>
        </div>
      `;
    }).join('');
  } catch (err) {
    fichesHistoryList.innerHTML = `<div class="empty-state"><p>Error cargando fichas: ${err.message}</p></div>`;
  }
}

if (btnRefreshFiches) btnRefreshFiches.addEventListener('click', loadFichesHistory);

// ==========================================
// 7. HEALTH CHECK & MONITOREO
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

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(text) {
  if (!text) return '';
  return text.replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[m]);
}

// Inicialización
checkHealth();
loadAppointmentsData();
setInterval(checkHealth, 20000);
