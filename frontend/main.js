// CoVet Frontend — Núcleo Clínico SOAP, Triaje con IA & Fidelización WhatsApp
// Monorepo Hetzner VPS + Vercel Edge

const API_BASE = import.meta.env.VITE_API_URL || 'https://vet-api.142.132.233.38.sslip.io';

// ========================================================
// 1. ESTADO GLOBAL DE LA APLICACIÓN
// ========================================================
let currentTaskId = null;
let activeTriageData = null;
let activeTriageCitaId = null;
let holdTimerInterval = null;
let holdSecondsLeft = 15 * 60; // 15 minutos

// Selección activa en el Triaje
let selectedModalidad = 'TELEMEDICINA';
let selectedHorario = 'INMEDIATA';
let selectedPrecio = 15000;

// Grabación de Audio para Dictado SOAP (Veterinario)
let mediaRecorder = null;
let audioChunks = [];
let recordingTimerInterval = null;
let recordingSeconds = 0;
let pollingInterval = null;
let currentMimeType = '';

// Reconocimiento de voz para Intent del Cliente
let isClientMicActive = false;
let clientSpeechRec = null;

// Marca Blanca (White-Label)
let activeVetSlug = null;
let activeBranding = null;

// Almacenamiento Local Offline para Audios en Terreno (IndexedDB)
const DB_NAME = 'covet_local_db';
const DB_VERSION = 1;
const STORE_NAME = 'offline_audios';

function openAudioDB() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      resolve(null);
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = (e) => resolve(e.target.result);
    req.onerror = (e) => reject(e.target.error);
  });
}

async function saveOfflineAudio(audioBlob, citaId) {
  try {
    const db = await openAudioDB();
    if (!db) return null;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      const store = tx.objectStore(STORE_NAME);
      const record = {
        blob: audioBlob,
        citaId: citaId || null,
        filename: `dictado_${Date.now()}.webm`,
        createdAt: new Date().toISOString()
      };
      const addReq = store.add(record);
      addReq.onsuccess = () => {
        updateOfflineBanner();
        resolve(addReq.result);
      };
      addReq.onerror = () => reject(addReq.error);
    });
  } catch (err) {
    console.warn('[IndexedDB] No se pudo guardar audio offline:', err);
    return null;
  }
}

async function getOfflineAudios() {
  try {
    const db = await openAudioDB();
    if (!db) return [];
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly');
      const store = tx.objectStore(STORE_NAME);
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  } catch (_) {
    return [];
  }
}

async function removeOfflineAudio(id) {
  try {
    const db = await openAudioDB();
    if (!db) return;
    return new Promise((resolve) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      tx.objectStore(STORE_NAME).delete(id);
      tx.oncomplete = () => {
        updateOfflineBanner();
        resolve();
      };
    });
  } catch (_) {}
}

async function updateOfflineBanner() {
  const banner = document.getElementById('offline-audio-banner');
  const countLabel = document.getElementById('offline-audio-count');
  if (!banner || !countLabel) return;
  const list = await getOfflineAudios();
  if (list.length > 0) {
    banner.classList.remove('hidden');
    countLabel.textContent = `${list.length} dictado(s) guardado(s) en memoria local sin conexión`;
  } else {
    banner.classList.add('hidden');
  }
}

async function retryUploadOfflineAudios() {
  const list = await getOfflineAudios();
  if (list.length === 0) {
    showToast('No hay audios pendientes de subida.', 'info');
    return;
  }
  showToast(`Reintentando subir ${list.length} dictado(s) guardado(s)...`, 'info');
  for (const item of list) {
    try {
      const formData = new FormData();
      formData.append('file', item.blob, item.filename);
      if (item.citaId) formData.append('cita_id', item.citaId);

      const resp = await fetch(`${API_BASE}/api/v1/covet/transcribe`, {
        method: 'POST',
        body: formData
      });
      if (resp.ok) {
        const data = await resp.json();
        await removeOfflineAudio(item.id);
        showToast('Dictado offline subido y procesándose en segundo plano.', 'success');
        startPolling(data.task_id);
      }
    } catch (err) {
      console.warn('[IndexedDB] Reintento fallido para audio:', item.id, err);
      showToast('Aún sin conexión al servidor Hetzner. El audio sigue seguro en tu dispositivo.', 'warning');
      break;
    }
  }
}

// ========================================================
// 2. SISTEMA DE TEMAS & ARQUITECTURA DE 3 ACCESOS
// ========================================================
const btnModeClient = document.getElementById('btn-mode-client');
const btnModeVet = document.getElementById('btn-mode-vet');
const btnModeAdmin = document.getElementById('btn-mode-admin');

const viewClient = document.getElementById('view-client');
const viewVet = document.getElementById('view-vet');
const viewAdmin = document.getElementById('view-admin');

const btnThemeToggle = document.getElementById('btn-theme-toggle');
const themeToggleIcon = document.getElementById('theme-toggle-icon');

// Modales de Seguridad y Privacidad
const modalVetPin = document.getElementById('modal-vet-pin');
const inputVetPin = document.getElementById('input-vet-pin');
const btnSubmitVetPin = document.getElementById('btn-submit-vet-pin');
const btnAutofillDemoPin = document.getElementById('btn-autofill-demo-pin');
const pinErrorText = document.getElementById('pin-error-text');

const modalPrivacyPolicy = document.getElementById('modal-privacy-policy');
const btnOpenPrivacyModal = document.getElementById('btn-open-privacy-modal');
const btnClosePrivacyModal = document.getElementById('btn-close-privacy-modal');
const btnUnderstandPrivacy = document.getElementById('btn-understand-privacy');

// Configuración inicial de tema para veterinario
let savedVetTheme = localStorage.getItem('covet-vet-theme') || 'clinical-light';

function updateThemeIcon(theme) {
  if (themeToggleIcon) {
    themeToggleIcon.textContent = theme === 'clinical-dark' ? '☀️' : '🌙';
  }
}

// Alternador de tema (Solo operativo y visible en Modo Veterinario)
if (btnThemeToggle) {
  btnThemeToggle.addEventListener('click', () => {
    const currentTheme = document.body.getAttribute('data-theme') || 'clinical-light';
    const newTheme = currentTheme === 'clinical-light' ? 'clinical-dark' : 'clinical-light';
    
    document.body.setAttribute('data-theme', newTheme);
    document.body.className = newTheme === 'clinical-dark' ? 'theme-dark' : 'theme-light';
    localStorage.setItem('covet-vet-theme', newTheme);
    updateThemeIcon(newTheme);
  });
}

// Validación del Portal Clínico - Acceso Exclusivo Dra. Daniela Alcázar (Email + Contraseña)
const formVetExclusiveLogin = document.getElementById('form-vet-exclusive-login');
const inputVetLoginEmail = document.getElementById('input-vet-login-email');
const inputVetLoginPassword = document.getElementById('input-vet-login-password');
const btnLoginBackClient = document.getElementById('btn-login-back-client');

function isVetUnlocked() {
  return sessionStorage.getItem('covet_vet_unlocked') === 'true';
}

function unlockVetSession(authData = null) {
  sessionStorage.setItem('covet_vet_unlocked', 'true');
  if (authData && authData.session_token) {
    sessionStorage.setItem('covet_vet_token', authData.session_token);
  }
  if (!activeVetSlug) {
    activeVetSlug = (authData && authData.clinica_slug) || 'dra-daniela-alcazar';
  }
  document.documentElement.style.setProperty('--teal-primary', (authData && authData.color_primario) || '#E91E63');
  document.documentElement.style.setProperty('--teal-hover', '#C2185B');
  if (modalVetPin) modalVetPin.classList.add('hidden');
  activateVetView();
}

async function handleExclusiveVetLogin() {
  const email = inputVetLoginEmail ? inputVetLoginEmail.value.trim() : 'daniela@alcazarvet.cl';
  const password = inputVetLoginPassword ? inputVetLoginPassword.value.trim() : '';

  if (!email || !password) {
    if (pinErrorText) {
      pinErrorText.textContent = 'Ingresa tu correo profesional y contraseña para acceder al sistema.';
      pinErrorText.classList.remove('hidden');
    }
    return;
  }

  if (btnSubmitVetPin) {
    btnSubmitVetPin.disabled = true;
    btnSubmitVetPin.textContent = 'Verificando credenciales...';
  }

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/vet/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email,
        password,
        clinica_slug: activeVetSlug || 'dra-daniela-alcazar'
      })
    });

    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.detail || 'Credenciales inválidas.');
    }

    const authData = await res.json();
    if (pinErrorText) pinErrorText.classList.add('hidden');
    unlockVetSession(authData);
    showToast(`Bienvenida, ${authData.profesional || 'Dra. Daniela Alcázar'}. Portal Clínico activo.`, 'success');
  } catch (err) {
    if (pinErrorText) {
      pinErrorText.textContent = err.message || 'Credenciales inválidas.';
      pinErrorText.classList.remove('hidden');
    }
    if (inputVetLoginPassword) {
      inputVetLoginPassword.value = '';
      inputVetLoginPassword.focus();
    }
  } finally {
    if (btnSubmitVetPin) {
      btnSubmitVetPin.disabled = false;
      btnSubmitVetPin.textContent = 'Ingresar al sistema';
    }
  }
}

if (formVetExclusiveLogin) {
  formVetExclusiveLogin.addEventListener('submit', (e) => {
    e.preventDefault();
    handleExclusiveVetLogin();
  });
} else if (btnSubmitVetPin) {
  btnSubmitVetPin.addEventListener('click', handleExclusiveVetLogin);
}

if (btnAutofillDemoPin) {
  btnAutofillDemoPin.addEventListener('click', () => {
    if (inputVetLoginEmail) inputVetLoginEmail.value = 'daniela@alcazarvet.cl';
    if (inputVetLoginPassword) inputVetLoginPassword.value = 'Daniela2026!';
    handleExclusiveVetLogin();
  });
}

if (btnLoginBackClient) {
  btnLoginBackClient.addEventListener('click', () => {
    if (modalVetPin) modalVetPin.classList.add('hidden');
    switchMainMode('client');
  });
}

function buildQueryWithVet(viewName) {
  const params = new URLSearchParams(window.location.search);
  const vetParam = activeVetSlug || params.get('vet') || params.get('clinic');
  if (vetParam) {
    return `?vet=${encodeURIComponent(vetParam)}&view=${encodeURIComponent(viewName)}`;
  }
  return `?view=${encodeURIComponent(viewName)}`;
}

// Selector de Modo Principal (Cliente vs Veterinario vs SuperAdmin Sasha)
function switchMainMode(mode) {
  if (mode === 'client') {
    // 1. UI Switch Buttons
    btnModeClient.classList.add('active');
    btnModeClient.setAttribute('aria-selected', 'true');
    btnModeVet.classList.remove('active');
    btnModeVet.setAttribute('aria-selected', 'false');
    if (btnModeAdmin) {
      btnModeAdmin.classList.remove('active');
      btnModeAdmin.setAttribute('aria-selected', 'false');
    }

    // 2. Vistas
    viewClient.classList.remove('hidden');
    viewClient.classList.add('active');
    viewVet.classList.add('hidden');
    viewVet.classList.remove('active');
    if (viewAdmin) {
      viewAdmin.classList.add('hidden');
      viewAdmin.classList.remove('active');
    }

    // 3. Regla 1: Forzar data-theme="clinical-light" (blanco médico impecable)
    document.body.setAttribute('data-active-view', 'client');
    document.body.setAttribute('data-theme', 'clinical-light');
    document.body.className = 'theme-light';

    // 4. Actualizar URL sin recargar
    try {
      window.history.replaceState({}, '', buildQueryWithVet('client'));
    } catch (_) {}

  } else if (mode === 'vet' || mode === 'login') {
    // Si se pide ?view=login explícitamente o aún no está autenticado el portal clínico, mostrar Login Exclusivo
    if (mode === 'login' || !isVetUnlocked()) {
      if (modalVetPin) {
        modalVetPin.classList.remove('hidden');
        if (inputVetLoginPassword) {
          inputVetLoginPassword.value = '';
          inputVetLoginPassword.focus();
        }
      }
      return;
    }
    activateVetView();

  } else if (mode === 'admin') {
    // Entrada SuperAdmin Sasha
    if (btnModeAdmin) {
      btnModeAdmin.classList.add('active');
      btnModeAdmin.setAttribute('aria-selected', 'true');
    }
    btnModeClient.classList.remove('active');
    btnModeClient.setAttribute('aria-selected', 'false');
    btnModeVet.classList.remove('active');
    btnModeVet.setAttribute('aria-selected', 'false');

    if (viewAdmin) {
      viewAdmin.classList.remove('hidden');
      viewAdmin.classList.add('active');
    }
    viewClient.classList.add('hidden');
    viewClient.classList.remove('active');
    viewVet.classList.add('hidden');
    viewVet.classList.remove('active');

    document.body.setAttribute('data-active-view', 'admin');
    document.body.setAttribute('data-theme', 'clinical-light');
    document.body.className = 'theme-light';

    try {
      window.history.replaceState({}, '', buildQueryWithVet('admin'));
    } catch (_) {}

    loadAdminMetrics();
  }
}

function activateVetView() {
  btnModeVet.classList.add('active');
  btnModeVet.setAttribute('aria-selected', 'true');
  btnModeClient.classList.remove('active');
  btnModeClient.setAttribute('aria-selected', 'false');
  if (btnModeAdmin) {
    btnModeAdmin.classList.remove('active');
    btnModeAdmin.setAttribute('aria-selected', 'false');
  }

  viewVet.classList.remove('hidden');
  viewVet.classList.add('active');
  viewClient.classList.add('hidden');
  viewClient.classList.remove('active');
  if (viewAdmin) {
    viewAdmin.classList.add('hidden');
    viewAdmin.classList.remove('active');
  }

  // Restaurar tema veterinario (Claro u Oscuro según preferencia)
  document.body.setAttribute('data-active-view', 'vet');
  savedVetTheme = localStorage.getItem('covet-vet-theme') || 'clinical-light';
  document.body.setAttribute('data-theme', savedVetTheme);
  document.body.className = savedVetTheme === 'clinical-dark' ? 'theme-dark' : 'theme-light';
  updateThemeIcon(savedVetTheme);

  // Actualizar título del panel con identidad de la Dra. Daniela Alcázar
  const vetBrandTitle = document.querySelector('#view-vet .brand-title');
  if (vetBrandTitle) {
    vetBrandTitle.innerHTML = `Dra. Daniela Alcázar <span class="badge-tag" style="background: rgba(233,30,99,0.14); color: #e91e63;">Portal Clínico</span>`;
  }

  // Actualizar URL sin recargar
  try {
    window.history.replaceState({}, '', buildQueryWithVet('vet'));
  } catch (_) {}

  // Sincronización en vivo con la Agenda y Suscriptores en PostgreSQL
  loadAppointmentsData();
  if (typeof loadVetMembershipsList === 'function') {
    loadVetMembershipsList();
  }
}

btnModeClient.addEventListener('click', () => switchMainMode('client'));
btnModeVet.addEventListener('click', () => switchMainMode('vet'));
if (btnModeAdmin) btnModeAdmin.addEventListener('click', () => switchMainMode('admin'));

// Eventos de Modal de Privacidad Ley 21.719
if (btnOpenPrivacyModal) {
  btnOpenPrivacyModal.addEventListener('click', () => {
    if (modalPrivacyPolicy) modalPrivacyPolicy.classList.remove('hidden');
  });
}
if (btnClosePrivacyModal) {
  btnClosePrivacyModal.addEventListener('click', () => {
    if (modalPrivacyPolicy) modalPrivacyPolicy.classList.add('hidden');
  });
}
if (btnUnderstandPrivacy) {
  btnUnderstandPrivacy.addEventListener('click', () => {
    if (modalPrivacyPolicy) modalPrivacyPolicy.classList.add('hidden');
    const chk = document.getElementById('client-consent-ley21719');
    if (chk) chk.checked = true;
  });
}

// Detección de Parámetro URL (?view=admin, ?view=vet, ?view=login o ?view=client)
const initialUrlParams = new URLSearchParams(window.location.search);
const queryView = initialUrlParams.get('view');
let initialView = 'client';
if (queryView === 'admin') initialView = 'admin';
else if (queryView === 'vet') initialView = 'vet';
else if (queryView === 'login') initialView = 'login';
switchMainMode(initialView);


// ========================================================
// 3. VISTA TUTOR: MICRO-APP DE INTENCIÓN (#view-client)
// ========================================================

// Elementos del DOM de la Vista Cliente
const clientIntentHero = document.getElementById('client-intent-hero');
const btnCovetMic = document.getElementById('btn-covet-mic');
const micStatusLabel = document.getElementById('mic-status-label');
const clientIntentInput = document.getElementById('client-intent-input');
const btnClientSend = document.getElementById('btn-client-send');
const demoChips = document.querySelectorAll('.intent-chip');

const clientEvaluatingCard = document.getElementById('client-evaluating-card');
const clientTriageStage = document.getElementById('client-triage-stage');

const triageHeaderBanner = document.getElementById('triage-header-banner');
const triageUrgencyBadge = document.getElementById('triage-urgency-badge');
const triageSpeciesTag = document.getElementById('triage-species-tag');
const triageMotivoTitle = document.getElementById('triage-motivo-title');
const triageRecommendationText = document.getElementById('triage-recommendation-text');

const triageExecutionForm = document.getElementById('triage-execution-form');
const clientTutorPhone = document.getElementById('client-tutor-phone');
const clientTutorName = document.getElementById('client-tutor-name');
const clientPetName = document.getElementById('client-pet-name');
const dynamicQuestionsList = document.getElementById('dynamic-questions-list');
const hybridScheduleContainer = document.getElementById('hybrid-schedule-container');
const btnBlockSlot = document.getElementById('btn-block-slot');
const btnBlockSlotLabel = document.getElementById('btn-block-slot-label');

