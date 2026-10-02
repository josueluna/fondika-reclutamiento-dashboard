#!/usr/bin/env python3
"""Generador local de data/dashboard.json (contrato 1.2.0) a partir de hira-sync.json.

Uso recomendado (primero siempre el dry-run):

    python3 scripts/build_dashboard.py --input /ruta/copia/hira-sync.json --dry-run
    python3 scripts/build_dashboard.py --input /ruta/copia/hira-sync.json

Garantías:
- Solo Python stdlib; no depende del repositorio ATS ni llama APIs.
- La fuente se abre en modo lectura y se verifica su SHA-256 antes y después.
- Histórico (8 métricas de actividad): la unidad es la postulación a una vacante; cada métrica
  cuenta una vez por postulación, en su primera evidencia estructurada con fecha.
  Ausencia de evidencia != cero. Nunca se lee texto libre (resumen/nota) para inferir nada.
- Estado al corte (alCorte): etapa actual de cada postulación; independiente del histórico.
- Snapshot 1.1.0 (metricas.*): se conserva con su semántica original por compatibilidad;
  entrevistados/ofertas quedan obsoletos (metadata.metricasObsoletas) y se eliminarán en 2.0.0.
- Antes de escribir se valida el contrato, la privacidad y la línea base del diagnóstico.
  Si algo no cuadra, no se escribe nada (el modo normal ejecuta el mismo dry-run primero).
- Los logs solo contienen conteos agregados y fechas mínima/máxima.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1.2.0"
TIMEZONE = "America/Mexico_City"
TZ = ZoneInfo(TIMEZONE)
SOURCE = "Hira"
DIMENSION_SEMANTICS = "vacante_actual_al_generar"
# Métricas de actividad (historico.actividad), en el orden de la UI.
METRICS = ["nuevos", "rechazados", "contactados", "entrevistados_ch", "enviados_hm",
           "entrevistados_hm", "aprobados_hm", "contrataciones"]
# Acumulados 1.1.0 de estadoActual.vacantes[].metricas (compatibilidad; misma semántica que 1.1.0).
LEGACY_METRICS = ["contactados", "entrevistados", "ofertas", "contrataciones"]
OBSOLETE_METRICS = ["entrevistados", "ofertas"]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "dashboard.json"

# Hira EstatusVacante -> estatus del contrato.
STATUS_MAP = {"abierta": "Activa", "en_pausa": "Pausada", "cubierta": "Cubierta", "cerrada": "Cancelada"}
CLOSED_STATUSES = {"Cubierta", "Cancelada"}

# Catálogo canónico de etapas de Hira (src/lib/data-store.ts:ETAPAS), en orden.
CANONICAL_STAGES = [
    ("nuevo", "Nuevo"),
    ("por_contactar", "Por contactar"),
    ("contactado", "Contactado"),
    ("screening", "Screening"),
    ("entrevista_ch_agendada", "Entrevista CH agendada"),
    ("entrevista_ch_realizada", "Entrevista CH realizada"),
    ("validado_ch", "Validado por CH"),
    ("registro_pendiente", "Registro en plataforma pendiente"),
    ("registro_enviado", "Registro en plataforma enviado"),
    ("enviar_a_hm", "Enviar a Hiring Manager"),
    ("enviado_a_hm", "Enviado a Hiring Manager"),
    ("entrevista_hm_agendada", "Entrevista con HM agendada"),
    ("feedback_hm_pendiente", "Feedback de HM pendiente"),
    ("oferta", "Oferta / cierre"),
    ("contratado", "Contratado"),
    ("descartado", "Descartado"),
    ("no_interesado", "No interesado"),
]
CANONICAL_IDS = {sid for sid, _ in CANONICAL_STAGES}
TERMINAL_STAGES = {"contratado", "descartado", "no_interesado"}
FALLBACK_STAGE_LABEL = "Etapa sin nombre"
# Etapa custom de la organización que equivale a "Oferta rechazada". Se resuelve por la etiqueta
# configurada en organizationStageConfigs (configuración, no texto libre) y debe ser única.
REJECTED_OFFER_LABEL = "oferta rechazada"
STAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 1.1.0 (snapshot legacy): etapa destino canónica -> métrica. Solo la etapa exacta.
STAGE_METRIC = {"contactado": "contactados", "oferta": "ofertas", "contratado": "contrataciones"}

# 1.2.0 (actividad): entrada a la etapa destino canónica -> métrica. Solo la etapa exacta; nunca se
# infiere desde etapas posteriores. Las etapas custom de "Oferta rechazada" se agregan a rechazados.
ACTIVITY_STAGE_METRIC = {
    "contactado": "contactados",
    "entrevista_ch_agendada": "entrevistados_ch",
    "enviado_a_hm": "enviados_hm",
    "entrevista_hm_agendada": "entrevistados_hm",
    "contratado": "contrataciones",
    "descartado": "rechazados",
    "no_interesado": "rechazados",
}
# Hitos write-once que alimentan la actividad (misma lógica 1.1.0 de contactados/contrataciones).
ACTIVITY_MILESTONE_METRIC = {"fechaPrimerContacto": "contactados", "fechaContratacionAplicada": "contrataciones"}

# Entrevistas: tipo estructurado -> métrica de entrevista agendada.
INTERVIEW_KIND_METRIC = {"CH": "entrevistados_ch", "Hiring Manager": "entrevistados_hm", "HM": "entrevistados_hm"}
INTERVIEW_HM_TYPES = {"Hiring Manager", "HM"}

# Resultado positivo del Hiring Manager (valores estructurados de catálogo).
HM_POSITIVE_RESULTS = {"Avanza", "Aprobado"}
# Hitos HM confirmados (db.hmOperationalMilestones) con resultado positivo en su payload estructurado.
HM_POSITIVE_MILESTONES = {
    "profile_feedback_received": ("decision", {"advance"}),
    "hm_final_feedback_received": ("recommendation", {"advance"}),
    "hm_hiring_decision_received": ("hiringDecision", {"hire"}),
}

# Estado al corte: grupo -> etapas canónicas (las etapas custom de "Oferta rechazada" van a rechazado;
# cualquier otra etapa actual cae en "otros").
AL_CORTE_GROUPS = ["nuevo", "contactado", "entrevistaCh", "enviadoHm", "entrevistaHm", "contratado", "rechazado", "otros"]
AL_CORTE_STAGES = {
    "nuevo": ["nuevo"],
    "contactado": ["contactado"],
    "entrevistaCh": ["entrevista_ch_agendada", "entrevista_ch_realizada"],
    "enviadoHm": ["enviado_a_hm"],
    "entrevistaHm": ["entrevista_hm_agendada", "feedback_hm_pendiente"],
    "contratado": ["contratado"],
    "rechazado": ["descartado", "no_interesado"],
}
AL_CORTE_KEYS = AL_CORTE_GROUPS + ["fueraDeNuevo"]

# Hitos write-once de la postulación (backend/pipeline-milestones.mjs).
MILESTONE_METRIC = {
    "fechaPrimerContacto": "contactados",
    "fechaPrimeraEntrevistaRealizada": "entrevistados",
    "fechaOfertaRealizada": "ofertas",
    "fechaContratacionAplicada": "contrataciones",
}

# Reglas de entrevista realizada (backend/pipeline-milestones.mjs:pipelineInterviewDate).
INTERVIEW_DONE_STATUSES = {"Realizada", "Pendiente feedback"}
INTERVIEW_INVALID_ATTENDANCE = {"No se presentó", "no_show", "Canceló", "Reprogramó", "reprogramo"}

# Transiciones de etapa con destino: las únicas que alimentan las métricas.
AUDIT_STAGE_TYPES = {"application_stage_changed", "bulk_stage_transition"}
# Ciclo de vida de etapa auditado (diagnóstico): incluye descartes/reactivaciones, que no
# tienen etapa destino de las cuatro métricas. Solo informativo; no alimenta el histórico.
AUDIT_STAGE_LIFECYCLE_TYPES = AUDIT_STAGE_TYPES | {"application_discarded", "application_reactivated"}
EVENT_STAGE_TYPES = {"cambio_etapa", "bulk_stage_transition"}

# Línea base aprobada sobre la copia de producción 2026-10-01. Se compara antes de escribir.
# - metricas: regresión mensual dentro de `ventana` (mayo–septiembre). Los meses posteriores se
#   reportan como evidencia nueva, no como discrepancia.
# - totales: total por métrica con toda la evidencia <= generatedAt de esa copia (incluye octubre).
# - legacy: semántica 1.1.0 de los acumulados del snapshot; no debe cambiar silenciosamente.
# Contactados = 177 (no 187): 10 transiciones a Contactado de julio pertenecen a postulaciones
# eliminadas de Hira; quedan fuera por dimensionSemantics = vacante_actual_al_generar.
# entrevistados_ch = 99 / entrevistados_hm = 25: 2 postulaciones con entrevista CH y HM cuyo evento
# entrevista_agendada no trae entrevistaId son ambiguas y no cuentan (decisión aprobada).
# aprobados_hm = 22: un hito "advance" aislado, sin resultadoHM ni entrevista HM positiva, no cuenta.
EXPECTED_BASELINE = {
    "ventana": {"desde": "2026-05", "hasta": "2026-09"},
    "scope": {
        "postulaciones": 256,
        "eventos": 1425,
        "entrevistas": 136,
        "audit": 3605,
        "transicionesEtapa": 514,
    },
    "metricas": {
        "rechazados": {"2026-05": 3, "2026-06": 11, "2026-07": 26, "2026-08": 82, "2026-09": 25},
        "contactados": {"2026-05": 22, "2026-06": 63, "2026-07": 17, "2026-08": 15, "2026-09": 60},
        "aprobados_hm": {"2026-05": 1, "2026-06": 5, "2026-07": 11, "2026-08": 3, "2026-09": 2},
        "contrataciones": {"2026-05": 1, "2026-07": 7, "2026-08": 1, "2026-09": 1},
    },
    "totales": {
        "nuevos": 256, "rechazados": 147, "contactados": 177, "entrevistados_ch": 99,
        "enviados_hm": 40, "entrevistados_hm": 25, "aprobados_hm": 22, "contrataciones": 10,
    },
    "legacy": {
        "contactados": {"2026-05": 22, "2026-06": 63, "2026-07": 17, "2026-08": 15, "2026-09": 60},
        "entrevistados": {"2026-05": 12, "2026-06": 10, "2026-07": 21, "2026-08": 8, "2026-09": 9},
        "ofertas": {"2026-05": 1, "2026-06": 3, "2026-07": 1, "2026-09": 2},
        "contrataciones": {"2026-05": 1, "2026-07": 7, "2026-08": 1, "2026-09": 1},
    },
    "alCorte": {"postulaciones": 256, "nuevo": 19, "fueraDeNuevo": 237},
}

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d(?:[\s().-]?\d){9,}")


class BuildBlocked(Exception):
    """El snapshot no puede generarse sin revisión humana. Mensajes solo agregados."""

    def __init__(self, messages, report=None):
        super().__init__("; ".join(messages))
        self.messages = messages
        self.report = report or {}


class ValidationFailed(Exception):
    def __init__(self, messages):
        super().__init__("; ".join(messages))
        self.messages = messages


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

def to_local_date(value):
    """Fecha local (America/Mexico_City) de un valor de Hira, o None si no es inequívoca.

    - 'YYYY-MM-DD' ya es fecha local y se valida tal cual.
    - ISO con zona (Z u offset) se convierte a America/Mexico_City antes de truncar.
    - Datetime sin zona, texto libre ('Hoy') o valores inválidos -> None.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if ISO_DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if ISO_DATETIME_TZ.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ).date()
        except ValueError:
            return None
    return None


