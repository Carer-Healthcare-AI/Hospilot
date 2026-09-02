import type { PipelineEdgeDef } from './scenarios'

export interface TaskDef {
  id: string
  label: string
  condition?: string   // per-task gate streamed in agent_plan ("run_if" / "when")
}

export interface TaskEdgeDef {
  source: string
  target: string
  condition?: string
}

export interface SubAgentDef {
  id: string
  label: string
  description: string
  active: boolean
  capabilities: string[]
  tasks?: TaskDef[]
  taskEdges?: TaskEdgeDef[]
  terminal?: boolean
}

export interface AgentDef {
  id: string
  label: string
  emoji: string
  color: string
  description: string
  subAgents: SubAgentDef[]
  edges?: PipelineEdgeDef[]
}

export const AGENTS: AgentDef[] = [
  {
    id: 'er',
    label: 'ER Coordination',
    emoji: '🚑',
    color: '#ef4444',
    description: 'Monitors emergency patients, assigns urgency scores, and routes patients to the right care setting',
    subAgents: [
      {
        id: 'sa_er_triage', label: 'Triage Monitor', description: 'Reviews all active ER patients and identifies those who need reassessment', active: true,
        capabilities: ['CTAS Score', 'Re-triage Flag', 'Wait Time'],
        tasks: [
          { id: 'tr-1', label: 'Review all active emergency department patients' },
          { id: 'tr-2', label: 'Assign urgency scores to each patient (CTAS 1–5)' },
          { id: 'tr-3', label: 'Identify patients whose condition may have changed and need reassessment' },
          { id: 'tr-4', label: 'Update patient records with new triage scores' },
        ],
      },
      {
        id: 'sa_er_critical_select', label: 'Admission Router', description: 'Identifies the most critical patients and determines the appropriate care setting for each', active: true,
        capabilities: ['CTAS Ranking', 'Vitals Severity', 'Bed Type Classification'],
        tasks: [
          { id: 'ar-1', label: 'Identify high-priority patients (CTAS 1–3) who need immediate admission' },
          { id: 'ar-2', label: 'Rank patients by urgency using triage score, oxygen levels, and heart rate' },
          { id: 'ar-3', label: 'Determine the appropriate care setting: ICU, High Dependency, or General Ward' },
          { id: 'ar-4', label: 'Send the most critical patients for immediate bed assignment' },
        ],
      },
      {
        id: 'sa_er_fasttrack', label: 'FastTrack Router', description: 'Identifies lower-acuity patients who have been waiting too long and redirects them to fast-track', active: true,
        capabilities: ['CTAS 4–5', 'Wait ≥ 30 min', 'Fast-track Lane'],
        tasks: [
          { id: 'ft-1', label: 'Identify lower-urgency patients (CTAS 4–5) waiting more than 30 minutes' },
          { id: 'ft-2', label: 'Notify staff to move eligible patients to the fast-track lane' },
          { id: 'ft-3', label: 'Record routing decisions for clinical review' },
        ],
      },
    ],
    edges: [
      { source: 'sa_er_triage', target: 'sa_er_critical_select', condition: 'high_acuity' },
      { source: 'sa_er_triage', target: 'sa_er_fasttrack',       condition: 'low_acuity'  },
    ],
  },
  {
    id: 'icu',
    label: 'ICU Operations',
    emoji: '🫀',
    color: '#dc2626',
    description: 'Monitors ICU capacity, tracks ventilated patients, and identifies patients ready for step-down',
    subAgents: [
      {
        id: 'sa_icu_census', label: 'ICU Census', description: 'Reviews current ICU occupancy and flags stable patients who may be ready to move to a lower-acuity ward', active: true,
        capabilities: ['ICU Occupancy', 'Ventilator Count', 'Step-down Candidates'],
        tasks: [
          { id: 'ic-1', label: 'Review current ICU bed occupancy' },
          { id: 'ic-2', label: 'Count ventilated patients vs those on standard ICU monitoring' },
          { id: 'ic-3', label: 'Identify clinically stable patients who may be ready for step-down' },
          { id: 'ic-4', label: 'Analyse census data to generate transfer recommendations' },
        ],
      },
      {
        id: 'sa_icu_stepdown', label: 'Step-Down Coordinator', description: 'Confirms clinical criteria for step-down and arranges transfer to a lower-acuity bed', active: true,
        capabilities: ['Step-down Criteria', 'Progressive Care Bed'],
        tasks: [
          { id: 'sd-1', label: 'Review clinical criteria for each step-down candidate' },
          { id: 'sd-2', label: 'Request a step-down or progressive care bed for each eligible patient' },
          { id: 'sd-3', label: 'Send transfer recommendations to the clinical team for approval' },
        ],
      },
    ],
  },
  {
    id: 'bed',
    label: 'Bed Management',
    emoji: '🛏️',
    color: '#3b82f6',
    description: 'Finds available beds, recommends the best match for each patient, and manages bed reservations',
    subAgents: [
      // ── Forecasting sub-agent (bed_prediction_agent flow) ──
      {
        id: 'sa_bed_prediction', label: 'Bed Prediction', description: 'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours', active: true,
        capabilities: ['Bed Census', 'Discharge Horizon', 'ER Pressure', 'Overflow Risk', 'ICU Risk'],
        tasks: [
          { id: 'bpc-1', label: 'Review current bed availability across all wards' },
          { id: 'bpc-2', label: 'Check ICU occupancy and number of ventilators in use' },
          { id: 'bpc-3', label: 'Estimate how many beds will free up in the next 4 and 24 hours' },
          { id: 'bpc-4', label: 'Assess incoming patient load from the emergency department' },
          { id: 'bpc-5', label: 'Identify ward patients who may need ICU escalation' },
          { id: 'bpf-1', label: 'Generate a plain-language capacity forecast for clinical decision-making' },
          { id: 'bpf-2', label: 'Classify bed shortage risk: low, medium, or high' },
          { id: 'bpf-3', label: 'Produce recommended actions to prevent bed shortages' },
          { id: 'bpf-4', label: 'Alert the team if capacity risk is medium or above' },
        ],
      },
      // ── Reservation sub-agents (bed_agent flow) ──
      {
        id: 'sa_bed_availability', label: 'Bed Availability', description: 'Identifies all available beds that are clean, unblocked, and ready for a patient', active: true,
        capabilities: ['Vacancy Filter', 'Bed Type', 'Maintenance Exclusion'],
        tasks: [
          { id: 'ba-1', label: 'Query all beds across every ward and bed type' },
          { id: 'ba-2', label: 'Check if clean, unblocked beds are ready for a patient' },
          { id: 'ba-3', label: 'Filter beds by patient requirements and exclude maintenance beds' },
          { id: 'ba-4', label: 'Locate dirty or near-discharge beds as overflow fallback' },
        ],
        taskEdges: [
          { source: 'ba-1', target: 'ba-2' },
          { source: 'ba-2', target: 'ba-3', condition: 'beds_available' },
          { source: 'ba-2', target: 'ba-4', condition: 'no_beds_available' },
        ],
      },
      {
        id: 'sa_bed_ranking', label: 'Bed Assignment', description: 'Uses clinical AI to recommend the best available bed based on the patient\'s needs', active: true,
        capabilities: ['AI Ranking', 'Patient Acuity', 'Clinical Reasoning'],
        tasks: [
          { id: 'br-1', label: 'Match available beds against the patient\'s acuity, isolation needs, and ward preference' },
          { id: 'br-2', label: 'Produce a ranked list of recommended beds with clinical rationale' },
        ],
      },
      {
        id: 'sa_bed_reservation', label: 'Bed Reservation', description: 'Reserves the selected bed and notifies the receiving ward after clinical approval', active: true,
        capabilities: ['Approval Gate', 'Ward Notification', 'Double-booking Prevention'],
        tasks: [
          { id: 'bres-1', label: 'Hold the selected bed to prevent double booking' },
          { id: 'bres-2', label: 'Send the reservation for clinical approval' },
          { id: 'bres-3', label: 'Confirm the bed reservation once approved' },
          { id: 'bres-4', label: 'Notify the emergency department and receiving ward of the assigned bed' },
        ],
      },
      {
        id: 'sa_bed_cleaning', label: 'Bed Cleaning', description: 'Identifies vacated beds and dispatches housekeeping staff to clean and prepare them', active: true,
        capabilities: ['Vacated Bed Detection', 'Housekeeping Dispatch', 'Room Turnover'],
        tasks: [
          { id: 'bc-1', label: 'Identify beds vacated by recently discharged patients' },
          { id: 'bc-2', label: 'Dispatch housekeeping staff to clean and prepare each bed' },
          { id: 'bc-3', label: 'Record all cleaning tasks for audit and handover' },
        ],
      },
      { id: 'sa_dirty_bed_recovery', label: 'Dirty Bed Recovery', description: 'Dispatches emergency housekeeping for dirty beds detected by availability check, making them available for ranking', active: true, capabilities: ['Vacated Bed Detection', 'Housekeeping Dispatch', 'Room Turnover'] },
      { id: 'sa_bed_no_beds',  label: 'No Beds Available',   description: '', active: false, capabilities: [], terminal: true },
      { id: 'sa_bed_skipped',  label: 'Reservation Skipped', description: '', active: false, capabilities: [], terminal: true },
    ],
    edges: [
      { source: 'sa_bed_availability',   target: 'sa_bed_ranking',          condition: 'beds_available'    },
      { source: 'sa_bed_availability',   target: 'sa_dirty_bed_recovery',   condition: 'no_beds_available' },
      { source: 'sa_dirty_bed_recovery', target: 'sa_bed_ranking'                                          },
      { source: 'sa_bed_availability',   target: 'sa_bed_no_beds',          condition: 'no_beds_available' },
      { source: 'sa_bed_ranking',        target: 'sa_bed_reservation'                                      },
      { source: 'sa_bed_reservation',    target: 'sa_bed_skipped',          condition: 'bed_not_reserved'  },
    ],
  },
  {
    id: 'staffing',
    label: 'Staffing',
    emoji: '👥',
    color: '#f59e0b',
    description: 'Monitors staffing levels across all wards and deploys additional nurses where needed',
    subAgents: [
      {
        id: 'sa_ratio_monitor', label: 'Ratio Monitor', description: 'Reviews nurse-to-patient ratios across all wards and flags understaffed areas', active: true,
        capabilities: ['Nurse-Patient Ratio', 'Safe Staffing', 'Ward Census'],
        tasks: [
          { id: 'rm-1', label: 'Review current nurse shift assignments across all wards' },
          { id: 'rm-2', label: 'Calculate nurse-to-patient ratio for each ward' },
          { id: 'rm-3', label: 'Identify wards that are below safe staffing levels' },
          { id: 'rm-4', label: 'Analyse workload data to generate staffing recommendations' },
        ],
      },
      {
        id: 'sa_float_pool', label: 'Float Pool Dispatcher', description: 'Identifies available float nurses and recommends where to deploy them', active: true,
        capabilities: ['Float Nurses', 'Skill Matching', 'Reallocation'],
        tasks: [
          { id: 'fp-1', label: 'Check which float nurses are currently available' },
          { id: 'fp-2', label: 'Match nurse skills to the needs of understaffed wards' },
          { id: 'fp-3', label: 'Send staffing reallocation recommendations for approval' },
        ],
      },
    ],
  },
  {
    id: 'discharge',
    label: 'Discharge Planning',
    emoji: '📤',
    color: '#10b981',
    description: 'Identifies patients ready for discharge, resolves barriers, and generates discharge documentation',
    subAgents: [
      {
        id: 'sa_discharge_ready', label: 'Readiness Assessor', description: 'Reviews each admitted patient to determine if they are clinically ready for discharge', active: true,
        capabilities: ['Discharge Checklist', 'Clinical Readiness', 'Approval Gate'],
        tasks: [
          { id: 'dr-1', label: 'Review all currently admitted patients' },
          { id: 'dr-2', label: 'Check clinical readiness: vitals stable, care tasks complete, medications prescribed' },
          { id: 'dr-3', label: 'Flag discharge-ready patients for clinical team sign-off' },
          { id: 'dr-4', label: 'Document all clinical barriers preventing discharge' },
        ],
        taskEdges: [
          { source: 'dr-1', target: 'dr-2' },
          { source: 'dr-2', target: 'dr-3', condition: 'discharge_ready' },
          { source: 'dr-2', target: 'dr-4', condition: 'discharge_not_ready' },
        ],
      },
      {
        id: 'sa_discharge_barriers', label: 'Discharge Approver', description: 'Identifies what is holding up each discharge and escalates where needed', active: true,
        capabilities: ['Blocker Detection', 'Case Manager Escalation'],
        tasks: [
          { id: 'db-1', label: 'Review outstanding lab results, consents, and transport arrangements' },
          { id: 'db-2', label: 'Document all barriers preventing discharge' },
          { id: 'db-3', label: 'Escalate complex or delayed cases to the case manager' },
        ],
      },
      {
        id: 'sa_discharge_summary', label: 'AI Summary Generator', description: 'Automatically generates a clinical discharge summary for each ready patient', active: true,
        capabilities: ['AI Discharge Notes', 'Clinical Summary', 'Patient Record Update'],
        tasks: [
          { id: 'ds-1', label: "Review the patient's recent vitals and completed care tasks" },
          { id: 'ds-2', label: 'Generate a structured discharge summary from clinical data' },
          { id: 'ds-3', label: 'Save the discharge summary to the patient record' },
        ],
      },
    ],
    edges: [
      { source: 'sa_discharge_ready', target: 'sa_discharge_summary',  condition: 'discharge_ready'     },
      { source: 'sa_discharge_ready', target: 'sa_discharge_barriers', condition: 'discharge_not_ready' },
      { source: 'sa_discharge_barriers', target: 'sa_discharge_summary' },
    ],
  },
  {
    id: 'ot',
    label: 'OT Scheduling',
    emoji: '⚕️',
    color: '#7c3aed',
    description: 'Reviews today\'s surgical schedule against available post-op beds and flags any conflicts',
    subAgents: [
      {
        id: 'sa_ot_census', label: 'OT Census', description: "Reviews today's surgical list and checks how many post-operative beds are available", active: true,
        capabilities: ['Surgical Case List', 'Post-op Beds'],
        tasks: [
          { id: 'oc-1', label: "Review today's scheduled surgical cases" },
          { id: 'oc-2', label: 'Count available post-operative recovery beds' },
        ],
      },
      {
        id: 'sa_ot_analysis', label: 'OT Capacity Analyser', description: 'Assesses whether there is sufficient post-op capacity for each planned surgery and recommends action', active: true,
        capabilities: ['Capacity Risk', 'Conflict Detection', 'AI Assessment'],
        tasks: [
          { id: 'oa-1', label: 'Analyse surgical caseload against post-op bed availability' },
          { id: 'oa-2', label: 'Identify cases at risk due to bed shortages or scheduling conflicts' },
          { id: 'oa-3', label: 'Recommend whether each case should proceed, be delayed, or be escalated' },
          { id: 'oa-4', label: 'Alert the surgical team for any cases requiring urgent attention' },
        ],
      },
    ],
  },
  {
    id: 'lab',
    label: 'Lab & Diagnostics',
    emoji: '🧪',
    color: '#0891b2',
    description: 'Lab results, pending tests, critical value alerting',
    subAgents: [
      { id: 'latest', label: 'Lab Results', description: 'Latest lab result retrieval', active: true, capabilities: ['CBC', 'LFT', 'RFT'], tasks: [{ id: 'll-1', label: 'Pull latest results from the laboratory system' }, { id: 'll-2', label: 'Flag out-of-range values for clinical review' }] },
      { id: 'pending', label: 'Pending Tests', description: 'Pending test tracking', active: true, capabilities: ['TAT Monitor', 'Escalation'], tasks: [{ id: 'lp-1', label: 'List all pending tests and how long they have been waiting' }, { id: 'lp-2', label: 'Escalate overdue tests to the supervising clinician' }] },
      { id: 'imaging', label: 'Imaging', description: 'Imaging order management', active: false, capabilities: ['CT', 'MRI', 'X-Ray'], tasks: [] },
      { id: 'critical', label: 'Critical Values', description: 'Critical value alerting', active: true, capabilities: ['Panic Values', 'Clinician Alert'], tasks: [{ id: 'lc-1', label: 'Detect critical or panic-level lab values' }, { id: 'lc-2', label: 'Notify the responsible clinician immediately' }] },
    ],
  },
  {
    id: 'pharmacy',
    label: 'Pharmacy',
    emoji: '💊',
    color: '#06b6d4',
    description: 'Drug inventory monitoring, low-stock alerting, and medication reconciliation at discharge',
    subAgents: [
      {
        id: 'sa_stock_monitor', label: 'Stock Monitor', description: 'Reviews current medication stock levels and flags drugs running low', active: true,
        capabilities: ['Drug Inventory', 'Low Stock Flag', 'Reorder Alert'],
        tasks: [
          { id: 'sm-1', label: 'Review current medication stock levels across the pharmacy' },
          { id: 'sm-2', label: 'Flag medications that have fallen below safe stock levels' },
          { id: 'sm-3', label: 'Alert the pharmacy team about medications that need to be reordered' },
        ],
      },
      {
        id: 'sa_dispense', label: 'Dispensing', description: 'Medication dispensing queue', active: false,
        capabilities: ['Order Queue', 'TAT Monitor'],
        tasks: [
          { id: 'pd-1', label: 'Review the medication dispensing queue' },
          { id: 'pd-2', label: 'Flag orders that are taking longer than expected' },
        ],
      },
    ],
  },
  {
    id: 'clinical',
    label: 'Clinical Agent',
    emoji: '🩺',
    color: '#2563eb',
    description: 'Patient summaries, vitals monitoring, care plans',
    subAgents: [
      { id: 'summary', label: 'Patient Summary', description: 'Clinical summary generation', active: true, capabilities: ['Diagnosis', 'Medications', 'Notes'], tasks: [{ id: 'cs-1', label: 'Compile diagnosis and current medication list' }, { id: 'cs-2', label: 'Generate a structured clinical summary for handover' }] },
      { id: 'vitals', label: 'Vitals Monitor', description: 'Real-time vitals tracking', active: true, capabilities: ['HR', 'BP', 'SpO₂', 'Temp'], tasks: [{ id: 'cv-1', label: 'Review latest vitals from bedside monitoring' }, { id: 'cv-2', label: 'Alert clinical staff on abnormal readings' }] },
    ],
  },
  {
    id: 'patientflow',
    label: 'Patient Flow',
    emoji: '🔄',
    color: '#f97316',
    description: 'Admissions, transfers, discharge flow coordination',
    subAgents: [
      { id: 'admission', label: 'Admission', description: 'Patient admission tracking', active: true, capabilities: ['Registration', 'Bed Request'], tasks: [{ id: 'pfa-1', label: 'Register the incoming patient and record presenting complaint' }, { id: 'pfa-2', label: 'Submit a bed request based on clinical criteria' }] },
      { id: 'transfer', label: 'Transfer', description: 'Inter-ward transfer management', active: true, capabilities: ['Transfer Order', 'Escort'], tasks: [{ id: 'pft-1', label: 'Initiate an inter-ward transfer order' }, { id: 'pft-2', label: 'Assign a patient escort for the transfer' }] },
    ],
  },
  {
    id: 'billing',
    label: 'Billing & Insurance',
    emoji: '📋',
    color: '#84cc16',
    description: 'Pre-authorization, TPA liaison, deposit management',
    subAgents: [
      { id: 'preauth', label: 'Pre-auth', description: 'Insurance pre-authorization', active: true, capabilities: ['TPA Submit', 'Verbal Auth'], tasks: [{ id: 'bpa-1', label: 'Submit pre-authorization request to the insurance provider' }, { id: 'bpa-2', label: 'Track verbal authorizations and follow up for written confirmation' }] },
      { id: 'tpaliaison', label: 'TPA Liaison', description: 'TPA coordination & follow-up', active: true, capabilities: ['Follow-up', 'Escalation'], tasks: [{ id: 'btl-1', label: 'Follow up with the insurance provider on pending cases' }, { id: 'btl-2', label: 'Handle denied claims with resubmission and escalation' }] },
      { id: 'deposit', label: 'Deposit', description: 'Deposit collection management', active: true, capabilities: ['Estimate', 'Collection'], tasks: [{ id: 'bdc-1', label: 'Generate the estimated patient payment liability' }, { id: 'bdc-2', label: 'Track deposit collection status' }] },
    ],
  },
  {
    id: 'revenue',
    label: 'Revenue',
    emoji: '💰',
    color: '#f97316',
    description: 'Monitors outstanding invoices, daily collections, and insurance claims to flag financial risks',
    subAgents: [
      {
        id: 'sa_rev_invoice_census',
        label: 'Invoice Monitor',
        description: 'Reviews all unpaid invoices and groups them by how long they have been outstanding',
        active: true,
        capabilities: ['Billing Gap Detection', 'Invoice Ageing', 'Inpatient Billing Audit'],
        tasks: [
          { id: 'ri-1', label: 'Review all outstanding patient invoices' },
          { id: 'ri-2', label: 'Group invoices by age: same-day, 1–7 days, 7–30 days, and over 30 days' },
          { id: 'ri-3', label: 'Flag admitted inpatients who have not yet been billed' },
        ],
      },
      {
        id: 'sa_rev_collections',
        label: 'Collections Monitor',
        description: "Compares today's payments received against yesterday and checks for any cash reconciliation issues",
        active: true,
        capabilities: ['Daily Collections', 'Day-over-Day Trend', 'Reconciliation Check'],
        tasks: [
          { id: 'rc-1', label: 'Review total payments collected today' },
          { id: 'rc-2', label: "Compare with yesterday's collections to identify trends" },
          { id: 'rc-3', label: 'Flag any discrepancy between cash collected and cash recorded' },
        ],
      },
      {
        id: 'sa_rev_claims',
        label: 'Claims Pipeline',
        description: 'Reviews the status of all insurance claims and flags any that have been denied',
        active: true,
        capabilities: ['Claims Status', 'Denial Detection', 'TPA Tracking'],
        tasks: [
          { id: 'rcl-1', label: 'Review all insurance claims by current status' },
          { id: 'rcl-2', label: 'Calculate the total value of denied claims requiring resubmission' },
        ],
      },
      {
        id: 'sa_rev_analyst',
        label: 'Revenue Risk Analyst',
        description: 'Analyses all financial data and provides a risk assessment with the top 3 recommended actions',
        active: true,
        capabilities: ['Risk Classification', 'Action Prioritisation', 'Financial Narrative'],
        tasks: [
          { id: 'ra-1', label: 'Analyse outstanding invoices, collections, and claims together' },
          { id: 'ra-2', label: 'Classify overall financial risk: low, medium, or high' },
          { id: 'ra-3', label: "Identify the top 3 actions to improve the hospital's financial position" },
        ],
      },
    ],
  },
  {
    id: 'ambulance',
    label: 'Ambulance Agent',
    emoji: '🚑',
    color: '#0ea5e9',
    description: 'Dispatch available ambulances to intercept and transport the patient',
    subAgents: [],
  },
]

export const AGENT_MAP = Object.fromEntries(AGENTS.map((a) => [a.id, a]))