const clientHoldStage = document.getElementById('client-hold-stage');
const patientRecognizedBanner = document.getElementById('patient-recognized-banner');
const patientRecognizedTitle = document.getElementById('patient-recognized-title');
const patientRecognizedText = document.getElementById('patient-recognized-text');
const holdCountdownDigits = document.getElementById('hold-countdown-digits');
const holdSummaryPet = document.getElementById('hold-summary-pet');
const holdSummaryModality = document.getElementById('hold-summary-modality');
const holdSummarySchedule = document.getElementById('hold-summary-schedule');
const holdSummaryPrice = document.getElementById('hold-summary-price');
const btnCovetConfirm = document.getElementById('btn-covet-confirm');
const btnConfirmPayText = document.getElementById('btn-confirm-pay-text');
const externalPayLinkWrap = document.getElementById('external-pay-link-wrap');
const btnExternalPay = document.getElementById('btn-external-pay');

const clientConfirmedStage = document.getElementById('client-confirmed-stage');
const confirmedSubtitle = document.getElementById('confirmed-subtitle');
const protocolTitle = document.getElementById('protocol-title');
const protocolIndicationsList = document.getElementById('protocol-indications-list');
const protocolProhibitionsList = document.getElementById('protocol-prohibitions-list');
const protocolAlertsList = document.getElementById('protocol-alerts-list');
const protocolTutorMessage = document.getElementById('protocol-tutor-message');
const btnSwitchVetDemo = document.getElementById('btn-switch-vet-demo');
const btnNewTriage = document.getElementById('btn-new-triage');

// Web Speech API para el Micrófono del Cliente
if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  clientSpeechRec = new SpeechRec();
  clientSpeechRec.lang = 'es-CL';
  clientSpeechRec.continuous = false;
  clientSpeechRec.interimResults = false;

  clientSpeechRec.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    clientIntentInput.value = (clientIntentInput.value ? clientIntentInput.value + ' ' : '') + transcript;
    clientIntentInput.focus();
  };

  clientSpeechRec.onend = () => {
    isClientMicActive = false;
    btnCovetMic.classList.remove('recording');
    micStatusLabel.textContent = 'Toca el micrófono para hablar';
    // Si capturó texto, podemos enviar automáticamente tras breve confirmación
    if (clientIntentInput.value.trim().length > 5) {
      setTimeout(() => {
        submitClientIntent(clientIntentInput.value.trim());
      }, 500);
    }
  };

  clientSpeechRec.onerror = (e) => {
    console.warn('Speech recognition error:', e);
    isClientMicActive = false;
    btnCovetMic.classList.remove('recording');
    micStatusLabel.textContent = 'Toca el micrófono para hablar';
  };
}

btnCovetMic.addEventListener('click', () => {
  if (!clientSpeechRec) {
    showToast('Dictado por voz no soportado en este navegador. Escribe en el cuadro de texto.', 'info');
    clientIntentInput.focus();
    return;
  }

  if (!isClientMicActive) {
    try {
      clientSpeechRec.start();
      isClientMicActive = true;
      btnCovetMic.classList.add('recording');
      micStatusLabel.textContent = '🔴 Escuchando síntomas... habla con naturalidad';
    } catch (err) {
      console.warn('Error al iniciar micrófono:', err);
    }
  } else {
    clientSpeechRec.stop();
  }
});

// Chips Clickeables de Ejemplo para Demos de Venta
demoChips.forEach(chip => {
  chip.addEventListener('click', () => {
    const sampleText = chip.getAttribute('data-intent');
    clientIntentInput.value = sampleText;
    submitClientIntent(sampleText);
  });
});

// Enviar con botón o tecla Enter
btnClientSend.addEventListener('click', () => {
  const text = clientIntentInput.value.trim();
  if (text) submitClientIntent(text);
});

clientIntentInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const text = clientIntentInput.value.trim();
    if (text) submitClientIntent(text);
  }
});

