"""Pruebas del generador con datos 100% sintéticos (ningún dato real de Hira).

    python3 -m unittest discover -s tests -v
"""

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_dashboard as bd  # noqa: E402

ORG = "org-sintetica"
GENERATED_AT = bd.parse_generated_at("2026-10-01T18:00:00-06:00")


def vacancy(vid, req, estatus="abierta", **extra):
    v = {"id": vid, "organizationId": ORG, "reqId": req, "titulo": f"Puesto {req}", "area": "Operaciones",
         "hiringManager": "Gerente Sintético", "estatus": estatus, "posiciones": 1, "fechaApertura": "2026-04-01"}
    v.update(extra)
    return v


def candidate(cid, n):
    return {"id": cid, "organizationId": ORG, "nombre": f"Nombreprueba{n}", "apellidoPaterno": f"Apellidoprueba{n}",
            "email": f"persona{n}@correo-sintetico.test", "telefono": f"55 1234 56{n:02d}",
            "observacionesIniciales": f"Observación privada sintética número {n}"}


def application(aid, cid, vid, etapa="nuevo", **extra):
    a = {"id": aid, "organizationId": ORG, "candidatoId": cid, "vacanteId": vid, "etapa": etapa,
         "fechaCaptura": "2026-01-15T10:00:00Z", "ultimaActividad": "Hoy", "fechaEntradaEtapa": "2026-09-30T10:00:00Z",
         "notasOrigen": "Nota privada de origen sintética"}
    a.update(extra)
    return a


def stage_audit(aid, vid, to, at, frm="nuevo", n=[0]):
    n[0] += 1
    return {"id": f"aud_sint_{n[0]:06d}", "type": "application_stage_changed", "organizationId": ORG, "userId": "usr-sint-1",
            "entityType": "application", "entityId": aid, "relatedVacancyId": vid, "at": at,
            "metadata": {"from": frm, "to": to}}


def event(aid, vid, tipo, fecha, metadata=None, n=[0]):
    n[0] += 1
    e = {"id": f"evt_sint_{n[0]:06d}", "organizationId": ORG, "postulacionId": aid, "vacanteId": vid, "tipo": tipo,
         "resumen": "Cambio de etapa: Nuevo → Contactado", "usuario": "reclutador@correo-sintetico.test", "fecha": fecha}
    if metadata is not None:
        e["metadata"] = metadata
    return e


def interview(iid, aid, cid, vid, fecha, estatus="Realizada", asistencia="Asistió", tipo="CH", resultado=None):
    return {"id": iid, "organizationId": ORG, "postulacionId": aid, "candidatoId": cid, "vacanteId": vid,
            "fecha": fecha, "estatus": estatus, "asistencia": asistencia, "tipo": tipo, "resultado": resultado,
            "notas": "Notas privadas de entrevista"}


def discard_audit(aid, vid, at, n=[0]):
    n[0] += 1
    return {"id": f"aud_sint_disc_{n[0]:06d}", "type": "application_discarded", "organizationId": ORG,
            "entityType": "application", "entityId": aid, "relatedVacancyId": vid, "at": at,
            "metadata": {"applicationId": aid, "vacancyId": vid}}


def hm_milestone(aid, vid, mtype, payload, occurred_at, status="confirmed", n=[0]):
    n[0] += 1
    return {"id": f"hm_ms_sint_{n[0]:06d}", "organizationId": ORG, "vacancyId": vid, "applicationId": aid,
            "candidateId": "cand-x", "type": mtype, "payload": {"type": mtype, **payload}, "occurredAt": occurred_at,
            "recordedAt": occurred_at, "status": status, "voidedAt": occurred_at if status == "voided" else None,
            "metadata": {"note": "Nota privada del hito"}}


REJECTED_OFFER_ID = "custom_stage_00000000-0000-4000-8000-000000000001"
STAGE_CONFIG = [
    {"id": "nuevo", "canonicalId": "nuevo", "label": "Nuevo", "order": 1},
    {"id": "contactado", "canonicalId": "contactado", "label": "Contactado", "order": 2},
    {"id": "custom_stage_00000000-0000-4000-8000-000000000002", "label": "Candidato finalista", "order": 10},
    {"id": REJECTED_OFFER_ID, "label": "Oferta rechazada", "order": 15},
    {"id": "custom_stage_00000000-0000-4000-8000-000000000003", "label": "Documentación solicitada", "order": 16},
]


def make_db(vacancies=None, candidates=None, applications=None, interviews=None, events=None, audit=None,
            milestones=None, stage_config=None):
    return {
        "schemaVersion": 1,
        "users": [{"id": "usr-sint-1", "email": "reclutador@correo-sintetico.test"}],
        "workspaces": [{
            "id": "ws-sint-1", "ownerUserId": "usr-sint-1",
            "store": {
                "currentOrganizationId": ORG,
                "organizations": [{"id": ORG, "name": "Organización sintética"}],
                "organizationMemberships": [{"organizationId": ORG, "userId": "usr-sint-1", "role": "owner", "status": "active"}],
                "vacantes": vacancies if vacancies is not None else [vacancy("vac-a", "REQ-2026-0001")],
                "candidatos": candidates or [],
                "postulaciones": applications or [],
                "entrevistas": interviews or [],
                "eventos": events or [],
                "organizationStageConfigs": {ORG: {"stages": stage_config if stage_config is not None else STAGE_CONFIG}},
            },
        }],
        "audit": audit or [],
        "hmOperationalMilestones": milestones or [],
    }


def build(db):
    dashboard, report, forbidden = bd.build_dashboard(db, GENERATED_AT)
    bd.validate_contract(dashboard)
    bd.validate_privacy(dashboard, forbidden)
    return dashboard, report


def activity(dashboard):
    return [(r["fecha"], r["metrica"], r["vacanteId"], r["cantidad"]) for r in dashboard["historico"]["actividad"]]


def metric_records(dashboard, metric):
    return [r for r in activity(dashboard) if r[1] == metric]


def without_nuevos(dashboard):
    return [r for r in activity(dashboard) if r[1] != "nuevos"]


def legacy_first(db):
    """Evidencia 1.1.0 del snapshot compatible: {(appId, métrica): 'YYYY-MM-DD'}."""
    org, ws = bd.select_organization(db)
    scope = bd.scope_entities(db, org, ws)
    resolver = bd.StageResolver(scope["stageConfig"])
    stats = bd.Counter()
    ids = {a["id"] for a in scope["applications"]}
    transitions = bd.collect_stage_transitions(scope["auditApplications"], scope["events"], ids, stats)
    candidates_by_id = {c["id"]: c for c in scope["candidates"]}
    first, _ = bd.legacy_first_evidence(scope["applications"], scope["interviews"], transitions, resolver, stats,
                                        candidates_by_id)
    return {k: d.isoformat() for k, d in first.items()}


def vac(dashboard, vid="vac-a"):
    return [v for v in dashboard["estadoActual"]["vacantes"] if v["vacanteId"] == vid][0]


