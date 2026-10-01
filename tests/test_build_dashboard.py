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


def interview(iid, aid, cid, vid, fecha, estatus="Realizada", asistencia="Asistió"):
    return {"id": iid, "organizationId": ORG, "postulacionId": aid, "candidatoId": cid, "vacanteId": vid,
            "fecha": fecha, "estatus": estatus, "asistencia": asistencia, "notas": "Notas privadas de entrevista"}


def make_db(vacancies=None, candidates=None, applications=None, interviews=None, events=None, audit=None):
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
            },
        }],
        "audit": audit or [],
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
        d, _ = build(db)
        self.assertEqual(metric_records(d, "entrevistados"), [("2026-07-02", "entrevistados", "vac-a", 1)])

    def test_B2_milestone_and_interview_keep_minimum(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "screening", fechaPrimeraEntrevistaRealizada="2026-05-11")],
                     interviews=[interview("int-1", "app-1", "cand-1", "vac-a", "2026-05-20")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "entrevistados"), [("2026-05-11", "entrevistados", "vac-a", 1)])
        self.assertEqual(d["metadata"]["cobertura"]["desdeEvidencia"], "2026-05-11")

    def test_B3_unrealized_interviews_do_not_count(self):
        bad = [("No-show", "Pendiente"), ("Cancelada", "Canceló"), ("Reprogramada", "Reprogramó"),
               ("Agendada", "Pendiente"), ("Realizada", "No se presentó"), ("Realizada", "no_show"), ("Realizada", "reprogramo")]
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "entrevista_ch_agendada"),
                                   application("app-2", "cand-2", "vac-a", "contactado", fechaPrimerContacto="2026-06-01T15:00:00Z")],
                     interviews=[interview(f"int-{i}", "app-1", "cand-1", "vac-a", "2026-07-01", e, a) for i, (e, a) in enumerate(bad)])
        d, r = build(db)
        self.assertEqual(metric_records(d, "entrevistados"), [])
        self.assertEqual(r["evidencia"]["entrevistasNoValidas"], len(bad))

    def test_C_hired_without_offer_evidence_does_not_invent_offer(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "contratado",
                                                fechaContratacionAplicada="2026-07-20T17:00:00Z")])
        d, _ = build(db)
        self.assertEqual(metric_records(d, "ofertas"), [])
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
        self.assertEqual(activity(d), [("2026-06-01", "contactados", "vac-a", 1)])
        m = d["estadoActual"]["vacantes"][0]["metricas"]
        self.assertEqual((m["contactados"], m["ofertas"], m["entrevistados"], m["contrataciones"]), (2, 1, 0, 0))

    def test_later_stage_never_implies_earlier_metric(self):
        db = make_db(applications=[application("app-1", "cand-1", "vac-a", "oferta",
                                                fechaOfertaRealizada="2026-08-03T15:00:00Z")])
        d, _ = build(db)
        self.assertEqual(activity(d), [("2026-08-03", "ofertas", "vac-a", 1)])
        self.assertEqual(d["estadoActual"]["vacantes"][0]["metricas"]["contactados"], 0)


class Aggregation(unittest.TestCase):
    def setUp(self):
        # Semana 2026-06-01..07 con actividad; 2026-06-08..14 sin actividad; 2026-06-15.. solo contactados.
        self.db = make_db(
            vacancies=[vacancy("vac-a", "REQ-2026-0001"), vacancy("vac-b", "REQ-2026-0002")],
            applications=[application("app-1", "cand-1", "vac-a", "screening", fechaPrimerContacto="2026-06-02T15:00:00Z"),
                          application("app-2", "cand-2", "vac-a", "screening", fechaPrimerContacto="2026-06-02T20:00:00Z"),
                          application("app-3", "cand-3", "vac-b", "screening", fechaPrimerContacto="2026-06-02T15:00:00Z"),
                          application("app-4", "cand-4", "vac-b", "screening", fechaPrimerContacto="2026-06-16T15:00:00Z")],
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
        d, _ = build(self.db)  # vacantes abiertas y postulaciones capturadas en ene–abr, sin evidencia
        self.assertFalse([r for r in activity(d) if r[0] < "2026-05-01"])
        self.assertEqual(d["metadata"]["cobertura"], {"desdeEvidencia": "2026-06-02", "hastaEvidencia": "2026-06-16"})

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
            "schema": lambda d: d.update(schemaVersion="1.0.0"),
            "null no permitido": lambda d: d["estadoActual"]["vacantes"][0]["metricas"].update(postulaciones=None),
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
        self.assertEqual(r["historico"]["totales"], {"contactados": 2, "entrevistados": 0, "ofertas": 1, "contrataciones": 0})

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
        self.assertEqual(ex["porMetrica"], {"contactados": 1, "entrevistados": 0, "ofertas": 0, "contrataciones": 0})
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
        self.assertEqual(r["historico"]["totales"]["ofertas"], 1)  # descarte/reactivación no alimentan métricas

    def test_default_baseline_uses_514_transitions(self):
        self.assertEqual(bd.EXPECTED_BASELINE["scope"]["transicionesEtapa"], 514)
        self.assertNotIn("auditCambiosEtapa", bd.EXPECTED_BASELINE["scope"])
        self.assertEqual(sum(bd.EXPECTED_BASELINE["metricas"]["contactados"].values()), 177)


class BaselineWindow(unittest.TestCase):
    report = {"scope": {}, "cobertura": {"desdeEvidencia": "2026-05-11", "hastaEvidencia": "2026-10-01"},
              "historico": {"porMes": {"contactados": {"2026-05": 2, "2026-10": 3}}}}
    baseline = {"ventana": {"desde": "2026-05", "hasta": "2026-09"},
                "cobertura": {"desdeEvidencia": "2026-05-11", "hastaEvidencia": "2026-09-29"},
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
            "cobertura": {"desdeEvidencia": "2026-06-01"},
            "metricas": {"contactados": {"2026-06": 1}, "entrevistados": {"2026-06": 1}, "contrataciones": {"2026-06": 1}},
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


if __name__ == "__main__":
    unittest.main()