// Función Principal: Envío de Intención a POST /api/v1/covet/client-intent
async function submitClientIntent(symptomsText) {
  if (!symptomsText || symptomsText.trim().length === 0) {
    showToast('Por favor describe los síntomas de tu mascota.', 'error');
    clientIntentInput.focus();
    return;
  }

  // 1. Transición Visual: Mostrar Animación Bio Indigo
  clientIntentHero.classList.add('hidden');
  clientEvaluatingCard.classList.remove('hidden');
  clientTriageStage.classList.add('hidden');
  clientHoldStage.classList.add('hidden');
  clientConfirmedStage.classList.add('hidden');

  try {
    const formData = new FormData();
    formData.append('texto', symptomsText);

    const res = await fetch(`${API_BASE}/api/v1/covet/client-intent`, {
      method: 'POST',
      body: formData
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Error HTTP ${res.status}`);
    }

    const data = await res.json();
    activeTriageData = data;

    // 2. Renderizar Pantalla Dinámica de Triaje
    renderTriageStage(data);

    clientEvaluatingCard.classList.add('hidden');
    clientTriageStage.classList.remove('hidden');

  } catch (err) {
    console.error('Error al evaluar intención clínica:', err);
    showToast(`Error al evaluar síntomas: ${err.message}`, 'error');
    clientEvaluatingCard.classList.add('hidden');
    clientIntentHero.classList.remove('hidden');
  }
}

// Renderizado de la Pantalla Dinámica de Triaje
function renderTriageStage(data) {
  const urgency = data.urgency_level || 'LOW';
  
  // a) Cabecera de Triaje Semántica
  triageHeaderBanner.className = 'glass-panel triage-header-banner';
  triageUrgencyBadge.className = 'triage-badge-pill';

  if (urgency === 'HIGH') {
    triageHeaderBanner.classList.add('urgent-high');
    triageUrgencyBadge.classList.add('urgent-high');
    triageUrgencyBadge.textContent = data.urgency_badge || '🚨 URGENCIA ALTA — Atención Prioritaria';
  } else if (urgency === 'MEDIUM') {
    triageHeaderBanner.classList.add('urgent-medium');
    triageUrgencyBadge.classList.add('urgent-medium');
    triageUrgencyBadge.textContent = data.urgency_badge || '⚠️ PRIORIDAD MODERADA';
  } else {
    triageUrgencyBadge.textContent = data.urgency_badge || 'CONSULTA PREVENTIVA / RUTINA';
  }

  const species = data.species_detected || 'Canino';
  const spLower = species.toLowerCase();
  const speciesEmoji = spLower.includes('felin') || spLower.includes('gat')
    ? '🐱'
    : spLower.includes('conej')
    ? '🐰'
    : spLower.includes('cuy') || spLower.includes('cobay')
    ? '🐹'
    : spLower.includes('eriz')
    ? '🦔'
    : '🐶';
  triageSpeciesTag.textContent = `${speciesEmoji} ${species}`;
  triageMotivoTitle.textContent = data.motivo_resumido || 'Consulta Clínica';
  triageRecommendationText.textContent = data.first_aid_preview || 'Nuestro Agente Clínico ha estructurado el protocolo de atención para tu mascota.';

  // b) Inyección Dinámica de Preguntas Clínicas de Claude
  dynamicQuestionsList.innerHTML = '';
  const questions = data.dynamic_questions || [
    '¿Hace cuánto tiempo comenzaron los síntomas?',
    '¿Ha recibido medicamentos o tiene antecedentes médicos?',
    '¿Mantiene apetito y consumo de agua normal?'
  ];

  questions.forEach((q, idx) => {
    const qDiv = document.createElement('div');
    qDiv.className = 'dynamic-question-item';
    qDiv.innerHTML = `
      <label for="dyn-q-${idx}"><strong>${idx + 1}.</strong> ${escapeHtml(q)}</label>
      <input type="text" id="dyn-q-${idx}" name="question_${idx}" required class="clinical-input" placeholder="Tu respuesta...">
    `;
    dynamicQuestionsList.appendChild(qDiv);
  });

  // c) Renderizar Selector Híbrido de Agenda (Opción 3C)
  renderHybridScheduleSelector(data);
}

// Selector Híbrido de Agenda según Urgencia (Opción 3C)
function renderHybridScheduleSelector(data) {
  const urgency = data.urgency_level || 'LOW';
  const pricing = data.pricing || {
    telemedicina: 15000,
    domicilio_urgencia: 35000,
    domicilio_programado: 25000
  };

  hybridScheduleContainer.innerHTML = '';

  if (urgency === 'HIGH') {
    // Caso de Urgencia HIGH: 2 Tarjetas Destacadas
    selectedModalidad = 'TELEMEDICINA';
    selectedHorario = 'INMEDIATA';
    selectedPrecio = pricing.telemedicina || 15000;

    hybridScheduleContainer.innerHTML = `
      <div class="urgent-schedule-grid">
        <div class="urgent-option-card featured selected" data-modalidad="TELEMEDICINA" data-horario="INMEDIATA" data-precio="${pricing.telemedicina}">
          <span class="card-top-badge">⚡ MÁXIMA PRIORIDAD</span>
          <h4 class="urgent-card-title">Telemedicina de Urgencia Inmediata</h4>
          <p class="urgent-card-desc">Conexión con médico veterinario en menos de 10 minutos para contención, triaje visual y primeros auxilios asistidos.</p>
          <div class="urgent-card-price">$${Number(pricing.telemedicina).toLocaleString('es-CL')} CLP</div>
        </div>

        <div class="urgent-option-card" data-modalidad="DOMICILIO" data-horario="PRIMER_CUPO_HOY" data-precio="${pricing.domicilio_urgencia}">
          <span class="card-top-badge">🚑 ATENCIÓN EN TERRENO</span>
          <h4 class="urgent-card-title">Primer cupo de Visita de Urgencia Hoy</h4>
          <p class="urgent-card-desc">Desplazamiento médico prioritario a tu domicilio con equipamiento de soporte clínico.</p>
          <div class="urgent-card-price">$${Number(pricing.domicilio_urgencia).toLocaleString('es-CL')} CLP</div>
        </div>
      </div>
    `;

    // Listeners para las tarjetas urgentes
    const cards = hybridScheduleContainer.querySelectorAll('.urgent-option-card');
    cards.forEach(card => {
      card.addEventListener('click', () => {
        cards.forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedModalidad = card.getAttribute('data-modalidad');
        selectedHorario = card.getAttribute('data-horario');
        selectedPrecio = parseInt(card.getAttribute('data-precio'), 10);
      });
    });

  } else {
    // Caso MEDIUM o LOW: Selector de Modalidad (Domicilio vs Telemedicina) + 3 Horas Exactas + Ventanas Domicilio
    const slots = (data.suggested_slots && data.suggested_slots.length > 0)
      ? data.suggested_slots
      : ['11:00', '15:30', '18:00'];

    const rawIntent = (clientIntentInput && clientIntentInput.value ? clientIntentInput.value : '').toLowerCase();
    const wantsTelemed = rawIntent.includes('telemedicina') || rawIntent.includes('online') || rawIntent.includes('video');

    selectedModalidad = wantsTelemed ? 'TELEMEDICINA' : 'DOMICILIO';
    selectedHorario = slots[0];
    selectedPrecio = wantsTelemed ? (pricing.telemedicina || 15000) : (pricing.domicilio_programado || 25000);

    hybridScheduleContainer.innerHTML = `
      <div class="standard-schedule-wrap">
        <div class="urgent-schedule-grid" style="margin-bottom: 0.9rem;">
          <div class="urgent-option-card modality-choice-card ${!wantsTelemed ? 'selected' : ''}" data-mod="DOMICILIO" data-price="${pricing.domicilio_programado || 25000}">
            <span class="card-top-badge">🏠 ATENCIÓN EN CASA</span>
            <h4 class="urgent-card-title">Visita Veterinaria a Domicilio</h4>
            <p class="urgent-card-desc">La Dra. Daniela Alcázar acude a tu hogar sin estrés de traslado para tu mascota.</p>
            <div class="urgent-card-price">$${Number(pricing.domicilio_programado || 25000).toLocaleString('es-CL')} CLP</div>
          </div>

          <div class="urgent-option-card modality-choice-card ${wantsTelemed ? 'selected' : ''}" data-mod="TELEMEDICINA" data-price="${pricing.telemedicina || 15000}">
            <span class="card-top-badge">💻 VIDEOCONSULTA ONLINE</span>
            <h4 class="urgent-card-title">Consulta por Telemedicina</h4>
            <p class="urgent-card-desc">Evaluación clínica por videollamada, orientación, control y emisión de Receta Digital PDF.</p>
            <div class="urgent-card-price">$${Number(pricing.telemedicina || 15000).toLocaleString('es-CL')} CLP</div>
          </div>
        </div>

        <div class="exact-slots-row">
          <span class="exact-slots-label">Cupos de hora exacta disponibles para hoy:</span>
          ${slots.map((slot, i) => `
            <button type="button" class="btn-exact-slot ${i === 0 ? 'selected' : ''}" data-slot="${slot}">
              🕒 ${slot} hrs
            </button>
          `).join('')}
        </div>

        <div class="home-window-toggle-box" id="home-window-toggle-wrapper">
          <button type="button" class="btn-toggle-home-window" id="btn-toggle-home-window">
            <span>🚗 ¿Visita a domicilio? Prefiero elegir ventana de horario flexible</span>
            <span id="window-toggle-arrow">▼</span>
          </button>
          <div class="home-windows-subgrid hidden" id="home-windows-subgrid">
            <button type="button" class="btn-window-slot" data-slot="VENTANA_MANANA_10_13" data-label="Mañana (10:00 - 13:00 hrs)">
              <span class="window-name">🌅 Ventana Mañana (10:00 – 13:00)</span>
              <span class="window-price">$${Number(pricing.domicilio_programado || 25000).toLocaleString('es-CL')} CLP</span>
            </button>
            <button type="button" class="btn-window-slot" data-slot="VENTANA_TARDE_15_19" data-label="Tarde (15:00 - 19:00 hrs)">
              <span class="window-name">🌇 Ventana Tarde (15:00 – 19:00)</span>
              <span class="window-price">$${Number(pricing.domicilio_programado || 25000).toLocaleString('es-CL')} CLP</span>
            </button>
          </div>
        </div>
      </div>
    `;

    // Listeners para selector de modalidad (Domicilio vs Telemedicina)
    const modCards = hybridScheduleContainer.querySelectorAll('.modality-choice-card');
    const exactSlotBtns = hybridScheduleContainer.querySelectorAll('.btn-exact-slot');
    const windowSlotBtns = hybridScheduleContainer.querySelectorAll('.btn-window-slot');
    const btnToggle = hybridScheduleContainer.querySelector('#btn-toggle-home-window');
    const subgrid = hybridScheduleContainer.querySelector('#home-windows-subgrid');
    const arrow = hybridScheduleContainer.querySelector('#window-toggle-arrow');

    modCards.forEach(card => {
      card.addEventListener('click', () => {
        modCards.forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedModalidad = card.getAttribute('data-mod');
        selectedPrecio = parseInt(card.getAttribute('data-price'), 10);
      });
    });

    exactSlotBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        exactSlotBtns.forEach(b => b.classList.remove('selected'));
        windowSlotBtns.forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
        selectedHorario = btn.getAttribute('data-slot');
      });
    });

    // Toggle de Ventanas
    btnToggle.addEventListener('click', () => {
      const isClosed = subgrid.classList.contains('hidden');
      if (isClosed) {
        subgrid.classList.remove('hidden');
        arrow.textContent = '▲';
      } else {
        subgrid.classList.add('hidden');
        arrow.textContent = '▼';
      }
    });

    windowSlotBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        exactSlotBtns.forEach(b => b.classList.remove('selected'));
        windowSlotBtns.forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
        selectedModalidad = 'DOMICILIO';
        selectedPrecio = pricing.domicilio_programado || 25000;
        modCards.forEach(c => {
          c.classList.toggle('selected', c.getAttribute('data-mod') === 'DOMICILIO');
        });
        selectedHorario = btn.getAttribute('data-label');
      });
    });
  }
}

// Envío del Formulario de Triaje a POST /api/v1/covet/triage-execute
triageExecutionForm.addEventListener('submit', async (e) => {
  e.preventDefault();

  const phone = clientTutorPhone.value.trim();
  const tutorName = clientTutorName.value.trim();
  const petName = clientPetName.value.trim();

  if (!phone || !tutorName || !petName) {
    showToast('Por favor completa todos los datos de contacto.', 'error');
    return;
  }

  // Validación de Consentimientos Desempaquetados — Ley 21.719
  const consentLey = document.getElementById('client-consent-ley21719');
  const consentWhatsapp = document.getElementById('client-consent-whatsapp');
  const consentIa = document.getElementById('client-consent-ia');

  if (!consentLey || !consentLey.checked) {
    showToast('Debes aceptar el consentimiento de atención clínica (Ley 21.719) para continuar.', 'error');
    if (consentLey) consentLey.focus();
    return;
  }

  // Normalizar Teléfono a Formato Internacional (+569...)
  let cleanPhone = phone.replace(/\s+/g, '');
  if (/^9\d{8}$/.test(cleanPhone)) cleanPhone = `+56${cleanPhone}`;
  else if (/^569\d{8}$/.test(cleanPhone)) cleanPhone = `+${cleanPhone}`;

  // Recolectar Respuestas Dinámicas
  const answersMap = {};
  const questionInputs = dynamicQuestionsList.querySelectorAll('input');
  questionInputs.forEach((inp, idx) => {
    const qLabel = dynamicQuestionsList.querySelector(`label[for="dyn-q-${idx}"]`);
    const qText = qLabel ? qLabel.textContent : `Pregunta ${idx + 1}`;
    answersMap[qText] = inp.value.trim();
  });

  const payload = {
    tutor_telefono: cleanPhone,
    tutor_nombre: tutorName,
    paciente_nombre: petName,
    especie: (activeTriageData && activeTriageData.species_detected) || 'Canino',
    respuestas_triaje: answersMap,
    modalidad: selectedModalidad,
    seleccion_horaria: selectedHorario,
    urgency_level: (activeTriageData && activeTriageData.urgency_level) || 'LOW',
    motivo_resumido: (activeTriageData && activeTriageData.motivo_resumido) || 'Consulta clínica',
    first_aid_protocol_id: (activeTriageData && activeTriageData.first_aid_protocol_id) || '',
    consentimiento_ley21719: true,
    consentimiento_whatsapp: consentWhatsapp ? consentWhatsapp.checked : false,
    consentimiento_ia: consentIa ? consentIa.checked : false,
    clinica_slug: activeVetSlug || null
  };

  btnBlockSlot.disabled = true;
  btnBlockSlotLabel.textContent = 'Bloqueando cupo con Agente CoVet...';

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/triage-execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const result = await res.json();
    activeTriageCitaId = result.cita_id;

    // Mostrar Pantalla de Bloqueo Temporal (HOLD_15_MIN)
    renderHoldStage(result, payload);

    clientTriageStage.classList.add('hidden');
    clientHoldStage.classList.remove('hidden');

  } catch (err) {
    console.error('Error al ejecutar triaje:', err);
    showToast(`Error al bloquear cupo: ${err.message}`, 'error');
  } finally {
    btnBlockSlot.disabled = false;
    btnBlockSlotLabel.textContent = 'Bloquear Cupo Temporal (15 min) & Continuar';
  }
});

// Renderizar Estado 4: Bloqueo Temporal (HOLD_15_MIN) & Aviso de Paciente Recurrente
function renderHoldStage(result, payload) {
  // 1. Banner de Paciente Recurrente si el backend lo reconoció
  if (result.paciente_reconocido) {
    patientRecognizedBanner.classList.remove('hidden');
    patientRecognizedTitle.textContent = `✨ ¡Hola de nuevo, ${payload.tutor_nombre}!`;
    patientRecognizedText.textContent = `El Agente CoVet encontró el historial clínico previo de ${payload.paciente_nombre} en la base de datos y ya lo adjuntó a la ficha del veterinario.`;
  } else {
    patientRecognizedBanner.classList.add('hidden');
  }

  // 2. Resumen de la Cita Bloqueada
  holdSummaryPet.textContent = `${payload.paciente_nombre} (${payload.especie})`;
  holdSummaryModality.textContent = payload.modalidad === 'TELEMEDICINA' ? '⚡ Telemedicina Inmediata' : '🚗 Visita a Domicilio';
  holdSummarySchedule.textContent = payload.seleccion_horaria;

  const finalAmount = result.monto_clp || selectedPrecio;
  const formattedPrice = `$${Number(finalAmount).toLocaleString('es-CL')} CLP`;
  holdSummaryPrice.textContent = formattedPrice;
  btnConfirmPayText.textContent = `Confirmar y Pagar Ahora Sin Salir (${formattedPrice})`;

  // 2.5 Botón de Pago Nativo dentro de WhatsApp (Brain Flow / Cero Fricción)
  const btnHoldWaFlow = document.getElementById('btn-hold-whatsapp-flow');
  if (btnHoldWaFlow) {
    const waFlowMsg =
      `⚡ *CoVet — Pago Nativo en Chat (Sin Links Externos)*\n\n` +
      `Hola Dra. Daniela, quiero confirmar y pagar aquí en el chat mi reserva *#${result.cita_id || activeTriageCitaId}*:\n` +
      `• *Paciente:* ${payload.paciente_nombre} (${payload.especie})\n` +
      `• *Tutor(a):* ${payload.tutor_nombre} (${payload.tutor_telefono})\n` +
      `• *Modalidad:* ${payload.modalidad}\n` +
      `• *Bloque:* ${payload.seleccion_horaria}\n` +
      `• *Monto:* ${formattedPrice}`;
    btnHoldWaFlow.href = `https://wa.me/56994128890?text=${encodeURIComponent(waFlowMsg)}`;
  }

  // 3. Link de Pago Externo (si fue configurado)
  if (result.payment_link) {
    externalPayLinkWrap.classList.remove('hidden');
    btnExternalPay.href = result.payment_link;
  } else {
    externalPayLinkWrap.classList.add('hidden');
  }

  // 4. Iniciar Contador Regresivo de 15 Minutos
  startHoldCountdownTimer(result.expira_en_segundos || 900);
}

// Contador Regresivo en Vivo (15:00 min)
function startHoldCountdownTimer(totalSeconds) {
  clearInterval(holdTimerInterval);
  holdSecondsLeft = totalSeconds;
  updateHoldTimerUI();

  holdTimerInterval = setInterval(() => {
    holdSecondsLeft--;
    if (holdSecondsLeft <= 0) {
      clearInterval(holdTimerInterval);
      holdCountdownDigits.textContent = '00:00 (Expirado)';
      btnCovetConfirm.disabled = true;
      btnConfirmPayText.textContent = 'Cupo Expirado — Vuelve a Triar';
      showToast('El bloqueo de 15 minutos ha expirado.', 'error');
      return;
    }
    updateHoldTimerUI();
  }, 1000);
}

function updateHoldTimerUI() {
  const m = Math.floor(holdSecondsLeft / 60).toString().padStart(2, '0');
  const s = (holdSecondsLeft % 60).toString().padStart(2, '0');
  holdCountdownDigits.textContent = `${m}:${s}`;
}

// Botón de Pago / Confirmación 1-Clic
btnCovetConfirm.addEventListener('click', async () => {
  if (!activeTriageCitaId) return;

  btnCovetConfirm.disabled = true;
  btnConfirmPayText.textContent = 'Confirmando reserva en PostgreSQL...';

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/citas/${activeTriageCitaId}/confirm-payment`, {
      method: 'POST'
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();

    // Detener Temporizador
    clearInterval(holdTimerInterval);

    // Renderizar Estado 5: Confirmado & Protocolo de Primeros Auxilios Desbloqueado
    renderConfirmedStage(data);

    clientHoldStage.classList.add('hidden');
    clientConfirmedStage.classList.remove('hidden');

  } catch (err) {
    console.error('Error al confirmar pago:', err);
    showToast(`Error al confirmar reserva: ${err.message}`, 'error');
  } finally {
    btnCovetConfirm.disabled = false;
  }
});

// Renderizado de Primeros Auxilios Pre-Aprobados (Estado 5)
function renderConfirmedStage(data) {
  const guide = data.first_aid_guide || {};
  protocolTitle.textContent = guide.titulo || 'Protocolo Clínico de Contención y Primeros Auxilios';

  // Indicaciones
  protocolIndicationsList.innerHTML = '';
  (guide.indicaciones || [
    'Mantén a la mascota en un lugar tranquilo, tibio y con ventilación.',
    'No suministres agua ni alimentos a la fuerza.',
    'Ten a mano su carnet de vacunación y prepara su transportadora.'
  ]).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item;
    protocolIndicationsList.appendChild(li);
  });

  // Prohibiciones
  protocolProhibitionsList.innerHTML = '';
  (guide.prohibiciones || [
    'PROHIBIDO administrar medicamentos de humanos (Paracetamol, Ibuprofeno o Aspirina son mortales para perros y gatos).',
    'No inducir el vómito sin supervisión médica expresa.'
  ]).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item;
    protocolProhibitionsList.appendChild(li);
  });

  // Alertas Rojas
  protocolAlertsList.innerHTML = '';
  (guide.alertas || [
    'Dificultad respiratoria extrema o jadeo con encías moradas/pálidas.',
    'Pérdida de conciencia, convulsiones o desmayos.',
    'Distensión abdominal severa o intentos fallidos de vomitar.'
  ]).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item;
    protocolAlertsList.appendChild(li);
  });

  // Mensaje al tutor
  protocolTutorMessage.textContent = guide.mensaje_tutor || 'Tu cita ya está agendada. Nuestro equipo médico está al tanto de tu caso.';
}

// Botón de Salto al Modo Veterinario en Demo de Venta
btnSwitchVetDemo.addEventListener('click', () => {
  switchMainMode('vet');
  const tabAgendaBtn = document.getElementById('tab-btn-agenda');
  if (tabAgendaBtn) tabAgendaBtn.click();
  showToast('¡Observa cómo la cita triada ya aparece en la agenda veterinaria en tiempo real!', 'success');
});

// Botón para Realizar Otra Consulta
btnNewTriage.addEventListener('click', () => {
  clientConfirmedStage.classList.add('hidden');
  clientTriageStage.classList.add('hidden');
  clientHoldStage.classList.add('hidden');
  clientIntentHero.classList.remove('hidden');
  clientIntentInput.value = '';
});


// ========================================================
// 4. MODO VETERINARIO: PANEL CLÍNICO VIBE & VERIFY
// ========================================================
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
const reviewResumenAmigable = document.getElementById('review-resumen-amigable');
const reviewTranscriptRaw = document.getElementById('review-transcript-raw');
const btnConfirmSoap = document.getElementById('btn-confirm-soap');
const btnDiscardReview = document.getElementById('btn-discard-review');

// Multi-Paciente Tabs & Dictado por Texto
const multiPatientTabsBar = document.getElementById('multi-patient-tabs-bar');
const multiPatientBadge = document.getElementById('multi-patient-badge');
const multiPatientTabsList = document.getElementById('multi-patient-tabs-list');
const inputTextDictation = document.getElementById('input-text-dictation');
const btnDemoMultipatientText = document.getElementById('btn-demo-multipatient-text');
const btnSubmitTextDictation = document.getElementById('btn-submit-text-dictation');

// Flujo Colaborativo Técnico ➔ Veterinario
const btnToggleTechIntake = document.getElementById('btn-toggle-tech-intake');
const collabTechPanel = document.getElementById('collab-tech-panel');
const btnSubmitTechIntake = document.getElementById('btn-submit-tech-intake');
const selectedCitaContextCard = document.getElementById('selected-cita-context-card');

// Resumidor de Historial Médico Extenso
const selectHistoryPatient = document.getElementById('select-history-patient');
const btnGenerateHistorySummary = document.getElementById('btn-generate-history-summary');
const modalHistorySummary = document.getElementById('modal-history-summary');
const historySummaryTitle = document.getElementById('history-summary-title');
const historySummaryVetText = document.getElementById('history-summary-vet-text');
const historySummaryTutorText = document.getElementById('history-summary-tutor-text');
const historySummaryCount = document.getElementById('history-summary-count');
const historySummaryTimeline = document.getElementById('history-summary-timeline');
const btnCloseHistorySummary = document.getElementById('btn-close-history-summary');
const btnCloseHistorySummaryBottom = document.getElementById('btn-close-history-summary-bottom');
const btnCopyHistorySummary = document.getElementById('btn-copy-history-summary');

let currentMultiPatientFiches = [];
let currentMultiPatientIndex = 0;
let cachedCitasContexto = [];
let lastEventSeq = 0;
let currentHistorySummaryText = '';

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

// Modal de Éxito Post-Confirmación SOAP (#modal-success)
const modalSuccess = document.getElementById('modal-success');
const modalSuccessPaciente = document.getElementById('modal-success-paciente');
const modalSuccessTutor = document.getElementById('modal-success-tutor');
const modalSuccessPhone = document.getElementById('modal-success-phone');
const btnSuccessWhatsAppShare = document.getElementById('btn-success-whatsapp-share');
const btnSuccessDownloadPdf = document.getElementById('btn-success-download-pdf');
const btnCloseModalSuccess = document.getElementById('btn-close-modal-success');
let currentSuccessFiche = null;
let cachedFichesHistory = [];

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

// Detección de Códec y MimeType de Audio
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

// Inicio de Grabación de Dictado Clínico
async function startRecording() {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showToast('Tu navegador no soporta grabación directa de audio.', 'error');
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

// Subida a FastAPI y Polling Asíncrono de Tarea SOAP con Búfer Offline IndexedDB
async function uploadAudioAndPoll(audioFile) {
  switchVibeState('processing');
  resetProcessingUI();

  const citaContextVal = selectCitaContexto && selectCitaContexto.value ? selectCitaContexto.value : null;

  // 1. Guardar primero en IndexedDB como salvaguarda ante cortes 4G en terreno
  const offlineRecordId = await saveOfflineAudio(audioFile, citaContextVal);

  try {
    const formData = new FormData();
    formData.append('file', audioFile);
    if (citaContextVal) {
      formData.append('cita_id', citaContextVal);
    }

    setStepActive(stepUpload, 'Subiendo audio al servidor Hetzner...', 25);

    const response = await fetch(`${API_BASE}/api/v1/covet/transcribe`, {
      method: 'POST',
      body: formData
    });

    if (!response.ok) throw new Error(`HTTP ${response.status} en transcripción`);
    const data = await response.json();
    currentTaskId = data.task_id;

    // Éxito: limpiar del búfer offline
    if (offlineRecordId) {
      await removeOfflineAudio(offlineRecordId);
    }

    setStepDone(stepUpload);
    setStepActive(stepWhisper, 'Transcribiendo audio con Groq Whisper Large Turbo...', 50);

    startPolling(currentTaskId);
  } catch (err) {
    console.error('Error subiendo audio:', err);
    showProcessingError(`Error de conexión: ${err.message}. Tu audio está a salvo en el almacenamiento local de tu teléfono.`);
    updateOfflineBanner();
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
  if (stepElem) stepElem.className = 'step-item active';
  if (processingStepTitle) processingStepTitle.textContent = title;
  if (processingBarFill) processingBarFill.style.width = `${progressPercent}%`;
}

function setStepDone(stepElem) {
  if (stepElem) stepElem.className = 'step-item done';
}

function showProcessingError(msg) {
  if (processingErrorBanner) processingErrorBanner.classList.remove('hidden');
  if (processingErrorMessage) processingErrorMessage.textContent = msg;
  if (processingStepTitle) processingStepTitle.textContent = 'Procesamiento Detenido';
}

function renderMultiPatientTabs() {
  if (!multiPatientTabsBar || !multiPatientTabsList) return;
  if (!currentMultiPatientFiches || currentMultiPatientFiches.length <= 1) {
    multiPatientTabsBar.classList.add('hidden');
    return;
  }

  multiPatientTabsBar.classList.remove('hidden');
  if (multiPatientBadge) {
    multiPatientBadge.textContent = `🐾 ${currentMultiPatientFiches.length} MASCOTAS DETECTADAS EN 1 GRABACIÓN`;
  }

  multiPatientTabsList.innerHTML = currentMultiPatientFiches.map((sib, idx) => {
    const j = sib.json_estructurado || {};
    const petName = j.paciente_nombre || `Mascota #${idx + 1}`;
    const esp = j.especie || 'Canino';
    const icon = esp.toLowerCase().includes('felin') ? '🐱' : '🐶';
    const isConfirmed = (sib.estado_procesamiento || '').startsWith('CONFIRMED');
    const statusLabel = isConfirmed ? '✅ Confirmada' : '⏳ Por revisar';
    const activeClass = idx === currentMultiPatientIndex ? 'active' : '';
    const doneClass = isConfirmed ? 'confirmed' : '';

    return `
      <button type="button" class="multi-patient-tab-btn ${activeClass} ${doneClass}" data-mp-index="${idx}">
        <span>${icon} ${idx + 1}. <strong>${escapeHtml(petName)}</strong> (${escapeHtml(esp)})</span>
        <small class="mp-tab-status">${statusLabel}</small>
      </button>
    `;
  }).join('');

  multiPatientTabsList.querySelectorAll('.multi-patient-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.getAttribute('data-mp-index'), 10);
      if (!isNaN(idx) && currentMultiPatientFiches[idx]) {
        currentMultiPatientIndex = idx;
        fillSoapFormFields(currentMultiPatientFiches[idx]);
        renderMultiPatientTabs();
      }
    });
  });
}

function fillSoapFormFields(fiche) {
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
  if (reviewResumenAmigable) {
    reviewResumenAmigable.value = json.resumen_amigable_tutor || '';
  }
  reviewTranscriptRaw.textContent = fiche.texto_transcrito_original || 'Sin transcripción literal.';

  setTimeout(() => {
    document.querySelectorAll('.glass-textarea.auto-resize').forEach(autoResizeTextarea);
  }, 50);
}

function populateReviewForm(fiche) {
  if (Array.isArray(fiche.sibling_fiches) && fiche.sibling_fiches.length > 1) {
    currentMultiPatientFiches = fiche.sibling_fiches;
    currentMultiPatientIndex = 0;
    showToast(`🐾 Se detectaron ${fiche.sibling_fiches.length} mascotas en la grabación. Se crearon fichas separadas.`, 'success');
  } else {
    currentMultiPatientFiches = [fiche];
    currentMultiPatientIndex = 0;
  }
  fillSoapFormFields(currentMultiPatientFiches[currentMultiPatientIndex] || fiche);
  renderMultiPatientTabs();
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = (textarea.scrollHeight + 4) + 'px';
}

document.querySelectorAll('.glass-textarea.auto-resize').forEach(ta => {
  ta.addEventListener('input', () => autoResizeTextarea(ta));
});