class EvidenceRules(unittest.TestCase):
    def test_A_two_contactado_transitions_count_first_only(self):
        db = make_db(candidates=[candidate("cand-1", 1)],
                     applications=[application("app-1", "cand-1", "vac-a", "screening")],
                     audit=[stage_audit("app-1", "vac-a", "contactado", "2026-06-03T16:00:00Z"),
                            stage_audit("app-1", "vac-a", "contactado", "2026-06-01T16:00:00Z", frm="por_contactar")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "contactados"), [("2026-06-01", "contactados", "vac-a", 1)])

    def test_A2_audit_and_event_for_same_application_dedupe_to_minimum(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening")],
                     audit=[stage_audit("app-1", "vac-a", "contactado", "2026-06-05T16:00:00Z")],
                     events=[event("app-1", "vac-a", "cambio_etapa", "2026-06-02T16:00:00Z", {"stageId": "contactado"})])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "contactados"), [("2026-06-02", "contactados", "vac-a", 1)])

    def test_B_two_valid_interviews_count_first_only(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening")],
                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-07-10"),
                                 interview("int-2", "app-1", "cand-1", "vac-a", "2026-07-02", estatus="Pendiente feedback")])
        build(db)
        self.assertEqual(legacy_first(db), {("app-1", "entrevistados"): "2026-07-02"})

    def test_B2_milestone_and_interview_keep_minimum(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening", fechaPrimeraEntrevistaRealizada="2026-05-11")],
                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-05-20")])
        d, _ = build(db)
        self.assertEqual(legacy_first(db), {("app-1", "entrevistados"): "2026-05-11"})
        self.assertEqual(vac(d)["metricas"]["entrevistados"], 1)
        self.assertNotIn("entrevistados", {r[1] for r in activity(d)})  # obsoleta: fuera de historico

    def test_B3_unrealized_interviews_do_not_count(self):
        bad = [("No-show", "Pendiente"), ("Cancelada", "Canceló"), ("Reprogramada", "Reprogramó"),
               ("Agendada", "Pendiente"), ("Realizada", "No se presentó"), ("Realizada", "no_show"), ("Realizada", "reprogramo")]
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "entrevista_ch_agendada"),
                                   application("app-2", "cand-2", "vac-a", "contactado", fechaPrimerContacto="2026-06-01T15:00:00Z")],
                     interviews=[interview(f"int-{i}", "app-1", "cand-1", "vac-a", "2026-07-01", e, a) for i, (e, a) in enumerate(bad)])
        d, r = build(db)
        self.assertNotIn(("app-1", "entrevistados"), legacy_first(db))
        self.assertEqual(vac(d)["metricas"]["entrevistados"], 0)
        self.assertEqual(r["evidencia"]["entrevistasNoValidas"], len(bad))

    def test_C_hired_without_offer_evidence_does_not_invent_offer(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "contratado",
                                                fechaContratacionAplicada="2026-07-20T17:00:00Z")])
        d, _ = build(db)
        self.assertNotIn(("app-1", "ofertas"), legacy_first(db))
        self.assertEqual(metric_records(d, "contrataciones"), [("2026-07-20", "contrataciones", "vac-a", 1)])
        m = d["estadoActual"]["vacantes"][0]["metricas"]
        self.assertEqual((m["ofertas"], m["contrataciones"], m["contactados"]), (0, 1, 0))

    def test_D_email_enviado_is_not_contactado(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "por_contactar"),
                                   application("app-2", "cand-2", "vac-a", "nuevo", fechaOfertaRealizada="2026-08-01T15:00:00Z")],
                     events=[event("app-1", "vac-a", "email_enviado", "2026-06-01T16:00:00Z"),
                             event("app-1", "vac-a", "comunicacion_envio_registrado_manual", "2026-06-02T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "contactados"), [])
        self.assertEqual(d["estadoActual"]["vacantes"][0]["metricas"]["contactados"], 0)

    def test_E_contacto_events_are_not_contactado(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "por_contactar"),
                                   application("app-2", "cand-2", "vac-a", "nuevo", fechaOfertaRealizada="2026-08-01T15:00:00Z")],
                     events=[event("app-1", "vac-a", "contacto_whatsapp", "2026-06-01T16:00:00Z", {"stageId": "contactado"}),
                             event("app-1", "vac-a", "contacto_llamada", "2026-06-02T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "contactados"), [])

    def test_stage_event_without_destination_is_ambiguous(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening"),
                                   application("app-2", "cand-2", "vac-a", "nuevo", fechaOfertaRealizada="2026-08-01T15:00:00Z")],
                     events=[event("app-1", "vac-a", "cambio_etapa", "2026-06-01T16:00:00Z")])  # solo resumen, sin metadata
        d, r = build(db)
        self.assertEqual(metric_records(d, "contactados"), [])
        self.assertEqual(r["evidencia"]["eventosEtapaAmbiguos"], 1)

    def test_application_created_in_contactado_has_no_history_but_counts_in_snapshot(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "contactado"),
                                   application("app-2", "cand-2", "vac-a", "oferta"),
                                   application("app-3", "cand-3", "vac-a", "entrevista_hm_agendada"),
                                   application("app-4", "cand-4", "vac-a", "nuevo", fechaPrimerContacto="2026-06-01T15:00:00Z")])
        d, _ = build(db)
        self.assertEqual(without_nuevos(d), [("2026-06-01", "contactados", "vac-a", 1)])
        m = d["estadoActual"]["vacantes"][0]["metricas"]
        self.assertEqual((m["contactados"], m["ofertas"], m["entrevistados"], m["contrataciones"]), (2, 1, 0, 0))

    def test_later_stage_never_implies_earlier_metric(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "oferta",
                                                fechaOfertaRealizada="2026-08-03T15:00:00Z")])
        d, _ = build(db)
        self.assertEqual(legacy_first(db), {("app-1", "ofertas"): "2026-08-03"})
        self.assertEqual(without_nuevos(d), [])  # oferta no implica contactado/entrevista/aprobación
        self.assertEqual(d["estadoActual"]["vacantes"][0]["metricas"]["contactados"], 0)


class Aggregation(unittest.TestCase):
    def setUp(self):
        # Semana 2026-06-01..07 con actividad; 2026-06-08..14 sin actividad; 2026-06-15.. solo contactados.
        cap = {"fechaCaptura": "2026-06-01T15:00:00Z"}
        self.db = make_db(
            vacancies=[vacancy("vac-a", "REQ-2026-0001"), vacancy("vac-b", "REQ-2026-0002")],
            applications=[application("app-1", "cand-1", "vac-a", "screening", fechaPrimerContacto="2026-06-02T15:00:00Z", **cap),
                          application("app-2", "cand-2", "vac-a", "screening", fechaPrimerContacto="2026-06-02T20:00:00Z", **cap),
                          application("app-3", "cand-3", "vac-b", "screening", fechaPrimerContacto="2026-06-02T15:00:00Z", **cap),
                          application("app-4", "cand-4", "vac-b", "screening", fechaPrimerContacto="2026-06-16T15:00:00Z", **cap)],
            interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-04")])

    def test_F_week_without_activity_has_no_records(self):
        d, _ = build(self.db)
        self.assertFalse([r for r in activity(d) if "2026-06-08" <= r[0] <= "2026-06-14"])
        self.assertTrue(all(r[3] >= 1 for r in activity(d)))

    def test_G_metric_without_activity_in_active_week_has_no_record(self):
        d, _ = build(self.db)
        week3 = [r for r in activity(d) if "2026-06-15" <= r[0] <= "2026-06-21"]
        self.assertEqual({r[1] for r in week3}, {"contactados"})

    def test_H_two_vacancies_same_day_are_separate_and_summed(self):
        d, _ = build(self.db)
        self.assertIn(("2026-06-02", "contactados", "vac-a", 2), activity(d))
        self.assertIn(("2026-06-02", "contactados", "vac-b", 1), activity(d))

    def test_I_january_april_without_evidence_is_absent(self):
        d, _ = build(self.db)  # vacantes abiertas en abril, sin evidencia antes de junio
        self.assertFalse([r for r in activity(d) if r[0] < "2026-06-01"])
        self.assertEqual(d["metadata"]["cobertura"], {"desdeEvidencia": "2026-06-01", "hastaEvidencia": "2026-06-16"})
        cpm = d["metadata"]["coberturaPorMetrica"]
        self.assertEqual(cpm["contactados"], {"desdeEvidencia": "2026-06-02", "hastaEvidencia": "2026-06-16"})
        self.assertIsNone(cpm["aprobados_hm"])  # métrica sin evidencia: null, nunca cero

    def test_coverage_ignores_workspace_updates(self):
        db = copy.deepcopy(self.db)
        db["workspaces"][0]["updatedAt"] = "2026-09-30T12:00:00Z"
        db["audit"].append({"id": "aud_sint_x", "type": "store_saved", "organizationId": ORG, "at": "2026-09-30T12:00:00Z"})
        d, _ = build(db)
        self.assertEqual(d["metadata"]["cobertura"]["hastaEvidencia"], "2026-06-16")

    def test_output_is_deterministic(self):
        a, _ = build(copy.deepcopy(self.db))
        b, _ = build(copy.deepcopy(self.db))
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))


