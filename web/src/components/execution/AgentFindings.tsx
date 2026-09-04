import { useRef, useEffect, useState } from 'react'
import { Loader2, CheckCircle, Clock, ChevronRight, ChevronDown, AlertTriangle, AlertCircle, Info, Sparkles, Save, MinusCircle, ShieldCheck, ArrowUpCircle, RotateCcw } from 'lucide-react'
import { useStore } from '../../store'
import type { SubAgentEvent, PolicyDecision } from '../../store'
import { AGENT_MAP, AGENTS } from '../../data/agents'
import type { BackendPipeline, Checkpoint } from '../../services/api'
import clsx from 'clsx'

// ── Label lookup: backend sub_agent ID → human label ─────────────────────────

const SA_LABEL: Record<string, string> = {
  // ER
  sa_er_census:           'ER Census',
  sa_er_triage:           'Triage Monitor',
  sa_er_save:             'Triage Save',
  sa_er_fasttrack:        'Fast-Track Router',
  sa_er_critical_select:  'Admission Router',
  // Bed
  sa_bed_availability:    'Bed Availability',
  sa_bed_ranking:         'Bed Ranking',
  sa_bed_batch_match:     'Batch Bed Match',
  sa_bed_reservation:     'Bed Reservation',
  // ICU
  sa_icu_census:          'ICU Census',
  sa_icu_analysis:        'ICU Analysis',
  sa_icu_confirm:         'ICU Escalation',
  // Staff
  sa_staff_census:        'Staff Census',
  sa_staff_analysis:      'Staff Analysis',
  sa_staff_confirm:       'Staff Reallocation',
  // Discharge
  sa_discharge_candidates: 'Discharge Candidates',
  sa_discharge_assessment: 'Discharge Assessment',
  sa_discharge_confirm:    'Discharge Confirmation',
  sa_discharge_summary:    'Discharge Summaries',
  // OT
  sa_ot_census:           'OT Census',
  sa_ot_analysis:         'OT Capacity Analysis',
  // Bed Prediction
  sa_bed_pred_census:     'Capacity Census',
  sa_bed_pred_forecast:   'Capacity Forecast',
  // Bed Cleaning
  sa_bed_cleaning:        'Bed Cleaning',
  sa_hk_census:           'Vacated Bed Census',
  sa_hk_dispatch:         'Housekeeping Dispatch',
  // Pharmacy
  sa_pharmacy_census:     'Pharmacy Census',
  sa_pharmacy_check:      'Medication Reconciliation',
  // Revenue
  sa_rev_invoice_census:  'Invoice Monitor',
  sa_rev_collections:     'Collections Monitor',
  sa_rev_claims:          'Claims Pipeline',
  sa_rev_analyst:         'Revenue Risk Analyst',
  sa_rev_patient_billing: 'Patient Billing Lookup',
  // Ambulance
  sa_ambulance_census:    'Fleet Census',
  sa_ambulance_assign:    'Dispatch Coordinator',
  sa_ambulance_confirm:   'Dispatch Confirmed',
  // Patient Verification
  sa_patient_identification: 'Patient Identification',
  // ER (extended — individual acuity broadcasts)
  sa_er_code_blue:           'Code Blue Alert',
  sa_er_spo2_critical:       'SpO₂ Critical',
  sa_er_protocol:            'Clinical Protocol',
  sa_er_specialist:          'Specialist Notification',
  sa_er_boarding:            'Boarding Monitor',
  // Revenue (extended)
  sa_rev_initiate_billing:   'Initiate Billing',
  sa_rev_denial_prevention:  'Denial Prevention',
  // ER (extended)
  sa_er_acuity_response:     'Acuity Response',
  sa_er_disposition:         'Disposition Coordinator',
  // ICU (extended)
  sa_icu_transfer:           'ICU Transfer',
}
// Populate any remaining from agents.ts
for (const agent of AGENTS) {
  for (const sa of agent.subAgents) {
    if (!SA_LABEL[sa.id]) SA_LABEL[sa.id] = sa.label
  }
}