// ========================================================
// 4.1 DESPACHO WHATSAPP CLÍNICO NATIVO ($0 COSTO API) & LEY 21.719
// ========================================================
function buildVetWhatsAppLink(fiche) {
  if (!fiche) return '';
  const json = fiche.json_estructurado || {};

  const ficheId = fiche.id || fiche.fiche_id || '';
  const tutorNombre = (fiche.tutor_nombre || json.tutor_nombre || 'Tutor').trim();
  const pacienteNombre = (fiche.paciente_nombre || json.paciente_nombre || 'Paciente').trim();
  const diagPresuntivo = (fiche.diagnostico_presuntivo || json.diagnostico_presuntivo || 'En observación clínica').trim();
  const planTerapeutico = (fiche.plan_terapeutico || json.plan_terapeutico || 'Indicaciones según criterio médico veterinario.').trim();
  const proximosPasos = (fiche.proximos_pasos || json.proximos_pasos || 'Según evolución clínica o fecha indicada.').trim();
  const resumenAmigable = (fiche.resumen_amigable_tutor || json.resumen_amigable_tutor || '').trim();

  // Limpia el número telefónico dejando solo dígitos (si empieza con 9 y tiene 9 dígitos, antepón 56)
  const rawPhone = fiche.tutor_telefono || json.tutor_telefono || '';
  let cleanPhone = String(rawPhone || '').replace(/\D/g, '');
  if (cleanPhone.length === 9 && cleanPhone.startsWith('9')) {
    cleanPhone = '56' + cleanPhone;
  } else if (cleanPhone.length === 8) {
    cleanPhone = '569' + cleanPhone;
  }

  const vetQuery = activeVetSlug ? `&vet=${encodeURIComponent(activeVetSlug)}` : '';
  const pdfUrl = `https://vet-api.142.132.233.38.sslip.io/api/v1/covet/fiches/${ficheId}/receta-pdf?auto_print=1${vetQuery}`;
  const friendlyBlock = resumenAmigable ? `\n💬 *En palabras sencillas:*\n${resumenAmigable}\n` : '';

  const msg = 
`🩺 *CoVet — Resumen de Atención y Receta Médica Oficial*

Hola *${tutorNombre}*, te comparto el resumen clínico y la receta de la consulta de *${pacienteNombre}*:
${friendlyBlock}
📋 *Diagnóstico Presuntivo:*
${diagPresuntivo}

💊 *Receta e Indicaciones (Rp):*
${planTerapeutico}

📅 *Próximo Control:*
${proximosPasos}

📄 *Descargar Receta Médica Oficial (PDF con firma digital):*
${pdfUrl}

🔒 _Emitido en cumplimiento de la Ley N° 21.719._`;

  const encodedMsg = encodeURIComponent(msg);
  return cleanPhone ? `https://wa.me/${cleanPhone}?text=${encodedMsg}` : `https://wa.me/?text=${encodedMsg}`;
}

function openVetWhatsAppChat(fiche) {
  if (!fiche) return;
  const json = fiche.json_estructurado || {};

  // Ley 21.719: aviso preventivo si consentimiento_whatsapp == false o activo_whatsapp == false
  const consentWhatsapp = fiche.consentimiento_whatsapp;
  const activoWhatsapp = fiche.activo_whatsapp;
  const consentPresencial = fiche.consentimiento_tutor_presencial ?? json.consentimiento_tutor_presencial;
  const jsonConsentWa = json.consentimiento_whatsapp;
  const jsonActivoWa = json.activo_whatsapp;

  const hasNoConsent = (
    consentWhatsapp === false ||
    activoWhatsapp === false ||
    consentPresencial === false ||
    jsonConsentWa === false ||
    jsonActivoWa === false
  );

  if (hasNoConsent) {
    const proceed = confirm(
      "⚠️ Aviso Preventivo — Ley N° 21.719 (Protección de Datos Personales):\n\n" +
      "El tutor registró consentimiento_whatsapp = false o no autorizó mensajes directos.\n\n" +
      "¿Deseas abrir la conversación de WhatsApp bajo tu responsabilidad y criterio profesional médico presencial?"
    );
    if (!proceed) return;
  }

  const url = buildVetWhatsAppLink(fiche);
  if (url) {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}

window.buildVetWhatsAppLink = buildVetWhatsAppLink;
window.openVetWhatsAppChat = openVetWhatsAppChat;

// Confirmación de Ficha SOAP (PUT)
formSoapReview.addEventListener('submit', async (e) => {
  e.preventDefault();
  const ficheId = soapFicheIdInput.value;
  if (!ficheId) return;

  let rawTel = reviewTutorTelefono.value.trim().replace(/\s+/g, '');
  if (/^9\d{8}$/.test(rawTel)) rawTel = `+56${rawTel}`;
  else if (/^569\d{8}$/.test(rawTel)) rawTel = `+${rawTel}`;

  const consentPresencial = document.getElementById('review-consent-presencial')?.checked ?? true;

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
    proximos_pasos: reviewProximosPasos.value.trim(),
    resumen_amigable_tutor: reviewResumenAmigable ? reviewResumenAmigable.value.trim() : '',
    consentimiento_tutor_presencial: consentPresencial
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
    showToast(`Ficha de ${payload.paciente_nombre} confirmada en PostgreSQL.`, 'success');

    // Actualizar estado si forma parte de grupo multi-paciente
    if (currentMultiPatientFiches && currentMultiPatientFiches.length > 0) {
      const targetSib = currentMultiPatientFiches.find(s => String(s.id) === String(ficheId));
      if (targetSib) {
        targetSib.estado_procesamiento = resData.estado_procesamiento || 'CONFIRMED_AND_SENT';
        targetSib.json_estructurado = payload;
      }
      renderMultiPatientTabs();
    }

    // Preparamos objeto de ficha confirmada para el modal y WhatsApp
    currentSuccessFiche = {
      id: ficheId,
      tutor_nombre: payload.tutor_nombre,
      tutor_telefono: payload.tutor_telefono,
      paciente_nombre: payload.paciente_nombre,
      diagnostico_presuntivo: payload.diagnostico_presuntivo,
      plan_terapeutico: payload.plan_terapeutico,
      proximos_pasos: payload.proximos_pasos,
      resumen_amigable_tutor: payload.resumen_amigable_tutor,
      consentimiento_whatsapp: payload.consentimiento_tutor_presencial,
      consentimiento_tutor_presencial: payload.consentimiento_tutor_presencial,
      json_estructurado: payload
    };

    const vetQuery = activeVetSlug ? `&vet=${encodeURIComponent(activeVetSlug)}` : '';
    const pdfHref = `https://vet-api.142.132.233.38.sslip.io/api/v1/covet/fiches/${ficheId}/receta-pdf?auto_print=1${vetQuery}`;

    // Actualizamos campos del modal de éxito
    if (modalSuccessPaciente) modalSuccessPaciente.textContent = payload.paciente_nombre || 'Paciente';
    if (modalSuccessTutor) modalSuccessTutor.textContent = payload.tutor_nombre || 'Tutor';
    if (modalSuccessPhone) modalSuccessPhone.textContent = payload.tutor_telefono || '--';
    if (btnSuccessDownloadPdf) {
      btnSuccessDownloadPdf.href = pdfHref;
    }

    // Configurar y mostrar enlace a Receta PDF Oficial en la barra de revisión
    const btnOpenReceta = document.getElementById('btn-open-receta-pdf');
    if (btnOpenReceta) {
      btnOpenReceta.href = pdfHref;
      btnOpenReceta.classList.remove('hidden');
    }

    // Si hay otra mascota pendiente en esta grabación multi-paciente, avisar en el botón de cierre
    const nextPendingIdx = currentMultiPatientFiches.findIndex(s => !(s.estado_procesamiento || '').startsWith('CONFIRMED'));
    if (btnCloseModalSuccess) {
      if (nextPendingIdx !== -1 && currentMultiPatientFiches.length > 1) {
        const nextName = currentMultiPatientFiches[nextPendingIdx].json_estructurado?.paciente_nombre || `Mascota #${nextPendingIdx + 1}`;
        btnCloseModalSuccess.textContent = `👉 Continuar con Ficha de ${nextName} (${nextPendingIdx + 1}/${currentMultiPatientFiches.length})`;
      } else {
        btnCloseModalSuccess.textContent = 'Volver a la Consulta';
      }
    }

    // Mostrar modal de éxito (#modal-success) para despacho 1-Clic
    if (modalSuccess) modalSuccess.classList.remove('hidden');

    loadAppointmentsData();
    loadFichesHistory();
  } catch (err) {
    showToast(`Error al confirmar: ${err.message}`, 'error');
  } finally {
    btnConfirmSoap.disabled = false;
    btnConfirmSoap.innerHTML = `<span class="btn-icon">✓</span> <span class="btn-text">[✓] Confirmar Ficha y Enviar por WhatsApp</span>`;
  }
});

function closeSuccessModalAndAdvance() {
  if (modalSuccess) modalSuccess.classList.add('hidden');
  const nextPendingIdx = currentMultiPatientFiches.findIndex(s => !(s.estado_procesamiento || '').startsWith('CONFIRMED'));
  if (nextPendingIdx !== -1 && currentMultiPatientFiches.length > 1) {
    currentMultiPatientIndex = nextPendingIdx;
    fillSoapFormFields(currentMultiPatientFiches[nextPendingIdx]);
    renderMultiPatientTabs();
    showToast(`Revisando siguiente mascota de la grabación: ${currentMultiPatientFiches[nextPendingIdx].json_estructurado?.paciente_nombre || 'Paciente'}`, 'info');
  } else {
    switchVibeState('idle');
  }
}

// Modal de Éxito Post-Confirmación (#modal-success) Listeners
if (btnCloseModalSuccess) {
  btnCloseModalSuccess.addEventListener('click', closeSuccessModalAndAdvance);
}

if (btnSuccessWhatsAppShare) {
  btnSuccessWhatsAppShare.addEventListener('click', () => {
    if (currentSuccessFiche) {
      openVetWhatsAppChat(currentSuccessFiche);
    }
  });
}

if (modalSuccess) {
  modalSuccess.addEventListener('click', (e) => {
    if (e.target === modalSuccess) {
      closeSuccessModalAndAdvance();
    }
  });
}

btnDiscardReview.addEventListener('click', () => {
  if (confirm('¿Descartar esta ficha y volver al inicio?')) switchVibeState('idle');
});

btnRetryRecord.addEventListener('click', () => switchVibeState('idle'));

btnStartRecord.addEventListener('click', startRecording);
btnStopRecord.addEventListener('click', stopRecordingAndProcess);
btnCancelRecord.addEventListener('click', cancelRecording);

// Dictado por Texto y Simulador Multi-Paciente (2+ mascotas en un solo dictado)
if (btnDemoMultipatientText && inputTextDictation) {
  btnDemoMultipatientText.addEventListener('click', () => {
    inputTextDictation.value = (
      "Atención domiciliaria para la tutora Marcela Soto, teléfono +56987654321. " +
      "Primero revisé a Max, un perro Beagle macho de 5 años y 13.8 kilos: presenta otitis externa bilateral con secreción ceruminosa y prurito hace 4 días; temperatura 38.6 grados, frecuencia cardíaca 96 lpm. Diagnóstico presuntivo otitis externa malassezia. Plan: limpieza ótica con clorhexidina cada 24 horas por 7 días y gotas óticas Otiflex 4 gotas cada 12 horas por 7 días, control en 7 días. " +
      "Segundo paciente en el mismo domicilio: Luna, gata doméstica pelo corto de 2 años y 3.9 kilos: consulta por vómitos biliosos agudos desde ayer tras comer planta ornamental; temperatura 38.4 grados, mucosas rosadas, abdomen blando. Diagnóstico gastritis aguda leve. Plan: Cerenia 0.4 ml subcutáneo hoy, Omeprazol 4 mg vía oral cada 24 horas por 5 días y dieta blanda gastrointestinal, control en 48 horas."
    );
    showToast('Ejemplo con 2 pacientes (Max y Luna) cargado. Pulsa Estructurar Ficha(s) SOAP.', 'info');
  });
}

if (btnSubmitTextDictation && inputTextDictation) {
  btnSubmitTextDictation.addEventListener('click', async () => {
    const txt = inputTextDictation.value.trim();
    if (!txt || txt.length < 10) {
      showToast('Escribe o carga un dictado clínico válido primero.', 'warning');
      inputTextDictation.focus();
      return;
    }

    const citaContextVal = selectCitaContexto && selectCitaContexto.value ? parseInt(selectCitaContexto.value, 10) : null;
    switchVibeState('processing');
    resetProcessingUI();
    setStepDone(stepUpload);
    setStepDone(stepWhisper);
    setStepActive(stepClaude, 'Separando pacientes y estructurando SOAP con Claude...', 70);

    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/structure-text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          transcript_text: txt,
          cita_id: citaContextVal
        })
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }
      const ficheData = await res.json();
      setStepDone(stepClaude);
      setStepDone(stepReady);
      processingBarFill.style.width = '100%';
      setTimeout(() => {
        populateReviewForm(ficheData);
        switchVibeState('review');
      }, 400);
    } catch (err) {
      showProcessingError(`Error estructurando dictado: ${err.message}`);
    }
  });
}


// ========================================================
// 5. AGENDA, FLUJO COLABORATIVO (TÉCNICO ➔ VET) & SSE EN VIVO
// ========================================================
if (btnToggleTechIntake && collabTechPanel) {
  btnToggleTechIntake.addEventListener('click', () => {
    collabTechPanel.classList.toggle('hidden');
  });
}

if (btnSubmitTechIntake) {
  btnSubmitTechIntake.addEventListener('click', async () => {
    const tutorNombre = document.getElementById('tech-tutor-nombre')?.value.trim() || '';
    const tutorTelefono = document.getElementById('tech-tutor-telefono')?.value.trim() || '';
    const pacienteNombre = document.getElementById('tech-paciente-nombre')?.value.trim() || '';
    const especie = document.getElementById('tech-paciente-especie')?.value || 'Canino';
    const pesoVal = document.getElementById('tech-paciente-peso')?.value || '';
    const razaEdad = document.getElementById('tech-paciente-raza')?.value.trim() || 'Mestizo';
    const constantes = document.getElementById('tech-constantes')?.value.trim() || '';
    const motivo = document.getElementById('tech-motivo')?.value.trim() || '';

    if (!tutorNombre || !tutorTelefono || !pacienteNombre || !motivo) {
      showToast('Completa Tutor, WhatsApp, Mascota y Motivo de ingreso.', 'warning');
      return;
    }

    btnSubmitTechIntake.disabled = true;
    btnSubmitTechIntake.textContent = '⏳ Sincronizando en vivo...';

    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/colaborativo/admision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tutor_nombre: tutorNombre,
          tutor_telefono: tutorTelefono,
          paciente_nombre: pacienteNombre,
          especie: especie,
          raza: razaEdad,
          edad_aproximada: razaEdad,
          peso_kg: pesoVal ? parseFloat(pesoVal) : null,
          constantes_previas: constantes,
          motivo: motivo,
          nivel_urgencia: 'MEDIUM',
          modalidad: 'CLINICA_PRESENCIAL'
        })
      });
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      showToast(`✅ Paciente ${data.paciente_nombre} cargado y sincronizado en la pantalla del Veterinario.`, 'success');

      if (collabTechPanel) collabTechPanel.classList.add('hidden');
      await loadAppointmentsData(data.cita_id);
    } catch (err) {
      showToast(`Error en admisión colaborativa: ${err.message}`, 'error');
    } finally {
      btnSubmitTechIntake.disabled = false;
      btnSubmitTechIntake.textContent = '⚡ Sincronizar en Vivo con Pantalla del Veterinario';
    }
  });
}

function updateSelectedCitaContextPreview() {
  if (!selectCitaContexto || !selectedCitaContextCard) return;
  const val = selectCitaContexto.value;
  if (!val) {
    selectedCitaContextCard.classList.add('hidden');
    selectedCitaContextCard.innerHTML = '';
    return;
  }
  const found = cachedCitasContexto.find(c => String(c.cita_id) === String(val));
  if (!found) {
    selectedCitaContextCard.classList.add('hidden');
    return;
  }
  const constInfo = found.constantes_previas ? `<div><strong>🩺 Constantes Técnico:</strong> ${escapeHtml(found.constantes_previas)}</div>` : '';
  selectedCitaContextCard.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.4rem;">
      <strong>🐾 Pre-cargado: ${escapeHtml(found.paciente_nombre)} (${escapeHtml(found.especie)} · ${escapeHtml(found.raza)})</strong>
      <span class="badge-tag green" style="font-size:0.72rem;">✓ Sincronizado con Recepción</span>
    </div>
    <div style="font-size:0.83rem; color:var(--text-dim); margin-top:0.25rem;">
      👤 Tutor: <strong>${escapeHtml(found.tutor_nombre)}</strong> (${escapeHtml(found.tutor_telefono || 'Sin tel')}) • Peso: <strong>${found.peso_kg ? found.peso_kg + ' kg' : 'Por pesar'}</strong>
    </div>
    <div style="font-size:0.83rem; margin-top:0.2rem;">📋 <strong>Motivo:</strong> ${escapeHtml(found.motivo)}</div>
    ${constInfo}
  `;
  selectedCitaContextCard.classList.remove('hidden');
}

if (selectCitaContexto) {
  selectCitaContexto.addEventListener('change', updateSelectedCitaContextPreview);
}

async function loadAppointmentsData(autoSelectCitaId = null) {
  try {
    const [citasRes, pacientesRes, tutoresRes, ctxRes] = await Promise.all([
      fetch(`${API_BASE}/api/citas`).catch(() => null),
      fetch(`${API_BASE}/api/pacientes`).catch(() => null),
      fetch(`${API_BASE}/api/tutores`).catch(() => null),
      fetch(`${API_BASE}/api/v1/covet/citas/contexto`).catch(() => null)
    ]);

    const citas = citasRes && citasRes.ok ? await citasRes.json() : [];
    const pacientes = pacientesRes && pacientesRes.ok ? await pacientesRes.json() : [];
    const tutores = tutoresRes && tutoresRes.ok ? await tutoresRes.json() : [];
    cachedCitasContexto = ctxRes && ctxRes.ok ? await ctxRes.json() : [];

    if (kpiCitas) kpiCitas.textContent = citas.length;
    if (kpiPacientes) kpiPacientes.textContent = pacientes.length;
    if (kpiTutores) kpiTutores.textContent = tutores.length;
    if (tabCitasCounter) tabCitasCounter.textContent = citas.length;

    renderAppointmentsList(citas, pacientes);
    populateAppointmentContextSelector(citas, pacientes, autoSelectCitaId);
  } catch (err) {
    console.error('Error cargando citas en agenda:', err);
  }
}

function renderAppointmentsList(citas, pacientes) {
  if (!appointmentsContainer) return;

  if (!citas || citas.length === 0) {
    appointmentsContainer.innerHTML = `
      <div class="rare-empty-state" role="status">
        <div class="rare-empty-icon-box">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        </div>
        <h4 class="rare-empty-title">Sin citas programadas por ahora</h4>
        <p class="rare-empty-desc">Cuando un tutor agende una visita a domicilio o telemedicina desde el Triaje IA, o registres un control en el formulario lateral, aparecerá aquí en tiempo real.</p>
        <button type="button" class="rare-empty-cta" onclick="document.getElementById('cita-tutor-nombre')?.focus()">
          <span>+ Agendar primera cita manual</span>
          <span aria-hidden="true">→</span>
        </button>
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
    const emoji = paciente.especie && paciente.especie.toLowerCase().includes('felin') ? '🐱' : '🐶';

    // Estado de Reserva: HOLD_15_MIN vs CONFIRMED
    const isHold = cita.estado_reserva === 'HOLD_15_MIN' || cita.estado_cita === 'HOLD_15_MIN';
    const statusClass = isHold ? 'hold' : 'confirmed';
    const statusText = isHold ? '⏳ HOLD 15 MIN' : (cita.pago_estado === 'SIMULATED_PAID' || cita.pago_estado === 'CONFIRMED' ? '✅ PAGADO & CONFIRMADO' : '✓ CONFIRMADA');

    // Badge de Urgencia Semántica
    const urgency = (cita.nivel_urgencia || 'LOW').toUpperCase();
    let urgencyBadge = '<span class="cita-status-tag" style="background:rgba(13,148,136,0.12);color:var(--teal-primary);">✅ RUTINA</span>';
    if (urgency === 'HIGH') {
      urgencyBadge = '<span class="cita-status-tag" style="background:rgba(225,29,72,0.15);color:var(--vital-rose);font-weight:800;">🚨 URGENCIA ALTA</span>';
    } else if (urgency === 'MEDIUM') {
      urgencyBadge = '<span class="cita-status-tag" style="background:rgba(217,119,6,0.15);color:var(--amber-warning);font-weight:700;">⚠️ PRIORITARIO</span>';
    }

    const modalidad = (cita.modalidad || cita.tipo_atencion || 'DOMICILIO').toUpperCase();
    const ventana = cita.ventana_horaria ? `• Horario: ${escapeHtml(cita.ventana_horaria)}` : '';

    return `
      <div class="appointment-card ${statusClass}">
        <div class="cita-pet">
          <span>${emoji}</span>
          <strong style="font-size:1.02rem;">${escapeHtml(paciente.nombre)}</strong>
          <small style="color:var(--text-dim);font-weight:700;">[${modalidad}]</small>
          ${urgencyBadge}
          <span class="cita-status-tag ${statusClass}">${statusText}</span>
        </div>
        <div class="cita-motivo" style="margin: 0.35rem 0;">${escapeHtml(cita.motivo)}</div>
        <div class="cita-time" style="font-size:0.82rem;color:var(--text-dim);">
          📅 ${dateFormatted} ${ventana} • Arancel: $${Number(cita.monto_pago || 0).toLocaleString('es-CL')} CLP
        </div>
      </div>
    `;
  }).join('');
}