class PrivacyAndContract(unittest.TestCase):
    def full_db(self):
        return make_db(
            vacancies=[vacancy("vac-a", "REQ-2026-0001"),
                       vacancy("vac-c", "REQ-2026-0003", estatus="cubierta", fechaCambioEstatus="2026-07-01T03:00:00Z")],
            candidates=[candidate("cand-1", 1), candidate("cand-2", 2)],
            applications=[application("app-1", "cand-1", "vac-a", "contactado", fechaPrimerContacto="2026-06-01T15:00:00Z"),
                          application("app-2", "cand-2", "vac-c", "contratado", fechaContratacionAplicada="2026-06-20T15:00:00Z")],
            interviews=[interview("int-1", "app-2", "cand-2", "vac-c", "2026-06-10")],
            events=[event("app-1", "vac-a", "cambio_etapa", "2026-06-01T15:00:00Z", {"stageId": "contactado"})],
            audit=[stage_audit("app-2", "vac-c", "contratado", "2026-06-20T15:00:00Z", frm="oferta")])

    def test_J_personal_data_never_reaches_output(self):
        d, _ = build(self.full_db())
        text = json.dumps(d, ensure_ascii=False)
        for needle in ("cand-1", "app-1", "int-1", "evt_sint", "aud_sint", "Nombreprueba", "Apellidoprueba",
                       "correo-sintetico", "1234", "privada", "usr-sint"):
            self.assertNotIn(needle, text)

    def test_J2_privacy_validation_rejects_leaks(self):
        db = self.full_db()
        dashboard, _, forbidden = bd.build_dashboard(db, GENERATED_AT)
        leaks = {"hiringManager": "Nombreprueba1 Apellidoprueba1", "area": "persona@correo.test",
                 "nombre": "Llamar al 55 1234 5601", "reqId": "app-1"}
        for field, value in leaks.items():
            tampered = copy.deepcopy(dashboard)
            tampered["estadoActual"]["vacantes"][0][field] = value
            with self.assertRaises(bd.ValidationFailed, msg=field):
                bd.validate_privacy(tampered, forbidden)
        tampered = copy.deepcopy(dashboard)
        tampered["historico"]["actividad"][0]["applicationId"] = "x"
        with self.assertRaises(bd.ValidationFailed):
            bd.validate_contract(tampered)

    def test_K_generated_at_and_dates_in_mexico_city(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening")],
                     audit=[stage_audit("app-1", "vac-a", "contactado", "2026-06-02T03:30:00Z")])  # 21:30 del 1-jun en CDMX
        d, _ = build(db)
        self.assertEqual(d["metadata"]["generatedAt"], "2026-10-01T18:00:00-06:00")
        self.assertEqual(d["metadata"]["timezone"], "America/Mexico_City")
        self.assertEqual(metric_records(d, "contactados"), [("2026-06-01", "contactados", "vac-a", 1)])
        self.assertEqual(bd.parse_generated_at("2026-10-02T00:30:00Z").isoformat(), "2026-10-01T18:30:00-06:00")

    def test_L_closed_vacancy_is_kept(self):
        d, _ = build(self.full_db())
        closed = [v for v in d["estadoActual"]["vacantes"] if v["vacanteId"] == "vac-c"][0]
        self.assertEqual((closed["estatus"], closed["fechaCierre"]), ("Cubierta", "2026-06-30"))
        self.assertIn("vac-c", {r[2] for r in activity(d)})

    def test_M_unreliable_metrics_are_null(self):
        d, _ = build(self.full_db())
        for v in d["estadoActual"]["vacantes"]:
            self.assertIsNone(v["metricas"]["feedbackPendiente"])
            self.assertIsNone(v["metricas"]["sinMovimiento7d"])

    def test_snapshot_is_independent_of_history(self):
        d, _ = build(self.full_db())
        a = [v for v in d["estadoActual"]["vacantes"] if v["vacanteId"] == "vac-a"][0]
        self.assertEqual(a["metricas"]["postulaciones"], 1)
        self.assertEqual(a["metricas"]["activas"], 1)
        self.assertEqual(a["etapas"], [{"id": "contactado", "label": "Contactado", "orden": 1, "cantidad": 1}])
        self.assertEqual(a["alCorte"]["contactado"], 1)
        self.assertEqual(a["alCorte"]["fueraDeNuevo"], 1)

    def test_contract_rejects_bad_records(self):
        base, _ = build(self.full_db())
        cases = {
            "cantidad cero": lambda d: d["historico"]["actividad"][0].update(cantidad=0),
            "duplicado": lambda d: d["historico"]["actividad"].append(dict(d["historico"]["actividad"][0])),
            "vacante inexistente": lambda d: d["historico"]["actividad"][0].update(vacanteId="vac-x"),
            "fuera de cobertura": lambda d: d["historico"]["actividad"][0].update(fecha="2026-04-01"),
            "métrica desconocida": lambda d: d["historico"]["actividad"][0].update(metrica="vistas"),
            "sin timezone": lambda d: d["metadata"].update(generatedAt="2026-10-01T18:00:00"),
            "timezone": lambda d: d["metadata"].update(timezone="UTC"),
            "schema": lambda d: d.update(schemaVersion="1.1.0"),
            "null no permitido": lambda d: d["estadoActual"]["vacantes"][0]["metricas"].update(postulaciones=None),
            "cantidad negativa": lambda d: d["historico"]["actividad"][0].update(cantidad=-1),
            "métrica obsoleta en histórico": lambda d: d["historico"]["actividad"][0].update(metrica="entrevistados"),
            "orden de métricas": lambda d: d["metadata"].update(metricas=list(reversed(bd.METRICS))),
            "metricasObsoletas": lambda d: d["metadata"].update(metricasObsoletas=["entrevistados"]),
            "sin coberturaPorMetrica": lambda d: d["metadata"].pop("coberturaPorMetrica"),
            "cobertura de métrica con registros = null": lambda d: d["metadata"]["coberturaPorMetrica"].update(contactados=None),
            "cobertura de métrica sin registros": lambda d: d["metadata"]["coberturaPorMetrica"].update(
                aprobados_hm={"desdeEvidencia": "2026-06-01", "hastaEvidencia": "2026-06-01"}),
            "cobertura global incoherente": lambda d: d["metadata"]["cobertura"].update(desdeEvidencia="2026-01-01"),
            "etapasAlCorte sin grupo": lambda d: d["metadata"]["etapasAlCorte"].pop("otros"),
            "etapa en dos grupos": lambda d: d["metadata"]["etapasAlCorte"]["otros"].append("nuevo"),
            "alCorte no suma": lambda d: d["estadoActual"]["vacantes"][0]["alCorte"].update(otros=99),
            "fueraDeNuevo": lambda d: d["estadoActual"]["vacantes"][0]["alCorte"].update(fueraDeNuevo=0),
            "sin alCorte": lambda d: d["estadoActual"]["vacantes"][0].pop("alCorte"),
        }
        for name, mutate in cases.items():
            d = copy.deepcopy(base)
            mutate(d)
            with self.assertRaises(bd.ValidationFailed, msg=name):
                bd.validate_contract(d)


