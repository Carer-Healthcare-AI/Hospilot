export type StatLevel = 'critical' | 'warning' | 'normal'

export interface ContextStat {
  label: string
  value: string
  level: StatLevel
}

export interface ApprovalGate {
  agentId: string
  title: string
  recommendation: string
  action: string
}

export interface PipelineNodeDef {
  id: string
  agentId: string
  taskType?: string
  isDecision?: boolean
  question?: string
  isTerminal?: boolean
}

export interface PipelineEdgeDef {
  source: string
  target: string
  condition?: string
  condition_label?: string
  isDecisionBranch?: 'yes' | 'no'
}

export interface StreamingOutputs {
  [nodeId: string]: string[]
}

export interface ScenarioDef {
  id: string
  label: string
  promptText: string
  constraintText: string
  contextStats: ContextStat[]
  nodes: PipelineNodeDef[]
  edges: PipelineEdgeDef[]
  approvalGates: ApprovalGate[]
  streamingOutputs: StreamingOutputs
}

export const SCENARIOS: ScenarioDef[] = [
  {
    id: 'er_icu_crisis',
    label: 'ER / ICU Crisis',
    promptText:
      'Ambulance arriving in 18 minutes with critical patient requiring ICU admission. Current ICU occupancy is 100%. Identify transfer candidates and free a bed before arrival.',
    constraintText:
      'Do not move unstable ICU patients. Follow isolation and acuity rules. Require human approval before ICU transfer.',
    contextStats: [
      { label: 'Ambulance ETA', value: '18 min', level: 'critical' },
      { label: 'ICU Occupancy', value: '100%', level: 'critical' },
      { label: 'ER Queue', value: 'High', level: 'warning' },
      { label: 'Patient Need', value: 'ICU Bed', level: 'warning' },
    ],
    nodes: [
      { id: 'er-1', agentId: 'er' },
      { id: 'icu-1', agentId: 'icu' },
      { id: 'bed-1', agentId: 'bed' },
      { id: 'discharge-1', agentId: 'discharge' },
      { id: 'staffing-1', agentId: 'staffing' },
      { id: 'housekeeping-1', agentId: 'housekeeping' },
    ],
    edges: [
      { source: 'er-1', target: 'icu-1' },
      { source: 'icu-1', target: 'discharge-1', condition: 'icu_full',      condition_label: 'if ICU full' },
      { source: 'icu-1', target: 'bed-1',       condition: 'icu_not_full',  condition_label: 'if ICU has space' },
      { source: 'discharge-1', target: 'bed-1', condition: 'has_discharge_candidates', condition_label: 'if discharge candidates found' },
      { source: 'bed-1', target: 'staffing-1' },
      { source: 'staffing-1', target: 'housekeeping-1' },
    ],
    approvalGates: [
      {
        agentId: 'icu-1',
        title: 'ICU Bed Reallocation Required',
        recommendation:
          'Transfer Patient Rajesh Gupta (ICU-3) to HDU — stable for 18h, discharge eligible. This frees ICU bed for incoming critical patient (ETA 18 min).',
        action: 'Approve ICU Transfer',
      },
    ],
    streamingOutputs: {
      'er-1': [
        'Receiving ambulance alert — ETA 18 minutes...',
        '⚠ Incoming patient: male, 54, chest pain + hypotension. GCS 14.',
        'Triage prediction: ICU admission probability 89%.',
        'Running ER Load sub-agent — current queue: 14 patients, 3 critical.',
        '⚠ ER capacity strained — 2 resus bays occupied.',
        'Escalating to ICU Management Agent...',
        '✓ ER Agent complete — ICU bed needed within 15 minutes.',
      ],
      'icu-1': [
        'Checking ICU occupancy — 8/8 beds occupied (100%)...',
        'Running Bed Candidate sub-agent...',
        '⚠ Patient ICU-3 Rajesh Gupta: stable for 18 hours, HDU-eligible.',
        '⚠ Patient ICU-7 Anita Sinha: stable, awaiting step-down order.',
        'Risk assessment — ICU-3 transfer risk: LOW. ICU-7 transfer risk: MODERATE.',
        '✓ Approval received — initiating transfer workflow.',
        '✓ ICU Agent complete — 1 bed being freed, ETA 12 minutes.',
      ],
      'bed-1': [
        'Scanning HDU for transfer-eligible beds...',
        '✓ HDU-2 available — compatible with Rajesh Gupta acuity level.',
        '✓ Bed assignment confirmed: ICU-3 → HDU-2.',
        'Running Housekeeping trigger for ICU-3...',
        '✓ Bed Intelligence Agent complete — ICU-3 will be ready in 10 minutes.',
      ],
      'discharge-1': [
        'Checking ICU patients eligible for early discharge...',
        '⚠ Patient ICU-5 Kavya Nair: discharge order signed, waiting pharmacy clearance.',
        'Running Pharmacy Clearance sub-agent...',
        '✓ Kavya Nair pharmacy clearance fast-tracked.',
        '✓ Discharge Agent complete — 1 additional ICU bed potentially available.',
      ],
      'staffing-1': [
        'Assessing staff for ICU-3 transfer...',
        '✓ Nurse Seema available for transfer escort.',
        '⚠ Incoming critical patient will require 1:1 nursing for first 2 hours.',
        'Checking on-call pool — 1 ICU nurse on standby, alerting...',
        '✓ Staffing Agent complete — transfer team and ICU nurse confirmed.',
      ],
      'housekeeping-1': [
        'Receiving bed turnover request for ICU-3...',
        'Running Bed Prep sub-agent — standard ICU protocol...',
        '✓ Housekeeping team dispatched to ICU-3.',
        'Estimated clean and ready: 8 minutes.',
        '✓ Housekeeping Agent complete — ICU-3 will be ready before ambulance arrives.',
      ],
    },
  },
  {
    id: 'surgery_optimization',
    label: 'Surgery Optimization',
    promptText:
      'OT running 35 minutes behind schedule. Lab reports pending for Case 3. Optimize today\'s surgical schedule and ensure post-op HDU availability.',
    constraintText:
      'Do not cancel emergency cases. Prioritize patient safety over schedule efficiency. Get surgeon sign-off before rescheduling.',
    contextStats: [
      { label: 'Surgery Time', value: '10:00 AM', level: 'normal' },
      { label: 'OT Delay', value: '+35 min', level: 'warning' },
      { label: 'Lab Report', value: 'Pending', level: 'critical' },
      { label: 'Post-op HDU', value: 'Uncertain', level: 'warning' },
    ],
    nodes: [
      { id: 'ot-1', agentId: 'ot' },
      { id: 'lab-1', agentId: 'lab' },
      { id: 'bed-1', agentId: 'bed' },
      { id: 'billing-1', agentId: 'billing' },
      { id: 'staffing-1', agentId: 'staffing' },
      { id: 'patientflow-1', agentId: 'patientflow' },
    ],
    edges: [
      { source: 'ot-1', target: 'lab-1' },
      { source: 'ot-1', target: 'bed-1' },
      { source: 'lab-1', target: 'billing-1' },
      { source: 'bed-1', target: 'staffing-1' },
      { source: 'billing-1', target: 'patientflow-1' },
      { source: 'staffing-1', target: 'patientflow-1' },
    ],
    approvalGates: [
      {
        agentId: 'ot-1',
        title: 'OT Schedule Adjustment Required',
        recommendation:
          'Shift elective surgery (Case 3 — Arjun Das, knee replacement) from 10:00 AM to 11:15 AM. Lab TAT is 45 min + buffer.',
        action: 'Approve Schedule Change',
      },
    ],
    streamingOutputs: {
      'ot-1': [
        "Loading tomorrow's OT schedule...",
        'Current schedule: 4 cases, first knife 08:00.',
        '⚠ Running 35 minutes behind — Case 2 (laparoscopy) extended.',
        'Pre-op checklist for Arjun Das (Case 3, 10:00 AM)...',
        '⚠ Pre-op CBC and coagulation report: PENDING.',
        'Estimated lab TAT: 45 minutes from now.',
        '✓ Approval received — schedule adjusted to 11:15 AM.',
        '✓ OT Orchestration Agent complete — revised schedule generated.',
      ],
      'lab-1': [
        'Checking pre-op labs for Arjun Das...',
        '⚠ CBC ordered at 07:30 — still processing.',
        '⚠ PT/INR ordered at 07:30 — still processing.',
        'Running Critical TAT sub-agent — escalating to lab supervisor...',
        '✓ Lab expedited — results expected by 09:45.',
        '⚠ Preliminary: Hb 9.8 g/dL — borderline, surgeon to review.',
        '✓ Lab Agent complete — results flagged for anesthesiologist review.',
      ],
      'bed-1': [
        'Checking HDU bed availability for post-op...',
        '⚠ HDU currently full — 6/6 beds occupied.',
        'Running Discharge Prediction sub-agent...',
        '✓ HDU-4 patient expected discharge by 11:00 AM.',
        '✓ Bed reserved conditionally for post-op admission.',
        '✓ Bed Intelligence Agent complete — HDU-4 reservation set.',
      ],
      'staffing-1': [
        'Checking OT staffing for revised schedule...',
        '✓ Scrub nurse and anesthesia team confirmed for 11:15 AM.',
        '⚠ Post-op HDU nurse ratio will be 1:3 at peak — acceptable.',
        '✓ Staffing Agent complete — team briefed on revised timeline.',
      ],
      'billing-1': [
        'Verifying insurance pre-authorization for Arjun Das...',
        '⚠ Package approval status: PENDING with TPA.',
        'Running Insurance Escalation sub-agent...',
        '✓ TPA contacted — verbal pre-auth received, written confirmation expected by 09:00.',
        '⚠ Estimated patient liability: ₹45,000 — consent form needed.',
        '✓ Billing Agent complete — pre-auth tracked, patient counseling scheduled.',
      ],
      'patientflow-1': [
        'Tracking patient Arjun Das pre-admission status...',
        '✓ Patient arrived at pre-op bay at 07:15 AM.',
        '⚠ Pre-op checklist 80% complete — labs pending.',
        '✓ Patient Flow Agent complete — patient on track for 11:15 AM surgery.',
      ],
    },
  },
  {
    id: 'opd_admission',
    label: 'OPD Admission',
    promptText:
      'New OPD patient requires semi-private bed admission. No beds currently available. Insurance pre-auth pending. Coordinate admission workflow.',
    constraintText:
      'Patient preference: semi-private room. Do not move patients against consent. Insurance approval required before final admission.',
    contextStats: [
      { label: 'Bed Required', value: 'Semi-private', level: 'normal' },
      { label: 'Available Now', value: '0 beds', level: 'critical' },
      { label: 'Insurance', value: 'Pending', level: 'warning' },
      { label: 'Exp. Discharge', value: '2 today', level: 'normal' },
    ],
    nodes: [
      { id: 'patientflow-1', agentId: 'patientflow' },
      { id: 'bed-1', agentId: 'bed' },
      { id: 'billing-1', agentId: 'billing' },
      { id: 'discharge-1', agentId: 'discharge' },
      { id: 'pharmacy-1', agentId: 'pharmacy' },
      { id: 'staffing-1', agentId: 'staffing' },
    ],
    edges: [
      { source: 'patientflow-1', target: 'bed-1' },
      { source: 'patientflow-1', target: 'billing-1' },
      { source: 'bed-1', target: 'discharge-1' },
      { source: 'billing-1', target: 'discharge-1' },
      { source: 'discharge-1', target: 'pharmacy-1' },
      { source: 'discharge-1', target: 'staffing-1' },
    ],
    approvalGates: [],
    streamingOutputs: {
      'patientflow-1': [
        'Registering new OPD patient — Meera Pillai, F/42...',
        'Diagnosis: Dengue fever with thrombocytopenia.',
        'Admission type: Semi-private, estimated stay 3-4 days.',
        '⚠ No semi-private beds available currently.',
        'Checking expected discharges today...',
        '✓ 2 semi-private discharges expected by 12:00 PM.',
        '✓ Patient Flow Agent complete — admission queued pending bed.',
      ],
      'bed-1': [
        'Scanning semi-private ward for availability...',
        '⚠ SP-101 through SP-108: all occupied.',
        'Checking discharge list — SP-104 Suresh Menon: discharge order signed.',
        'Checking discharge list — SP-107 Lakshmi Rao: discharge order pending doctor sign.',
        '✓ SP-104 targeted for turnover — estimated ready by 11:30 AM.',
        '✓ Bed Intelligence Agent complete — SP-104 reserved for Meera Pillai.',
      ],
      'billing-1': [
        'Initiating insurance pre-authorization for Meera Pillai...',
        'Insurer: Star Health. Policy no. SH-2024-88123.',
        '⚠ Pre-auth request submitted — TAT 2-4 hours.',
        '✓ Escalation flag raised — dengue with low platelets qualifies for fast-track.',
        '⚠ Estimated co-pay: ₹12,000. Patient counseled.',
        '✓ Billing Agent complete — pre-auth in progress, admission can proceed.',
      ],
      'discharge-1': [
        'Checking SP-104 discharge status for Suresh Menon...',
        '✓ Discharge summary signed by Dr. Krishnan at 08:45 AM.',
        'Running Pharmacy Clearance sub-agent...',
        '✓ Pharmacy clearance complete — take-home meds dispensed.',
        'Running Billing Clearance sub-agent...',
        '✓ Final bill generated and shared with patient.',
        '✓ Discharge Agent complete — SP-104 vacating by 11:00 AM.',
      ],
      'pharmacy-1': [
        'Preparing admission medication kit for Meera Pillai...',
        'Medications: IV fluids, paracetamol, platelet-boosting protocol.',
        '⚠ Platelet concentrate — checking blood bank availability...',
        '✓ 2 units platelets available and tagged.',
        '✓ Pharmacy Agent complete — admission kit ready.',
      ],
      'staffing-1': [
        'Assigning ward nurse for SP-104...',
        '✓ Nurse Priya assigned — shift coverage until 3 PM.',
        '✓ Treating physician: Dr. Krishnan notified of new admission.',
        '✓ Staffing Agent complete — SP-104 handover scheduled 11:30 AM.',
      ],
    },
  },
]