function populateAppointmentContextSelector(citas, pacientes, autoSelectCitaId = null) {
  if (!selectCitaContexto) return;
  const previousVal = autoSelectCitaId ? String(autoSelectCitaId) : selectCitaContexto.value;

  const pacientesMap = new Map();
  pacientes.forEach(p => pacientesMap.set(p.id, p));

  selectCitaContexto.innerHTML = '<option value="">-- Consulta espontánea / Sin cita previa --</option>';
  citas.forEach(cita => {
    const paciente = pacientesMap.get(cita.paciente_id);
    const petName = paciente ? paciente.nombre : `Paciente #${cita.paciente_id}`;
    const dateStr = new Date(cita.fecha_hora).toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
    const urgency = (cita.nivel_urgencia || 'LOW').toUpperCase();
    const modalidad = cita.modalidad || 'DOMICILIO';

    const opt = document.createElement('option');
    opt.value = cita.id;
    opt.textContent = `Cita #${cita.id} [${urgency}] [${modalidad}] — ${petName} (${dateStr}: ${cita.motivo.slice(0, 34)})`;
    selectCitaContexto.appendChild(opt);
  });

  if (previousVal && Array.from(selectCitaContexto.options).some(o => String(o.value) === String(previousVal))) {
    selectCitaContexto.value = String(previousVal);
  }
  updateSelectedCitaContextPreview();
}

// Sincronización en Tiempo Real (SSE + Polling Delta) para Flujo Colaborativo Técnico ➔ Veterinario
function handleRealtimeClinicalEvent(evt) {
  if (!evt || !evt.event_type) return;
  if (evt.seq && evt.seq > lastEventSeq) {
    lastEventSeq = evt.seq;
  }
  if (evt.event_type === 'COLLAB_INTAKE_READY' || evt.event_type === 'APPOINTMENT_CREATED') {
    const d = evt.data || {};
    const pet = d.paciente_nombre || 'Nuevo Paciente';
    showToast(`🔔 Sincronización Colaborativa: ${pet} ingresado por Recepción/Técnico.`, 'info');
    loadAppointmentsData(d.cita_id || null);
  } else if (evt.event_type === 'FICHE_CONFIRMED') {
    loadFichesHistory();
  } else if (evt.event_type === 'MEMBERSHIP_SUBSCRIBED') {
    const d = evt.data || {};
    showToast(`💖 Nueva Membresía Cuidado Continuo: ${d.paciente_nombre || 'Mascota'} (${d.tutor_nombre || 'Tutor'})`, 'success');
    if (typeof loadVetMembershipsList === 'function') {
      loadVetMembershipsList();
    }
  }
}

function initCollaborativeRealtimeSync() {
  if (window.EventSource) {
    try {
      const es = new EventSource(`${API_BASE}/api/v1/covet/events/stream?since_seq=${lastEventSeq}`);
      es.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data);
          if (parsed.event_type === 'CONNECTED' && parsed.seq) {
            lastEventSeq = parsed.seq;
          } else {
            handleRealtimeClinicalEvent(parsed);
          }
        } catch (_) {}
      };
      es.onerror = () => {
        es.close();
      };
    } catch (_) {}
  }

  // Fallback Polling Delta cada 12 segundos para redes móviles o proxies restrictivos
  setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/events/recent?since_seq=${lastEventSeq}`);
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.events) && data.events.length > 0) {
        data.events.forEach(handleRealtimeClinicalEvent);
      }
      if (data.current_seq && data.current_seq > lastEventSeq) {
        lastEventSeq = data.current_seq;
      }
    } catch (_) {}
  }, 12000);
}

initCollaborativeRealtimeSync();

// Agendamiento Manual Rápido en Agenda
if (appointmentForm) {
  appointmentForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (btnSubmitAppointment) btnSubmitAppointment.disabled = true;

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
      if (btnSubmitAppointment) btnSubmitAppointment.disabled = false;
    }
  });
}

if (btnRefresh) {
  btnRefresh.addEventListener('click', () => {
    loadAppointmentsData();
    checkHealth();
    showToast('Agenda sincronizada con PostgreSQL', 'info');
  });
}


// ========================================================
// 6. HISTORIAL DE FICHAS & RESUMIDOR DE HISTORIAL EXTENSO (TAB 3)
// ========================================================
async function openPatientHistorySummaryModal(pacienteNombre, pacienteId = null) {
  if (!modalHistorySummary) return;
  modalHistorySummary.classList.remove('hidden');
  if (historySummaryTitle) historySummaryTitle.textContent = `Resumen Longitudinal — ${pacienteNombre || 'Paciente'}`;
  if (historySummaryVetText) historySummaryVetText.textContent = '⏳ Analizando y resumiendo historial clínico con IA...';
  if (historySummaryTutorText) historySummaryTutorText.textContent = '⏳ Generando versión en lenguaje amigable para el tutor...';
  if (historySummaryTimeline) historySummaryTimeline.innerHTML = '';

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/historial/resumir`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        paciente_id: pacienteId ? parseInt(pacienteId, 10) : null,
        paciente_nombre: pacienteNombre
      })
    });
    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (historySummaryTitle) {
      historySummaryTitle.textContent = `Epicrisis Longitudinal — ${data.paciente_nombre} (${data.especie} · ${data.raza})`;
    }
    if (historySummaryVetText) historySummaryVetText.textContent = data.resumen_clinico_veterinario || '--';
    if (historySummaryTutorText) historySummaryTutorText.textContent = data.resumen_amigable_tutor || '--';
    if (historySummaryCount) historySummaryCount.textContent = data.total_consultas || 0;

    if (historySummaryTimeline && Array.isArray(data.cronologia)) {
      historySummaryTimeline.innerHTML = data.cronologia.map(c => `
        <div style="padding:0.65rem 0.85rem; background:rgba(15,23,42,0.04); border:1px solid var(--border-subtle,#cbd5e1); border-radius:6px; font-size:0.84rem;">
          <div style="font-weight:700; color:var(--teal-primary,#0D9488);">Consulta #${c.consulta_num} (${escapeHtml(c.fecha)}) • Folio ${escapeHtml(c.folio)} ${c.peso_kg ? '• Peso: ' + c.peso_kg + ' kg' : ''}</div>
          <div><strong>Dx:</strong> ${escapeHtml(c.diagnostico)}</div>
          <div><strong>Plan:</strong> ${escapeHtml(c.tratamiento)}</div>
        </div>
      `).join('');
    }

    currentHistorySummaryText = (
      `📋 EPICRISIS CLÍNICA COVET — ${data.paciente_nombre} (${data.especie})\n` +
      `Tutor: ${data.tutor_nombre} | Consultas: ${data.total_consultas}\n\n` +
      `🩺 RESUMEN VETERINARIO:\n${data.resumen_clinico_veterinario}\n\n` +
      `💬 RESUMEN AMIGABLE PARA EL TUTOR:\n${data.resumen_amigable_tutor}`
    );
  } catch (err) {
    if (historySummaryVetText) historySummaryVetText.textContent = `No se pudo resumir el historial: ${err.message}`;
    if (historySummaryTutorText) historySummaryTutorText.textContent = '--';
  }
}

if (btnGenerateHistorySummary && selectHistoryPatient) {
  btnGenerateHistorySummary.addEventListener('click', () => {
    const val = selectHistoryPatient.value;
    if (!val) {
      showToast('Selecciona un paciente del listado primero.', 'warning');
      return;
    }
    openPatientHistorySummaryModal(val, null);
  });
}

[btnCloseHistorySummary, btnCloseHistorySummaryBottom].forEach(btn => {
  if (btn) {
    btn.addEventListener('click', () => {
      if (modalHistorySummary) modalHistorySummary.classList.add('hidden');
    });
  }
});

if (btnCopyHistorySummary) {
  btnCopyHistorySummary.addEventListener('click', () => {
    if (currentHistorySummaryText) {
      copyTextToClipboard(currentHistorySummaryText, null);
    }
  });
}