class Preconditions(unittest.TestCase):
    def test_missing_req_id_blocks(self):
        db = make_db(vacancies=[vacancy("vac-a", "REQ-1"), vacancy("vac-b", "")],
                     applications=[application("app-1", "cand-1", "vac-a", fechaPrimerContacto="2026-06-01T15:00:00Z")])
        with self.assertRaises(bd.BuildBlocked) as ctx:
            bd.build_dashboard(db, GENERATED_AT)
        self.assertEqual(ctx.exception.messages, ["1 vacante(s) en scope sin reqId."])

    def test_orphan_application_blocks(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-borrada", fechaPrimerContacto="2026-06-01T15:00:00Z")])
        with self.assertRaises(bd.BuildBlocked) as ctx:
            bd.build_dashboard(db, GENERATED_AT)
        self.assertIn("1 postulación(es) en scope apuntan a una vacante que no existe.", ctx.exception.messages)

    def test_scope_includes_legacy_and_excludes_other_orgs(self):
        legacy = application("app-2", "cand-2", "vac-a", fechaPrimerContacto="2026-06-03T15:00:00Z")
        legacy.pop("organizationId")
        other = application("app-3", "cand-3", "vac-a", fechaPrimerContacto="2026-06-04T15:00:00Z", organizationId="otra-org")
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", fechaPrimerContacto="2026-06-01T15:00:00Z"), legacy, other],
                     audit=[stage_audit("app-1", "vac-a", "oferta", "2026-06-05T15:00:00Z"),
                            {**stage_audit("app-2", "vac-a", "oferta", "2026-06-06T15:00:00Z"), "organizationId": "otra-org"}])
        d, r = bd.build_dashboard(db, GENERATED_AT)[:2]
        self.assertEqual(r["scope"]["postulaciones"], 2)
        self.assertEqual(r["scope"]["transicionesEtapa"], 1)
        self.assertEqual(r["historico"]["totales"], {"nuevos": 2, "rechazados": 0, "contactados": 2, "entrevistados_ch": 0,
                                                     "enviados_hm": 0, "entrevistados_hm": 0, "aprobados_hm": 0, "contrataciones": 0})
        self.assertEqual(r["legacy"]["totales"], {"contactados": 2, "entrevistados": 0, "ofertas": 1, "contrataciones": 0})

    def test_evidence_after_generated_at_blocks(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", fechaPrimerContacto="2026-10-05T15:00:00Z")])
        with self.assertRaises(bd.BuildBlocked):
            bd.build_dashboard(db, GENERATED_AT)

    def test_frontend_backup_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "backup.json"
            p.write_text(json.dumps({"app": "hira", "version": "1", "data": {}}))
            with self.assertRaises(bd.BuildBlocked):
                bd.load_source(p)


class DeletedApplicationsAndTransitions(unittest.TestCase):
    def db(self):
        # app-borrada ya no existe en el store, pero su audit conserva relatedVacancyId válido (vac-a).
        return make_db(
            applications=[application("app-1", "cand-1", "vac-a", "screening")],
            events=[event("app-borrada", "vac-a", "cambio_etapa", "2026-07-09T16:00:00Z", {"stageId": "contactado"})],
            audit=[stage_audit("app-1", "vac-a", "contactado", "2026-07-01T16:00:00Z"),
                   {**stage_audit("app-1", "vac-a", "oferta", "2026-07-02T16:00:00Z"), "type": "bulk_stage_transition",
                    "metadata": {"previousStageId": "contactado", "targetStageId": "oferta"}},
                   {"id": "aud_sint_d1", "type": "application_discarded", "organizationId": ORG,
                    "entityType": "application", "entityId": "app-1", "at": "2026-07-03T16:00:00Z"},
                   {"id": "aud_sint_r1", "type": "application_reactivated", "organizationId": ORG,
                    "entityType": "application", "entityId": "app-1", "at": "2026-07-04T16:00:00Z"},
                   {"id": "aud_sint_c1", "type": "application_created", "organizationId": ORG,
                    "entityType": "application", "entityId": "app-borrada", "relatedVacancyId": "vac-a", "at": "2026-07-05T16:00:00Z"},
                   stage_audit("app-borrada", "vac-a", "contactado", "2026-07-06T16:00:00Z"),
                   stage_audit("app-borrada", "vac-a", "contactado", "2026-07-08T16:00:00Z", frm="por_contactar")])

    def test_deleted_application_never_enters_history_even_with_valid_related_vacancy(self):
        d, r = build(self.db())
        self.assertEqual(metric_records(d, "contactados"), [("2026-07-01", "contactados", "vac-a", 1)])
        self.assertEqual(r["historico"]["totales"]["contactados"], 1)
        self.assertEqual(d["estadoActual"]["vacantes"][0]["metricas"]["postulaciones"], 1)
        self.assertEqual(d["estadoActual"]["vacantes"][0]["metricas"]["contactados"], 1)

    def test_deleted_applications_are_reported_in_aggregate(self):
        _, r = build(self.db())
        ex = r["excluidas"]
        self.assertEqual(ex["postulacionesEliminadas"], 1)
        self.assertEqual(ex["porMetrica"], {m: int(m == "contactados") for m in bd.METRICS})
        self.assertEqual(ex["porMetricaMes"], {"contactados": {"2026-07": 1}})
        self.assertIn("vacante_actual_al_generar", ex["motivo"])
        self.assertNotIn("app-borrada", json.dumps(r, ensure_ascii=False))

    def test_transitions_vs_stage_lifecycle_audit(self):
        # Generador: solo application_stage_changed + bulk_stage_transition de postulaciones en scope (514 en producción).
        # Diagnóstico: + application_discarded/application_reactivated y postulaciones eliminadas (584 en producción).
        _, r = build(self.db())
        s = r["scope"]
        self.assertEqual(s["transicionesEtapa"], 2)
        self.assertEqual(s["transicionesEtapaPorTipo"], {"application_stage_changed": 1, "bulk_stage_transition": 1})
        self.assertEqual(s["eventosCicloEtapaAuditados"], 6)
        self.assertEqual(s["eventosCicloEtapaPorTipo"], {"application_discarded": 1, "application_reactivated": 1,
                                                         "application_stage_changed": 3, "bulk_stage_transition": 1})
        self.assertEqual(r["legacy"]["totales"]["ofertas"], 1)  # descarte/reactivación no alimentan el snapshot 1.1.0
        self.assertEqual(r["historico"]["totales"]["rechazados"], 1)  # 1.2.0: el descarte auditado sí es rechazo

    def test_default_baseline_uses_514_transitions(self):
        self.assertEqual(bd.EXPECTED_BASELINE["scope"]["transicionesEtapa"], 514)
        self.assertNotIn("auditCambiosEtapa", bd.EXPECTED_BASELINE["scope"])
        self.assertEqual(sum(bd.EXPECTED_BASELINE["metricas"]["contactados"].values()), 177)
        self.assertEqual(bd.EXPECTED_BASELINE["totales"], {
            "nuevos": 256, "rechazados": 147, "contactados": 177, "entrevistados_ch": 99,
            "enviados_hm": 40, "entrevistados_hm": 25, "aprobados_hm": 22, "contrataciones": 10})
        self.assertEqual(bd.EXPECTED_BASELINE["alCorte"], {"postulaciones": 256, "nuevo": 19, "fueraDeNuevo": 237})


class BaselineWindow(unittest.TestCase):
    report = {"scope": {}, "cobertura": {"desdeEvidencia": "2026-05-11", "hastaEvidencia": "2026-10-01"},
              "historico": {"porMes": {"contactados": {"2026-05": 2, "2026-10": 3}}}}
    baseline = {"ventana": {"desde": "2026-05", "hasta": "2026-09"},
                "metricas": {"contactados": {"2026-05": 2}}}

    def test_evidence_after_window_is_reported_not_a_discrepancy(self):
        self.assertEqual(bd.compare_with_baseline(self.report, self.baseline), [])
        self.assertEqual(bd.new_evidence_after_baseline(self.report, self.baseline), {"contactados": {"2026-10": 3}})

    def test_difference_inside_window_is_a_discrepancy(self):
        report = copy.deepcopy(self.report)
        report["historico"]["porMes"]["contactados"]["2026-05"] = 1
        self.assertIn("contactados 2026-05: esperado 2, obtenido 1 (Δ -1)", bd.compare_with_baseline(report, self.baseline))


class PrivacyAllowlist(unittest.TestCase):
    TITLE, AREA = "Asesor Patrimonial Senior", "Operaciones Comerciales"

    def db(self, hiring_manager="Gerente Sintético"):
        return make_db(
            vacancies=[vacancy("vac-a", "REQ-2026-0001", titulo=self.TITLE, area=self.AREA, hiringManager=hiring_manager)],
            candidates=[candidate("cand-1", 1)],
            applications=[application("app-1", "cand-1", "vac-a", "entrevista_ch_agendada",
                                      fechaPrimerContacto="2026-06-01T15:00:00Z")],
            events=[{**event("app-1", "vac-a", "nota", "2026-06-01T15:00:00Z"), "nota": n}
                    for n in (self.TITLE, "Entrevista CH agendada", "entrevista ch", self.AREA)])

    def built(self, **kw):
        dashboard, _, forbidden = bd.build_dashboard(self.db(**kw), GENERATED_AT)
        return dashboard, forbidden

    def assert_rejected(self, field, value, kind):
        dashboard, forbidden = self.built()
        dashboard["estadoActual"]["vacantes"][0][field] = value
        with self.assertRaises(bd.ValidationFailed) as ctx:
            bd.validate_privacy(dashboard, forbidden)
        self.assertTrue(any(kind in m for m in ctx.exception.messages), ctx.exception.messages)

    def test_title_stage_label_and_area_in_event_notes_are_allowed(self):
        dashboard, forbidden = self.built()
        v = dashboard["estadoActual"]["vacantes"][0]
        self.assertEqual((v["nombre"], v["area"], v["etapas"][0]["label"]), (self.TITLE, self.AREA, "Entrevista CH agendada"))
        bd.validate_privacy(dashboard, forbidden)  # no lanza

    def test_free_text_outside_known_dimensions_is_rejected(self):
        self.assert_rejected("nombre", "Nota privada de origen sintética", "texto libre")

    def test_candidate_name_in_free_text_is_rejected(self):
        self.assert_rejected("nombre", "Vacante para Nombreprueba1 Apellidoprueba1", "nombre de candidato")

    def test_candidate_email_is_rejected(self):
        self.assert_rejected("area", "persona1@correo-sintetico.test", "email")

    def test_candidate_phone_is_rejected(self):
        self.assert_rejected("reqId", "REQ-5512345601", "teléfono de candidato")

    def test_candidate_and_application_ids_are_rejected(self):
        self.assert_rejected("reqId", "cand-1", "ID individual")
        self.assert_rejected("hiringManager", "app-1", "ID individual")

    def test_value_both_dimension_and_candidate_data_is_rejected(self):
        # El HM de la fuente coincide con el nombre de un candidato: es dimensión permitida y dato de candidato.
        dashboard, forbidden = self.built(hiring_manager="Nombreprueba1 Apellidoprueba1")
        self.assertIn("nombreprueba1 apellidoprueba1", forbidden["allowedDimensions"])
        with self.assertRaises(bd.ValidationFailed) as ctx:
            bd.validate_privacy(dashboard, forbidden)
        self.assertTrue(any("nombre de candidato" in m for m in ctx.exception.messages))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "hira-sync.json"
        self.out = root / "data" / "dashboard.json"
        self.baseline = root / "baseline.json"
        db = PrivacyAndContract().full_db()
        self.src.write_text(json.dumps(db))
        os.chmod(self.src, 0o444)  # copia read-only
        self.baseline.write_text(json.dumps({
            "scope": {"postulaciones": 2, "eventos": 1, "entrevistas": 1, "audit": 1, "transicionesEtapa": 1},
            "metricas": {"contactados": {"2026-06": 1}, "contrataciones": {"2026-06": 1}},
            "totales": {"nuevos": 2, "contactados": 1, "contrataciones": 1, "aprobados_hm": 0},
            "legacy": {"entrevistados": {"2026-06": 1}},
            "alCorte": {"postulaciones": 2, "nuevo": 0, "fueraDeNuevo": 2},
        }))

    def tearDown(self):
        os.chmod(self.src, 0o644)
        self.tmp.cleanup()

    def run_cli(self, *extra):
        out = io.StringIO()
        args = bd.parse_args(["--input", str(self.src), "--output", str(self.out), "--expected", str(self.baseline),
                              "--generated-at", "2026-10-01T18:00:00-06:00", *extra])
        return bd.run(args, out), out.getvalue()

    def test_dry_run_does_not_write_and_source_is_untouched(self):
        before = hashlib.sha256(self.src.read_bytes()).hexdigest()
        code, log = self.run_cli("--dry-run")
        self.assertEqual(code, 0, log)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, hashlib.sha256(self.src.read_bytes()).hexdigest())
        self.assertIn("COINCIDE", log)

    def test_write_after_successful_checks(self):
        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        written = json.loads(self.out.read_text())
        bd.validate_contract(written)

    def test_baseline_mismatch_blocks_write_and_reports_difference(self):
        data = json.loads(self.baseline.read_text())
        data["metricas"]["contactados"] = {"2026-06": 2}
        self.baseline.write_text(json.dumps(data))
        code, log = self.run_cli()
        self.assertEqual(code, 1)
        self.assertFalse(self.out.exists())
        self.assertIn("contactados 2026-06: esperado 2, obtenido 1 (Δ -1)", log)

    def test_logs_contain_no_personal_data_or_ids(self):
        _, log = self.run_cli("--dry-run")
        for needle in ("cand-", "app-", "int-", "evt_sint", "aud_sint", "Nombreprueba", "correo-sintetico", "usr-sint"):
            self.assertNotIn(needle, log)


# ---------------------------------------------------------------------------
# 1.2.0 — métricas de actividad
# ---------------------------------------------------------------------------

CAP = "2026-06-01T15:00:00Z"


def app(aid, etapa="nuevo", vid="vac-a", **extra):
    return application(aid, "cand-" + aid.split("-")[-1], vid, etapa, fechaCaptura=extra.pop("fechaCaptura", CAP), **extra)


def transition(aid, frm, to, at, vid="vac-a"):
    return stage_audit(aid, vid, to, at, frm=frm)