function getLabel(subAgentId: string): string {
  return SA_LABEL[subAgentId] ?? subAgentId.replace('sa_', '').replace(/_/g, ' ')
    .split(' ').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

// ── Doctor-friendly narrative per sub-agent ───────────────────────────────────

function narratise(subAgentId: string, result: Record<string, unknown> | undefined): string {
  if (!result) return 'Completed.'
  const n = (v: unknown) => Number(v ?? 0)
  const pl = (count: unknown, word: string) => `${n(count)} ${word}${n(count) === 1 ? '' : 's'}`
  const risk = (v: unknown) => String(v ?? 'unknown').toUpperCase()

  switch (subAgentId) {
    // ── ER ──────────────────────────────────────────────────────────────────
    case 'sa_er_census': {
      const count = result.active_er_count ?? result.untriaged_count ?? 0
      return `${pl(count, 'patient')} currently in the ER queue.`
    }
    case 'sa_er_triage': {
      const ctas1 = n(result.ctas_1), ctas2 = n(result.ctas_2)
      const rest  = n(result.triaged) - n(result.critical)
      return `Scored ${pl(result.triaged, 'patient')}: ` +
        `${ctas1} resuscitation (CTAS 1), ${ctas2} emergent (CTAS 2), ${rest} lower acuity.`
    }
    case 'sa_er_save':
      return n(result.critical_alerts) > 0
        ? `Scores recorded. ${pl(result.critical_alerts, 'critical alert')} raised — attending notified.`
        : `Triage scores recorded for ${pl(result.saved, 'patient')}. No critical alerts.`
    case 'sa_er_fasttrack':
      return n(result.fasttrack_candidates) > 0
        ? `${pl(result.fasttrack_candidates, 'patient')} (CTAS 4–5) waiting over 30 min — fast-track routing recommended.`
        : 'No CTAS 4–5 patients waiting over 30 min at this time.'
    case 'sa_er_critical_select':
      return n(result.critical_selected) > 0
        ? `${pl(result.critical_selected, 'critical patient')} ranked by severity and cleared for bed placement.`
        : 'No critical patients requiring immediate bed placement.'

    // ── Bed ─────────────────────────────────────────────────────────────────
    case 'sa_bed_availability':
      return n(result.candidate_count) > 0
        ? `${pl(result.candidate_count, 'bed')} available — checking suitability for patient.`
        : 'No suitable beds available at this time.'
    case 'sa_bed_ranking':
      return result.recommendation
        ? String(result.recommendation).slice(0, 200)
        : 'Beds evaluated and ranked by clinical suitability.'
    case 'sa_bed_batch_match':
      return `${pl(result.assigned, 'patient')} matched to appropriate beds ` +
        `(${n(result.requested)} requested, ${n(result.requested) - n(result.assigned)} unmatched).`
    case 'sa_bed_reservation': {
      if (result.bed_names && Array.isArray(result.bed_names) && (result.bed_names as string[]).length > 0) {
        const names = result.bed_names as string[]
        const shown = names.slice(0, 3).join(', ')
        const extra = names.length > 3 ? ` +${names.length - 3} more` : ''
        return `${pl(result.beds_reserved, 'bed')} reserved: ${shown}${extra}.`
      }
      if (result.ward || result.bed_number) {
        const name = [result.ward, result.bed_number].filter(Boolean).join(' – ')
        return `${name} reserved and confirmed.`
      }
      return result.bed_id
        ? `Bed ${String(result.bed_id).slice(-6)} reserved. Ward has been notified.`
        : `${pl(result.beds_reserved, 'bed')} reserved and confirmed.`
    }

    // ── ICU ─────────────────────────────────────────────────────────────────
    case 'sa_icu_census': {
      const total = n(result.icu_occupied) + n(result.icu_available)
      const pct   = total > 0 ? Math.round((n(result.icu_occupied) / total) * 100) : 0
      return `ICU at ${pct}% capacity (${n(result.icu_occupied)}/${total} beds). ` +
        `${n(result.non_icu_admitted)} ward patients also tracked.`
    }
    case 'sa_icu_analysis': {
      const parts: string[] = []
      if (n(result.step_down) > 0)    parts.push(`${pl(result.step_down, 'patient')} suitable for step-down to general ward`)
      if (n(result.escalations) > 0)  parts.push(`${pl(result.escalations, 'escalation')} flagged`)
      if (n(result.critical_vitals) > 0) parts.push(`${pl(result.critical_vitals, 'patient')} with critical vitals`)
      return parts.length > 0 ? parts.join('; ') + '.' : 'All ICU patients stable — no transfers recommended.'
    }
    case 'sa_icu_confirm':
      return n(result.critical_vitals_flagged) > 0
        ? `${pl(result.critical_vitals_flagged, 'patient')} with critical vitals escalated to attending physician.`
        : 'No critical vitals requiring immediate escalation.'

    // ── Staff ────────────────────────────────────────────────────────────────
    case 'sa_staff_census': {
      const overdue = n(result.overdue_tasks)
      return `${pl(result.wards, 'ward')} checked, ${pl(result.total_patients, 'patient')} on floor. ` +
        `${n(result.total_tasks)} nursing tasks (${overdue} overdue).`
    }
    case 'sa_staff_analysis': {
      const wards = Array.isArray(result.high_pressure_wards) ? result.high_pressure_wards as string[] : []
      if (n(result.recommendations) === 0) return 'All wards within acceptable nurse-to-patient ratios.'
      const wardNote = wards.length > 0 ? ` High pressure: ${wards.slice(0, 3).join(', ')}.` : ''
      return `${pl(result.recommendations, 'reallocation')} recommended.${wardNote}`
    }
    case 'sa_staff_confirm':
      return result.summary
        ? String(result.summary).slice(0, 200)
        : `${pl(result.recommendations, 'staffing action')} confirmed and dispatched.`

    // ── Discharge ────────────────────────────────────────────────────────────
    case 'sa_discharge_candidates':
      return n(result.candidate_count) > 0
        ? `${pl(result.candidate_count, 'patient')} identified as potentially discharge-eligible.`
        : 'No discharge-eligible patients identified at this time.'
    case 'sa_discharge_assessment': {
      const ready   = n(result.ready)
      const blocked = n(result.blocked)
      return `${pl(result.assessed, 'patient')} assessed — ` +
        `${ready} discharge-ready, ${blocked} with unresolved barriers.`
    }
    case 'sa_discharge_confirm':
      return `${pl(result.updated, 'patient record')} updated with discharge status.`
    case 'sa_discharge_summary':
      return `${pl(result.summaries_generated, 'AI discharge summary')} generated and filed.`

    // ── OT ───────────────────────────────────────────────────────────────────
    case 'sa_ot_census': {
      const upcoming = n(result.upcoming_count)
      const surgeryWord = upcoming === 1 ? 'surgery' : 'surgeries'
      const postOp = n(result.post_op_beds_available)
      const postOpNote = postOp > 0 ? ` ${postOp} post-op bed${postOp === 1 ? '' : 's'} available.` : ''
      return `${upcoming} ${surgeryWord} scheduled today. ${pl(result.rooms, 'OT room')} available.${postOpNote}`
    }
    case 'sa_ot_analysis': {
      if (result.summary) return String(result.summary).slice(0, 200)
      const proceed  = n(result.proceed_count)
      const delay    = n(result.delay_count)
      const escalate = n(result.escalate_count)
      const total    = proceed + delay + escalate
      if (total === 0) return 'No scheduled cases to evaluate.'
      const parts: string[] = []
      if (proceed  > 0) parts.push(`${proceed} can proceed`)
      if (delay    > 0) parts.push(`${delay} delayed`)
      if (escalate > 0) parts.push(`${escalate} require escalation`)
      return parts.join(', ') + '.'
    }

    // ── Bed Prediction ───────────────────────────────────────────────────────
    case 'sa_bed_pred_census': {
      const pct = n(result.occupancy_pct)
      return `Hospital at ${pct}% occupancy (${n(result.total_beds)} beds). ` +
        `${pl(result.discharge_ready, 'patient')} discharge-ready. ` +
        `ER pressure: ~${n(result.er_pressure)} estimated admissions.`
    }
    case 'sa_bed_pred_forecast': {
      const freeing = n(result.beds_freeing_4h)
      const needed  = n(result.beds_needed)
      const gap     = needed - freeing
      const gapNote = gap > 0 ? ` Shortfall of ${gap} beds.` : ' Capacity looks manageable.'
      return `Overflow risk: ${risk(result.overflow_risk)}. ICU risk: ${risk(result.icu_risk)}.` +
        ` ${freeing} beds freeing within 4h, ${needed} needed.${gapNote}`
    }

    // ── Bed Cleaning ──────────────────────────────────────────────────────────
    case 'sa_bed_cleaning':
      return n(result.dispatched) > 0
        ? `Housekeeping dispatched to ${pl(result.dispatched, 'bed')} — rooms queued for cleaning.`
        : 'No vacated beds requiring cleaning at this time.'

    // ── Housekeeping (legacy) ─────────────────────────────────────────────────
    case 'sa_hk_census':
      return n(result.vacated_count) > 0
        ? `${pl(result.vacated_count, 'bed')} vacated and awaiting cleaning.`
        : 'No vacated beds requiring cleaning at this time.'
    case 'sa_hk_dispatch':
      return n(result.dispatched) > 0
        ? `Housekeeping team dispatched to ${pl(result.dispatched, 'bed')}.`
        : 'No cleaning tasks dispatched.'

    // ── Pharmacy ─────────────────────────────────────────────────────────────
    case 'sa_pharmacy_census':
      return `${pl(result.discharge_ready_count, 'discharge-ready patient')} flagged for medication reconciliation.`
    case 'sa_pharmacy_check': {
      const incomplete = n(result.incomplete)
      return incomplete > 0
        ? `Reconciliation: ${n(result.checked)} reviewed — ${n(result.complete)} complete, ${incomplete} requiring follow-up.`
        : `All ${n(result.checked)} medication reconciliations complete.`
    }

    // ── Revenue ───────────────────────────────────────────────────────────────
    case 'sa_rev_invoice_census': {
      const total   = n(result.total_outstanding_count)
      const amount  = Number(result.total_outstanding_amount ?? 0)
      const aged    = n(result.over_30d)
      const gaps    = n(result.ipd_billing_gaps)
      const parts: string[] = [`${total} outstanding invoice${total === 1 ? '' : 's'} totalling ₹${amount.toLocaleString('en-IN')}.`]
      if (aged > 0)  parts.push(`${aged} over 30 days old.`)
      if (gaps > 0)  parts.push(`${gaps} IPD patient${gaps === 1 ? '' : 's'} admitted with no invoice raised.`)
      return parts.join(' ')
    }
    case 'sa_rev_collections': {
      const today = Number(result.today_total ?? 0)
      const dod   = Number(result.day_over_day_pct ?? 0)
      const sign  = dod >= 0 ? '+' : ''
      const recon = result.is_reconciled ? 'Reconciled.' : 'Not yet reconciled.'
      return `Today's collections: ₹${today.toLocaleString('en-IN')} (${sign}${dod}% vs yesterday). ${recon}`
    }
    case 'sa_rev_claims': {
      const pending = n(result.pending_count)
      const denied  = n(result.denied_count)
      const deniedAmt = Number(result.denied_amount ?? 0)
      if (pending === 0 && denied === 0) return `All ${n(result.total_claims)} claims settled — no action needed.`
      const parts: string[] = []
      if (pending > 0) parts.push(`${pending} pending`)
      if (denied  > 0) parts.push(`${denied} denied (₹${deniedAmt.toLocaleString('en-IN')} at risk)`)
      return `Claims: ${parts.join(', ')}. ${n(result.settled_count)} settled.`
    }
    case 'sa_rev_analyst': {
      const riskLabel = String(result.risk_level ?? 'unknown').toUpperCase()
      const actions = Array.isArray(result.top_actions) ? result.top_actions as Array<{priority: number; action: string; impact: string}> : []
      const topAction = actions[0]
      const suffix = topAction ? ` Top action: ${topAction.action} (${topAction.impact}).` : ''
      return `Revenue risk: ${riskLabel}.${suffix}`
    }
    case 'sa_rev_patient_billing': {
      if (result.error) return String(result.error)
      const token = result.patient_token ? `Patient ${String(result.patient_token)}` : 'Patient'
      const outstanding = Number(result.outstanding ?? 0)
      const billed = Number(result.total_billed ?? 0)
      const paid   = Number(result.total_paid ?? 0)
      const parts: string[] = [
        `${token}: ₹${billed.toLocaleString('en-IN')} billed, ₹${paid.toLocaleString('en-IN')} paid.`,
      ]
      if (outstanding > 0) parts.push(`₹${outstanding.toLocaleString('en-IN')} outstanding.`)
      else parts.push('Account settled.')
      if (n(result.claims_denied) > 0) parts.push(`${n(result.claims_denied)} denied claim(s) — resubmission needed.`)
      else if (n(result.claims_pending) > 0) parts.push(`${n(result.claims_pending)} claim(s) pending.`)
      return parts.join(' ')
    }

    // ── Ambulance ────────────────────────────────────────────────────────────
    case 'sa_ambulance_census': {
      const available = n(result.available)
      const total     = n(result.total)
      if (total === 0) return 'No ambulances registered in fleet.'
      return `${available} of ${total} ambulance${total === 1 ? '' : 's'} available.`
    }
    case 'sa_ambulance_assign': {
      const vehicle  = result.assigned ? String(result.assigned) : null
      const eta      = n(result.eta_mins)
      const escalate = result.escalate === true
      if (!vehicle) return escalate ? 'No units available — escalation required.' : 'No units available.'
      const base = `Unit ${vehicle} selected, ETA ${eta} min.`
      return escalate ? base + ' Escalation also recommended.' : base
    }
    case 'sa_ambulance_confirm': {
      const vehicle = result.vehicle ? String(result.vehicle) : null
      const eta     = n(result.eta_mins)
      if (!vehicle) return result.summary ? String(result.summary).slice(0, 200) : 'Dispatch confirmed.'
      return `Unit ${vehicle} dispatched, ETA ${eta} min.`
    }

    // ── Patient Identification ────────────────────────────────────────────────
    case 'sa_patient_identification': {
      const verified = n(result.verified_count)
      const unknown  = n(result.unknown_count)
      const patients = Array.isArray(result.patients)
        ? (result.patients as Array<{ patient_name?: string; known_patient?: boolean }>)
        : []
      const knownNames = patients.filter((p) => p.known_patient).map((p) => p.patient_name).filter(Boolean)
      if (knownNames.length > 0) {
        const shown = knownNames.slice(0, 2).join(', ')
        const extra = knownNames.length > 2 ? ` +${knownNames.length - 2} more` : ''
        const unknownNote = unknown > 0 ? ` ${unknown} unrecognised mobile${unknown === 1 ? '' : 's'}.` : ''
        return `Identified: ${shown}${extra}.${unknownNote}`
      }
      if (verified === 0 && unknown === 0) return 'Patient identification completed.'
      const parts: string[] = []
      if (verified > 0) parts.push(`${verified} matched`)
      if (unknown  > 0) parts.push(`${unknown} unrecognised`)
      return `Identification: ${parts.join(', ')}.`
    }

    // ── Revenue (extended) ────────────────────────────────────────────────────
    case 'sa_rev_initiate_billing': {
      if (result.status === 'no_patient' || result.error) return 'No patient resolved — billing request not staged.'
      const count = n(result.patient_count)
      return count > 0 ? `Billing staged for ${pl(count, 'patient')}.` : 'Billing request processed.'
    }
    case 'sa_rev_denial_prevention': {
      const high   = n(result.high_risk_count)
      const medium = n(result.medium_risk_count)
      const esc    = n(result.escalation_count)
      const prob   = Number(result.denial_probability ?? 0)
      if (high === 0 && medium === 0) return 'No high-risk denial patterns detected.'
      const parts: string[] = []
      if (high   > 0) parts.push(`${pl(high, 'high-risk claim')} (${prob.toFixed(0)}% denial probability)`)
      if (medium > 0) parts.push(`${pl(medium, 'medium-risk claim')}`)
      return parts.join(', ') + (esc > 0 ? `. ${pl(esc, 'escalation')} recommended.` : '.')
    }

    // ── ER (individual acuity broadcasts) ────────────────────────────────────
    case 'sa_er_code_blue':
      return result.code_blue_triggered === true
        ? 'Code blue triggered — cardiac arrest protocol activated.'
        : n(result.cardiac_arrest_suspected) > 0
          ? `${pl(result.cardiac_arrest_suspected, 'cardiac arrest suspect')} detected — criteria not met for code blue.`
          : 'No cardiac arrest detected.'
    case 'sa_er_spo2_critical':
      return n(result.escalated) > 0
        ? `${pl(result.escalated, 'SpO₂-critical patient')} escalated — oxygen therapy protocol initiated.`
        : 'No critical SpO₂ readings at this time.'
    case 'sa_er_protocol': {
      const activated = n(result.protocol_count)
      if (activated === 0) return 'No clinical protocols triggered at this time.'
      const names = result.protocols && typeof result.protocols === 'object'
        ? Object.keys(result.protocols as Record<string, unknown>).join(', ')
        : ''
      return `${pl(activated, 'clinical protocol')} activated${names ? ': ' + names : ''}.`
    }
    case 'sa_er_specialist': {
      const notified = n(result.notified)
      if (notified === 0) return 'No specialist notifications required.'
      const names = Array.isArray(result.specialists_notified)
        ? (result.specialists_notified as string[]).slice(0, 3).join(', ')
        : ''
      return `${pl(notified, 'specialist')} notified${names ? ': ' + names : ''}.`
    }
    case 'sa_er_boarding': {
      const boarders = n(result.boarders_count)
      const breached = n(result.sla_breached)
      if (boarders === 0) return 'No ER boarders at this time.'
      return breached > 0
        ? `${pl(boarders, 'ER boarder')} — ${breached} SLA breach${breached === 1 ? '' : 'es'} escalated.`
        : `${pl(boarders, 'ER boarder')} monitored — no SLA breaches.`
    }

    // ── ER (extended grouped) ─────────────────────────────────────────────────
    case 'sa_er_acuity_response': {
      const parts: string[] = []
      if (result.code_blue_triggered === true) parts.push('Code blue triggered')
      if (n(result.spo2_critical)    > 0) parts.push(`${pl(result.spo2_critical, 'SpO₂-critical patient')} escalated`)
      if (n(result.protocol_count)   > 0) parts.push(`${n(result.protocol_count)} clinical protocol${n(result.protocol_count) === 1 ? '' : 's'} activated`)
      if (n(result.notified)         > 0) parts.push(`${pl(result.notified, 'specialist')} notified`)
      return parts.length > 0 ? parts.join('; ') + '.' : 'Acuity checks complete — no critical events.'
    }
    case 'sa_er_disposition': {
      const critical = n(result.critical_selected)
      const fast     = n(result.fasttrack_candidates)
      const parts: string[] = []
      if (critical > 0) parts.push(`${pl(critical, 'critical patient')} cleared for bed placement`)
      if (fast     > 0) parts.push(`${pl(fast, 'fast-track candidate')} routed`)
      return parts.length > 0 ? parts.join('; ') + '.' : 'No critical or fast-track dispositions at this time.'
    }

    // ── ICU (extended) ────────────────────────────────────────────────────────
    case 'sa_icu_transfer': {
      const ventDep  = n(result.ventilator_dependent_count)
      const detRisk  = n(result.deterioration_risk_count)
      const overflow = result.overflow_triggered === true
      const approval = result.approval_id ? String(result.approval_id).slice(-8) : null
      const parts: string[] = []
      if (ventDep > 0) parts.push(`${pl(ventDep, 'ventilator-dependent patient')}`)
      if (detRisk > 0) parts.push(`${pl(detRisk, 'deterioration-risk patient')}`)
      const base = parts.length > 0 ? `ICU admission ranked — ${parts.join(', ')}.` : 'ICU transfer evaluation complete.'
      return base + (approval ? ` Approval staged (…${approval}).` : '') + (overflow ? ' Overflow evaluation triggered.' : '')
    }

    default: {
      if (result.recommendation)           return String(result.recommendation).slice(0, 200)
      if (result.active_er_count != null)  return `${pl(result.active_er_count, 'patient')} in ER.`
      if (result.candidate_count != null)  return `${pl(result.candidate_count, 'candidate')} found.`
      if (result.triaged         != null)  return `${pl(result.triaged, 'patient')} triaged, ${pl(result.critical, 'critical')}.`
      if (result.wards           != null)  return `${pl(result.wards, 'ward')}, ${pl(result.total_patients, 'patient')}.`
      return 'Completed.'
    }
  }
}

// ── Key-value detail chips per sub-agent ─────────────────────────────────────

function detail(subAgentId: string, result: Record<string, unknown> | undefined): { label: string; value: string }[] {
  if (!result) return []
  const n   = (v: unknown) => Number(v ?? 0)
  const fmt = (v: number)  => `₹${v.toLocaleString('en-IN')}`

  switch (subAgentId) {
    // ── ER ──────────────────────────────────────────────────────────────────
    case 'sa_er_census': {
      const count = n(result.active_er_count ?? result.untriaged_count)
      return [{ label: 'In queue', value: String(count) }]
    }
    case 'sa_er_triage':
      return [
        { label: 'CTAS 1', value: String(n(result.ctas_1)) },
        { label: 'CTAS 2', value: String(n(result.ctas_2)) },
        { label: 'Total',  value: String(n(result.triaged)) },
      ]
    case 'sa_er_save': {
      const chips: { label: string; value: string }[] = [{ label: 'Saved', value: String(n(result.saved)) }]
      if (n(result.critical_alerts) > 0) chips.push({ label: 'Critical alerts', value: String(n(result.critical_alerts)) })
      return chips
    }
    case 'sa_er_fasttrack':
      return n(result.fasttrack_candidates) > 0
        ? [{ label: 'Fast-track', value: String(n(result.fasttrack_candidates)) }]
        : []
    case 'sa_er_critical_select':
      return [{ label: 'Selected', value: String(n(result.critical_selected)) }]

    // ── Bed ─────────────────────────────────────────────────────────────────
    case 'sa_bed_availability': {
      const chips: { label: string; value: string }[] = []
      if (result.icu_count     != null) chips.push({ label: 'ICU',     value: String(n(result.icu_count)) })
      if (result.hdu_count     != null) chips.push({ label: 'HDU',     value: String(n(result.hdu_count)) })
      if (result.general_count != null) chips.push({ label: 'General', value: String(n(result.general_count)) })
      if (chips.length === 0)           chips.push({ label: 'Available', value: String(n(result.candidate_count)) })
      return chips
    }
    case 'sa_bed_batch_match':
      return [
        { label: 'Matched',   value: String(n(result.assigned)) },
        { label: 'Unmatched', value: String(n(result.requested) - n(result.assigned)) },
      ]
    case 'sa_bed_reservation':
      return [{ label: 'Reserved', value: String(n(result.beds_reserved) || 1) }]

    // ── ICU ─────────────────────────────────────────────────────────────────
    case 'sa_icu_census':
      return [
        { label: 'Occupied',  value: String(n(result.icu_occupied)) },
        { label: 'Available', value: String(n(result.icu_available)) },
      ]
    case 'sa_icu_analysis': {
      const chips: { label: string; value: string }[] = []
      if (n(result.step_down)      > 0) chips.push({ label: 'Step-down',      value: String(n(result.step_down)) })
      if (n(result.escalations)    > 0) chips.push({ label: 'Escalations',    value: String(n(result.escalations)) })
      if (n(result.critical_vitals) > 0) chips.push({ label: 'Critical vitals', value: String(n(result.critical_vitals)) })
      return chips
    }
    case 'sa_icu_confirm':
      return n(result.critical_vitals_flagged) > 0
        ? [{ label: 'Escalated', value: String(n(result.critical_vitals_flagged)) }]
        : []

    // ── Staff ────────────────────────────────────────────────────────────────
    case 'sa_staff_census': {
      const chips: { label: string; value: string }[] = [
        { label: 'Wards',    value: String(n(result.wards)) },
        { label: 'Patients', value: String(n(result.total_patients)) },
      ]
      if (n(result.overdue_tasks) > 0) chips.push({ label: 'Overdue tasks', value: String(n(result.overdue_tasks)) })
      return chips
    }
    case 'sa_staff_analysis':
      return [{ label: 'Reallocs recommended', value: String(n(result.recommendations)) }]

    // ── Discharge ────────────────────────────────────────────────────────────
    case 'sa_discharge_candidates':
      return [{ label: 'Candidates', value: String(n(result.candidate_count)) }]
    case 'sa_discharge_assessment':
      return [
        { label: 'Ready',   value: String(n(result.ready)) },
        { label: 'Blocked', value: String(n(result.blocked)) },
      ]

    // ── OT ───────────────────────────────────────────────────────────────────
    case 'sa_ot_census': {
      const chips: { label: string; value: string }[] = [
        { label: 'Surgeries', value: String(n(result.upcoming_count)) },
        { label: 'Rooms',     value: String(n(result.rooms)) },
      ]
      if (n(result.post_op_beds_available) > 0)
        chips.push({ label: 'Post-op beds', value: String(n(result.post_op_beds_available)) })
      return chips
    }
    case 'sa_ot_analysis': {
      const chips: { label: string; value: string }[] = []
      if (n(result.proceed_count)  > 0) chips.push({ label: 'Proceed',   value: String(n(result.proceed_count)) })
      if (n(result.delay_count)    > 0) chips.push({ label: 'Delayed',   value: String(n(result.delay_count)) })
      if (n(result.escalate_count) > 0) chips.push({ label: 'Escalate',  value: String(n(result.escalate_count)) })
      if (result.efficiency_score  != null) chips.push({ label: 'Efficiency', value: `${Number(result.efficiency_score).toFixed(0)}%` })
      return chips
    }

    // ── Bed Prediction ───────────────────────────────────────────────────────
    case 'sa_bed_pred_census':
      return [
        { label: 'Occupancy',       value: `${n(result.occupancy_pct)}%` },
        { label: 'Discharge ready', value: String(n(result.discharge_ready)) },
        { label: 'ER pressure',     value: String(n(result.er_pressure)) },
      ]
    case 'sa_bed_pred_forecast':
      return [
        { label: 'Overflow', value: String(result.overflow_risk ?? 'unknown').toUpperCase() },
        { label: 'ICU risk', value: String(result.icu_risk     ?? 'unknown').toUpperCase() },
        { label: 'Beds freeing (4h)', value: String(n(result.beds_freeing_4h)) },
      ]

    // ── Pharmacy ─────────────────────────────────────────────────────────────
    case 'sa_pharmacy_check':
      return [
        { label: 'Complete', value: String(n(result.complete)) },
        { label: 'Pending',  value: String(n(result.incomplete)) },
      ]

    // ── Revenue ───────────────────────────────────────────────────────────────
    case 'sa_rev_invoice_census': {
      const chips: { label: string; value: string }[] = [
        { label: 'Outstanding', value: String(n(result.total_outstanding_count)) },
      ]
      if (n(result.over_30d)         > 0) chips.push({ label: 'Aged >30d',   value: String(n(result.over_30d)) })
      if (n(result.ipd_billing_gaps) > 0) chips.push({ label: 'Billing gaps', value: String(n(result.ipd_billing_gaps)) })
      return chips
    }
    case 'sa_rev_collections': {
      const today = Number(result.today_total ?? 0)
      const dod   = Number(result.day_over_day_pct ?? 0)
      const sign  = dod >= 0 ? '+' : ''
      return [
        { label: 'Collected',    value: fmt(today) },
        { label: 'vs yesterday', value: `${sign}${dod}%` },
      ]
    }
    case 'sa_rev_claims': {
      const chips: { label: string; value: string }[] = []
      if (n(result.pending_count) > 0) chips.push({ label: 'Pending', value: String(n(result.pending_count)) })
      if (n(result.denied_count)  > 0) chips.push({ label: 'Denied',  value: String(n(result.denied_count)) })
      chips.push({ label: 'Settled', value: String(n(result.settled_count)) })
      return chips
    }
    case 'sa_rev_analyst':
      return [{ label: 'Risk level', value: String(result.risk_level ?? 'unknown').toUpperCase() }]
    case 'sa_rev_patient_billing': {
      const chips: { label: string; value: string }[] = [
        { label: 'Billed', value: fmt(Number(result.total_billed ?? 0)) },
        { label: 'Paid',   value: fmt(Number(result.total_paid   ?? 0)) },
      ]
      if (Number(result.outstanding ?? 0) > 0) chips.push({ label: 'Outstanding', value: fmt(Number(result.outstanding)) })
      return chips
    }

    // ── ER (individual acuity broadcasts) ────────────────────────────────────
    case 'sa_er_code_blue':
      return result.code_blue_triggered === true
        ? [{ label: 'Code blue', value: 'Active' }]
        : n(result.cardiac_arrest_suspected) > 0
          ? [{ label: 'Suspects', value: String(n(result.cardiac_arrest_suspected)) }]
          : []
    case 'sa_er_spo2_critical': {
      const chips: { label: string; value: string }[] = []
      if (n(result.spo2_critical) > 0) chips.push({ label: 'SpO₂ crit.', value: String(n(result.spo2_critical)) })
      if (n(result.escalated)     > 0) chips.push({ label: 'Escalated',  value: String(n(result.escalated)) })
      return chips
    }
    case 'sa_er_protocol':
      return n(result.protocol_count) > 0
        ? [{ label: 'Protocols', value: String(n(result.protocol_count)) }]
        : []
    case 'sa_er_specialist':
      return n(result.notified) > 0
        ? [{ label: 'Notified', value: String(n(result.notified)) }]
        : []
    case 'sa_er_boarding': {
      const chips: { label: string; value: string }[] = []
      if (n(result.boarders_count) > 0) chips.push({ label: 'Boarders',     value: String(n(result.boarders_count)) })
      if (n(result.sla_breached)   > 0) chips.push({ label: 'SLA breached', value: String(n(result.sla_breached)) })
      return chips
    }

    // ── Revenue (extended) ────────────────────────────────────────────────────
    case 'sa_rev_initiate_billing': {
      const chips: { label: string; value: string }[] = []
      if (result.patient_count != null) chips.push({ label: 'Patients', value: String(n(result.patient_count)) })
      if (result.status)                chips.push({ label: 'Status',   value: String(result.status) })
      return chips
    }
    case 'sa_rev_denial_prevention': {
      const chips: { label: string; value: string }[] = []
      if (n(result.high_risk_count)   > 0) chips.push({ label: 'High risk',   value: String(n(result.high_risk_count)) })
      if (n(result.medium_risk_count) > 0) chips.push({ label: 'Med risk',    value: String(n(result.medium_risk_count)) })
      if (result.denial_probability  != null) chips.push({ label: 'Deny prob', value: `${Number(result.denial_probability).toFixed(0)}%` })
      if (n(result.escalation_count)  > 0) chips.push({ label: 'Escalations', value: String(n(result.escalation_count)) })
      return chips
    }

    // ── ER (extended) ─────────────────────────────────────────────────────────
    case 'sa_er_acuity_response': {
      const chips: { label: string; value: string }[] = []
      if (result.code_blue_triggered === true) chips.push({ label: 'Code blue',   value: 'Active' })
      if (n(result.spo2_critical)    > 0)      chips.push({ label: 'SpO₂ crit.',  value: String(n(result.spo2_critical)) })
      if (n(result.protocol_count)   > 0)      chips.push({ label: 'Protocols',   value: String(n(result.protocol_count)) })
      if (n(result.notified)         > 0)      chips.push({ label: 'Specialists', value: String(n(result.notified)) })
      return chips
    }
    case 'sa_er_disposition':
      return [
        { label: 'Critical',    value: String(n(result.critical_selected)) },
        { label: 'Fast-track',  value: String(n(result.fasttrack_candidates)) },
      ]

    // ── ICU (extended) ────────────────────────────────────────────────────────
    case 'sa_icu_transfer': {
      const chips: { label: string; value: string }[] = []
      if (result.ventilator_dependent_count != null) chips.push({ label: 'Ventilator', value: String(n(result.ventilator_dependent_count)) })
      if (result.deterioration_risk_count   != null) chips.push({ label: 'Deterioration', value: String(n(result.deterioration_risk_count)) })
      if (result.overflow_triggered === true)         chips.push({ label: 'Overflow', value: 'Triggered' })
      return chips
    }

    // ── Ambulance ────────────────────────────────────────────────────────────
    case 'sa_ambulance_census':
      return [
        { label: 'Available', value: String(n(result.available)) },
        { label: 'Total',     value: String(n(result.total)) },
      ]
    case 'sa_ambulance_assign': {
      const chips: { label: string; value: string }[] = []
      if (result.assigned)         chips.push({ label: 'Unit', value: String(result.assigned) })
      if (result.eta_mins != null) chips.push({ label: 'ETA',  value: `${n(result.eta_mins)} min` })
      if (result.escalate === true) chips.push({ label: 'Escalation', value: 'Required' })
      return chips
    }
    case 'sa_ambulance_confirm': {
      const chips: { label: string; value: string }[] = []
      if (result.vehicle)          chips.push({ label: 'Unit', value: String(result.vehicle) })
      if (result.eta_mins != null) chips.push({ label: 'ETA',  value: `${n(result.eta_mins)} min` })
      return chips
    }

    // ── Patient Identification ────────────────────────────────────────────────
    case 'sa_patient_identification': {
      const chips: { label: string; value: string }[] = []
      const patients = Array.isArray(result.patients)
        ? (result.patients as Array<{ patient_name?: string; known_patient?: boolean }>)
        : []
      const known = patients.filter((p) => p.known_patient)
      if (known.length > 0 && known[0].patient_name) {
        chips.push({ label: 'Verified', value: String(known[0].patient_name) })
        if (known.length > 1) chips.push({ label: '+more', value: String(known.length - 1) })
      } else if (result.verified_count != null) {
        chips.push({ label: 'Matched', value: String(n(result.verified_count)) })
      }
      if (n(result.unknown_count) > 0) chips.push({ label: 'Unknown', value: String(n(result.unknown_count)) })
      return chips
    }

    default:
      return []
  }
}

// ── Build ordered render list from raw events ─────────────────────────────────

type RenderItem =
  | { kind: 'subagent'; subAgentId: string; status: 'running' | 'complete'; result?: Record<string, unknown> }
  | { kind: 'alert';    message: string; severity: string; count: number }

function buildRenderList(events: SubAgentEvent[]): RenderItem[] {
  const completedIds = new Set(
    events.filter((e) => e.type === 'completed').map((e) => e.subAgentId)
  )
  const seenStarted   = new Set<string>()
  const seenCompleted = new Set<string>()
  const items: RenderItem[] = []

  for (const ev of events) {
    if (ev.type === 'started') {
      if (!seenStarted.has(ev.subAgentId)) {
        seenStarted.add(ev.subAgentId)
        if (!completedIds.has(ev.subAgentId)) {
          items.push({ kind: 'subagent', subAgentId: ev.subAgentId, status: 'running' })
        }
      }
    } else if (ev.type === 'completed') {
      if (!seenCompleted.has(ev.subAgentId)) {
        seenCompleted.add(ev.subAgentId)
        items.push({ kind: 'subagent', subAgentId: ev.subAgentId, status: 'complete', result: ev.result })
      }
    } else if (ev.type === 'alert') {
      const msg      = ev.message ?? ''
      const severity = ev.severity ?? 'info'
      const last     = items[items.length - 1]
      // Deduplicate consecutive identical alerts — increment count badge instead of new row
      if (last?.kind === 'alert' && last.message === msg && last.severity === severity) {
        last.count += 1
      } else {
        items.push({ kind: 'alert', message: msg, severity, count: 1 })
      }
    }
  }
  return items
}

// ── Component ─────────────────────────────────────────────────────────────────

// ── Autonomous Decisions stream ──────────────────────────────────────────────
// Renders the policy engine's decisions for the current session (autonomous mode).
const OUTCOME_META: Record<PolicyDecision['outcome'], { label: string; color: string; Icon: typeof CheckCircle }> = {
  auto_approve:  { label: 'Auto-approved', color: '#14b8a6', Icon: CheckCircle },
  require_human: { label: 'Needs human',   color: '#f59e0b', Icon: AlertTriangle },
  escalate:      { label: 'Escalated',     color: '#ef4444', Icon: ArrowUpCircle },
}

function riskColor(risk: string) {
  return risk === 'high' ? '#ef4444' : risk === 'medium' ? '#f59e0b' : '#64748b'
}

function PolicyDecisions({ decisions }: { decisions: PolicyDecision[] }) {
  // Collapsed by default: the decision stream can grow tall enough to push the
  // session recommendation out of view entirely. Starting closed keeps the count
  // badge visible (so nothing is hidden) without blocking the rest of the panel;
  // expand on demand to review.
  const [expanded, setExpanded] = useState(false)
  const autoCount = decisions.filter((d) => d.outcome === 'auto_approve').length
  const flagged = decisions.length - autoCount
  const ordered = [...decisions].reverse()   // newest first
  return (
    <div className="border-b border-[var(--border)] px-3 py-2 flex-shrink-0">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 mb-1.5"
      >
        {expanded
          ? <ChevronDown size={13} className="text-slate-500 flex-shrink-0" />
          : <ChevronRight size={13} className="text-slate-500 flex-shrink-0" />}
        <ShieldCheck size={13} className="text-blue-400 flex-shrink-0" />
        <span className="text-sm font-semibold text-slate-500 uppercase tracking-wider">Autonomous Decisions</span>
        <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-400 border border-blue-500/30">
          {autoCount} auto{flagged ? ` · ${flagged} flagged` : ''}
        </span>
      </button>
      {!expanded ? null : (
      <div className="flex flex-col gap-1.5 max-h-52 overflow-y-auto pr-0.5">
        {ordered.map((d) => {
          const m = OUTCOME_META[d.outcome]
          const Icon = m.Icon
          return (
            <div
              key={d.id}
              className="rounded-lg px-2.5 py-2 bg-[var(--bg-surface)] border border-[var(--border-a)]"
              style={{ borderLeft: `3px solid ${m.color}` }}
            >
              <div className="flex items-center gap-1.5">
                <Icon size={12} style={{ color: m.color }} className="flex-shrink-0" />
                <span className="text-[11px] font-semibold" style={{ color: m.color }}>{m.label}</span>
                <span className="text-[11px] text-slate-300 truncate">{(d.actionType || d.kind).replace(/_/g, ' ')}</span>
                <span
                  className="ml-auto text-[9px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 uppercase"
                  style={{ background: riskColor(d.risk) + '22', color: riskColor(d.risk) }}
                >
                  {d.risk}
                </span>
              </div>
              {d.reason && (
                <div className="text-[10px] text-slate-500 leading-snug mt-1 break-words">{d.reason}</div>
              )}
            </div>
          )
        })}
      </div>
      )}
    </div>
  )
}

// ── Checkpoints (revert points) — only meaningful while paused ─────────────────
// Read-only reference list; actually acting on a checkpoint (rewind, skip, reorder)
// happens on the dedicated Edit Checkpoint screen (see CheckpointEditorScreen),
// opened from the canvas's Paused controls.
function CheckpointsList({
  checkpoints, checkpointsLoading,
}: {
  checkpoints: Checkpoint[]
  checkpointsLoading: boolean
}) {
  const [expanded, setExpanded] = useState(true)
  return (
    <div className="border-b border-[var(--border)] px-3 py-2 flex-shrink-0">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-1.5 mb-1.5"
      >
        {expanded
          ? <ChevronDown size={12} className="text-slate-500 flex-shrink-0" />
          : <ChevronRight size={12} className="text-slate-500 flex-shrink-0" />}
        <RotateCcw size={13} className="text-amber-400 flex-shrink-0" />
        <span className="text-sm font-semibold text-slate-500 uppercase tracking-wider">Checkpoints</span>
        <span className="ml-auto text-[10px] font-bold text-slate-600">{checkpoints.length}</span>
      </button>
      {!expanded ? null : checkpointsLoading ? (
        <div className="flex items-center gap-2 text-slate-500 text-[10px] px-1 py-1.5">
          <Loader2 size={11} className="animate-spin" /> Loading revert points…
        </div>
      ) : checkpoints.length === 0 ? (
        <p className="text-[10px] text-slate-600 italic px-1 py-1">No revert points yet</p>
      ) : (
        <div className="flex flex-col gap-1.5 max-h-56 overflow-y-auto pr-0.5">
          {checkpoints.map((c) => (
            <div
              key={c.checkpoint_id}
              className="rounded-lg px-2.5 py-2 border bg-[var(--bg-surface)] border-[var(--border-a)]"
            >
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] font-semibold text-slate-300">Step {c.step}</span>
                <span className="ml-auto text-[9px] text-slate-600 flex-shrink-0">
                  {new Date(c.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
              {c.completed_agents.length > 0 && (
                <div className="text-[10px] text-slate-500 mt-1 leading-snug">
                  Kept: <span className="text-slate-400">{c.completed_agents.join(', ')}</span>
                </div>
              )}
              {c.next.length > 0 && (
                <div className="text-[10px] text-slate-500 mt-0.5 leading-snug">
                  Re-runs: <span className="text-amber-400">{c.next.join(', ')}</span>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function AgentFindings() {
  const nodeStates            = useStore((s) => s.nodeStates)
  const selectedNodeId        = useStore((s) => s.selectedNodeId)
  const selectNode            = useStore((s) => s.selectNode)
  const nodes                 = useStore((s) => s.nodes)
  const openSubAgent          = useStore((s) => s.openSubAgent)
  const sessionRecommendation = useStore((s) => s.sessionRecommendation)
  const synthesisRunning      = useStore((s) => s.synthesisRunning)
  const committedSession      = useStore((s) => s.committedSession)
  const commitSession         = useStore((s) => s.commitSession)
  const backendPipeline       = useStore((s) => s.backendPipeline)
  const policyDecisions       = useStore((s) => s.policyDecisions)
  const executionStatus       = useStore((s) => s.executionStatus)
  const checkpoints           = useStore((s) => s.checkpoints)
  const checkpointsLoading    = useStore((s) => s.checkpointsLoading)
  const bottomRef             = useRef<HTMLDivElement>(null)
  const [agentsExpanded, setAgentsExpanded] = useState(true)

  // Agent identity: static catalog first; fall back to the backend pipeline's own
  // label/color when no static entry exists (e.g. billing_agent).
  const resolveIdentity = (nodeId: string, agentId: string) => {
    const st = AGENT_MAP[agentId]
    if (st) return { label: st.label, emoji: st.emoji, color: st.color }
    const bp = backendPipeline?.agents.find((a) => a.id === nodeId)
    if (bp) return { label: bp.label, emoji: '💳', color: bp.color ?? '#94a3b8' }
    return null
  }

  const selectedState = selectedNodeId ? nodeStates[selectedNodeId] : null
  const selectedNode = selectedNodeId ? nodes.find((n) => n.id === selectedNodeId) : null
  const selectedAgent = selectedNode
    ? resolveIdentity(selectedNode.id, (selectedNode.data as { agentId: string }).agentId)
    : null

  const useRichEvents = (selectedState?.events?.length ?? 0) > 0

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [selectedState?.events?.length, selectedState?.lines?.length])

  // Ordered by canvas layout position (left-to-right, top-to-bottom) so the list
  // matches what's visually first in the pipeline, not insertion/backend order.
  const activeNodes = nodes
    .filter((n) => {
      const s = nodeStates[n.id]?.status
      return n.type === 'agentNode' && s && s !== 'idle'
    })
    .sort((a, b) => a.position.x - b.position.x || a.position.y - b.position.y)

  return (
    <div className="flex flex-col h-full bg-[var(--bg-base)]">
      {/* Autonomous Decisions — only rendered once the policy engine has acted */}
      {policyDecisions.length > 0 && <PolicyDecisions decisions={policyDecisions} />}

      {/* Checkpoints — only meaningful while paused */}
      {executionStatus === 'paused' && (
        <CheckpointsList
          checkpoints={checkpoints}
          checkpointsLoading={checkpointsLoading}
        />
      )}

      {/* Agent list — collapsible, and internally scrollable once expanded, so a long
          roster can't push the recommendation further down out of the panel entirely. */}
      <div className="border-b border-[var(--border)] px-3 py-2 flex-shrink-0">
        <button
          onClick={() => setAgentsExpanded((v) => !v)}
          className="w-full flex items-center gap-1.5 mb-1.5"
        >
          {agentsExpanded
            ? <ChevronDown size={12} className="text-slate-500 flex-shrink-0" />
            : <ChevronRight size={12} className="text-slate-500 flex-shrink-0" />}
          <span className="text-sm font-semibold text-slate-500 uppercase tracking-wider">Agents</span>
          <span className="ml-auto text-[10px] font-bold text-slate-600">{activeNodes.length}</span>
        </button>
        {!agentsExpanded ? null : (
        <div className="flex flex-col gap-1 max-h-56 overflow-y-auto pr-0.5">
          {activeNodes.length === 0 && (
            <span className="text-sm text-slate-600 italic">Execution output will appear here</span>
          )}
          {activeNodes.map((n) => {
            const agentId = (n.data as { agentId: string }).agentId
            const ident = resolveIdentity(n.id, agentId)
            const state = nodeStates[n.id]
            const isSelected = selectedNodeId === n.id
            return (
              <button
                key={n.id}
                onClick={() => selectNode(n.id)}
                className={clsx(
                  'flex items-center gap-2 px-2 py-1 rounded-lg text-left transition-colors text-sm',
                  isSelected ? 'bg-[var(--bg-raised)]' : 'hover:bg-[var(--bg-surface)]'
                )}
              >
                <span>{ident?.emoji ?? '🤖'}</span>
                <span
                  className="flex-1 truncate"
                  title={ident?.label ?? agentId}
                  style={{ color: ident?.color ?? '#94a3b8' }}
                >
                  {ident?.label ?? agentId}
                </span>
                {state?.reused && (
                  <span
                    className="text-[9px] font-semibold px-1.5 py-0.5 rounded bg-slate-700/40 text-slate-400 flex-shrink-0"
                    title="Carried over from before the checkpoint — not re-executed"
                  >
                    Reused
                  </span>
                )}
                <StatusIcon status={state?.status ?? 'idle'} />
              </button>
            )
          })}
        </div>
        )}
      </div>

      {/* Output pane — agent findings + recommendation scroll together */}
      <div className="flex-1 min-h-0 overflow-y-auto p-3">
        {selectedAgent && selectedState ? (
          <>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-1.5">
                <span className="text-base">{selectedAgent.emoji}</span>
                <span className="text-sm font-semibold" style={{ color: selectedAgent.color }}>
                  {selectedAgent.label}
                </span>
              </div>
              <button
                onClick={() => openSubAgent(selectedNodeId!)}
                className="flex items-center gap-1 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                Sub-agents <ChevronRight size={11} />
              </button>
            </div>

            {useRichEvents ? (
              <RichEventList events={selectedState.events} agentRunning={selectedState.status === 'running'} pipeline={backendPipeline} />
            ) : selectedState.status === 'complete' && selectedState.lines.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center gap-2">
                <span className="text-2xl">📋</span>
                <div className="text-sm text-slate-400 font-medium">Session completed</div>
                <div className="text-xs text-slate-600 max-w-[200px]">Live output is only available during active execution.</div>
              </div>
            ) : (
              <div className="font-mono text-sm leading-relaxed space-y-0.5">
                {selectedState.lines.map((line, i) => (
                  <div
                    key={i}
                    className={clsx(
                      'stream-line',
                      line.startsWith('✓') ? 'text-teal-400' : line.startsWith('⚠') ? 'text-amber-400' : 'text-slate-400'
                    )}
                  >
                    {line}
                  </div>
                ))}
                {selectedState.status === 'running' && (
                  <div className="flex items-center gap-1 text-blue-400">
                    <Loader2 size={10} className="animate-spin" />
                    <span>Processing...</span>
                  </div>
                )}
              </div>
            )}
          </>
        ) : (
          <div className="text-sm text-slate-600 italic">Select an agent to view output</div>
        )}

        {/* Synthesis loader / recommendation card */}
        {(synthesisRunning || sessionRecommendation) && (
          <div className="mt-3 border-t border-[var(--border)] pt-3">
            {sessionRecommendation
              ? (
                <>
                  <RecommendationCard rec={sessionRecommendation} />
                  <CommitButton committed={committedSession} onCommit={commitSession} />
                </>
              )
              : (
                <div className="flex items-center gap-2 text-sm text-blue-400 px-1 py-1">
                  <Loader2 size={11} className="animate-spin flex-shrink-0" />
                  <span>Generating recommendation…</span>
                </div>
              )
            }
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  )
}

// ── Rich event list ────────────────────────────────────────────────────────────

function RichEventList({ events, agentRunning, pipeline }: { events: SubAgentEvent[]; agentRunning: boolean; pipeline?: BackendPipeline | null }) {
  const items = buildRenderList(events)
  const pipelineLabels: Record<string, string> = {}
  for (const a of pipeline?.agents ?? []) {
    for (const sa of (a.sub_agents ?? [])) pipelineLabels[sa.id] = sa.label
  }

  return (
    <div className="space-y-2">
      {items.map((item, i) => {
        if (item.kind === 'alert') {
          return <AlertCard key={i} message={item.message} severity={item.severity} count={item.count} />
        }
        return (
          <SubAgentCard
            key={item.subAgentId}
            label={pipelineLabels[item.subAgentId] ?? getLabel(item.subAgentId)}
            status={item.status}
            narrative={item.status === 'complete' ? narratise(item.subAgentId, item.result) : undefined}
            details={item.status === 'complete' ? detail(item.subAgentId, item.result) : undefined}
          />
        )
      })}
      {agentRunning && items.length === 0 && (
        <div className="flex items-center gap-2 text-sm text-blue-400">
          <Loader2 size={11} className="animate-spin" />
          <span>Initialising…</span>
        </div>
      )}
    </div>
  )
}

function SubAgentCard({
  label,
  status,
  narrative,
  details,
}: {
  label: string
  status: 'running' | 'complete'
  narrative?: string
  details?: { label: string; value: string }[]
}) {
  return (
    <div className={clsx(
      'rounded-lg border px-3 py-2 text-sm transition-colors',
      status === 'complete'
        ? 'border-teal-800/40 bg-teal-950/20'
        : 'border-blue-800/40 bg-blue-950/20'
    )}>
      <div className="flex items-center gap-2 mb-0.5">
        {status === 'complete'
          ? <CheckCircle size={11} className="text-teal-400 flex-shrink-0" />
          : <Loader2 size={11} className="text-blue-400 animate-spin flex-shrink-0" />
        }
        <span className={clsx(
          'font-semibold',
          status === 'complete' ? 'text-teal-300' : 'text-blue-300'
        )}>
          {label}
        </span>
      </div>
      {narrative && (
        <p
          className="text-slate-300 leading-relaxed pl-[19px] line-clamp-3 cursor-default"
          title={narrative}
        >
          {narrative}
        </p>
      )}
      {details && details.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pl-[19px] mt-1.5">
          {details.map((chip) => (
            <span
              key={chip.label}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-800/60 border border-slate-700/50 text-[10px] text-slate-300"
            >
              <span className="text-slate-500">{chip.label}</span>
              <span className="font-semibold text-slate-200">{chip.value}</span>
            </span>
          ))}
        </div>
      )}
      {status === 'running' && (
        <p className="text-blue-400/70 pl-[19px]">Processing…</p>
      )}
    </div>
  )
}

function AlertCard({ message, severity, count }: { message: string; severity: string; count: number }) {
  const isCritical = severity === 'critical'
  const isWarning  = severity === 'warning'
  return (
    <div className={clsx(
      'rounded-lg border px-3 py-2 text-sm flex gap-2 items-start',
      isCritical ? 'border-red-700/50 bg-red-950/30'
        : isWarning ? 'border-amber-700/50 bg-amber-950/30'
        : 'border-slate-700/50 bg-slate-800/30'
    )}>
      <span className="flex-shrink-0 mt-0.5">
        {isCritical
          ? <AlertCircle size={12} className="text-red-400" />
          : isWarning
          ? <AlertTriangle size={12} className="text-amber-400" />
          : <Info size={12} className="text-slate-400" />
        }
      </span>
      <p className={clsx(
        'leading-relaxed flex-1',
        isCritical ? 'text-red-200' : isWarning ? 'text-amber-200' : 'text-slate-300'
      )}>
        {message}
      </p>
      {count > 1 && (
        <span className={clsx(
          'flex-shrink-0 text-[10px] font-bold px-1.5 py-0.5 rounded-full border self-start mt-0.5',
          isCritical ? 'bg-red-900/50 border-red-700/50 text-red-300'
            : isWarning ? 'bg-amber-900/50 border-amber-700/50 text-amber-300'
            : 'bg-slate-800 border-slate-700 text-slate-400'
        )}>
          ×{count}
        </span>
      )}
    </div>
  )
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'running')  return <Loader2      size={12} className="text-blue-400 animate-spin flex-shrink-0" />
  if (status === 'complete') return <CheckCircle  size={12} className="text-teal-400 flex-shrink-0" />
  if (status === 'waiting')  return <Clock        size={12} className="text-amber-400 flex-shrink-0" />
  if (status === 'skipped')  return <MinusCircle  size={12} className="text-slate-600 flex-shrink-0" />
  return null
}

function CommitButton({ committed, onCommit }: { committed: boolean; onCommit: () => Promise<void> }) {
  const [saving, setSaving] = useState(false)

  if (committed) {
    return (
      <div className="mt-2 flex items-center gap-1.5 text-sm text-teal-400 px-1">
        <CheckCircle size={11} className="flex-shrink-0" />
        <span>Saved to hospital system</span>
      </div>
    )
  }

  return (
    <button
      disabled={saving}
      onClick={async () => {
        setSaving(true)
        await onCommit()
        setSaving(false)
      }}
      className="mt-2 w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-semibold bg-teal-600 hover:bg-teal-500 disabled:opacity-50 disabled:cursor-not-allowed text-white transition-colors"
    >
      {saving
        ? <><Loader2 size={11} className="animate-spin flex-shrink-0" /> Saving…</>
        : <><Save size={11} className="flex-shrink-0" /> Save &amp; Confirm</>
      }
    </button>
  )
}

// ── Final Recommendation Card ─────────────────────────────────────────────────

function RecommendationCard({
  rec,
}: {
  rec: { headline: string; actions: string[]; risk: string; summary: string }
}) {
  const [expanded, setExpanded] = useState(false)

  const riskColor =
    rec.risk === 'high'   ? 'text-red-400'    :
    rec.risk === 'medium' ? 'text-amber-400'  : 'text-teal-400'

  const borderColor =
    rec.risk === 'high'   ? 'border-red-700/50 bg-red-950/20'     :
    rec.risk === 'medium' ? 'border-amber-700/50 bg-amber-950/20' :
    'border-teal-700/50 bg-teal-950/20'

  return (
    <div className={clsx('rounded-xl border text-sm', borderColor)}>
      {/* Header row — always visible, click to toggle */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-1.5 px-3 py-2.5 text-left"
      >
        <Sparkles size={12} className={clsx('flex-shrink-0', riskColor)} />
        <span className={clsx('font-bold text-sm uppercase tracking-wide', riskColor)}>
          Hospilot Recommendation
        </span>
        <span className="ml-auto">
          {expanded
            ? <ChevronDown size={12} className="text-slate-400" />
            : <ChevronRight size={12} className="text-slate-400" />
          }
        </span>
      </button>

      {/* Headline + summary — always visible */}
      <div className="px-3 pb-3 space-y-1.5">
        <p className="text-slate-100 font-semibold leading-snug">
          {rec.headline}
        </p>
        {rec.summary && (
          <p className="text-slate-400 text-xs leading-relaxed">
            {rec.summary}
          </p>
        )}
      </div>

      {/* Actions — expand to view */}
      {rec.actions.length > 0 && expanded && (
        <div className="border-t border-white/5 px-3 py-2 max-h-64 overflow-y-auto">
          <ul className="space-y-1.5">
            {rec.actions.map((a, i) => (
              <li key={i} className="flex gap-2 text-slate-300 text-xs leading-snug">
                <span className={clsx('flex-shrink-0 font-bold', riskColor)}>{i + 1}.</span>
                {a}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