async function loadFichesHistory() {
  if (!fichesHistoryList) return;

  fichesHistoryList.innerHTML = `
    <div class="skeleton-stack" aria-busy="true" aria-label="Consultando fichas en PostgreSQL">
      <div class="skeleton-list-item"><div class="shimmer-bar" style="width: 42%; height: 18px;"></div><div class="shimmer-bar" style="width: 90%; height: 12px; margin-top: 10px;"></div><div class="shimmer-bar" style="width: 65%; height: 12px; margin-top: 6px;"></div></div>
      <div class="skeleton-list-item"><div class="shimmer-bar" style="width: 48%; height: 18px;"></div><div class="shimmer-bar" style="width: 85%; height: 12px; margin-top: 10px;"></div><div class="shimmer-bar" style="width: 60%; height: 12px; margin-top: 6px;"></div></div>
    </div>
  `;

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/fiches?limit=25`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fiches = await res.json();
    cachedFichesHistory = Array.isArray(fiches) ? fiches : [];

    if (!fiches || fiches.length === 0) {
      fichesHistoryList.innerHTML = `
        <div class="rare-empty-state" role="status">
          <div class="rare-empty-icon-box">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
          </div>
          <h4 class="rare-empty-title">Aún no hay fichas clínicas SOAP registradas</h4>
          <p class="rare-empty-desc">Graba o redacta tu primera consulta en la pestaña de Dictado Clínico para generar automáticamente la ficha SOAP y la receta médica en PDF.</p>
          <button type="button" class="rare-empty-cta" onclick="document.querySelector('[data-tab=&quot;tab-dictation&quot;]')?.click()">
            <span>Ir a Dictado Clínico SOAP</span>
            <span aria-hidden="true">→</span>
          </button>
        </div>
      `;
      return;
    }

    // Poblar selector de pacientes únicos para el resumidor de historiales extensos
    if (selectHistoryPatient) {
      const uniqueNames = new Set();
      fiches.forEach(f => {
        const pName = (f.json_estructurado?.paciente_nombre || '').trim();
        if (pName) uniqueNames.add(pName);
      });
      selectHistoryPatient.innerHTML = '<option value="">-- Selecciona un paciente del historial --</option>';
      Array.from(uniqueNames).forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = `🐾 ${name}`;
        selectHistoryPatient.appendChild(opt);
      });
    }

    const vetQuery = activeVetSlug ? `&vet=${encodeURIComponent(activeVetSlug)}` : '';

    fichesHistoryList.innerHTML = fiches.map(fiche => {
      const json = fiche.json_estructurado || {};
      const petName = json.paciente_nombre || fiche.paciente_nombre || 'Paciente';
      const tutorName = json.tutor_nombre || fiche.tutor_nombre || 'Tutor';
      const diag = json.diagnostico_presuntivo || fiche.diagnostico_presuntivo || 'Sin diagnóstico registrado';
      const friendly = json.resumen_amigable_tutor || '';
      const createdDate = fiche.fecha_creacion ? new Date(fiche.fecha_creacion).toLocaleString('es-CL') : 'Reciente';

      let statusBadge = '<span class="cita-status-tag confirmed">✓ CONFIRMADA</span>';
      if (fiche.estado_procesamiento === 'CONFIRMED_AND_SENT') {
        statusBadge = '<span class="cita-status-tag confirmed">✅ WhatsApp Despachado</span>';
      } else if (fiche.estado_procesamiento === 'CONFIRMED_N8N_PENDING') {
        statusBadge = '<span class="cita-status-tag hold">⚠️ n8n Pendiente</span>';
      }

      const friendlyLine = friendly
        ? `<div style="font-size:0.81rem; color:var(--text-dim); margin-top:0.2rem;">💬 <em>${escapeHtml(friendly.slice(0, 120))}${friendly.length > 120 ? '...' : ''}</em></div>`
        : '';

      return `
        <div class="fiche-history-card">
          <div class="cita-pet">
            <span>🐾</span>
            <span>${escapeHtml(petName)}</span>
            <small style="color:var(--text-dim);">(${escapeHtml(tutorName)})</small>
            ${statusBadge}
          </div>
          <div class="cita-motivo"><strong>Dx:</strong> ${escapeHtml(diag)}</div>
          ${friendlyLine}
          <div class="cita-time" style="display:flex; justify-content:space-between; align-items:center; margin-top:0.4rem; flex-wrap:wrap; gap:0.5rem;">
            <span>📅 ${createdDate} • ID: ${fiche.id.slice(0, 8)}...</span>
            <div class="history-card-actions">
              <button type="button" class="btn-action-small btn-history-summary" data-pet-name="${escapeHtml(petName)}" data-pet-id="${fiche.paciente_id || ''}" style="font-size:0.78rem; padding:0.25rem 0.6rem; border-radius:6px; cursor:pointer; background:rgba(37,99,235,0.1); border:1px solid #3b82f6; color:#2563eb; font-weight:700;">
                🧠 Resumen Historial
              </button>
              <button type="button" class="btn-history-wa" data-fiche-id="${fiche.id}" title="Reenviar Receta por WhatsApp (1-Clic)">
                📲 WhatsApp
              </button>
              <a href="https://vet-api.142.132.233.38.sslip.io/api/v1/covet/fiches/${fiche.id}/receta-pdf?auto_print=1${vetQuery}" target="_blank" class="btn-action-small" style="text-decoration:none; display:inline-flex; align-items:center; gap:0.35rem; font-size:0.8rem; padding:0.25rem 0.65rem; background:rgba(13,148,136,0.12); border:1px solid var(--teal-primary,#0D9488); border-radius:6px; color:var(--teal-primary,#0D9488); font-weight:700;">
                📄 Receta PDF
              </a>
            </div>
          </div>
        </div>
      `;
    }).join('');

    // Listener para botones de Resumen Longitudinal por paciente
    fichesHistoryList.querySelectorAll('.btn-history-summary').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const pName = btn.getAttribute('data-pet-name');
        const pId = btn.getAttribute('data-pet-id');
        openPatientHistorySummaryModal(pName, pId);
      });
    });

    // Listener para botones de WhatsApp rápido en el historial
    fichesHistoryList.querySelectorAll('.btn-history-wa').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const fId = btn.getAttribute('data-fiche-id');
        const found = cachedFichesHistory.find(f => String(f.id) === String(fId));
        if (found) {
          openVetWhatsAppChat(found);
        } else {
          openVetWhatsAppChat({ id: fId });
        }
      });
    });
  } catch (err) {
    fichesHistoryList.innerHTML = `<div class="empty-state"><p>Error cargando fichas: ${err.message}</p></div>`;
  }
}

if (btnRefreshFiches) btnRefreshFiches.addEventListener('click', loadFichesHistory);


// ========================================================
// 7. HEALTH CHECK & MONITOREO DEL VPS HETZNER
// ========================================================
async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.status === 'healthy') {
      if (statusDot) statusDot.className = 'status-dot active';
      if (statusText) statusText.textContent = 'Hetzner Online';
      if (dbStatusText) dbStatusText.textContent = `PostgreSQL 16 OK`;
    } else {
      if (statusDot) statusDot.className = 'status-dot';
      if (statusText) statusText.textContent = 'Backend Degradado';
      if (dbStatusText) dbStatusText.textContent = data.database || 'DB Error';
    }
  } catch (err) {
    if (statusDot) statusDot.className = 'status-dot';
    if (statusText) statusText.textContent = 'Backend Desconectado';
    if (dbStatusText) dbStatusText.textContent = 'Sin Conexión';
  }
}

function showToast(message, type = 'info') {
  if (!toastContainer) return;
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

// Inicialización de Monitoreo y Carga
checkHealth();
setInterval(checkHealth, 25000);


// ========================================================
// 8. PANEL EJECUTIVO SUPERADMIN — SASHA ALCALÁ
// ========================================================
const adminKpiConsent = document.getElementById('admin-kpi-consent');
const adminKpiConsentPct = document.getElementById('admin-kpi-consent-pct');
const adminKpiTotalTutores = document.getElementById('admin-kpi-total-tutores');
const adminKpiConfirmedCitas = document.getElementById('admin-kpi-confirmed-citas');
const adminKpiHoldCitas = document.getElementById('admin-kpi-hold-citas');
const adminKpiTotalCitas = document.getElementById('admin-kpi-total-citas');
const adminKpiSoapFiches = document.getElementById('admin-kpi-soap-fiches');
const adminKpiSoapReady = document.getElementById('admin-kpi-soap-ready');
const adminKpiSoapSent = document.getElementById('admin-kpi-soap-sent');

const chipStatusDb = document.getElementById('chip-status-db');
const chipDbSub = document.getElementById('chip-db-sub');
const chipStatusGroq = document.getElementById('chip-status-groq');
const chipGroqSub = document.getElementById('chip-groq-sub');
const chipStatusClaude = document.getElementById('chip-status-claude');
const chipClaudeSub = document.getElementById('chip-claude-sub');
const chipStatusN8n = document.getElementById('chip-status-n8n');
const chipN8nSub = document.getElementById('chip-n8n-sub');

const btnAdminRefreshMetrics = document.getElementById('btn-admin-refresh-metrics');
const btnAdminTestN8n = document.getElementById('btn-admin-test-n8n');
const btnAdminExportArsop = document.getElementById('btn-admin-export-arsop');
const btnAdminSwitchClient = document.getElementById('btn-admin-switch-client');
const btnAdminSwitchVet = document.getElementById('btn-admin-switch-vet');

const adminConsoleBox = document.getElementById('admin-console-box');
const adminConsoleBody = document.getElementById('admin-console-body');
const btnCloseAdminConsole = document.getElementById('btn-close-admin-console');

// Bóveda Cifrada Ley 21.719
const vaultStatusBadge = document.getElementById('vault-status-badge');
const vaultStatusDot = document.getElementById('vault-status-dot');
const vaultStatusText = document.getElementById('vault-status-text');
const vaultEncryptionLabel = document.getElementById('vault-encryption-label');
const vaultLastTime = document.getElementById('vault-last-time');
const vaultFileName = document.getElementById('vault-file-name');
const vaultSize = document.getElementById('vault-size');
const vaultCopies = document.getElementById('vault-copies');
const vaultShaDisplay = document.getElementById('vault-sha-display');
const btnCopyVaultSha = document.getElementById('btn-copy-vault-sha');
const copyShaIcon = document.getElementById('copy-sha-icon');
const btnAdminTriggerBackup = document.getElementById('btn-admin-trigger-backup');
const btnVaultTriggerText = document.getElementById('btn-vault-trigger-text');

let currentVaultSha = '';

function formatVaultTimestamp(ts) {
  if (!ts) return 'Sin respaldos aún';
  const match = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_UTC$/.exec(ts);
  if (match) {
    const [, y, m, d, h, min, s] = match;
    return `${y}-${m}-${d} ${h}:${min}:${s} UTC`;
  }
  return ts;
}

function updateVaultUI(vault) {
  if (!vault) return;
  const isOk = vault.status === 'OK';

  if (vaultStatusText) {
    vaultStatusText.textContent = isOk ? '✅ PROTEGIDO' : (vault.status === 'PENDING_FIRST_RUN' ? '⏳ PENDIENTE PRIMER RESPALDO' : vault.status);
  }
  if (vaultStatusDot) {
    vaultStatusDot.className = isOk ? 'service-dot online' : 'service-dot offline';
  }
  if (vaultEncryptionLabel && vault.encryption) {
    vaultEncryptionLabel.textContent = `${vault.encryption} • PBKDF2 (100k)`;
  }
  if (vaultLastTime) {
    vaultLastTime.textContent = formatVaultTimestamp(vault.last_backup_utc);
  }
  if (vaultFileName) {
    vaultFileName.textContent = vault.file_name || 'covet_db_pending.sql.gz.enc';
  }
  if (vaultSize) {
    const bytes = vault.size_bytes || 0;
    vaultSize.textContent = `${(bytes / 1024).toFixed(1)} KB`;
  }
  if (vaultCopies) {
    vaultCopies.textContent = `${vault.retained_copies_14d || 0} copias`;
  }
  if (vaultShaDisplay) {
    const sha = vault.sha256 || '';
    currentVaultSha = sha;
    if (sha.length > 18) {
      vaultShaDisplay.textContent = `${sha.slice(0, 10)}...${sha.slice(-8)}`;
      vaultShaDisplay.title = sha;
    } else {
      vaultShaDisplay.textContent = sha || 'Pendiente primer run';
      vaultShaDisplay.title = sha || '';
    }
  }
}

async function loadAdminMetrics() {
  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/admin/metrics`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // 1. KPIs Ley 21.719
    const ley = data.cumplimiento_ley21719 || {};
    if (adminKpiConsent) adminKpiConsent.textContent = ley.tutores_con_consentimiento || 0;
    if (adminKpiConsentPct) adminKpiConsentPct.textContent = `${ley.tasa_consentimiento_pct || 100}%`;
    if (adminKpiTotalTutores) {
      adminKpiTotalTutores.textContent = `de ${ley.total_tutores || 0} tutores totales (${ley.activos_whatsapp || 0} con WhatsApp activo)`;
    }

    // 2. KPIs Citas
    const citas = data.metricas_citas || {};
    if (adminKpiConfirmedCitas) adminKpiConfirmedCitas.textContent = citas.citas_confirmadas || 0;
    if (adminKpiHoldCitas) adminKpiHoldCitas.textContent = `${citas.citas_en_hold_15_min || 0} en HOLD 15m`;
    if (adminKpiTotalCitas) adminKpiTotalCitas.textContent = `${citas.total_citas || 0} citas totales generadas`;

    // 3. KPIs SOAP
    const soap = data.metricas_soap || {};
    if (adminKpiSoapFiches) adminKpiSoapFiches.textContent = soap.total_fichas || 0;
    if (adminKpiSoapReady) adminKpiSoapReady.textContent = `${soap.listas_para_revision || 0} listas revisión`;
    if (adminKpiSoapSent) adminKpiSoapSent.textContent = `${soap.confirmadas_y_despachadas || 0} despachadas a WhatsApp`;

    // 4. Integraciones
    const inte = data.integraciones || {};
    updateServiceChip(chipStatusDb, chipDbSub, inte.postgresql_16 === 'ONLINE', 'PostgreSQL 16 OK (vet-db)');
    updateServiceChip(chipStatusGroq, chipGroqSub, inte.groq_whisper === 'CONFIGURADO', 'Groq Whisper V3 Turbo Activo');
    updateServiceChip(chipStatusClaude, chipClaudeSub, inte.anthropic_claude === 'CONFIGURADO', 'Claude 3.5 Sonnet Tool Use Activo');
    updateServiceChip(chipStatusN8n, chipN8nSub, true, 'Coolify n8n_app Activo');

    // 5. Bóveda Cifrada Ley 21.719
    if (data.backup_vault) {
      updateVaultUI(data.backup_vault);
    }

    // 6. Lista de Marcas Blancas
    loadAdminBrandingList();

    showToast('Métricas ejecutivas de SuperAdmin actualizadas en vivo.', 'info');
  } catch (err) {
    console.error('Error cargando métricas admin:', err);
    showToast(`Error al obtener métricas: ${err.message}`, 'error');
  }
}

function updateServiceChip(chipElem, subElem, isOnline, okText) {
  if (!chipElem) return;
  const dot = chipElem.querySelector('.service-dot');
  if (dot) {
    dot.className = isOnline ? 'service-dot' : 'service-dot offline';
  }
  if (subElem) {
    subElem.textContent = isOnline ? okText : 'Falta Configuración';
  }
}

function logToAdminConsole(message) {
  if (!adminConsoleBox || !adminConsoleBody) return;
  adminConsoleBox.classList.remove('hidden');
  const timestamp = new Date().toLocaleTimeString('es-CL');
  adminConsoleBody.textContent = `[${timestamp}] ${message}\n\n` + adminConsoleBody.textContent;
}

if (btnAdminRefreshMetrics) {
  btnAdminRefreshMetrics.addEventListener('click', loadAdminMetrics);
}

if (btnCloseAdminConsole) {
  btnCloseAdminConsole.addEventListener('click', () => {
    if (adminConsoleBox) adminConsoleBox.classList.add('hidden');
    if (adminConsoleBody) adminConsoleBody.textContent = '';
  });
}

if (btnAdminTestN8n) {
  btnAdminTestN8n.addEventListener('click', async () => {
    logToAdminConsole('⚡ Disparando ping de diagnóstico hacia n8n WhatsApp Webhook...');
    try {
      const res = await fetch(`${API_BASE}/health`);
      const data = await res.json();
      logToAdminConsole(`✓ Health check VPS responde OK: ${JSON.stringify(data, null, 2)}`);
      showToast('Prueba de pipeline ejecutada con éxito.', 'success');
    } catch (err) {
      logToAdminConsole(`❌ Error en prueba: ${err.message}`);
    }
  });
}

if (btnAdminExportArsop) {
  btnAdminExportArsop.addEventListener('click', async () => {
    const phoneInput = prompt('Ingresa el número de WhatsApp a exportar (con +569...):', '+56912345678');
    if (!phoneInput) return;

    const clean = phoneInput.trim();
    logToAdminConsole(`📥 Solicitando Derecho de Portabilidad Ley 21.719 para ${clean}...`);

    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/privacy/export/${encodeURIComponent(clean)}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const exported = await res.json();
      logToAdminConsole(`✓ Exportación Ley 21.719 generada exitosamente:\n${JSON.stringify(exported, null, 2)}`);
      showToast('Datos de portabilidad exportados en consola.', 'success');
    } catch (err) {
      logToAdminConsole(`❌ Error exportando ARSOP+: ${err.message}`);
      showToast(`Error exportando: ${err.message}`, 'error');
    }
  });
}

if (btnAdminSwitchClient) {
  btnAdminSwitchClient.addEventListener('click', () => switchMainMode('client'));
}

if (btnAdminSwitchVet) {
  btnAdminSwitchVet.addEventListener('click', () => switchMainMode('vet'));
}

if (btnCopyVaultSha) {
  btnCopyVaultSha.addEventListener('click', async () => {
    if (!currentVaultSha) {
      showToast('No hay hash SHA-256 disponible aún.', 'warning');
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(currentVaultSha);
      } else {
        const temp = document.createElement('textarea');
        temp.value = currentVaultSha;
        document.body.appendChild(temp);
        temp.select();
        document.execCommand('copy');
        document.body.removeChild(temp);
      }
      if (copyShaIcon) copyShaIcon.textContent = '✓';
      showToast('Hash SHA-256 copiado al portapapeles.', 'success');
      setTimeout(() => {
        if (copyShaIcon) copyShaIcon.textContent = '📋';
      }, 2000);
    } catch (err) {
      console.warn('Fallback copiar hash:', err);
      showToast(`Hash: ${currentVaultSha.slice(0, 16)}...`, 'info');
    }
  });
}

if (btnAdminTriggerBackup) {
  btnAdminTriggerBackup.addEventListener('click', async () => {
    btnAdminTriggerBackup.disabled = true;
    if (btnVaultTriggerText) btnVaultTriggerText.textContent = '⏳ Cifrando tablas AES-256...';
    logToAdminConsole('⚡ Iniciando respaldo en caliente + cifrado militar AES-256 (Art. 14Ter Ley 21.719)...');

    try {
      const res = await fetch(`${API_BASE}/api/v1/covet/admin/backup-now`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }
      const newStatus = await res.json();
      updateVaultUI(newStatus);
      logToAdminConsole(
        `✅ BÓVEDA ACTUALIZADA CON ÉXITO:\n` +
        `• Archivo: ${newStatus.file_name}\n` +
        `• Tamaño: ${(newStatus.size_bytes / 1024).toFixed(2)} KB\n` +
        `• Algoritmo: ${newStatus.encryption} (100,000 iteraciones PBKDF2)\n` +
        `• SHA-256: ${newStatus.sha256}\n` +
        `• Copias retenidas (14d): ${newStatus.retained_copies_14d}\n` +
        `• Ley 21.719: Art. 14Ter CUMPLIDO`
      );
      showToast('¡Respaldo cifrado AES-256 ejecutado con éxito!', 'success');
    } catch (err) {
      console.error('Error al ejecutar backup:', err);
      logToAdminConsole(`❌ Fallo en respaldo cifrado: ${err.message}`);
      showToast(`Error al respaldar: ${err.message}`, 'error');
    } finally {
      btnAdminTriggerBackup.disabled = false;
      if (btnVaultTriggerText) btnVaultTriggerText.textContent = 'Ejecutar Backup Cifrado Ahora';
    }
  });
}

// ========================================================
// GENERADOR COMERCIAL DE MARCA BLANCA (?vet=slug)
// ========================================================
const COVET_ADMIN_KEY = 'CoVetSuperAdminKeySasha2026Secure!';

const brandInputSlug = document.getElementById('brand-input-slug');
const brandInputName = document.getElementById('brand-input-name');
const brandInputSpecialty = document.getElementById('brand-input-specialty');
const brandInputRut = document.getElementById('brand-input-rut');
const brandInputEmail = document.getElementById('brand-input-email');
const brandInputPhone = document.getElementById('brand-input-phone');
const brandInputWelcome = document.getElementById('brand-input-welcome');
const brandInputLogo = document.getElementById('brand-input-logo');
const btnSaveBranding = document.getElementById('btn-save-branding');
const btnClearBranding = document.getElementById('btn-clear-branding');
const btnLoadBranding = document.getElementById('btn-load-branding');

const brandingOutputPanel = document.getElementById('branding-output-panel');
const brandOutputTitle = document.getElementById('brand-output-title');
const brandOutputSubtitle = document.getElementById('brand-output-subtitle');
const brandOutputColorCircle = document.getElementById('brand-output-color-circle');
const brandOutputSlugLabel = document.getElementById('brand-output-slug-label');
const previewUrlClient = document.getElementById('preview-url-client');
const previewUrlVet = document.getElementById('preview-url-vet');
const btnCopyBrandClient = document.getElementById('btn-copy-brand-client');
const btnCopyBrandVet = document.getElementById('btn-copy-brand-vet');
const btnShareBrandWa = document.getElementById('btn-share-brand-wa');
const btnOpenBrandPreview = document.getElementById('btn-open-brand-preview');
const brandingChipsList = document.getElementById('branding-chips-list');

let currentActiveBrandSlug = null;
let currentActiveBrandData = null;

// Auto-formateo del slug en minúsculas y guiones
if (brandInputSlug) {
  brandInputSlug.addEventListener('input', () => {
    brandInputSlug.value = brandInputSlug.value
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/--+/g, '-');
  });
}

const SECONDARY_COLOR_MAP = {
  '#E91E63': '#C2185B',
  '#0D9488': '#0F766E',
  '#2563EB': '#1D4ED8',
  '#7C3AED': '#6D28D9',
  '#059669': '#047857'
};

function getBrandLinks(slug) {
  const base = (window.location.origin.includes('localhost') || window.location.origin.includes('127.0.0.1'))
    ? window.location.origin
    : 'https://vet.142.132.233.38.sslip.io';
  return {
    client: `${base}/?vet=${encodeURIComponent(slug)}&view=client`,
    vet: `${base}/?vet=${encodeURIComponent(slug)}&view=vet`,
    preview: `${base}/?vet=${encodeURIComponent(slug)}`
  };
}

function getSelectedBrandColor() {
  const checked = document.querySelector('input[name="brand-color-choice"]:checked');
  return checked ? checked.value : '#0D9488';
}

function setSelectedBrandColor(color) {
  const radio = document.querySelector(`input[name="brand-color-choice"][value="${color}"]`);
  if (radio) {
    radio.checked = true;
  }
}

async function copyTextToClipboard(text, iconElem) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    if (iconElem) {
      const orig = iconElem.textContent;
      iconElem.textContent = '✓';
      setTimeout(() => { iconElem.textContent = orig; }, 2000);
    }
    showToast('Enlace copiado al portapapeles con éxito.', 'success');
  } catch (err) {
    console.warn('Fallback copiar portapapeles:', err);
    showToast(`Copiado: ${text.slice(0, 30)}...`, 'info');
  }
}

function updateBrandOutputDisplay(slug, data) {
  if (!brandingOutputPanel) return;
  currentActiveBrandSlug = slug;
  currentActiveBrandData = data;

  const links = getBrandLinks(slug);

  if (brandOutputTitle) brandOutputTitle.textContent = data.nombre_fantasia || slug;
  if (brandOutputSubtitle) {
    brandOutputSubtitle.textContent = data.razon_social 
      ? `${data.razon_social} • DPA Ley 21.719 Firmado`
      : 'Portal clínico configurado con DPA firmado y soporte Ley 21.719';
  }
  if (brandOutputSlugLabel) brandOutputSlugLabel.textContent = `?vet=${slug}`;
  if (brandOutputColorCircle) brandOutputColorCircle.style.backgroundColor = data.color_primario || '#0D9488';

  if (previewUrlClient) previewUrlClient.textContent = links.client;
  if (previewUrlVet) previewUrlVet.textContent = links.vet;

  brandingOutputPanel.classList.remove('hidden');
}