class Nuevos(unittest.TestCase):
    def test_counted_on_fechaCaptura(self):
        d, _ = build(make_db(applications=[app("app-1", fechaCaptura="2026-06-01T15:00:00Z"),
                                           app("app-2", fechaCaptura="2026-06-03T15:00:00Z"),
                                           app("app-3", fechaCaptura="2026-06-03T18:00:00Z")]))
        self.assertEqual(metric_records(d, "nuevos"), [("2026-06-01", "nuevos", "vac-a", 1), ("2026-06-03", "nuevos", "vac-a", 2)])

    def test_capture_date_uses_mexico_city_timezone(self):
        d, _ = build(make_db(applications=[app("app-1", fechaCaptura="2026-06-02T03:30:00Z")]))  # 21:30 del 1-jun en CDMX
        self.assertEqual(metric_records(d, "nuevos"), [("2026-06-01", "nuevos", "vac-a", 1)])

    def test_application_that_advances_still_counts_once_on_capture(self):
        db = make_db(applications=[app("app-1", "contratado", fechaCaptura="2026-06-01T15:00:00Z")],
                     audit=[transition("app-1", "nuevo", "contactado", "2026-06-05T16:00:00Z"),
                            transition("app-1", "contactado", "contratado", "2026-07-01T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "nuevos"), [("2026-06-01", "nuevos", "vac-a", 1)])
        self.assertEqual(vac(d)["alCorte"]["nuevo"], 0)  # ya no está en Nuevo, pero su captura sigue contando

    def test_invalid_capture_date_is_not_counted(self):
        d, r = build(make_db(applications=[app("app-1", fechaCaptura="Hoy"), app("app-2")]))
        self.assertEqual(metric_records(d, "nuevos"), [("2026-06-01", "nuevos", "vac-a", 1)])
        self.assertEqual(r["evidencia"]["capturaFechaNoValida"], 1)


class Rechazados(unittest.TestCase):
    def test_nuevo_descartado_contactado_descartado_counts_once_each(self):
        db = make_db(applications=[app("app-1", "descartado")],
                     audit=[transition("app-1", "nuevo", "descartado", "2026-06-02T16:00:00Z"),
                            transition("app-1", "descartado", "contactado", "2026-06-05T16:00:00Z"),
                            transition("app-1", "contactado", "descartado", "2026-06-10T16:00:00Z")])
        d, r = build(db)
        self.assertEqual(metric_records(d, "nuevos"), [("2026-06-01", "nuevos", "vac-a", 1)])
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-02", "rechazados", "vac-a", 1)])
        self.assertEqual(metric_records(d, "contactados"), [("2026-06-05", "contactados", "vac-a", 1)])

    def test_rejection_from_several_sources_counts_once_on_first_date(self):
        db = make_db(applications=[app("app-1", "descartado")],
                     audit=[transition("app-1", "contactado", "descartado", "2026-06-04T16:00:00Z"),
                            discard_audit("app-1", "vac-a", "2026-06-03T16:00:00Z")],
                     events=[event("app-1", "vac-a", "descartado", "2026-06-05T16:00:00Z"),
                             event("app-1", "vac-a", "cambio_etapa", "2026-06-06T16:00:00Z", {"stageId": "descartado"})])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-03", "rechazados", "vac-a", 1)])

    def test_reactivated_application_still_counts_once(self):
        db = make_db(applications=[app("app-1", "contactado")],
                     audit=[discard_audit("app-1", "vac-a", "2026-06-03T16:00:00Z"),
                            {"id": "aud_sint_react", "type": "application_reactivated", "organizationId": ORG,
                             "entityType": "application", "entityId": "app-1", "at": "2026-06-04T16:00:00Z",
                             "metadata": {"previousStageId": "descartado", "targetStageId": "contactado"}},
                            discard_audit("app-1", "vac-a", "2026-08-01T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-03", "rechazados", "vac-a", 1)])
        self.assertEqual(vac(d)["alCorte"]["rechazado"], 0)  # hoy está en contactado

    def test_no_interesado_counts(self):
        d, _ = build(make_db(applications=[app("app-1", "no_interesado")],
                             audit=[transition("app-1", "contactado", "no_interesado", "2026-06-07T16:00:00Z")]))
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-07", "rechazados", "vac-a", 1)])

    def test_rejected_offer_custom_stage_counts(self):
        db = make_db(applications=[app("app-1", REJECTED_OFFER_ID)],
                     audit=[{**stage_audit("app-1", "vac-a", REJECTED_OFFER_ID, "2026-06-09T16:00:00Z"),
                             "type": "bulk_stage_transition",
                             "metadata": {"previousStageId": "oferta", "targetStageId": REJECTED_OFFER_ID}}])
        d, r = build(db)
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-09", "rechazados", "vac-a", 1)])
        self.assertEqual(vac(d)["alCorte"]["rechazado"], 1)
        self.assertIn(REJECTED_OFFER_ID, d["metadata"]["etapasAlCorte"]["rechazado"])
        self.assertEqual(r["etapas"]["ofertaRechazadaResuelta"], 1)

    def test_fechaCierre_alone_is_not_rejection_evidence(self):
        db = make_db(applications=[app("app-1", "descartado", fechaCierre="2026-06-10T16:00:00Z", resultadoFinal="Descartado")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "rechazados"), [])
        self.assertIsNone(d["metadata"]["coberturaPorMetrica"]["rechazados"])
        self.assertEqual(vac(d)["alCorte"]["rechazado"], 1)  # al corte sí está rechazada

    def test_ambiguous_rejected_offer_configuration_blocks(self):
        config = STAGE_CONFIG + [{"id": "custom_stage_00000000-0000-4000-8000-000000000009", "label": "Oferta  RECHAZADA", "order": 30}]
        with self.assertRaises(bd.BuildBlocked) as ctx:
            bd.build_dashboard(make_db(applications=[app("app-1")], stage_config=config), GENERATED_AT)
        self.assertTrue(any("Oferta rechazada" in m for m in ctx.exception.messages))

    def test_invalid_custom_stage_id_blocks(self):
        config = STAGE_CONFIG + [{"id": "custom stage con espacios", "label": "X", "order": 30}]
        with self.assertRaises(bd.BuildBlocked):
            bd.build_dashboard(make_db(applications=[app("app-1")], stage_config=config), GENERATED_AT)

    def test_canonical_alias_resolves_to_canonical_stage(self):
        config = STAGE_CONFIG + [{"id": "descartado_v2", "canonicalId": "descartado", "label": "Descartado", "order": 31}]
        d, _ = build(make_db(applications=[app("app-1", "descartado_v2")], stage_config=config,
                             audit=[transition("app-1", "nuevo", "descartado_v2", "2026-06-11T16:00:00Z")]))
        self.assertEqual(metric_records(d, "rechazados"), [("2026-06-11", "rechazados", "vac-a", 1)])
        self.assertEqual(vac(d)["alCorte"]["rechazado"], 1)


class InterviewMetrics(unittest.TestCase):
    """Mismas reglas para CH (entrevistados_ch) y HM (entrevistados_hm)."""

    CASES = [("CH", "entrevistados_ch", "entrevista_ch_agendada"),
             ("Hiring Manager", "entrevistados_hm", "entrevista_hm_agendada")]

    def test_transition_counts(self):
        for tipo, metric, stage in self.CASES:
            with self.subTest(tipo=tipo):
                d, _ = build(make_db(applications=[app("app-1", stage)],
                                     audit=[transition("app-1", "contactado", stage, "2026-06-04T16:00:00Z")]))
                self.assertEqual(metric_records(d, metric), [("2026-06-04", metric, "vac-a", 1)])

    def test_scheduled_event_with_interview_id_counts_by_interview_type(self):
        for tipo, metric, _ in self.CASES:
            with self.subTest(tipo=tipo):
                ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z"), "entrevistaId": "int-1"}
                other = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-06T16:00:00Z"), "entrevistaId": "int-2"}
                d, _ = build(make_db(applications=[app("app-1", "contactado")], events=[ev, other],
                                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-20", "Agendada", tipo=tipo),
                                                 interview("int-2", "app-1", "cand-1", "vac-a", "2026-06-21", "Agendada",
                                                           tipo="CH" if tipo != "CH" else "Hiring Manager")]))
                self.assertEqual(metric_records(d, metric), [("2026-06-05", metric, "vac-a", 1)])

    def test_transition_and_event_for_same_application_count_once_earliest(self):
        for tipo, metric, stage in self.CASES:
            with self.subTest(tipo=tipo):
                ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-03T16:00:00Z"), "entrevistaId": "int-1"}
                d, _ = build(make_db(applications=[app("app-1", stage)], events=[ev],
                                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-10", "Agendada", tipo=tipo)],
                                     audit=[transition("app-1", "contactado", stage, "2026-06-04T16:00:00Z")]))
                self.assertEqual(metric_records(d, metric), [("2026-06-03", metric, "vac-a", 1)])

    def test_event_without_interview_id_and_only_free_text_does_not_count(self):
        for tipo, metric, _ in self.CASES:
            with self.subTest(tipo=tipo):
                ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z"),
                      "resumen": f"Entrevista {tipo} agendada", "nota": f"Entrevista {tipo}"}
                d, r = build(make_db(applications=[app("app-1", "contactado")], events=[ev]))
                self.assertEqual(metric_records(d, metric), [])
                self.assertEqual(r["evidencia"]["agendadaSinEntrevistaIdSinEntrevista"], 1)

    def test_event_without_interview_id_is_ambiguous_with_ch_and_hm_interviews(self):
        ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z"), "resumen": "Entrevista CH agendada"}
        d, r = build(make_db(applications=[app("app-1", "contactado")], events=[ev],
                             interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-10", tipo="CH"),
                                         interview("int-2", "app-1", "cand-1", "vac-a", "2026-06-20", tipo="Hiring Manager")]))
        self.assertEqual(metric_records(d, "entrevistados_ch") + metric_records(d, "entrevistados_hm"), [])
        self.assertEqual(r["evidencia"]["agendadaSinEntrevistaIdAmbigua"], 1)

    def test_event_without_interview_id_is_ambiguous_with_two_interviews_of_same_type(self):
        ev = event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z")
        d, r = build(make_db(applications=[app("app-1", "contactado")], events=[ev],
                             interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-10", tipo="CH"),
                                         interview("int-2", "app-1", "cand-1", "vac-a", "2026-06-20", tipo="CH")]))
        self.assertEqual(metric_records(d, "entrevistados_ch"), [])
        self.assertEqual(r["evidencia"]["agendadaSinEntrevistaIdAmbigua"], 1)

    def test_event_without_interview_id_links_by_application_with_exactly_one_interview(self):
        for tipo, metric, _ in self.CASES:
            with self.subTest(tipo=tipo):
                evs = [event("app-1", "vac-a", "entrevista_agendada", "2026-06-07T16:00:00Z"),
                       event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z")]
                d, _ = build(make_db(applications=[app("app-1", "contactado")], events=evs,
                                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-25", tipo=tipo)]))
                self.assertEqual(metric_records(d, metric), [("2026-06-05", metric, "vac-a", 1)])

    def test_interview_date_is_never_the_scheduling_date(self):
        for tipo, metric, _ in self.CASES:
            with self.subTest(tipo=tipo):
                ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z"), "entrevistaId": "int-1"}
                d, _ = build(make_db(applications=[app("app-1", "contactado"), app("app-2", "contactado")], events=[ev],
                                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-25", tipo=tipo),
                                                 interview("int-2", "app-2", "cand-2", "vac-a", "2026-06-26", tipo=tipo)]))
                self.assertEqual(metric_records(d, metric), [("2026-06-05", metric, "vac-a", 1)])  # int-2 sin evento: no cuenta

    def test_event_pointing_to_missing_interview_does_not_count(self):
        ev = {**event("app-1", "vac-a", "entrevista_agendada", "2026-06-05T16:00:00Z"), "entrevistaId": "int-borrada"}
        d, r = build(make_db(applications=[app("app-1", "contactado")], events=[ev],
                             interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-06-25", tipo="CH")]))
        self.assertEqual(metric_records(d, "entrevistados_ch"), [])
        self.assertEqual(r["evidencia"]["agendadaEntrevistaInexistente"], 1)

    def test_feedback_hm_pendiente_is_not_a_new_hm_interview(self):
        db = make_db(applications=[app("app-1", "feedback_hm_pendiente")],
                     audit=[transition("app-1", "entrevista_hm_agendada", "feedback_hm_pendiente", "2026-06-12T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "entrevistados_hm"), [])
        self.assertEqual(vac(d)["alCorte"]["entrevistaHm"], 1)

    def test_enviado_a_hm_counts_only_on_exact_stage(self):
        db = make_db(applications=[app("app-1", "enviado_a_hm"), app("app-2", "enviar_a_hm")],
                     audit=[transition("app-1", "enviar_a_hm", "enviado_a_hm", "2026-06-12T16:00:00Z"),
                            transition("app-2", "contactado", "enviar_a_hm", "2026-06-12T16:00:00Z")],
                     milestones=[hm_milestone("app-2", "vac-a", "cv_sent_to_hm", {}, "2026-06-12T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "enviados_hm"), [("2026-06-12", "enviados_hm", "vac-a", 1)])


class AprobadosHm(unittest.TestCase):
    def hm(self, aid, resultado, fecha="2026-07-01"):
        return interview(f"int-{aid}", aid, "cand-" + aid.split("-")[-1], "vac-a", fecha, tipo="Hiring Manager", resultado=resultado)

    def test_avanza_counts(self):
        d, _ = build(make_db(applications=[app("app-1", "contactado", resultadoHM="Avanza")],
                             events=[event("app-1", "vac-a", "resultado_hm", "2026-07-03T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-07-03", "aprobados_hm", "vac-a", 1)])

    def test_aprobado_counts_through_feedback_and_positive_hm_interview(self):
        d, _ = build(make_db(applications=[app("app-1", "contratado")], interviews=[self.hm("app-1", "Aprobado")],
                             events=[event("app-1", "vac-a", "feedback_hm", "2026-07-02T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-07-02", "aprobados_hm", "vac-a", 1)])

    def test_no_avanza_does_not_count(self):
        d, r = build(make_db(applications=[app("app-1", "descartado", resultadoHM="No avanza")],
                             interviews=[self.hm("app-1", "No avanza")],
                             events=[event("app-1", "vac-a", "resultado_hm", "2026-07-03T16:00:00Z"),
                                     event("app-1", "vac-a", "feedback_hm", "2026-07-04T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [])
        self.assertIsNone(d["metadata"]["coberturaPorMetrica"]["aprobados_hm"])

    def test_resultado_hm_and_feedback_hm_duplicates_count_once_earliest(self):
        d, _ = build(make_db(applications=[app("app-1", "contratado", resultadoHM="Avanza")], interviews=[self.hm("app-1", "Avanza")],
                             events=[event("app-1", "vac-a", "feedback_hm", "2026-07-09T16:00:00Z"),
                                     event("app-1", "vac-a", "resultado_hm", "2026-07-02T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-07-02", "aprobados_hm", "vac-a", 1)])

    def test_approval_without_hm_interview_counts(self):
        d, _ = build(make_db(applications=[app("app-1", "descartado", resultadoHM="Avanza")],
                             events=[event("app-1", "vac-a", "resultado_hm", "2026-08-11T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-08-11", "aprobados_hm", "vac-a", 1)])

    def test_first_valid_date_wins_and_voided_milestone_is_ignored(self):
        db = make_db(applications=[app("app-1", "contratado", resultadoHM="Avanza")],
                     events=[event("app-1", "vac-a", "resultado_hm", "2026-06-17T16:00:00Z")],
                     milestones=[hm_milestone("app-1", "vac-a", "profile_feedback_received", {"decision": "advance"},
                                              "2026-06-01T16:00:00Z", status="voided"),
                                 hm_milestone("app-1", "vac-a", "hm_hiring_decision_received", {"hiringDecision": "hire"},
                                              "2026-06-16T16:00:00Z")])
        d, r = build(db)
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-06-16", "aprobados_hm", "vac-a", 1)])
        self.assertEqual(r["evidencia"]["hitoHmPositivoNoConfirmado"], 1)

    def test_isolated_advance_milestone_without_structured_confirmation_does_not_count(self):
        db = make_db(applications=[app("app-1", "descartado")],
                     milestones=[hm_milestone("app-1", "vac-a", "profile_feedback_received", {"decision": "advance"},
                                              "2026-06-29T16:00:00Z")])
        d, r = build(db)
        self.assertEqual(metric_records(d, "aprobados_hm"), [])
        self.assertEqual(r["evidencia"]["hitoHmPositivoSinConfirmacionEstructurada"], 1)

    def test_milestone_confirmed_by_positive_hm_interview_counts(self):
        db = make_db(applications=[app("app-1", "contratado")], interviews=[self.hm("app-1", "Avanza")],
                     milestones=[hm_milestone("app-1", "vac-a", "hm_final_feedback_received", {"recommendation": "advance"},
                                              "2026-06-29T16:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "aprobados_hm"), [("2026-06-29", "aprobados_hm", "vac-a", 1)])

    def test_free_text_never_decides_the_result(self):
        ev = {**event("app-1", "vac-a", "resultado_hm", "2026-07-03T16:00:00Z"), "resumen": "Resultado HM: Avanza", "nota": "Avanza"}
        fb = {**event("app-1", "vac-a", "feedback_hm", "2026-07-04T16:00:00Z"), "resumen": "Feedback HM: Aprobado"}
        d, r = build(make_db(applications=[app("app-1", "feedback_hm_pendiente")], events=[ev, fb]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [])
        self.assertEqual(r["evidencia"]["resultadoHmSinResultadoPositivo"], 1)
        self.assertEqual(r["evidencia"]["feedbackHmSinEntrevistaPositiva"], 1)

    def test_finalist_stage_is_not_an_approval(self):
        finalist = "custom_stage_00000000-0000-4000-8000-000000000002"
        d, _ = build(make_db(applications=[app("app-1", finalist)],
                             audit=[transition("app-1", "feedback_hm_pendiente", finalist, "2026-07-05T16:00:00Z")]))
        self.assertEqual(metric_records(d, "aprobados_hm"), [])


class AlCorte(unittest.TestCase):
    STAGES = ["nuevo", "nuevo", "contactado", "entrevista_ch_agendada", "entrevista_ch_realizada", "enviado_a_hm",
              "entrevista_hm_agendada", "feedback_hm_pendiente", "contratado", "descartado", "no_interesado",
              REJECTED_OFFER_ID, "screening", "enviar_a_hm", "custom_stage_00000000-0000-4000-8000-000000000003", "etapa_desconocida"]

    def db(self, audit=None):
        apps = [app(f"app-{i}", etapa) for i, etapa in enumerate(self.STAGES, start=1)]
        apps.append(app("app-99", "contactado", vid="vac-b"))
        return make_db(vacancies=[vacancy("vac-a", "REQ-2026-0001"), vacancy("vac-b", "REQ-2026-0002")],
                       applications=apps, audit=audit)

    def test_groups(self):
        d, r = build(self.db())
        self.assertEqual(vac(d)["alCorte"], {"nuevo": 2, "contactado": 1, "entrevistaCh": 2, "enviadoHm": 1, "entrevistaHm": 2,
                                             "contratado": 1, "rechazado": 3, "otros": 4, "fueraDeNuevo": 14})
        self.assertEqual(vac(d, "vac-b")["alCorte"]["contactado"], 1)
        self.assertEqual(r["snapshot"]["alCorte"]["postulaciones"], 17)
        self.assertEqual(r["evidencia"]["postulacionesEtapaActualDesconocida"], 1)

    def test_each_application_in_exactly_one_group_and_sum_matches(self):
        d, _ = build(self.db())
        for v in d["estadoActual"]["vacantes"]:
            self.assertEqual(sum(v["alCorte"][g] for g in bd.AL_CORTE_GROUPS), v["metricas"]["postulaciones"])
            self.assertEqual(v["alCorte"]["fueraDeNuevo"], v["metricas"]["postulaciones"] - v["alCorte"]["nuevo"])
        resolver = bd.StageResolver(STAGE_CONFIG)
        for etapa in self.STAGES:
            self.assertEqual(sum(resolver.al_corte_group(etapa) == g for g in bd.AL_CORTE_GROUPS), 1)

    def test_metadata_documents_stage_groups(self):
        d, _ = build(self.db())
        eac = d["metadata"]["etapasAlCorte"]
        self.assertEqual(list(eac), bd.AL_CORTE_GROUPS)
        self.assertEqual(eac["entrevistaCh"], ["entrevista_ch_agendada", "entrevista_ch_realizada"])
        self.assertEqual(eac["entrevistaHm"], ["entrevista_hm_agendada", "feedback_hm_pendiente"])
        self.assertEqual(eac["rechazado"], ["descartado", "no_interesado", REJECTED_OFFER_ID])
        for sid in ("screening", "enviar_a_hm", "oferta", "custom_stage_00000000-0000-4000-8000-000000000003", "etapa_desconocida"):
            self.assertIn(sid, eac["otros"])

    def test_al_corte_does_not_depend_on_history(self):
        with_history, _ = build(self.db(audit=[transition("app-3", "nuevo", "contactado", "2026-06-02T16:00:00Z"),
                                               discard_audit("app-10", "vac-a", "2026-06-03T16:00:00Z")]))
        without, _ = build(self.db())
        self.assertNotEqual(with_history["historico"], without["historico"])
        self.assertEqual([v["alCorte"] for v in with_history["estadoActual"]["vacantes"]],
                         [v["alCorte"] for v in without["estadoActual"]["vacantes"]])


class Contract120(unittest.TestCase):
    def db(self):
        return make_db(candidates=[candidate("cand-1", 1)],
                       applications=[app("app-1", "contactado", resultadoHM="Avanza")],
                       events=[event("app-1", "vac-a", "resultado_hm", "2026-07-03T16:00:00Z")],
                       audit=[transition("app-1", "nuevo", "contactado", "2026-06-02T16:00:00Z")])

    def test_schema_metadata_and_coverage(self):
        d, _ = build(self.db())
        m = d["metadata"]
        self.assertEqual(d["schemaVersion"], "1.2.0")
        self.assertEqual(m["metricas"], ["nuevos", "rechazados", "contactados", "entrevistados_ch", "enviados_hm",
                                         "entrevistados_hm", "aprobados_hm", "contrataciones"])
        self.assertEqual(m["metricasObsoletas"], ["entrevistados", "ofertas"])
        self.assertEqual(m["coberturaPorMetrica"]["nuevos"], {"desdeEvidencia": "2026-06-01", "hastaEvidencia": "2026-06-01"})
        self.assertEqual(m["coberturaPorMetrica"]["aprobados_hm"], {"desdeEvidencia": "2026-07-03", "hastaEvidencia": "2026-07-03"})
        for metric in ("rechazados", "entrevistados_ch", "enviados_hm", "entrevistados_hm", "contrataciones"):
            self.assertIsNone(m["coberturaPorMetrica"][metric])
        self.assertEqual(m["cobertura"], {"desdeEvidencia": "2026-06-01", "hastaEvidencia": "2026-07-03"})
        # Compatibilidad 1.1.0: los campos del snapshot se conservan.
        self.assertEqual(set(vac(d)["metricas"]), set(bd.SNAPSHOT_REQUIRED + bd.SNAPSHOT_NULLABLE))

    def test_activity_quantities_positive_and_unique(self):
        d, _ = build(self.db())
        keys = [(r["fecha"], r["metrica"], r["vacanteId"]) for r in d["historico"]["actividad"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(r["cantidad"] >= 1 for r in d["historico"]["actividad"]))
        self.assertTrue(all(r["metrica"] in bd.METRICS for r in d["historico"]["actividad"]))

    def test_privacy_of_new_sources(self):
        d, _ = build(self.db())
        text = json.dumps(d, ensure_ascii=False)
        for needle in ("hm_ms_sint", "evt_sint", "aud_sint", "app-1", "cand-1", "Nombreprueba", "privad", "Avanza"):
            self.assertNotIn(needle, text)

    def test_deterministic(self):
        a, _ = build(self.db())
        b, _ = build(self.db())
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))

    def test_sample_matches_contract_shape(self):
        sample = json.loads((Path(__file__).resolve().parent.parent / "data" / "dashboard.sample.json").read_text())
        bd.validate_contract(sample)


# ---------------------------------------------------------------------------
# contrataciones — fecha oficial por prioridad de fuente
# ---------------------------------------------------------------------------

LATE_GENERATED_AT = bd.parse_generated_at("2026-10-10T18:00:00-06:00")


def hired_candidate(cid, n, fecha_contratacion=None):
    c = candidate(cid, n)
    if fecha_contratacion is not None:
        c["fechaContratacion"] = fecha_contratacion
    return c


def build_at(db, generated_at=LATE_GENERATED_AT):
    dashboard, report, forbidden = bd.build_dashboard(db, generated_at)
    bd.validate_contract(dashboard)
    bd.validate_privacy(dashboard, forbidden)
    return dashboard, report


def hires(dashboard):
    return metric_records(dashboard, "contrataciones")


class OfficialHireDate(unittest.TestCase):
    def hired_db(self, effective=None, applied="2026-10-02T17:00:00Z", transition_at="2026-10-02T17:00:00Z",
                 etapa="contratado"):
        extra = {"fechaContratacionAplicada": applied} if applied is not None else {}
        audit = [transition("app-1", "oferta", "contratado", transition_at)] if transition_at else []
        return make_db(candidates=[hired_candidate("cand-1", 1, effective)],
                       applications=[app("app-1", etapa, **extra)], audit=audit)

    def test_T1_effective_date_wins_over_technical_timestamp(self):
        d, r = build_at(self.hired_db(effective="2026-09-28"))
        self.assertEqual(hires(d), [("2026-09-28", "contrataciones", "vac-a", 1)])
        self.assertEqual(r["evidencia"]["fuentes"]["contrataciones"], {"candidato:fechaContratacion": 1})

    def test_T2_without_effective_date_uses_applied_timestamp_not_earlier_transition(self):
        d, _ = build_at(self.hired_db(transition_at="2026-09-30T17:00:00Z"))
        self.assertEqual(hires(d), [("2026-10-02", "contrataciones", "vac-a", 1)])

    def test_T3_without_effective_or_applied_uses_first_transition(self):
        db = self.hired_db(applied=None, transition_at="2026-07-15T17:00:00Z")
        db["audit"].append(transition("app-1", "oferta", "contratado", "2026-07-20T17:00:00Z"))
        d, r = build_at(db)
        self.assertEqual(hires(d), [("2026-07-15", "contrataciones", "vac-a", 1)])
        self.assertEqual(r["evidencia"]["fuentes"]["contrataciones"], {"transicion:audit": 1})

    def test_T4_invalid_effective_date_falls_back_to_applied_timestamp(self):
        for invalid in ("", None, "2026-02-30", "pendiente", "2026-09-28T10:00:00Z", "28/09/2026", " 2026-09-28"):
            with self.subTest(invalid=invalid):
                db = self.hired_db()
                db["workspaces"][0]["store"]["candidatos"][0]["fechaContratacion"] = invalid
                d, r = build_at(db)
                self.assertEqual(hires(d), [("2026-10-02", "contrataciones", "vac-a", 1)])
                self.assertEqual(r["evidencia"].get("contratacionFechaEfectivaNoValida", 0), int(invalid not in ("", None)))

    def test_T5_priority_is_by_source_not_min_date(self):
        d, _ = build_at(self.hired_db(effective="2026-10-05"))
        self.assertEqual(hires(d), [("2026-10-05", "contrataciones", "vac-a", 1)])

    def test_T6_effective_date_without_hire_evidence_creates_no_hire(self):
        db = self.hired_db(effective="2026-09-28", applied=None, transition_at=None, etapa="oferta")
        d, r = build_at(db)
        self.assertEqual(hires(d), [])
        self.assertIsNone(d["metadata"]["coberturaPorMetrica"]["contrataciones"])
        self.assertNotIn(("app-1", "contrataciones"), legacy_first(db))
        self.assertEqual(vac(d)["metricas"]["contrataciones"], 0)
        self.assertNotIn("contrataciones", r["evidencia"]["fuentes"])

    def test_T6b_hire_is_never_inferred_from_text_closure_or_activity(self):
        db = make_db(candidates=[hired_candidate("cand-1", 1, "2026-09-28")],
                     applications=[app("app-1", "oferta", resultadoFinal="Contratado", fechaCierre="2026-09-28T17:00:00Z",
                                       ultimaActividad="Contratado hoy", notasCH="Contratado el 28 de septiembre")],
                     events=[{**event("app-1", "vac-a", "nota", "2026-09-28T17:00:00Z"), "resumen": "Contratado"},
                             event("app-1", "vac-a", "cambio_etapa", "2026-09-28T17:00:00Z", {"stageId": "etapa_inexistente"})])
        d, _ = build_at(db)
        self.assertEqual(hires(d), [])

    def test_T7_current_hired_stage_with_effective_date_and_no_timestamp(self):
        d, _ = build_at(self.hired_db(effective="2026-09-28", applied=None, transition_at=None))
        self.assertEqual(hires(d), [("2026-09-28", "contrataciones", "vac-a", 1)])
        self.assertEqual(vac(d)["metricas"]["contrataciones"], 1)

    def test_T7b_current_hired_stage_without_any_date_has_no_history(self):
        d, _ = build_at(self.hired_db(applied=None, transition_at=None))
        self.assertEqual(hires(d), [])
        self.assertEqual(vac(d)["metricas"]["contrataciones"], 1)  # snapshot 1.1.0 sí cuenta la etapa actual

    def test_T8_reactivated_application_keeps_historical_hire_and_effective_date_wins(self):
        db = self.hired_db(effective="2026-07-08", applied="2026-07-10T17:00:00Z",
                           transition_at="2026-07-10T17:00:00Z", etapa="contactado")
        db["audit"] += [discard_audit("app-1", "vac-a", "2026-07-20T17:00:00Z"),
                        {"id": "aud_sint_react_hire", "type": "application_reactivated", "organizationId": ORG,
                         "entityType": "application", "entityId": "app-1", "at": "2026-07-21T17:00:00Z",
                         "metadata": {"previousStageId": "descartado", "targetStageId": "contactado"}}]
        d, _ = build_at(db)
        self.assertEqual(hires(d), [("2026-07-08", "contrataciones", "vac-a", 1)])
        self.assertEqual(vac(d)["alCorte"]["contratado"], 0)

    def test_T9_editing_effective_date_reattributes_on_next_generation(self):
        db = self.hired_db()
        first, _ = build_at(copy.deepcopy(db))
        self.assertEqual(hires(first), [("2026-10-02", "contrataciones", "vac-a", 1)])
        edited = copy.deepcopy(db)
        edited["workspaces"][0]["store"]["candidatos"][0]["fechaContratacion"] = "2026-09-28"
        second, _ = build_at(edited)
        self.assertEqual(hires(second), [("2026-09-28", "contrataciones", "vac-a", 1)])
        self.assertEqual(edited["workspaces"][0]["store"]["postulaciones"][0]["fechaContratacionAplicada"],
                         "2026-10-02T17:00:00Z")

    def test_T10_two_hired_applications_of_same_candidate_count_per_application(self):
        db = make_db(vacancies=[vacancy("vac-a", "REQ-2026-0001"), vacancy("vac-b", "REQ-2026-0002")],
                     candidates=[hired_candidate("cand-1", 1, "2026-09-28")],
                     applications=[application("app-1", "cand-1", "vac-a", "contratado", fechaCaptura=CAP,
                                               fechaContratacionAplicada="2026-10-02T17:00:00Z"),
                                   application("app-2", "cand-1", "vac-b", "contratado", fechaCaptura=CAP,
                                               fechaContratacionAplicada="2026-10-03T17:00:00Z")])
        d, r = build_at(db)
        self.assertEqual(hires(d), [("2026-09-28", "contrataciones", "vac-a", 1), ("2026-09-28", "contrataciones", "vac-b", 1)])
        self.assertEqual(r["historico"]["totales"]["contrataciones"], 2)

    def test_T11_source_is_not_mutated(self):
        db = self.hired_db(effective="2026-09-28")
        before = copy.deepcopy(db)
        build_at(db)
        legacy_first(db)
        self.assertEqual(db, before)
        self.assertEqual(db["workspaces"][0]["store"]["postulaciones"][0]["fechaContratacionAplicada"], "2026-10-02T17:00:00Z")

    def full_activity_db(self, effective=None):
        hm = interview("int-1", "app-1", "cand-1", "vac-a", "2026-08-01", tipo="Hiring Manager", resultado="Avanza")
        return make_db(
            candidates=[hired_candidate("cand-1", 1, effective), hired_candidate("cand-2", 2, effective)],
            applications=[app("app-1", "contratado", resultadoHM="Avanza", fechaPrimerContacto="2026-06-02T17:00:00Z",
                              fechaPrimeraEntrevistaRealizada="2026-07-01", fechaOfertaRealizada="2026-09-01T17:00:00Z",
                              fechaContratacionAplicada="2026-10-02T17:00:00Z"),
                          app("app-2", "descartado")],
            interviews=[hm],
            events=[event("app-1", "vac-a", "resultado_hm", "2026-08-02T17:00:00Z"),
                    {**event("app-1", "vac-a", "entrevista_agendada", "2026-07-25T17:00:00Z"), "entrevistaId": "int-1"}],
            audit=[transition("app-1", "nuevo", "contactado", "2026-06-02T17:00:00Z"),
                   transition("app-1", "contactado", "entrevista_ch_agendada", "2026-06-10T17:00:00Z"),
                   transition("app-1", "validado_ch", "enviado_a_hm", "2026-07-20T17:00:00Z"),
                   transition("app-1", "oferta", "contratado", "2026-10-02T17:00:00Z"),
                   transition("app-2", "nuevo", "descartado", "2026-06-05T17:00:00Z")])

    def test_T12_other_seven_metrics_are_unchanged(self):
        without, _ = build_at(self.full_activity_db())
        with_effective, _ = build_at(self.full_activity_db("2026-09-28"))
        others = lambda d: [r for r in activity(d) if r[1] != "contrataciones"]
        self.assertEqual(others(without), others(with_effective))
        self.assertEqual({r[1] for r in others(without)}, set(bd.METRICS) - {"contrataciones"})
        self.assertEqual(hires(without), [("2026-10-02", "contrataciones", "vac-a", 1)])
        self.assertEqual(hires(with_effective), [("2026-09-28", "contrataciones", "vac-a", 1)])
        self.assertEqual(without["estadoActual"], with_effective["estadoActual"])

    def test_T13_effective_date_adds_no_fields_or_individual_data(self):
        without, _ = build_at(self.full_activity_db())
        with_effective, _ = build_at(self.full_activity_db("2026-09-28"))
        text = json.dumps(with_effective, ensure_ascii=False)
        for needle in ("fechaContratacion", "candidatoId", "cand-1", "cand-2", "app-1", "Nombreprueba", "correo-sintetico"):
            self.assertNotIn(needle, text)
        keys = lambda d: {(k, tuple(sorted(r))) for k in ("actividad",) for r in d["historico"][k]}
        self.assertEqual(keys(without), keys(with_effective))
        self.assertEqual(set(with_effective["metadata"]), set(without["metadata"]))

    def test_T14_contract_remains_schema_1_2_0(self):
        d, _ = build_at(self.full_activity_db("2026-09-28"))
        self.assertEqual(d["schemaVersion"], "1.2.0")
        self.assertEqual(set(d), bd.TOP_KEYS)
        self.assertEqual(set(d["metadata"]), bd.METADATA_KEYS)
        self.assertEqual(d["metadata"]["coberturaPorMetrica"]["contrataciones"],
                         {"desdeEvidencia": "2026-09-28", "hastaEvidencia": "2026-09-28"})

    def test_T15_legacy_uses_same_hire_date_without_touching_other_legacy_metrics(self):
        without_db, with_db = self.full_activity_db(), self.full_activity_db("2026-09-28")
        legacy_without, legacy_with = legacy_first(without_db), legacy_first(with_db)
        self.assertEqual(legacy_without[("app-1", "contrataciones")], "2026-10-02")
        self.assertEqual(legacy_with[("app-1", "contrataciones")], "2026-09-28")
        strip = lambda first: {k: v for k, v in first.items() if k[1] != "contrataciones"}
        self.assertEqual(strip(legacy_without), strip(legacy_with))
        _, r_without = build_at(without_db)
        _, r_with = build_at(with_db)
        self.assertEqual(r_with["legacy"]["porMes"]["contrataciones"], {"2026-09": 1})
        for metric in ("contactados", "entrevistados", "ofertas"):
            self.assertEqual(r_with["legacy"]["porMes"][metric], r_without["legacy"]["porMes"][metric])
        self.assertEqual(r_with["snapshot"]["acumuladosLegacy"], r_without["snapshot"]["acumuladosLegacy"])

    def test_resolver_reads_candidate_only_from_organization_scope(self):
        db = self.hired_db()
        db["workspaces"][0]["store"]["candidatos"][0]["organizationId"] = "otra-org"
        db["workspaces"][0]["store"]["candidatos"][0]["fechaContratacion"] = "2026-09-28"
        d, _ = build_at(db)
        self.assertEqual(hires(d), [("2026-10-02", "contrataciones", "vac-a", 1)])

    def test_embedded_baseline_is_unchanged(self):
        self.assertEqual(bd.EXPECTED_BASELINE["metricas"]["contrataciones"],
                         {"2026-05": 1, "2026-07": 7, "2026-08": 1, "2026-09": 1})
        self.assertEqual(bd.EXPECTED_BASELINE["legacy"]["contrataciones"],
                         {"2026-05": 1, "2026-07": 7, "2026-08": 1, "2026-09": 1})
        self.assertEqual(bd.EXPECTED_BASELINE["totales"]["contrataciones"], 10)



if __name__ == "__main__":
    unittest.main()
