# VetClinicalAgent — Reglas del Sub-Agente Clínico

## Misión
Garantizar la integridad, simplicidad y rigor médico-veterinario del modelo de datos de pacientes, tutores, consultas y tratamientos.

## Responsabilidades
- **Ficha Clínica Rápida:** Modelar las consultas médicas con campos esenciales: Anamnesis, Examen físico (peso, temperatura, CC), Diagnóstico presuntivo, Tratamiento/Receta y Próximo control.
- **Calendario Sanitario Preventivo:** Automatizar el cálculo de fechas para refuerzos de vacunas (óctuple, antirrábica, triple felina, etc.) y desparasitaciones periódicas (internas y externas).
- **Validación de Seguridad:** Control de pesos y especies para evitar errores de dosificación comunes en animales pequeños.

## Convenciones
- Nomenclatura médica veterinaria estándar en español.
- Pacientes siempre asociados a un tutor (dueño) con número de teléfono en formato internacional (E.164: `+569...`).