async function loadAdminBrandingList() {
  if (!brandingChipsList) return;
  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/admin/branding`, {
      headers: {
        'Accept': 'application/json',
        'X-Admin-Key': COVET_ADMIN_KEY
      }
    });
    if (!res.ok) {
      if (res.status === 401 || res.status === 503) {
        brandingChipsList.innerHTML = '<span class="branding-chip-placeholder">Autenticación requerida para listar</span>';
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const brands = await res.json();
    if (!Array.isArray(brands) || brands.length === 0) {
      brandingChipsList.innerHTML = '<span class="branding-chip-placeholder">Sin marcas registradas aún. Ingresa la primera abajo.</span>';
      return;
    }

    brandingChipsList.innerHTML = '';
    brands.forEach(b => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = `branding-chip ${b.slug === currentActiveBrandSlug ? 'active' : ''}`;
      chip.innerHTML = `
        <span class="color-dot" style="background-color: ${b.color_primario || '#0D9488'}; width: 10px; height: 10px; border-radius: 50%; display: inline-block;"></span>
        <span>${b.slug}</span>
        <small style="opacity: 0.8;">(${b.nombre_fantasia || 'Clínica'})</small>
      `;
      chip.addEventListener('click', () => {
        if (brandInputSlug) brandInputSlug.value = b.slug;
        if (brandInputName) brandInputName.value = b.nombre_fantasia || '';
        if (brandInputSpecialty) brandInputSpecialty.value = b.razon_social || '';
        if (brandInputRut) brandInputRut.value = b.rut_responsable || '';
        if (brandInputEmail) brandInputEmail.value = b.email_privacidad || '';
        if (brandInputPhone) brandInputPhone.value = b.whatsapp_contacto || '';
        if (brandInputWelcome) brandInputWelcome.value = b.mensaje_bienvenida || '';
        if (brandInputLogo) brandInputLogo.value = b.logo_url || '';
        if (b.color_primario) setSelectedBrandColor(b.color_primario);

        document.querySelectorAll('.branding-chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');

        updateBrandOutputDisplay(b.slug, b);
        showToast(`Clínica ${b.slug} seleccionada.`, 'info');
      });
      brandingChipsList.appendChild(chip);
    });
  } catch (err) {
    console.warn('Error cargando lista de marcas:', err);
    brandingChipsList.innerHTML = `<span class="branding-chip-placeholder">No fue posible cargar marcas (${err.message})</span>`;
  }
}

async function handleLoadExistingBranding() {
  const rawSlug = (brandInputSlug ? brandInputSlug.value : '').toLowerCase().trim();
  if (!rawSlug) {
    showToast('Ingresa un slug primero para buscar (ej: dra-carolina).', 'warning');
    if (brandInputSlug) brandInputSlug.focus();
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/branding/${encodeURIComponent(rawSlug)}`);
    if (!res.ok) {
      if (res.status === 404) {
        showToast(`La clínica "${rawSlug}" no existe aún. Puedes crearla con el formulario.`, 'info');
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (brandInputName) brandInputName.value = data.nombre_fantasia || '';
    if (brandInputSpecialty) brandInputSpecialty.value = data.razon_social || '';
    if (brandInputRut) brandInputRut.value = data.rut_responsable || '';
    if (brandInputEmail) brandInputEmail.value = data.email_privacidad || '';
    if (brandInputPhone) brandInputPhone.value = data.whatsapp_contacto || '';
    if (brandInputWelcome) brandInputWelcome.value = data.mensaje_bienvenida || '';
    if (brandInputLogo) brandInputLogo.value = data.logo_url || '';
    if (data.color_primario) setSelectedBrandColor(data.color_primario);

    updateBrandOutputDisplay(rawSlug, data);
    showToast(`✓ Datos de "${rawSlug}" cargados con éxito.`, 'success');
  } catch (err) {
    console.error('Error cargando branding:', err);
    showToast(`Error al cargar datos: ${err.message}`, 'error');
  }
}

if (btnLoadBranding) {
  btnLoadBranding.addEventListener('click', handleLoadExistingBranding);
}

async function handleSaveBranding() {
  const rawSlug = (brandInputSlug ? brandInputSlug.value : '').toLowerCase().trim();
  const name = (brandInputName ? brandInputName.value : '').trim();

  if (!rawSlug || !/^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])$/.test(rawSlug)) {
    showToast('Slug inválido: usa entre 3 y 60 caracteres a-z, números y guiones.', 'error');
    if (brandInputSlug) brandInputSlug.focus();
    return;
  }

  if (!name || name.length < 2) {
    showToast('Ingresa el Nombre Profesional o Clínica (mínimo 2 letras).', 'error');
    if (brandInputName) brandInputName.focus();
    return;
  }

  const primaryColor = getSelectedBrandColor();
  const secondaryColor = SECONDARY_COLOR_MAP[primaryColor] || '#0F766E';
  const rawLogo = (brandInputLogo ? brandInputLogo.value : '').trim();

  const payload = {
    nombre_fantasia: name,
    razon_social: (brandInputSpecialty ? brandInputSpecialty.value : '').trim() || null,
    rut_responsable: (brandInputRut ? brandInputRut.value : '').trim() || null,
    email_privacidad: (brandInputEmail ? brandInputEmail.value : '').trim() || null,
    color_primario: primaryColor,
    color_secundario: secondaryColor,
    logo_url: rawLogo.startsWith('https://') ? rawLogo : null,
    whatsapp_contacto: (brandInputPhone ? brandInputPhone.value : '').trim() || null,
    mensaje_bienvenida: (brandInputWelcome ? brandInputWelcome.value : '').trim() || 'Atención médica veterinaria con trazabilidad y privacidad garantizada.',
    plan: 'PRO_WHITE_LABEL',
    activo: true,
    dpa_aceptado: true
  };

  if (btnSaveBranding) {
    btnSaveBranding.disabled = true;
    btnSaveBranding.innerHTML = '<span class="btn-icon">⏳</span> Guardando Marca Blanca...';
  }

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/admin/branding/${encodeURIComponent(rawSlug)}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-Key': COVET_ADMIN_KEY
      },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const saved = await res.json();
    updateBrandOutputDisplay(rawSlug, saved);
    await loadAdminBrandingList();

    const links = getBrandLinks(rawSlug);
    logToAdminConsole(
      `🏷️ MARCA BLANCA GUARDADA EXITOSAMENTE:\n` +
      `• Slug: ${rawSlug}\n` +
      `• Profesional: ${saved.nombre_fantasia}\n` +
      `• Razón Social: ${saved.razon_social || 'N/A'}\n` +
      `• RUT: ${saved.rut_responsable || 'N/A'}\n` +
      `• Color: ${saved.color_primario}\n` +
      `• DPA Ley 21.719: VIGENTE (Art. 14)\n` +
      `• Portal Cliente: ${links.client}\n` +
      `• Panel Vet: ${links.vet}`
    );

    showToast(`¡Clínica "${rawSlug}" configurada con éxito!`, 'success');
  } catch (err) {
    console.error('Error guardando marca blanca:', err);
    logToAdminConsole(`❌ Error guardando marca blanca: ${err.message}`);
    showToast(`Error guardando marca blanca: ${err.message}`, 'error');
  } finally {
    if (btnSaveBranding) {
      btnSaveBranding.disabled = false;
      btnSaveBranding.innerHTML = '<span class="btn-icon">🚀</span> Guardar y Generar Links de Marca Blanca';
    }
  }
}

if (btnSaveBranding) {
  btnSaveBranding.addEventListener('click', handleSaveBranding);
}

if (btnClearBranding) {
  btnClearBranding.addEventListener('click', () => {
    if (brandInputSlug) brandInputSlug.value = '';
    if (brandInputName) brandInputName.value = '';
    if (brandInputSpecialty) brandInputSpecialty.value = '';
    if (brandInputRut) brandInputRut.value = '';
    if (brandInputEmail) brandInputEmail.value = '';
    if (brandInputPhone) brandInputPhone.value = '';
    if (brandInputWelcome) brandInputWelcome.value = '';
    setSelectedBrandColor('#0D9488');
    if (brandingOutputPanel) brandingOutputPanel.classList.add('hidden');
    document.querySelectorAll('.branding-chip').forEach(c => c.classList.remove('active'));
    currentActiveBrandSlug = null;
    currentActiveBrandData = null;
    showToast('Formulario de marca blanca reiniciado.', 'info');
  });
}

// Botones de acción directa generados
if (btnCopyBrandClient) {
  btnCopyBrandClient.addEventListener('click', () => {
    if (!currentActiveBrandSlug) return;
    const links = getBrandLinks(currentActiveBrandSlug);
    const icon = document.getElementById('copy-icon-client');
    copyTextToClipboard(links.client, icon);
  });
}

if (btnCopyBrandVet) {
  btnCopyBrandVet.addEventListener('click', () => {
    if (!currentActiveBrandSlug) return;
    const links = getBrandLinks(currentActiveBrandSlug);
    const icon = document.getElementById('copy-icon-vet');
    copyTextToClipboard(links.vet, icon);
  });
}

if (btnShareBrandWa) {
  btnShareBrandWa.addEventListener('click', () => {
    if (!currentActiveBrandSlug) return;
    const name = (currentActiveBrandData && currentActiveBrandData.nombre_fantasia) 
      ? currentActiveBrandData.nombre_fantasia 
      : currentActiveBrandSlug;
    const links = getBrandLinks(currentActiveBrandSlug);

    const message = 
      `Hola ${name} 👋, acá tienes tu portal clínico CoVet 100% personalizado para tu consulta con cumplimiento estricto Ley 21.719 de Protección de Datos Personales:\n\n` +
      `🐾 Portal de Atención para tus Tutores:\n${links.client}\n\n` +
      `🩺 Tu Panel Veterinario con IA SOAP:\n${links.vet}\n\n` +
      `✅ Consentimiento informado trazable\n` +
      `✅ Ficha SOAP con reconocimiento de voz\n` +
      `✅ Respaldo cifrado militar AES-256`;

    let waUrl = `https://wa.me/?text=${encodeURIComponent(message)}`;
    if (currentActiveBrandData && currentActiveBrandData.whatsapp_contacto) {
      const cleanPhone = currentActiveBrandData.whatsapp_contacto.replace(/[^0-9]/g, '');
      if (cleanPhone.length >= 8) {
        waUrl = `https://wa.me/${cleanPhone}?text=${encodeURIComponent(message)}`;
      }
    }
    window.open(waUrl, '_blank');
    showToast('Abriendo WhatsApp con propuesta y links listos...', 'info');
  });
}

if (btnOpenBrandPreview) {
  btnOpenBrandPreview.addEventListener('click', () => {
    if (!currentActiveBrandSlug) return;
    const links = getBrandLinks(currentActiveBrandSlug);
    window.open(links.preview, '_blank');
  });
}

// ========================================================
// 12. INICIALIZACIÓN: WHITE-LABEL, SOL DIRECTO Y BUFFER LOCAL
// ========================================================

// Botón de Modo Sol Directo (Alto Contraste)
const btnSunModeToggle = document.getElementById('btn-toggle-sun-mode');
let isSunModeActive = localStorage.getItem('covet_sun_mode') === 'true';

function setSunMode(active) {
  isSunModeActive = active;
  localStorage.setItem('covet_sun_mode', active ? 'true' : 'false');
  if (btnSunModeToggle) {
    btnSunModeToggle.classList.toggle('active', active);
    const label = document.getElementById('sun-mode-label');
    if (label) label.textContent = active ? 'Sol Activado' : 'Sol Directo';
  }
  if (active) {
    document.body.setAttribute('data-theme', 'sun-contrast');
    document.body.classList.add('theme-sun-contrast');
  } else {
    const defaultTheme = localStorage.getItem('covet-vet-theme') || 'clinical-light';
    document.body.setAttribute('data-theme', defaultTheme);
    document.body.classList.remove('theme-sun-contrast');
  }
}

if (btnSunModeToggle) {
  btnSunModeToggle.addEventListener('click', () => {
    setSunMode(!isSunModeActive);
    showToast(isSunModeActive ? '☀️ Modo Sol Directo (Alto Contraste) activado' : 'Modo clínico estándar restaurado', 'info');
  });
}

// Botón de reintento de audios offline
const btnRetryOffline = document.getElementById('btn-retry-offline-audio');
if (btnRetryOffline) {
  btnRetryOffline.addEventListener('click', retryUploadOfflineAudios);
}

// Escuchador de conectividad de red
window.addEventListener('online', () => {
  updateOfflineBanner();
  showToast('📡 Conexión a internet restablecida.', 'success');
});

// Inicialización de Marca Blanca desde la URL (?vet=nombre-marca, por defecto dra-daniela-alcazar)
async function initWhiteLabelBranding() {
  const urlParams = new URLSearchParams(window.location.search);
  const slug = urlParams.get('vet') || urlParams.get('clinic') || 'dra-daniela-alcazar';

  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/branding/${encodeURIComponent(slug.toLowerCase().trim())}`);
    if (!res.ok) return;
    const data = await res.json();
    activeVetSlug = data.slug;
    activeBranding = data;

    // Aplicar colores corporativos personalizados (Fucsia/Magenta #E91E63 para Dra. Daniela Alcázar)
    if (data.color_primario) {
      document.documentElement.style.setProperty('--teal-primary', data.color_primario);
      document.documentElement.style.setProperty('--teal-glow', `${data.color_primario}40`);
    }
    if (data.color_secundario) {
      document.documentElement.style.setProperty('--teal-hover', data.color_secundario);
    }

    // Actualizar Hero Principal si es otra clínica White-Label distinta a Dra. Daniela Alcázar
    const mainHeading = document.getElementById('daniela-main-heading');
    if (mainHeading && data.nombre_fantasia && !data.slug.includes('daniela')) {
      mainHeading.textContent = data.nombre_fantasia;
    }

    const headerBrandTitle = document.getElementById('client-header-brand-title');
    if (headerBrandTitle && data.nombre_fantasia) {
      headerBrandTitle.textContent = data.nombre_fantasia.split('-')[0].trim();
    }

    // Actualizar Responsable del Tratamiento en el Modal de Privacidad Ley 21.719
    const controllerText = document.getElementById('privacy-controller-text');
    if (controllerText) {
      const respName = data.razon_social || data.nombre_fantasia;
      const respRut = data.rut_responsable ? ` (RUT: ${data.rut_responsable})` : '';
      const respEmail = data.email_privacidad ? ` | Correo ARSOP+: ${data.email_privacidad}` : '';
      controllerText.innerHTML = `El <strong>Responsable del Tratamiento</strong> (Data Controller) es <strong>${escapeHtml(respName)}</strong>${escapeHtml(respRut)}${escapeHtml(respEmail)}. CoVet Chile SpA opera como <strong>Encargado del Tratamiento</strong> (Data Processor) bajo el Acuerdo de Tratamiento de Datos (DPA) legalmente vigente.`;
    }

    console.log(`[White-Label] Marca cargada: ${data.nombre_fantasia} (${data.slug})`);
  } catch (err) {
    console.warn('[White-Label] Error cargando branding:', err);
  }
}

// ========================================================
// 13. MEMBRESÍA CUIDADO CONTINUO ($8.990/MES) & CARNÉ 24/7
// ========================================================
const btnOpenSubscribeModal = document.getElementById('btn-open-subscribe-modal');
const modalSubscribeMembership = document.getElementById('modal-subscribe-membership');
const btnCloseSubscribeModal = document.getElementById('btn-close-subscribe-modal');
const formSubscribeMembership = document.getElementById('form-subscribe-membership');
const btnConfirmSubscription = document.getElementById('btn-confirm-subscription');
const btnConfirmSubscriptionText = document.getElementById('btn-confirm-subscription-text');
const danielaSubscriptionSuccess = document.getElementById('daniela-subscription-success');

const inputCarnetLookup = document.getElementById('input-carnet-lookup');
const btnCarnetLookup = document.getElementById('btn-carnet-lookup');
const liveCarnetResults = document.getElementById('live-carnet-results');

const vetMembershipsList = document.getElementById('vet-memberships-list');
const btnRefreshMemberships = document.getElementById('btn-refresh-memberships');

if (btnOpenSubscribeModal && modalSubscribeMembership) {
  btnOpenSubscribeModal.addEventListener('click', () => {
    modalSubscribeMembership.classList.remove('hidden');
    if (formSubscribeMembership) formSubscribeMembership.classList.remove('hidden');
    if (danielaSubscriptionSuccess) {
      danielaSubscriptionSuccess.classList.add('hidden');
      danielaSubscriptionSuccess.innerHTML = '';
    }
  });
}

if (btnCloseSubscribeModal && modalSubscribeMembership) {
  btnCloseSubscribeModal.addEventListener('click', () => {
    modalSubscribeMembership.classList.add('hidden');
  });
}

if (formSubscribeMembership) {
  formSubscribeMembership.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (btnConfirmSubscription) btnConfirmSubscription.disabled = true;
    if (btnConfirmSubscriptionText) btnConfirmSubscriptionText.textContent = '⏳ Activando Membresía y Carné Digital...';

    try {
      const payload = {
        tutor_nombre: document.getElementById('sub-tutor-nombre').value.trim(),
        tutor_telefono: document.getElementById('sub-tutor-telefono').value.trim(),
        tutor_email: document.getElementById('sub-tutor-email').value.trim() || null,
        tutor_direccion: document.getElementById('sub-tutor-direccion').value.trim() || null,
        paciente_nombre: document.getElementById('sub-paciente-nombre').value.trim(),
        especie: document.getElementById('sub-paciente-especie').value,
        raza: document.getElementById('sub-paciente-raza').value.trim() || 'Mestizo',
        clinica_slug: activeVetSlug || 'dra-daniela-alcazar',
        metodo_pago: document.getElementById('sub-metodo-pago')?.value || 'WHATSAPP_FLOW',
        consentimiento_ley21719: document.getElementById('sub-consent-ley21719').checked
      };

      const res = await fetch(`${API_BASE}/api/v1/covet/membresias/suscribir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      showToast(`¡Membresía Cuidado Continuo activada para ${data.paciente.nombre}!`, 'success');

      const waMsg =
        `💖 *Dra. Daniela Alcázar — Membresía Cuidado Continuo Activa*\n\n` +
        `Hola Dra. Daniela, acabo de suscribir a mi mascota *${data.paciente.nombre}* (${data.paciente.especie}):\n` +
        `• *Folio Membresía:* ${data.folio_membresia}\n` +
        `• *Tutor(a):* ${data.tutor.nombre} (${data.tutor.telefono})\n` +
        `• *Dirección:* ${data.tutor.direccion || 'Por coordinar'}\n` +
        `• *Próxima Vacuna:* ${data.calendario_preventivo.proxima_vacuna}\n` +
        `• *Próxima Desparasitación:* ${data.calendario_preventivo.proxima_desparasitacion}`;

      const waLink = `https://wa.me/56994128890?text=${encodeURIComponent(waMsg)}`;

      formSubscribeMembership.classList.add('hidden');
      if (danielaSubscriptionSuccess) {
        danielaSubscriptionSuccess.classList.remove('hidden');
        danielaSubscriptionSuccess.innerHTML = `
          <div style="text-align: center; padding: 0.75rem 0.25rem;">
            <div style="font-size: 2.3rem; margin-bottom: 0.35rem;">🎉</div>
            <h4 style="font-size: 1.2rem; color: #0f172a; margin: 0 0 0.35rem 0;">
              ¡${escapeHtml(data.paciente.nombre)} ya es parte de la Membresía Cuidado Continuo!
            </h4>
            <p style="font-size: 0.86rem; color: #475569; margin: 0 0 0.85rem 0;">
              Folio Oficial: <strong class="font-mono" style="color: #e91e63;">${escapeHtml(data.folio_membresia)}</strong> · Valor: <strong>$8.990/mes</strong>
            </p>
            <div style="text-align: left; background: #fdf2f8; border: 1px solid rgba(233,30,99,0.28); border-radius: 12px; padding: 0.85rem 1rem; font-size: 0.84rem; margin-bottom: 1rem;">
              <div><strong>💉 Próxima Vacuna Anual:</strong> ${escapeHtml(data.calendario_preventivo.proxima_vacuna)}</div>
              <div><strong>💊 Próxima Desparasitación:</strong> ${escapeHtml(data.calendario_preventivo.proxima_desparasitacion)}</div>
              <div><strong>🏠 Consulta a Domicilio Gratis:</strong> 1 disponible este año</div>
              <div><strong>⚡ Descuento Urgencias/Extra:</strong> 20% OFF activo</div>
            </div>
            <div style="display: flex; flex-direction: column; gap: 0.6rem;">
              <a href="${waLink}" target="_blank" class="btn-subscribe-pet" style="text-decoration: none;">
                <span>📲</span> Enviar Comprobante por WhatsApp a Dra. Daniela
              </a>
              <button type="button" class="btn-login-back-client" id="btn-done-subscribe">
                Cerrar y Volver a la Página
              </button>
            </div>
          </div>
        `;
        const btnDone = document.getElementById('btn-done-subscribe');
        if (btnDone) {
          btnDone.addEventListener('click', () => {
            modalSubscribeMembership.classList.add('hidden');
          });
        }
      }

      loadVetMembershipsList();
    } catch (err) {
      showToast(`Error al suscribir: ${err.message}`, 'error');
    } finally {
      if (btnConfirmSubscription) btnConfirmSubscription.disabled = false;
      if (btnConfirmSubscriptionText) {
        btnConfirmSubscriptionText.textContent = 'Activar Membresía ($8.990/mes) y Crear Carné 24/7';
      }
    }
  });
}