def parse_generated_at(value):
    if not ISO_DATETIME_TZ.match(value or ""):
        raise ValueError("--generated-at debe ser ISO 8601 con zona horaria, p. ej. 2026-10-01T18:00:00-06:00")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


# ---------------------------------------------------------------------------
# Fuente
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_source(path):
    with open(path, "rb") as fh:
        db = json.loads(fh.read().decode("utf-8"))
    if not isinstance(db, dict) or not isinstance(db.get("workspaces"), list) or not isinstance(db.get("audit"), list):
        raise BuildBlocked([
            "La fuente no tiene el formato de hira-sync.json (se requieren db.workspaces y db.audit). "
            "Los respaldos exportados desde el frontend (app/version/data) no contienen audit y no son válidos."
        ])
    return db


def _list(obj, key):
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, list) else []


def find_canonical_workspace(db, organization_id):
    """Réplica de backend/store.mjs:findWorkspaceForOrganization."""
    fallback = None
    for workspace in _list(db, "workspaces"):
        store = workspace.get("store")
        if not isinstance(store, dict) or store.get("currentOrganizationId") != organization_id:
            continue
        for m in _list(store, "organizationMemberships"):
            if (m.get("organizationId") == organization_id and m.get("role") == "owner"
                    and m.get("status") == "active" and m.get("userId") == workspace.get("ownerUserId")):
                return workspace
        if fallback is None:
            fallback = workspace
    return fallback


def select_organization(db, organization_id=None):
    if organization_id:
        workspace = find_canonical_workspace(db, organization_id)
        if workspace is None:
            raise BuildBlocked(["No existe workspace canónico para la organización indicada."])
        return organization_id, workspace
    candidates = {}
    for workspace in _list(db, "workspaces"):
        for org in _list(workspace.get("store") or {}, "organizations"):
            org_id = org.get("id")
            if org_id and org_id not in candidates and find_canonical_workspace(db, org_id) is not None:
                candidates[org_id] = org.get("name") or ""
    if len(candidates) != 1:
        listing = ", ".join(f"{k} ({v})" for k, v in sorted(candidates.items()))
        raise BuildBlocked([f"Se encontraron {len(candidates)} organizaciones con workspace canónico; "
                            f"indica --organization-id. Opciones: {listing or 'ninguna'}"])
    org_id = next(iter(candidates))
    return org_id, find_canonical_workspace(db, org_id)


def _in_org_or_legacy(entity, organization_id):
    org = entity.get("organizationId")
    return org == organization_id or org in (None, "")


def scope_entities(db, organization_id, workspace):
    """Universo del probe: entidades del workspace canónico de la organización (o legacy sin
    organizationId); db.audit filtrado estrictamente por organizationId."""
    store = workspace.get("store") or {}
    pick = lambda key: [e for e in _list(store, key) if isinstance(e, dict) and _in_org_or_legacy(e, organization_id)]
    applications = pick("postulaciones")
    app_ids = {a.get("id") for a in applications if a.get("id")}
    audit_org = [a for a in _list(db, "audit") if isinstance(a, dict) and a.get("organizationId") == organization_id]
    audit_apps = [a for a in audit_org if a.get("entityType") == "application" and a.get("entityId") in app_ids]
    stage_config = (store.get("organizationStageConfigs") or {}).get(organization_id) or {}
    # Hitos HM operativos (nivel raíz de hira-sync): estrictamente de la organización y de postulaciones en scope.
    hm_milestones = [m for m in _list(db, "hmOperationalMilestones")
                     if isinstance(m, dict) and m.get("organizationId") == organization_id and m.get("applicationId") in app_ids]
    return {
        "store": store,
        "vacancies": pick("vacantes"),
        "applications": applications,
        "interviews": pick("entrevistas"),
        "events": pick("eventos"),
        "audit": audit_org,
        "auditApplications": audit_apps,
        "hmMilestones": hm_milestones,
        "stageConfig": stage_config.get("stages") if isinstance(stage_config.get("stages"), list) else [],
    }


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------