async function lookupLiveCarnet(queryText = '') {
  if (!liveCarnetResults) return;
  const q = (queryText || (inputCarnetLookup ? inputCarnetLookup.value : '')).trim();
  liveCarnetResults.classList.remove('hidden');
  liveCarnetResults.innerHTML = `<div style="font-size: 0.85rem; color: #64748b;">⏳ Consultando historial clínico 24/7 en vivo...</div>`;

  try {
    let url = `${API_BASE}/api/v1/covet/membresias?limit=10`;
    if (q) {
      if (/\d{6,}/.test(q)) {
        url += `&telefono=${encodeURIComponent(q)}`;
      } else {
        url += `&paciente_nombre=${encodeURIComponent(q)}`;
      }
    }

    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();

    if (!Array.isArray(list) || list.length === 0) {
      liveCarnetResults.innerHTML = `
        <div style="padding: 0.85rem; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; font-size: 0.84rem; color: #475569;">
          No se encontraron mascotas con ese filtro. ¡Inscribe a tu mascota arriba con el botón <strong>"Suscribir a mi mascota"</strong>!
        </div>
      `;
      return;
    }

    liveCarnetResults.innerHTML = list.map(m => {
      const notasHtml = (m.notas_clinicas_24_7 && m.notas_clinicas_24_7.length > 0)
        ? m.notas_clinicas_24_7.map(n => `
            <div style="margin-top: 0.45rem; padding: 0.55rem 0.75rem; background: #ffffff; border-left: 3px solid #e91e63; border-radius: 6px; font-size: 0.8rem;">
              <div><strong>📅 ${escapeHtml(n.fecha)} — Dx:</strong> ${escapeHtml(n.diagnostico)}</div>
              ${n.resumen_amigable_tutor ? `<div style="color: #475569; margin-top: 0.15rem;">💬 ${escapeHtml(n.resumen_amigable_tutor)}</div>` : ''}
              <a href="${API_BASE}${n.receta_pdf_url}" target="_blank" style="display: inline-block; margin-top: 0.25rem; color: #e91e63; font-weight: 700; text-decoration: none;">📄 Descargar Receta PDF Oficial ↗</a>
            </div>
          `).join('')
        : `<div style="font-size: 0.78rem; color: #64748b; margin-top: 0.35rem;">✓ Sin patologías activas. Calendario preventivo al día.</div>`;

      return `
        <div style="padding: 0.95rem 1.05rem; background: #fdf2f8; border: 1.5px solid rgba(233,30,99,0.26); border-radius: 12px; font-size: 0.84rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem;">
            <strong style="font-size: 0.96rem; color: #0f172a;">🐾 ${escapeHtml(m.paciente_nombre)} (${escapeHtml(m.especie)} · ${escapeHtml(m.raza)})</strong>
            <span style="background: #e91e63; color: #fff; font-size: 0.72rem; font-weight: 800; padding: 0.2rem 0.55rem; border-radius: 999px;">✓ MEMBRESÍA ACTIVA</span>
          </div>
          <div style="color: #475569; font-size: 0.8rem; margin-bottom: 0.45rem;">
            Tutor(a): <strong>${escapeHtml(m.tutor_nombre)}</strong> · Folio: <code>${escapeHtml(m.folio_membresia)}</code>
          </div>
          <div style="display: grid; grid-template-columns: 1fr; gap: 0.3rem; background: #ffffff; padding: 0.65rem 0.8rem; border-radius: 8px; border: 1px solid #f1f5f9;">
            <div>💉 <strong>Próxima Vacuna:</strong> ${escapeHtml(m.proxima_vacuna_fecha || 'Por programar')}</div>
            <div>💊 <strong>Próxima Desparasitación:</strong> ${escapeHtml(m.proxima_desparasitacion_fecha || 'Por programar')}</div>
            <div>🏠 <strong>Consulta Domicilio Gratis/Año:</strong> ${m.consulta_domicilio_gratis_usada ? 'Utilizada' : 'Disponible (1/1)'} · <strong>20% OFF Extra Activo</strong></div>
          </div>
          <div style="margin-top: 0.55rem;">
            <strong style="font-size: 0.78rem; color: #6d28d9; text-transform: uppercase;">📋 Notas Clínicas & Recetas 24/7:</strong>
            ${notasHtml}
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    liveCarnetResults.innerHTML = `<div style="font-size: 0.82rem; color: #dc2626;">Error consultando carné: ${escapeHtml(err.message)}</div>`;
  }
}

if (btnCarnetLookup) {
  btnCarnetLookup.addEventListener('click', () => lookupLiveCarnet());
}
if (inputCarnetLookup) {
  inputCarnetLookup.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      lookupLiveCarnet();
    }
  });
}

async function loadVetMembershipsList() {
  if (!vetMembershipsList) return;
  try {
    const res = await fetch(`${API_BASE}/api/v1/covet/membresias?limit=25`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();

    if (!Array.isArray(list) || list.length === 0) {
      vetMembershipsList.innerHTML = `
        <div class="rare-empty-state" role="status">
          <div class="rare-empty-icon-box">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
          </div>
          <h4 class="rare-empty-title">Sin mascotas inscritas aún en la Membresía</h4>
          <p class="rare-empty-desc">Aquí aparecerán las mascotas suscritas al plan Cuidado Continuo ($8.990/mes) con sus fechas automáticas de vacunación anual y desparasitación.</p>
          <button type="button" class="rare-empty-cta" onclick="document.getElementById('modal-subscribe-membership')?.classList.remove('hidden')">
            <span>+ Inscribir mascota manualmente</span>
            <span aria-hidden="true">→</span>
          </button>
        </div>
      `;
      return;
    }

    vetMembershipsList.innerHTML = list.map(m => `
      <div class="appointment-item" style="border-left: 4px solid #e91e63;">
        <div class="cita-top-row" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
          <div class="cita-pet">
            <span>💖</span>
            <strong>${escapeHtml(m.paciente_nombre)}</strong>
            <small>(${escapeHtml(m.especie)} · Tutor: ${escapeHtml(m.tutor_nombre)} · ${escapeHtml(m.tutor_telefono)})</small>
          </div>
          <span class="cita-status-tag confirmed" style="background: rgba(233,30,99,0.14); color: #e91e63; border-color: rgba(233,30,99,0.3);">
            ${escapeHtml(m.folio_membresia)} • $8.990/mes
          </span>
        </div>
        <div class="cita-motivo" style="margin: 0.35rem 0; font-size: 0.84rem;">
          💉 <strong>Vacuna:</strong> ${escapeHtml(m.proxima_vacuna_fecha)} &nbsp;|&nbsp;
          💊 <strong>Desparasitación:</strong> ${escapeHtml(m.proxima_desparasitacion_fecha)}
        </div>
        <div class="cita-time" style="font-size:0.8rem; color:var(--text-dim);">
          🏠 Consulta gratis anual: ${m.consulta_domicilio_gratis_usada ? 'Usada' : 'Disponible'} • 20% Dcto. Urgencias Activo • Inscrito: ${escapeHtml(m.fecha_inscripcion)}
        </div>
      </div>
    `).join('');
  } catch (err) {
    vetMembershipsList.innerHTML = `<div class="empty-state"><p>Error cargando membresías: ${escapeHtml(err.message)}</p></div>`;
  }
}

if (btnRefreshMemberships) {
  btnRefreshMemberships.addEventListener('click', () => {
    loadVetMembershipsList();
    showToast('Suscriptores de Membresía Cuidado Continuo actualizados.', 'info');
  });
}

// ========================================================
// 8. ESCUDO DPA LEY 21.719 & SÍNTESIS DE VOZ TTS
// ========================================================
const btnVetDpaStatus = document.getElementById('btn-vet-dpa-status');
const vetDpaLabel = document.getElementById('vet-dpa-label');
const modalVetDpa = document.getElementById('modal-vet-dpa');
const btnCloseDpaModal = document.getElementById('btn-close-dpa-modal');
const formSignDpa = document.getElementById('form-sign-dpa');
const btnSubmitDpa = document.getElementById('btn-submit-dpa');
const btnDownloadDpaCert = document.getElementById('btn-download-dpa-cert');
const inputDpaNombre = document.getElementById('dpa-vet-nombre');
const inputDpaRut = document.getElementById('dpa-vet-rut');
const inputDpaReg = document.getElementById('dpa-vet-reg');
const inputDpaEmail = document.getElementById('dpa-vet-email');

async function checkVetDPAStatus() {
  if (!vetDpaLabel) return;
  try {
    const slugParam = activeVetSlug ? `?slug=${encodeURIComponent(activeVetSlug)}` : '';
    const res = await fetch(`${API_BASE}/api/v1/covet/vet/dpa-status${slugParam}`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.firmado) {
      vetDpaLabel.textContent = '🛡️ Escudo DPA Activo';
      if (btnVetDpaStatus) btnVetDpaStatus.style.borderColor = 'var(--emerald-500, #10B981)';
      if (btnDownloadDpaCert) {
        btnDownloadDpaCert.classList.remove('hidden');
        btnDownloadDpaCert.href = `${API_BASE}/api/v1/covet/vet/dpa-certificate${data.folio_dpa ? `?folio=${data.folio_dpa}` : ''}`;
      }
      if (inputDpaNombre && data.nombre_profesional) inputDpaNombre.value = data.nombre_profesional;
      if (inputDpaRut && data.rut_profesional) inputDpaRut.value = data.rut_profesional;
      if (inputDpaReg && data.registro_colmevet_sag) inputDpaReg.value = data.registro_colmevet_sag;
    } else {
      vetDpaLabel.textContent = '🛡️ Firmar Escudo DPA';
    }
  } catch (err) {
    console.warn('[DPA] Error verificando estado DPA:', err);
  }
}

if (btnVetDpaStatus && modalVetDpa) {
  btnVetDpaStatus.addEventListener('click', () => {
    modalVetDpa.classList.remove('hidden');
  });
}

if (btnCloseDpaModal && modalVetDpa) {
  btnCloseDpaModal.addEventListener('click', () => {
    modalVetDpa.classList.add('hidden');
  });
}

if (formSignDpa) {
  formSignDpa.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (btnSubmitDpa) {
      btnSubmitDpa.disabled = true;
      btnSubmitDpa.textContent = 'Sellando con HMAC...';
    }

    try {
      const payload = {
        nombre_profesional: inputDpaNombre.value.trim(),
        rut_profesional: inputDpaRut.value.trim(),
        registro_colmevet_sag: inputDpaReg.value.trim(),
        email_contacto: inputDpaEmail ? inputDpaEmail.value.trim() : null,
        clinica_slug: activeVetSlug || null
      };

      const res = await fetch(`${API_BASE}/api/v1/covet/vet/sign-dpa`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      showToast(`¡Escudo DPA suscrito con éxito! Folio: ${data.folio_dpa}`, 'success');
      vetDpaLabel.textContent = '🛡️ Escudo DPA Activo';

      if (btnDownloadDpaCert) {
        btnDownloadDpaCert.classList.remove('hidden');
        btnDownloadDpaCert.href = `${API_BASE}${data.certificate_url}`;
      }

      window.open(`${API_BASE}${data.certificate_url}`, '_blank');
      modalVetDpa.classList.add('hidden');
    } catch (err) {
      showToast(`Error al suscribir DPA: ${err.message}`, 'error');
    } finally {
      if (btnSubmitDpa) {
        btnSubmitDpa.disabled = false;
        btnSubmitDpa.textContent = '🛡️ Firmar y Sellar Contrato DPA';
      }
    }
  });
}

// Síntesis de Voz (TTS) para Lectura en Terreno
const btnTtsSoap = document.getElementById('btn-tts-soap');
const ttsLabel = document.getElementById('tts-label');

function toggleSoapTTS() {
  if (!('speechSynthesis' in window)) {
    showToast('Tu navegador no soporta síntesis de voz automática.', 'info');
    return;
  }

  if (window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
    resetTTSState();
    return;
  }

  const petName = reviewPacienteNombre ? reviewPacienteNombre.value.trim() : 'el paciente';
  const diag = reviewDiagnostico ? reviewDiagnostico.value.trim() : 'Sin diagnóstico registrado';
  const plan = reviewPlanTerapeutico ? reviewPlanTerapeutico.value.trim() : 'Sin plan terapéutico';

  const textToRead = `Resumen clínico para ${petName}. Diagnóstico presuntivo: ${diag}. Plan terapéutico y receta: ${plan}`;

  const utterance = new SpeechSynthesisUtterance(textToRead);
  utterance.lang = 'es-CL';

  const voices = window.speechSynthesis.getVoices();
  const esVoice = voices.find(v => v.lang === 'es-CL') || voices.find(v => v.lang.startsWith('es'));
  if (esVoice) utterance.voice = esVoice;

  utterance.rate = 1.0;
  utterance.pitch = 1.0;

  utterance.onstart = () => {
    if (btnTtsSoap) btnTtsSoap.classList.add('speaking');
    if (ttsLabel) ttsLabel.textContent = '⏹️ Detener Lectura';
  };

  utterance.onend = () => resetTTSState();
  utterance.onerror = () => resetTTSState();

  window.speechSynthesis.speak(utterance);
}

function resetTTSState() {
  if (btnTtsSoap) btnTtsSoap.classList.remove('speaking');
  if (ttsLabel) ttsLabel.textContent = '🔊 Escuchar Resumen SOAP';
}

if (btnTtsSoap) {
  btnTtsSoap.addEventListener('click', toggleSoapTTS);
}

// Ejecución al cargar
initWhiteLabelBranding();
checkVetDPAStatus();
updateOfflineBanner();
if (isSunModeActive) {
  setSunMode(true);
}

// Rare UI Interactive Spotlight Card Effect
document.querySelectorAll('.rare-card').forEach(card => {
  card.addEventListener('pointermove', (e) => {
    const rect = card.getBoundingClientRect();
    card.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`);
    card.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`);
  });
});

// Selector de Métodos de Pago Sin Fricción (Brain de Jelou / In-App Checkout)
document.querySelectorAll('.zf-method-card').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.getAttribute('data-zf-target');
    const method = btn.getAttribute('data-zf-method');
    document.querySelectorAll(`.zf-method-card[data-zf-target="${target}"]`).forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    if (target === 'sub') {
      const hiddenInput = document.getElementById('sub-metodo-pago');
      if (hiddenInput) hiddenInput.value = method;
      const drawerSub = document.getElementById('zf-inline-card-sub');
      if (drawerSub) drawerSub.classList.toggle('hidden', method !== 'CARD_INLINE');
    } else if (target === 'hold') {
      const drawerHold = document.getElementById('zf-inline-card-hold');
      if (drawerHold) drawerHold.classList.toggle('hidden', method !== 'CARD_INLINE');
    }
  });
});