def _norm_label(value):
    """Etiqueta de configuración normalizada (minúsculas, sin acentos, espacios colapsados)."""
    text = unicodedata.normalize("NFKD", value if isinstance(value, str) else "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.strip()).lower()


class StageResolver:
    """Resuelve IDs de etapa (canónicos y custom) desde organizationStageConfigs.

    - Un stage configurado con canonicalId del catálogo de Hira se trata como esa etapa canónica.
    - Las etapas custom se conservan con su ID; "Oferta rechazada" se identifica por su etiqueta
      configurada y debe ser única.
    - Problemas de configuración bloquean la generación (requieren revisión humana).
    """

    def __init__(self, stage_config):
        self.issues = []
        self.alias = {}
        self.configured = []
        seen = Counter()
        rejected = []
        for stage in stage_config:
            if not isinstance(stage, dict):
                continue
            sid = stage.get("id")
            if not isinstance(sid, str) or not STAGE_ID_RE.match(sid):
                self.issues.append("organizationStageConfigs contiene una etapa con id no válido.")
                continue
            seen[sid] += 1
            self.configured.append(sid)
            canonical = stage.get("canonicalId")
            if canonical not in (None, ""):
                if canonical not in CANONICAL_IDS:
                    self.issues.append("organizationStageConfigs contiene un canonicalId fuera del catálogo de Hira.")
                elif canonical != sid:
                    self.alias[sid] = canonical
            elif sid not in CANONICAL_IDS and _norm_label(stage.get("label")) == REJECTED_OFFER_LABEL:
                rejected.append(sid)
        dup = sum(n - 1 for n in seen.values() if n > 1)
        if dup:
            self.issues.append(f"organizationStageConfigs contiene {dup} id(s) de etapa duplicado(s).")
        if len(rejected) > 1:
            self.issues.append(f"{len(rejected)} etapas custom con etiqueta 'Oferta rechazada'; la equivalencia es ambigua.")
            rejected = []
        self.rejected_offer = set(rejected)

    def canon(self, stage_id):
        return self.alias.get(stage_id, stage_id)

    def known(self, stage_id):
        return stage_id in CANONICAL_IDS or stage_id in self.alias or stage_id in self.configured

    def activity_metric(self, stage_id):
        sid = self.canon(stage_id)
        if sid in self.rejected_offer:
            return "rechazados"
        return ACTIVITY_STAGE_METRIC.get(sid)

    def al_corte_group(self, stage_id):
        sid = self.canon(stage_id)
        if sid in self.rejected_offer:
            return "rechazado"
        for group, stages in AL_CORTE_STAGES.items():
            if sid in stages:
                return group
        return "otros"

    def al_corte_stages(self, catalog, current_stages=()):
        """metadata.etapasAlCorte: qué IDs de etapa alimentan cada grupo (otros = el resto del catálogo
        y cualquier etapa actual desconocida)."""
        groups = {g: list(AL_CORTE_STAGES.get(g, [])) for g in AL_CORTE_GROUPS}
        groups["rechazado"] += sorted(self.rejected_offer)
        assigned = {s for g in groups.values() for s in g}
        candidates = list(catalog) + [s for s in current_stages if isinstance(s, str) and s]
        order = {sid: (catalog.get(sid, (None, 10_000))[1], sid) for sid in candidates}
        for sid in sorted(set(candidates), key=lambda s: order[s]):
            if sid not in assigned and self.al_corte_group(sid) == "otros":
                groups["otros"].append(sid)
                assigned.add(sid)
        return groups


# ---------------------------------------------------------------------------
# Evidencia
# ---------------------------------------------------------------------------

def is_valid_interview(interview):
    """Fecha local de una entrevista realmente realizada, o None (port de pipelineInterviewDate)."""
    if (interview.get("estatus") or "") not in INTERVIEW_DONE_STATUSES:
        return None
    if (interview.get("asistencia") or "") in INTERVIEW_INVALID_ATTENDANCE:
        return None
    value = interview.get("fecha")
    if not isinstance(value, str) or not ISO_DATE.match(value):
        return None
    return to_local_date(value)


def audit_transition(entry):
    """(etapa destino, fecha local) de un audit de cambio de etapa, o None si es ambiguo."""
    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    stage_changed = entry.get("type") == "application_stage_changed"
    target = meta.get("to") if stage_changed else meta.get("targetStageId")
    origin = meta.get("from") if stage_changed else meta.get("previousStageId")
    day = to_local_date(entry.get("at"))
    if not isinstance(target, str) or not target or target == origin or day is None:
        return None
    return target, day


def event_transition(event):
    """(etapa destino, fecha local) de un evento de etapa, o None. Nunca se parsea resumen/texto libre."""
    meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    target = meta.get("stageId") if event.get("tipo") == "cambio_etapa" else meta.get("targetStageId")
    day = to_local_date(event.get("fecha"))
    if not isinstance(target, str) or not target or day is None:
        return None
    return target, day


def collect_stage_transitions(audit_apps, events, app_ids, stats):
    """Transiciones inequívocas (appId, etapa destino, fecha local) de postulaciones en scope.
    Solo se usan en memoria."""
    transitions = []
    for entry in audit_apps:
        if entry.get("type") not in AUDIT_STAGE_TYPES:
            continue
        parsed = audit_transition(entry)
        if parsed is None:
            stats["auditTransicionesAmbiguas"] += 1
            continue
        transitions.append((entry.get("entityId"), *parsed, "audit"))
    for event in events:
        if event.get("tipo") not in EVENT_STAGE_TYPES or event.get("postulacionId") not in app_ids:
            continue
        parsed = event_transition(event)
        if parsed is None:
            stats["eventosEtapaAmbiguos"] += 1
            continue
        stats["eventosEtapaConDestino"] += 1
        transitions.append((event.get("postulacionId"), *parsed, "eventos"))
    return transitions


def excluded_deleted_applications(audit_org, events, app_ids, resolver):
    """Trazabilidad agregada de postulaciones que ya no existen en el workspace canónico.

    Quedan fuera del histórico y del snapshot: con dimensionSemantics = vacante_actual_al_generar
    no tienen vacante actual, y no se reconstruye una dimensión con relatedVacancyId.
    Solo se reportan sus transiciones de etapa inequívocas hacia métricas de actividad.
    """
    deleted = {a.get("entityId") for a in audit_org
               if a.get("entityType") == "application" and a.get("entityId") and a.get("entityId") not in app_ids}
    deleted |= {e.get("postulacionId") for e in events
                if e.get("postulacionId") and e.get("postulacionId") not in app_ids}
    first = {}
    for entry in audit_org:
        if entry.get("type") in AUDIT_STAGE_TYPES and entry.get("entityId") in deleted:
            parsed = audit_transition(entry)
            metric = resolver.activity_metric(parsed[0]) if parsed else None
            if metric:
                key = (entry.get("entityId"), metric)
                first[key] = min(first.get(key, parsed[1]), parsed[1])
    for event in events:
        if event.get("tipo") in EVENT_STAGE_TYPES and event.get("postulacionId") in deleted:
            parsed = event_transition(event)
            metric = resolver.activity_metric(parsed[0]) if parsed else None
            if metric:
                key = (event.get("postulacionId"), metric)
                first[key] = min(first.get(key, parsed[1]), parsed[1])
    by_month = {m: Counter() for m in METRICS}
    for (_, metric), day in first.items():
        by_month[metric][day.strftime("%Y-%m")] += 1
    return {
        "postulacionesEliminadas": len(deleted),
        "porMetrica": {m: sum(c.values()) for m, c in by_month.items()},
        "porMetricaMes": {m: dict(sorted(c.items())) for m, c in by_month.items() if c},
        "motivo": "Excluidas: la postulación ya no existe en el workspace canónico "
                  "(dimensionSemantics = vacante_actual_al_generar).",
    }


def legacy_first_evidence(applications, interviews, transitions, resolver, stats):
    """Evidencia 1.1.0 (contactados/entrevistados/ofertas/contrataciones) para los acumulados del
    snapshot compatible. Semántica idéntica a 1.1.0; no alimenta historico.actividad."""
    first = {}
    sources = defaultdict(Counter)

    def offer(app_id, metric, day, source):
        sources[metric][source] += 1
        key = (app_id, metric)
        if key not in first or day < first[key]:
            first[key] = day

    for app in applications:
        for field, metric in MILESTONE_METRIC.items():
            if app.get(field) in (None, ""):
                continue
            day = to_local_date(app.get(field))
            if day is None:
                stats["hitosFechaNoValida"] += 1
                continue
            offer(app.get("id"), metric, day, f"hito:{field}")

    apps_by_id = {a.get("id"): a for a in applications}
    for interview in interviews:
        day = is_valid_interview(interview)
        if day is None:
            stats["entrevistasNoValidas"] += 1
            continue
        app = apps_by_id.get(interview.get("postulacionId"))
        # Vínculo estricto postulación + candidato + vacante (pipeline-milestones.mjs:linkedInterviews).
        if app is None or interview.get("candidatoId") != app.get("candidatoId") or interview.get("vacanteId") != app.get("vacanteId"):
            stats["entrevistasValidasSinVinculo"] += 1
            continue
        stats["entrevistasValidas"] += 1
        offer(app.get("id"), "entrevistados", day, "entrevista")

    for app_id, target, day, source in transitions:
        metric = STAGE_METRIC.get(resolver.canon(target))
        if metric and app_id in apps_by_id:
            offer(app_id, metric, day, f"transicion:{source}")

    return first, {m: dict(sorted(c.items())) for m, c in sorted(sources.items())}


def activity_first_evidence(scope, transitions, resolver, stats):
    """{(appId, métrica): primera fecha local} para las 8 métricas de actividad.

    Unidad: postulación a una vacante. Cada métrica cuenta una sola vez por postulación (primera
    evidencia histórica, aunque luego haya reactivaciones o repeticiones). Solo evidencia
    estructurada con fecha; nunca resumen/nota ni otro texto libre.
    """
    applications = scope["applications"]
    apps_by_id = {a.get("id"): a for a in applications}
    first = {}
    sources = defaultdict(Counter)

    def offer(app_id, metric, day, source):
        if app_id not in apps_by_id:
            return
        sources[metric][source] += 1
        key = (app_id, metric)
        if key not in first or day < first[key]:
            first[key] = day

    # nuevos (fechaCaptura) e hitos write-once de contactados/contrataciones (lógica 1.1.0).
    for app in applications:
        day = to_local_date(app.get("fechaCaptura"))
        if day is None:
            stats["capturaFechaNoValida"] += 1
        else:
            offer(app.get("id"), "nuevos", day, "fechaCaptura")
        for field, metric in ACTIVITY_MILESTONE_METRIC.items():
            day = to_local_date(app.get(field)) if app.get(field) not in (None, "") else None
            if day is not None:
                offer(app.get("id"), metric, day, f"hito:{field}")

    # Entrada exacta a la etapa destino (audit y eventos de etapa con destino estructurado).
    for app_id, target, day, source in transitions:
        metric = resolver.activity_metric(target)
        if metric:
            offer(app_id, metric, day, f"transicion:{source}")

    # rechazados: descarte auditado. fechaCierre nunca sustituye evidencia histórica.
    for entry in scope["auditApplications"]:
        if entry.get("type") == "application_discarded":
            day = to_local_date(entry.get("at"))
            if day is None:
                stats["descartesAuditSinFecha"] += 1
            else:
                offer(entry.get("entityId"), "rechazados", day, "audit:application_discarded")

    interviews_by_id = {i.get("id"): i for i in scope["interviews"] if i.get("id")}
    interviews_by_app = defaultdict(list)
    for interview in scope["interviews"]:
        interviews_by_app[interview.get("postulacionId")].append(interview)
    hm_positive_interview = {app_id for app_id, items in interviews_by_app.items()
                             if any(i.get("tipo") in INTERVIEW_HM_TYPES and i.get("resultado") in HM_POSITIVE_RESULTS
                                    for i in items)}
    # resultadoHM es la validación estructurada del resultado positivo actual (no necesariamente el
    # valor exacto en el momento del evento); la fecha siempre viene del evento/hito.
    hm_positive_result = {a.get("id") for a in applications if a.get("resultadoHM") in HM_POSITIVE_RESULTS}
    hm_confirmed = hm_positive_interview | hm_positive_result

    for event in scope["events"]:
        tipo, app_id = event.get("tipo"), event.get("postulacionId")
        if app_id not in apps_by_id or tipo not in ("descartado", "entrevista_agendada", "resultado_hm", "feedback_hm"):
            continue
        day = to_local_date(event.get("fecha"))
        if day is None:
            stats[f"evento_{tipo}_sinFecha"] += 1
            continue
        if tipo == "descartado":
            offer(app_id, "rechazados", day, "evento:descartado")
        elif tipo == "entrevista_agendada":
            interview_id = event.get("entrevistaId")
            if interview_id:
                interview = interviews_by_id.get(interview_id)
                if interview is None:
                    stats["agendadaEntrevistaInexistente"] += 1
                    continue
                if interview.get("postulacionId") != app_id:
                    stats["agendadaVinculoInconsistente"] += 1
                    continue
                link = "entrevistaId"
            else:
                # Sin entrevistaId: solo si la postulación tiene exactamente una entrevista registrada.
                linked = interviews_by_app.get(app_id, [])
                if len(linked) != 1:
                    stats["agendadaSinEntrevistaIdAmbigua" if linked else "agendadaSinEntrevistaIdSinEntrevista"] += 1
                    continue
                interview, link = linked[0], "postulacionId"
            metric = INTERVIEW_KIND_METRIC.get(interview.get("tipo"))
            if metric is None:
                stats["agendadaTipoDesconocido"] += 1
                continue
            offer(app_id, metric, day, f"evento:entrevista_agendada({link})")
        elif tipo == "resultado_hm":
            if app_id in hm_positive_result:
                offer(app_id, "aprobados_hm", day, "evento:resultado_hm+resultadoHM")
            else:
                stats["resultadoHmSinResultadoPositivo"] += 1
        elif tipo == "feedback_hm":
            if app_id in hm_positive_interview:
                offer(app_id, "aprobados_hm", day, "evento:feedback_hm+entrevistaHM")
            else:
                stats["feedbackHmSinEntrevistaPositiva"] += 1

    for milestone in scope["hmMilestones"]:
        rule = HM_POSITIVE_MILESTONES.get(milestone.get("type"))
        if rule is None:
            continue
        field, positive = rule
        payload = milestone.get("payload") if isinstance(milestone.get("payload"), dict) else {}
        if payload.get(field) not in positive:
            continue
        if milestone.get("status") != "confirmed" or milestone.get("voidedAt"):
            stats["hitoHmPositivoNoConfirmado"] += 1
            continue
        app_id = milestone.get("applicationId")
        if app_id not in hm_confirmed:
            stats["hitoHmPositivoSinConfirmacionEstructurada"] += 1
            continue
        day = to_local_date(milestone.get("occurredAt"))
        if day is None:
            stats["hitoHmSinFecha"] += 1
            continue
        offer(app_id, "aprobados_hm", day, f"hito:{milestone.get('type')}")

    return first, {m: dict(sorted(c.items())) for m, c in sorted(sources.items(), key=lambda kv: METRICS.index(kv[0]))}


def build_activity(first, apps_by_id):
    """Agrupa fecha × métrica × vacante actual. Los IDs de postulación no salen de aquí."""
    counts = Counter()
    for (app_id, metric), day in first.items():
        counts[(day.isoformat(), metric, apps_by_id[app_id].get("vacanteId"))] += 1
    order = {m: i for i, m in enumerate(METRICS)}
    return [
        {"fecha": fecha, "metrica": metric, "vacanteId": vac_id, "cantidad": n}
        for (fecha, metric, vac_id), n in sorted(counts.items(), key=lambda kv: (kv[0][0], order[kv[0][1]], kv[0][2]))
        if n >= 1
    ]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def stage_catalog(stage_config):
    """{stageId: (label, orden)} desde la configuración de la organización, con el catálogo canónico de respaldo."""
    catalog = {sid: (label, i) for i, (sid, label) in enumerate(CANONICAL_STAGES)}
    for stage in stage_config:
        if isinstance(stage, dict) and isinstance(stage.get("id"), str):
            label = stage.get("label") if isinstance(stage.get("label"), str) and stage.get("label").strip() else FALLBACK_STAGE_LABEL
            orden = stage.get("order") if isinstance(stage.get("order"), (int, float)) else len(catalog)
            catalog[stage["id"]] = (label, orden)
    return catalog


def snapshot_sets(applications, first, resolver):
    """Postulaciones que cuentan en los acumulados 1.1.0 del snapshot, por métrica legacy.

    Evidencia con fecha (hitos, transiciones, entrevistas) + etapa actual exacta para
    contactado/oferta/contratado. Entrevistados nunca usa la etapa actual.
    Se calcula desde las postulaciones, no desde historico.actividad.
    """
    sets = {m: set() for m in LEGACY_METRICS}
    for (app_id, metric) in first:
        sets[metric].add(app_id)
    for app in applications:
        metric = STAGE_METRIC.get(resolver.canon(app.get("etapa")))
        if metric:
            sets[metric].add(app.get("id"))
    return sets


def build_al_corte(apps, resolver):
    """Estado al corte: dónde está HOY cada postulación (etapa actual). Independiente del histórico.
    Cada postulación cae en exactamente un grupo; fueraDeNuevo = postulaciones - nuevo."""
    counts = Counter(resolver.al_corte_group(a.get("etapa")) for a in apps)
    al_corte = {g: counts.get(g, 0) for g in AL_CORTE_GROUPS}
    al_corte["fueraDeNuevo"] = len(apps) - al_corte["nuevo"]
    return al_corte


def build_vacancy_snapshot(vacancy, applications, sets, catalog, resolver):
    """Construye explícitamente el objeto de vacante (lista blanca de campos)."""
    apps = [a for a in applications if a.get("vacanteId") == vacancy.get("id")]
    ids = {a.get("id") for a in apps}
    estatus = STATUS_MAP[vacancy.get("estatus")]
    fecha_cierre = None
    if estatus in CLOSED_STATUSES:
        closed = to_local_date(vacancy.get("fechaCambioEstatus"))
        fecha_cierre = closed.isoformat() if closed else None

    stage_counts = Counter(a.get("etapa") for a in apps if a.get("etapa") not in TERMINAL_STAGES and a.get("etapa"))
    ordered = sorted(stage_counts, key=lambda sid: (catalog.get(sid, (None, 10_000))[1], sid))
    etapas = [
        {"id": sid, "label": catalog.get(sid, (FALLBACK_STAGE_LABEL,))[0], "orden": i, "cantidad": stage_counts[sid]}
        for i, sid in enumerate(ordered, start=1)
    ]
    return {
        "vacanteId": vacancy["id"],
        "reqId": vacancy["reqId"].strip(),
        "nombre": vacancy["titulo"].strip(),
        "area": vacancy["area"].strip(),
        "hiringManager": vacancy["hiringManager"].strip(),
        "estatus": estatus,
        "posiciones": int(vacancy["posiciones"]),
        "fechaApertura": to_local_date(vacancy.get("fechaApertura")).isoformat(),
        "fechaCierre": fecha_cierre,
        # Compatibilidad 1.1.0 (misma semántica). entrevistados/ofertas: obsoletos, se eliminan en 2.0.0.
        "metricas": {
            "postulaciones": len(apps),
            "activas": sum(1 for a in apps if a.get("etapa") not in TERMINAL_STAGES),
            "contactados": len(ids & sets["contactados"]),
            "entrevistados": len(ids & sets["entrevistados"]),
            "ofertas": len(ids & sets["ofertas"]),
            "contrataciones": len(ids & sets["contrataciones"]),
            # Sin evidencia confiable en hira-sync (ultimaActividad no es fecha): null, nunca 0.
            "feedbackPendiente": None,
            "sinMovimiento7d": None,
        },
        "alCorte": build_al_corte(apps, resolver),
        "etapas": etapas,
    }


def check_preconditions(vacancies, applications):
    """Problemas que requieren decisión humana. Solo conteos agregados."""
    issues = []
    vac_ids = {v.get("id") for v in vacancies}
    nonempty = lambda v, k: isinstance(v.get(k), str) and v.get(k).strip() != ""
    checks = [
        ("sin reqId", lambda v: not nonempty(v, "reqId")),
        ("sin vacanteId", lambda v: not nonempty(v, "id")),
        ("sin nombre (titulo)", lambda v: not nonempty(v, "titulo")),
        ("sin área", lambda v: not nonempty(v, "area")),
        ("sin hiringManager", lambda v: not nonempty(v, "hiringManager")),
        ("con estatus desconocido", lambda v: v.get("estatus") not in STATUS_MAP),
        ("con posiciones no enteras", lambda v: isinstance(v.get("posiciones"), bool) or not isinstance(v.get("posiciones"), int) or v.get("posiciones") < 0),
        ("con fechaApertura no válida", lambda v: to_local_date(v.get("fechaApertura")) is None),
    ]
    for label, predicate in checks:
        n = sum(1 for v in vacancies if predicate(v))
        if n:
            issues.append(f"{n} vacante(s) en scope {label}.")
    dup = sum(n - 1 for n in Counter(v.get("id") for v in vacancies).values() if n > 1)
    if dup:
        issues.append(f"{dup} vacanteId duplicado(s) en scope.")
    orphan = sum(1 for a in applications if a.get("vacanteId") not in vac_ids)
    if orphan:
        issues.append(f"{orphan} postulación(es) en scope apuntan a una vacante que no existe.")
    dup_apps = sum(n - 1 for n in Counter(a.get("id") for a in applications).values() if n > 1)
    if dup_apps:
        issues.append(f"{dup_apps} postulación(es) con id duplicado en scope.")
    return issues


# ---------------------------------------------------------------------------
# Construcción
# ---------------------------------------------------------------------------

def _coverage(days):
    return {"desdeEvidencia": min(days).isoformat(), "hastaEvidencia": max(days).isoformat()} if days else None


def _by_month(first, metrics):
    monthly = {m: Counter() for m in metrics}
    for (_, metric), day in first.items():
        monthly[metric][day.strftime("%Y-%m")] += 1
    return {m: dict(sorted(c.items())) for m, c in monthly.items()}


def build_dashboard(db, generated_at, organization_id=None):
    """Devuelve (dashboard, report, forbidden). report contiene únicamente agregados;
    forbidden son valores de la fuente que solo se usan en memoria para validar privacidad."""
    org_id, workspace = select_organization(db, organization_id)
    scope = scope_entities(db, org_id, workspace)
    applications, vacancies = scope["applications"], scope["vacancies"]
    stage_audit = [a for a in scope["auditApplications"] if a.get("type") in AUDIT_STAGE_TYPES]
    lifecycle_audit = [a for a in scope["audit"]
                       if a.get("entityType") == "application" and a.get("type") in AUDIT_STAGE_LIFECYCLE_TYPES]

    report = {
        "scope": {
            "postulaciones": len(applications),
            "eventos": len(scope["events"]),
            "entrevistas": len(scope["interviews"]),
            "audit": len(scope["audit"]),
            # Transiciones con destino de postulaciones en scope: lo que considera el generador.
            "transicionesEtapa": len(stage_audit),
            "transicionesEtapaPostulaciones": len({a.get("entityId") for a in stage_audit}),
            "transicionesEtapaPorTipo": dict(sorted(Counter(a.get("type") for a in stage_audit).items())),
            # Diagnóstico: ciclo de etapa auditado en toda la organización (incluye descartes,
            # reactivaciones y postulaciones eliminadas). No es el total de cambios de etapa del generador.
            "eventosCicloEtapaAuditados": len(lifecycle_audit),
            "eventosCicloEtapaPostulaciones": len({a.get("entityId") for a in lifecycle_audit}),
            "eventosCicloEtapaPorTipo": dict(sorted(Counter(a.get("type") for a in lifecycle_audit).items())),
            "hitosHm": len(scope["hmMilestones"]),
            "vacantes": len(vacancies),
            "legacySinOrganizationId": sum(1 for k in ("vacancies", "applications", "interviews", "events")
                                           for e in scope[k] if e.get("organizationId") in (None, "")),
        }
    }

    resolver = StageResolver(scope["stageConfig"])
    issues = check_preconditions(vacancies, applications) + resolver.issues
    if issues:
        raise BuildBlocked(issues, report)

    stats = Counter()
    app_ids = {a.get("id") for a in applications}
    transitions = collect_stage_transitions(scope["auditApplications"], scope["events"], app_ids, stats)
    first, sources = activity_first_evidence(scope, transitions, resolver, stats)
    legacy_first, legacy_sources = legacy_first_evidence(applications, scope["interviews"], transitions, resolver, stats)
    unknown_current = sorted({a.get("etapa") for a in applications if not resolver.known(a.get("etapa"))}, key=str)
    if unknown_current:
        stats["postulacionesEtapaActualDesconocida"] = sum(1 for a in applications if not resolver.known(a.get("etapa")))
    report["evidencia"] = {**dict(sorted(stats.items())), "fuentes": sources, "fuentesLegacy": legacy_sources}
    report["excluidas"] = excluded_deleted_applications(scope["audit"], scope["events"], app_ids, resolver)
    report["etapas"] = {
        "ofertaRechazadaResuelta": len(resolver.rejected_offer),
        "aliasCanonicos": len(resolver.alias),
        "etapasActualesDesconocidas": len(unknown_current),
    }

    if not first:
        raise BuildBlocked(["No existe evidencia histórica válida; no se puede definir cobertura."], report)

    generated_day = generated_at.date()
    late = sum(1 for d in list(first.values()) + list(legacy_first.values()) if d > generated_day)
    if late:
        raise BuildBlocked([f"{late} evidencia(s) con fecha posterior a generatedAt."], report)

    apps_by_id = {a.get("id"): a for a in applications}
    activity = build_activity(first, apps_by_id)
    days_by_metric = {m: [d for (_, metric), d in first.items() if metric == m] for m in METRICS}
    coverage_by_metric = {m: _coverage(days_by_metric[m]) for m in METRICS}

    catalog = stage_catalog(scope["stageConfig"])
    sets = snapshot_sets(applications, legacy_first, resolver)
    vacantes = sorted((build_vacancy_snapshot(v, applications, sets, catalog, resolver) for v in vacancies),
                      key=lambda v: (v["reqId"], v["vacanteId"]))

    dashboard = {
        "schemaVersion": SCHEMA_VERSION,
        "metadata": {
            "generatedAt": generated_at.isoformat(timespec="seconds"),
            "timezone": TIMEZONE,
            "source": SOURCE,
            "dimensionSemantics": DIMENSION_SEMANTICS,
            "cobertura": _coverage(list(first.values())),
            "metricas": list(METRICS),
            "metricasObsoletas": list(OBSOLETE_METRICS),
            "coberturaPorMetrica": coverage_by_metric,
            "etapasAlCorte": resolver.al_corte_stages(catalog, [a.get("etapa") for a in applications]),
        },
        "estadoActual": {"vacantes": vacantes},
        "historico": {"actividad": activity},
    }

    por_mes = _by_month(first, METRICS)
    report["historico"] = {
        "porMes": por_mes,
        "totales": {m: sum(c.values()) for m, c in por_mes.items()},
        "registros": len(activity),
    }
    legacy_mes = _by_month(legacy_first, LEGACY_METRICS)
    report["legacy"] = {"porMes": legacy_mes, "totales": {m: sum(c.values()) for m, c in legacy_mes.items()}}
    report["cobertura"] = dashboard["metadata"]["cobertura"]
    report["coberturaPorMetrica"] = coverage_by_metric
    al_corte = {k: sum(v["alCorte"][k] for v in vacantes) for k in AL_CORTE_KEYS}
    report["snapshot"] = {
        "vacantes": len(vacantes),
        "vacantesPorEstatus": dict(sorted(Counter(v["estatus"] for v in vacantes).items())),
        "acumuladosLegacy": {m: sum(v["metricas"][m] for v in vacantes) for m in LEGACY_METRICS},
        "alCorte": {"postulaciones": sum(v["metricas"]["postulaciones"] for v in vacantes), **al_corte},
    }
    return dashboard, report, collect_forbidden_values(db, vacancies, catalog)


# ---------------------------------------------------------------------------
# Validaciones
# ---------------------------------------------------------------------------

TOP_KEYS = {"schemaVersion", "metadata", "estadoActual", "historico"}
METADATA_KEYS = {"generatedAt", "timezone", "source", "dimensionSemantics", "cobertura", "metricas",
                 "metricasObsoletas", "coberturaPorMetrica", "etapasAlCorte"}
VACANCY_KEYS = {"vacanteId", "reqId", "nombre", "area", "hiringManager", "estatus", "posiciones",
                "fechaApertura", "fechaCierre", "metricas", "alCorte", "etapas"}
SNAPSHOT_REQUIRED = ["postulaciones", "activas", "contrataciones"]
SNAPSHOT_NULLABLE = ["contactados", "entrevistados", "ofertas", "feedbackPendiente", "sinMovimiento7d"]
STAGE_KEYS = {"id", "label", "orden", "cantidad"}
ACTIVITY_KEYS = {"fecha", "metrica", "vacanteId", "cantidad"}
COVERAGE_KEYS = {"desdeEvidencia", "hastaEvidencia"}


def _is_count(x, minimum=0):
    return isinstance(x, int) and not isinstance(x, bool) and x >= minimum


def _is_date(x):
    return isinstance(x, str) and ISO_DATE.match(x) is not None and to_local_date(x) is not None


def _is_coverage(c):
    return isinstance(c, dict) and set(c) == COVERAGE_KEYS and _is_date(c.get("desdeEvidencia")) \
        and _is_date(c.get("hastaEvidencia")) and c["desdeEvidencia"] <= c["hastaEvidencia"]


def validate_contract(d):
    errors = []
    err = errors.append
    if not isinstance(d, dict) or set(d) != TOP_KEYS:
        raise ValidationFailed(["Claves de primer nivel distintas al contrato."])
    if d["schemaVersion"] != SCHEMA_VERSION:
        err(f"schemaVersion debe ser {SCHEMA_VERSION}.")
    m = d["metadata"]
    if not isinstance(m, dict) or set(m) != METADATA_KEYS:
        raise ValidationFailed(["metadata no coincide con el contrato."])
    if not isinstance(m["generatedAt"], str) or not ISO_DATETIME_TZ.match(m["generatedAt"]):
        err("generatedAt debe incluir zona horaria.")
    if m["timezone"] != TIMEZONE:
        err("timezone debe ser America/Mexico_City.")
    if m["source"] != SOURCE or m["dimensionSemantics"] != DIMENSION_SEMANTICS:
        err("source/dimensionSemantics no coinciden con el contrato.")
    if m["metricas"] != METRICS:
        err("metadata.metricas debe ser exactamente el catálogo permitido, en el orden de la UI.")
    if m["metricasObsoletas"] != OBSOLETE_METRICS:
        err("metadata.metricasObsoletas debe ser exactamente entrevistados, ofertas.")
    if not _is_coverage(m["cobertura"]):
        raise ValidationFailed(errors + ["cobertura incompleta o con fechas no válidas."])
    desde, hasta = m["cobertura"]["desdeEvidencia"], m["cobertura"]["hastaEvidencia"]
    generated_day = parse_generated_at(m["generatedAt"]).date().isoformat() if ISO_DATETIME_TZ.match(m["generatedAt"] or "") else None
    if generated_day and hasta > generated_day:
        err("cobertura.hastaEvidencia es posterior a generatedAt.")

    cpm = m["coberturaPorMetrica"]
    if not isinstance(cpm, dict) or set(cpm) != set(METRICS):
        raise ValidationFailed(errors + ["coberturaPorMetrica debe tener exactamente las métricas del catálogo."])
    for metric, c in cpm.items():
        if c is not None and not _is_coverage(c):
            err(f"coberturaPorMetrica.{metric} debe ser {{desdeEvidencia, hastaEvidencia}} válido o null.")
    valid_cpm = [c for c in cpm.values() if _is_coverage(c)]
    if valid_cpm and (min(c["desdeEvidencia"] for c in valid_cpm) != desde or max(c["hastaEvidencia"] for c in valid_cpm) != hasta):
        err("cobertura global debe ir de la primera a la última evidencia de coberturaPorMetrica.")

    eac = m["etapasAlCorte"]
    if not isinstance(eac, dict) or list(eac) != AL_CORTE_GROUPS:
        err("etapasAlCorte debe tener exactamente los grupos al corte, en orden.")
    else:
        listed = [sid for g in eac.values() if isinstance(g, list) for sid in g]
        if any(not isinstance(g, list) for g in eac.values()) or any(not isinstance(x, str) or not x for x in listed):
            err("etapasAlCorte: cada grupo debe ser una lista de IDs de etapa.")
        elif len(listed) != len(set(listed)):
            err("etapasAlCorte: una etapa no puede alimentar dos grupos.")
        for group, stages in AL_CORTE_STAGES.items():
            if isinstance(eac.get(group), list) and not set(stages) <= set(eac[group]):
                err(f"etapasAlCorte.{group} no incluye sus etapas aprobadas.")

    vacantes = (d["estadoActual"] or {}).get("vacantes") if isinstance(d["estadoActual"], dict) else None
    if not isinstance(vacantes, list) or set(d["estadoActual"]) != {"vacantes"}:
        raise ValidationFailed(errors + ["estadoActual.vacantes no es una lista."])
    ids = set()
    for i, v in enumerate(vacantes):
        where = f"vacante #{i + 1}"
        if not isinstance(v, dict) or set(v) != VACANCY_KEYS:
            err(f"{where}: campos distintos al contrato.")
            continue
        for k in ("vacanteId", "reqId", "nombre", "area", "hiringManager"):
            if not isinstance(v[k], str) or not v[k].strip():
                err(f"{where}: {k} debe ser texto no vacío.")
        if v["vacanteId"] in ids:
            err(f"{where}: vacanteId duplicado.")
        ids.add(v["vacanteId"])
        if v["estatus"] not in STATUS_MAP.values():
            err(f"{where}: estatus fuera del catálogo.")
        if not _is_count(v["posiciones"]):
            err(f"{where}: posiciones debe ser entero ≥ 0.")
        if not _is_date(v["fechaApertura"]):
            err(f"{where}: fechaApertura no válida.")
        if v["fechaCierre"] is not None and not _is_date(v["fechaCierre"]):
            err(f"{where}: fechaCierre debe ser fecha o null.")
        if v["estatus"] not in CLOSED_STATUSES and v["fechaCierre"] is not None:
            err(f"{where}: fechaCierre solo aplica a vacantes cerradas.")
        vm = v["metricas"]
        metricas_ok = isinstance(vm, dict) and set(vm) == set(SNAPSHOT_REQUIRED + SNAPSHOT_NULLABLE)
        if not metricas_ok:
            err(f"{where}: metricas distintas al contrato.")
        else:
            for k in SNAPSHOT_REQUIRED:
                if not _is_count(vm[k]):
                    err(f"{where}: metricas.{k} debe ser entero ≥ 0 (null no permitido).")
            for k in SNAPSHOT_NULLABLE:
                if vm[k] is not None and not _is_count(vm[k]):
                    err(f"{where}: metricas.{k} debe ser entero ≥ 0 o null.")
        ac = v["alCorte"]
        if not isinstance(ac, dict) or list(ac) != AL_CORTE_KEYS or not all(_is_count(ac[k]) for k in AL_CORTE_KEYS):
            err(f"{where}: alCorte debe tener los grupos al corte y fueraDeNuevo como enteros ≥ 0.")
        elif metricas_ok and _is_count(vm["postulaciones"]):
            if sum(ac[g] for g in AL_CORTE_GROUPS) != vm["postulaciones"]:
                err(f"{where}: la suma de alCorte no coincide con metricas.postulaciones.")
            if ac["fueraDeNuevo"] != vm["postulaciones"] - ac["nuevo"]:
                err(f"{where}: alCorte.fueraDeNuevo debe ser postulaciones - nuevo.")
        if not isinstance(v["etapas"], list):
            err(f"{where}: etapas debe ser lista.")
        else:
            for e in v["etapas"]:
                if not isinstance(e, dict) or set(e) != STAGE_KEYS or not isinstance(e["id"], str) \
                        or not isinstance(e["label"], str) or not _is_count(e["orden"]) or not _is_count(e["cantidad"]):
                    err(f"{where}: etapa con estructura no válida.")

    actividad = d["historico"].get("actividad") if isinstance(d["historico"], dict) else None
    if not isinstance(actividad, list) or set(d["historico"]) != {"actividad"}:
        raise ValidationFailed(errors + ["historico.actividad no es una lista."])
    keys = set()
    hist_sum = Counter()
    for r in actividad:
        if not isinstance(r, dict) or set(r) != ACTIVITY_KEYS:
            err("Registro histórico con campos distintos al contrato.")
            continue
        if r["metrica"] not in METRICS:
            err(f"Métrica fuera del catálogo: {r['metrica']}")
        if not _is_count(r["cantidad"], 1):
            err(f"cantidad debe ser entero ≥ 1 ({r['fecha']} · {r['metrica']}).")
        if r["vacanteId"] not in ids:
            err(f"historico.actividad referencia una vacante inexistente ({r['fecha']} · {r['metrica']}).")
        if not _is_date(r["fecha"]):
            err("Fecha no válida en historico.actividad.")
        else:
            c = cpm.get(r["metrica"]) if r["metrica"] in METRICS else None
            if not _is_coverage(c) or not (c["desdeEvidencia"] <= r["fecha"] <= c["hastaEvidencia"]):
                err(f"Fecha fuera de la cobertura de la métrica: {r['fecha']} · {r['metrica']}.")
        k = (r["fecha"], r["metrica"], r["vacanteId"])
        if k in keys:
            err(f"Registro duplicado fecha+métrica+vacante: {r['fecha']} · {r['metrica']}.")
        keys.add(k)
        if _is_count(r["cantidad"]):
            hist_sum[(r["vacanteId"], r["metrica"])] += r["cantidad"]
    for metric, c in cpm.items():
        has_records = any(r.get("metrica") == metric for r in actividad if isinstance(r, dict))
        if (c is None) == has_records:
            err(f"coberturaPorMetrica.{metric} no corresponde con sus registros históricos.")
    for v in vacantes:
        if not isinstance(v, dict):
            continue
        # Las postulaciones de una vacante acotan cualquier métrica de actividad (una vez por postulación).
        postulaciones = (v.get("metricas") or {}).get("postulaciones")
        for metric in METRICS:
            if _is_count(postulaciones) and hist_sum[(v.get("vacanteId"), metric)] > postulaciones:
                err(f"Actividad {metric} de una vacante supera sus postulaciones.")
        # El acumulado 1.1.0 es un superconjunto de la evidencia fechada de la misma métrica.
        for metric in set(METRICS) & set(LEGACY_METRICS):
            snap = (v.get("metricas") or {}).get(metric)
            if snap is not None and _is_count(snap) and snap < hist_sum[(v.get("vacanteId"), metric)]:
                err(f"Acumulado {metric} de una vacante es menor que su evidencia histórica.")
    if errors:
        raise ValidationFailed(errors)


PII_KEY_RE = re.compile(r"(nombre|apellido|email|correo|tel[eé]fono|phone|celular|linkedin)", re.I)
PHONE_KEY_RE = re.compile(r"(tel[eé]fono|phone|celular|whatsapp)", re.I)
ID_COLLECTIONS = ["candidatos", "postulaciones", "entrevistas", "eventos", "seguimientos", "documentos", "notas",
                  "publicaciones", "teamInvitations", "organizationMemberships"]
FREE_TEXT_KEYS = ["nota", "notas", "resumen", "comentarios", "comentariosHM", "comentariosCierre", "observacionesIniciales",
                  "notasCH", "notasOrigen", "resumenParaHm", "fortalezas", "riesgosHm", "riesgosDetectados"]
MIN_FREE_TEXT = 12


def _norm(value):
    return re.sub(r"\s+", " ", value.strip()).lower()


def collect_forbidden_values(db, vacancies=(), catalog=None):
    """Valores de la fuente para validar privacidad (solo en memoria; nunca se imprimen).

    - candidate: datos individuales (IDs, nombres, emails, teléfonos). Siempre prohibidos;
      ninguna allowlist los anula.
    - freeTexts: textos libres de notas/comentarios. Prohibidos salvo que el valor de salida sea
      exactamente una dimensión de proceso conocida de la fuente (allowedDimensions).
    """
    ids, names, phones, emails, free_texts = set(), set(), set(), set(), set()
    for coll in ("users", "sessions", "invitations", "audit", "emailMessages", "hmFeedbackRequests"):
        for e in _list(db, coll):
            if isinstance(e, dict):
                if isinstance(e.get("id"), str):
                    ids.add(e["id"])
                if isinstance(e.get("email"), str) and e["email"].strip():
                    emails.add(e["email"].strip().lower())
    for workspace in _list(db, "workspaces"):
        store = workspace.get("store") or {}
        for coll in ID_COLLECTIONS:
            for e in _list(store, coll):
                if not isinstance(e, dict):
                    continue
                if isinstance(e.get("id"), str):
                    ids.add(e["id"])
                if coll == "candidatos":
                    parts = [str(e.get(k) or "").strip() for k in ("nombre", "apellidoPaterno", "apellidoMaterno")]
                    for full in (" ".join(parts), " ".join(parts[:2])):
                        if " " in full.strip():
                            names.add(_norm(full))
                    for k, val in e.items():
                        if isinstance(val, str) and PII_KEY_RE.search(k) and "@" in val:
                            emails.add(val.strip().lower())
                        if isinstance(val, str) and PHONE_KEY_RE.search(k):
                            digits = re.sub(r"\D", "", val)
                            if len(digits) >= 8:
                                phones.add(digits)
                for k in FREE_TEXT_KEYS:
                    v = e.get(k)
                    if isinstance(v, str) and len(v.strip()) >= MIN_FREE_TEXT:
                        free_texts.add(_norm(v))
    allowed = set()
    for v in vacancies:
        for k in ("titulo", "area", "hiringManager", "reqId"):
            if isinstance(v.get(k), str) and v.get(k).strip():
                allowed.add(_norm(v[k]))
    for label, _ in (catalog or stage_catalog([])).values():
        allowed.add(_norm(label))
    allowed |= {_norm(x) for x in STATUS_MAP.values()} | {_norm(x) for x in METRICS + LEGACY_METRICS + AL_CORTE_KEYS} | {
        _norm(TIMEZONE), _norm(SOURCE), _norm(DIMENSION_SEMANTICS), _norm(SCHEMA_VERSION), _norm(FALLBACK_STAGE_LABEL)}
    return {"candidate": {"ids": ids, "names": names, "phones": phones, "emails": emails},
            "freeTexts": free_texts, "allowedDimensions": allowed}


def _string_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _string_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _string_values(v)
    elif isinstance(obj, str):
        yield obj


def candidate_data_hits(s, candidate, vac_ids):
    """Categorías de dato individual encontradas en s. Independiente de cualquier allowlist."""
    hits = []
    low = _norm(s)
    if EMAIL_RE.search(s) or any(e in low for e in candidate["emails"]):
        hits.append("email")
    if s not in vac_ids and (s in candidate["ids"] or any(len(i) >= 8 and i in s for i in candidate["ids"])):
        hits.append("ID individual (candidato/postulación/entrevista/evento/audit/usuario)")
    if any(n in low for n in candidate["names"]):
        hits.append("nombre de candidato")
    digits = re.sub(r"\D", "", s)
    if digits and any(p in digits for p in candidate["phones"]):
        hits.append("teléfono de candidato")
    return hits


def validate_privacy(dashboard, forbidden):
    """Ningún dato de candidato/postulación en la salida. Mensajes sin revelar el valor encontrado.

    1. Datos de candidato: siempre prohibidos, en cualquier campo, aunque coincidan con una dimensión.
    2. Texto libre de la fuente: solo se tolera cuando el valor de salida es exactamente una
       dimensión de proceso conocida (título, área, HM, reqId, etiqueta de etapa, constantes).
    """
    errors = Counter()
    vac_ids = {v["vacanteId"] for v in dashboard["estadoActual"]["vacantes"]}
    dimension_text = [s for v in dashboard["estadoActual"]["vacantes"]
                      for s in [v["nombre"], v["area"], v["hiringManager"], v["reqId"]] + [e["label"] for e in v["etapas"]]]
    for s in _string_values(dashboard):
        for kind in candidate_data_hits(s, forbidden["candidate"], vac_ids):
            errors[kind] += 1
        low = _norm(s)
        if any(t in low for t in forbidden["freeTexts"]) and low not in forbidden["allowedDimensions"]:
            errors["texto libre de la fuente (notas/comentarios)"] += 1
    for s in dimension_text:
        if PHONE_RE.search(s):
            errors["teléfono"] += 1
    serialized = json.dumps(dashboard, ensure_ascii=False)
    for key in ("candidatoId", "candidateId", "postulacionId", "applicationId", "entrevistaId", "interviewId",
                "eventId", "auditId", "email", "telefono", "notas", "comentarios"):
        if f'"{key}"' in serialized:
            errors[f"campo prohibido {key}"] += 1
    if errors:
        raise ValidationFailed([f"Privacidad: {n} valor(es) con {kind}." for kind, n in sorted(errors.items())])


def _compare_months(label, expected, got_months, in_window, window_start, diffs):
    got = {m: n for m, n in (got_months or {}).items() if in_window(m)}
    for month in sorted(set(expected) | set(got)):
        e, g = expected.get(month, 0), got.get(month, 0)
        if e != g:
            where = month if not window_start or month >= window_start else f"{month} (antes de la ventana)"
            diffs.append(f"{label} {where}: esperado {e}, obtenido {g} (Δ {g - e:+d})")
    e_total, g_total = sum(expected.values()), sum(got.values())
    if e_total != g_total:
        diffs.append(f"{label} total ventana: esperado {e_total}, obtenido {g_total} (Δ {g_total - e_total:+d})")


def compare_with_baseline(report, baseline):
    """Diferencias contra la línea base aprobada. Lista vacía = coincide exactamente.

    - scope / alCorte / totales: comparación exacta.
    - metricas (actividad) y legacy (snapshot 1.1.0): mes a mes dentro de la ventana. Los meses
      posteriores a baseline.ventana.hasta son evidencia nueva (new_evidence_after_baseline).
    Solo se comparan las métricas presentes en la línea base.
    """
    diffs = []
    window = baseline.get("ventana") or {}
    window_end, window_start = window.get("hasta"), window.get("desde")
    in_window = lambda month: (not window_end or month <= window_end)
    for k, expected in (baseline.get("scope") or {}).items():
        got = report.get("scope", {}).get(k)
        if got != expected:
            diffs.append(f"scope.{k}: esperado {expected}, obtenido {got}")
    for k, expected in (baseline.get("cobertura") or {}).items():
        got = (report.get("cobertura") or {}).get(k)
        if got != expected:
            diffs.append(f"cobertura.{k}: esperado {expected}, obtenido {got}")
    got_months = (report.get("historico") or {}).get("porMes", {})
    for metric, expected in (baseline.get("metricas") or {}).items():
        _compare_months(metric, expected, got_months.get(metric), in_window, window_start, diffs)
    got_totals = (report.get("historico") or {}).get("totales", {})
    for metric, expected in (baseline.get("totales") or {}).items():
        got = got_totals.get(metric)
        if got != expected:
            diffs.append(f"{metric} total: esperado {expected}, obtenido {got}")
    legacy_months = (report.get("legacy") or {}).get("porMes", {})
    for metric, expected in (baseline.get("legacy") or {}).items():
        _compare_months(f"legacy {metric}", expected, legacy_months.get(metric), in_window, window_start, diffs)
    al_corte = (report.get("snapshot") or {}).get("alCorte", {})
    for k, expected in (baseline.get("alCorte") or {}).items():
        if al_corte.get(k) != expected:
            diffs.append(f"alCorte.{k}: esperado {expected}, obtenido {al_corte.get(k)}")
    return diffs


def new_evidence_after_baseline(report, baseline):
    """{métrica: {mes: n}} con evidencia de actividad posterior a la ventana de la línea base."""
    window_end = (baseline.get("ventana") or {}).get("hasta")
    if not window_end:
        return {}
    got_months = (report.get("historico") or {}).get("porMes", {})
    out = {}
    for metric in METRICS:
        later = {m: n for m, n in got_months.get(metric, {}).items() if m > window_end}
        if later:
            out[metric] = later
    return out


# ---------------------------------------------------------------------------
# Salida y CLI
# ---------------------------------------------------------------------------

def write_atomic(path, dashboard):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dashboard.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dashboard, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def print_report(report, out):
    p = lambda *a: print(*a, file=out)
    s = report.get("scope", {})
    if s:
        p("Scope:")
        for k in ("postulaciones", "eventos", "entrevistas", "audit", "hitosHm", "vacantes", "legacySinOrganizationId"):
            p(f"  {k}: {s.get(k)}")
        p(f"  Transiciones de etapa consideradas por el generador: {s.get('transicionesEtapa')} "
          f"({s.get('transicionesEtapaPostulaciones')} postulaciones) {s.get('transicionesEtapaPorTipo')}")
        p(f"  Eventos auditados relacionados con ciclo de etapa (diagnóstico): "
          f"{s.get('eventosCicloEtapaAuditados')} ({s.get('eventosCicloEtapaPostulaciones')} postulaciones) "
          f"{s.get('eventosCicloEtapaPorTipo')}")
    if report.get("etapas"):
        p(f"Etapas: {report['etapas']}")
    ev = report.get("evidencia")
    if ev:
        p("Evidencia:")
        for k, v in ev.items():
            if k not in ("fuentes", "fuentesLegacy"):
                p(f"  {k}: {v}")
        for metric, src in ev.get("fuentes", {}).items():
            p(f"  fuentes {metric}: {src}")
        for metric, src in ev.get("fuentesLegacy", {}).items():
            p(f"  fuentes legacy {metric}: {src}")
    h = report.get("historico")
    if h:
        p("Actividad (primera evidencia por postulación × métrica):")
        for metric in METRICS:
            months = " ".join(f"{m}={n}" for m, n in h["porMes"][metric].items()) or "sin evidencia"
            p(f"  {metric}: total {h['totales'][metric]} | {months}")
        p(f"  registros fecha×métrica×vacante: {h['registros']}")
    lg = report.get("legacy")
    if lg:
        p("Snapshot 1.1.0 (evidencia legacy, compatibilidad):")
        for metric in LEGACY_METRICS:
            months = " ".join(f"{m}={n}" for m, n in lg["porMes"][metric].items()) or "sin evidencia"
            p(f"  {metric}: total {lg['totales'][metric]} | {months}")
    ex = report.get("excluidas")
    if ex:
        p("Postulaciones eliminadas (fuera de scope, excluidas del histórico y del snapshot):")
        p(f"  detectadas: {ex['postulacionesEliminadas']}")
        p(f"  con transición inequívoca por métrica: {ex['porMetrica']}")
        if ex["porMetricaMes"]:
            p(f"  por métrica y mes: {ex['porMetricaMes']}")
        p(f"  motivo: {ex['motivo']}")
    if report.get("cobertura"):
        c = report["cobertura"]
        p(f"Cobertura: {c['desdeEvidencia']} → {c['hastaEvidencia']}")
    for metric, c in (report.get("coberturaPorMetrica") or {}).items():
        p(f"  {metric}: " + (f"{c['desdeEvidencia']} → {c['hastaEvidencia']}" if c else "sin evidencia (null)"))
    if report.get("snapshot"):
        sn = report["snapshot"]
        p(f"Snapshot: {sn['vacantes']} vacantes {sn['vacantesPorEstatus']} | acumulados legacy {sn['acumuladosLegacy']}")
        ac = sn["alCorte"]
        pct = f"{ac['fueraDeNuevo'] / ac['postulaciones'] * 100:.1f}%" if ac["postulaciones"] else "—"
        p(f"Estado al corte: {ac}")
        p(f"Fuera de Nuevo: {ac['fueraDeNuevo']} / {ac['postulaciones']} ({pct}); {ac['nuevo']} permanecen en Nuevo")


def run(args, out=sys.stdout):
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if output_path == input_path or output_path.name == "dashboard.sample.json":
        print("ERROR: la salida no puede ser la fuente ni dashboard.sample.json.", file=out)
        return 2
    try:
        generated_at = parse_generated_at(args.generated_at) if args.generated_at else datetime.now(TZ).replace(microsecond=0)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=out)
        return 2
    baseline = EXPECTED_BASELINE
    if args.expected:
        with open(args.expected, "rb") as fh:
            baseline = json.loads(fh.read().decode("utf-8"))

    mode = "DRY-RUN (no se escribe nada)" if args.dry_run else "GENERACIÓN (dry-run completo antes de escribir)"
    print(f"Modo: {mode}", file=out)
    print(f"generatedAt: {generated_at.isoformat(timespec='seconds')} ({TIMEZONE})", file=out)
    hash_before = sha256_file(input_path)
    try:
        db = load_source(input_path)
        dashboard, report, forbidden = build_dashboard(db, generated_at, args.organization_id)
    except BuildBlocked as exc:
        print_report(exc.report, out)
        print("\nDETENIDO — requiere revisión:", file=out)
        for msg in exc.messages:
            print(f"  - {msg}", file=out)
        return 1
    print_report(report, out)

    failures = []
    try:
        validate_contract(dashboard)
    except ValidationFailed as exc:
        failures += [f"Contrato: {m}" for m in exc.messages]
    try:
        validate_privacy(dashboard, forbidden)
    except ValidationFailed as exc:
        failures += exc.messages
    diffs = compare_with_baseline(report, baseline)
    new_evidence = new_evidence_after_baseline(report, baseline)

    if sha256_file(input_path) != hash_before:
        failures.append("La fuente cambió durante la ejecución; resultado descartado.")

    print(f"\nValidación de contrato {SCHEMA_VERSION} y privacidad: " + ("OK" if not failures else "FALLÓ"), file=out)
    for f in failures:
        print(f"  - {f}", file=out)
    print("Comparación contra diagnóstico: " + ("COINCIDE" if not diffs else f"{len(diffs)} DIFERENCIA(S)"), file=out)
    for d in diffs:
        print(f"  - {d}", file=out)
    window = baseline.get("ventana") or {}
    if window:
        print(f"Ventana comparada: {window.get('desde')} a {window.get('hasta')}", file=out)
        print("Evidencia nueva posterior a la ventana: "
              + (json.dumps(new_evidence, ensure_ascii=False) if new_evidence else "ninguna"), file=out)

    if failures or diffs:
        print("\nNo se escribió data/dashboard.json.", file=out)
        return 1
    if args.dry_run:
        print("\nDry-run OK. Ejecuta sin --dry-run para escribir el archivo.", file=out)
        return 0
    write_atomic(output_path, dashboard)
    print(f"\nEscrito: {output_path} ({len(dashboard['estadoActual']['vacantes'])} vacantes, "
          f"{len(dashboard['historico']['actividad'])} registros históricos).", file=out)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"Genera data/dashboard.json (contrato {SCHEMA_VERSION}) desde una copia de hira-sync.json.")
    parser.add_argument("--input", required=True, help="Ruta a una copia (read-only) de hira-sync.json.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Destino (por defecto data/dashboard.json).")
    parser.add_argument("--organization-id", help="Organización a generar (obligatoria si hay más de una).")
    parser.add_argument("--generated-at", help="Momento del snapshot ISO 8601 con zona (por defecto: ahora).")
    parser.add_argument("--expected", help="JSON con la línea base a comparar (por defecto la del diagnóstico).")
    parser.add_argument("--dry-run", action="store_true", help="Valida y compara sin escribir (primer paso recomendado).")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
