export type CapabilityLevel = 'Basic' | 'Advanced' | 'Expert'

export interface Capability {
  id: string
  name: string
  description: string
  level: CapabilityLevel
  tags: string[]
}

// Keyed by `${agentId}-${subAgentId}`
export const CAPABILITIES: Record<string, Capability[]> = {
  'er-triage': [
    { id: 'er-t-1', name: 'Manchester Triage Scoring', description: 'Score patients using MTS 5-level protocol in real time', level: 'Expert', tags: ['Manchester Triage System', 'EMR Integration'] },
    { id: 'er-t-2', name: 'Priority Queue Management', description: 'Dynamic reordering of ER queue based on acuity changes', level: 'Advanced', tags: ['Queue Engine', 'Alert API'] },
    { id: 'er-t-3', name: 'Vital Signs Integration', description: 'Ingest and trend vitals from bedside monitors', level: 'Advanced', tags: ['Patient Monitor API', 'Bedside IoT'] },
  ],
  'er-ambulance': [
    { id: 'er-a-1', name: 'ETA Prediction', description: 'Predict ambulance arrival using GPS and live traffic data', level: 'Expert', tags: ['GPS API', 'Traffic Feed'] },
    { id: 'er-a-2', name: 'Bay Pre-allocation', description: 'Reserve resus bay based on incoming patient severity score', level: 'Advanced', tags: ['Bay Management'] },
    { id: 'er-a-3', name: 'Handover Protocol', description: 'Structured handover checklist on ambulance arrival', level: 'Basic', tags: ['Handover Form'] },
  ],
  'er-erqueue': [
    { id: 'er-q-1', name: 'Wait Time Forecasting', description: 'Predict patient wait time based on current load and historical data', level: 'Expert', tags: ['ML Model', 'Historical Data'] },
    { id: 'er-q-2', name: 'Load Balancing', description: 'Distribute patients across available treatment bays', level: 'Basic', tags: ['Bay Census'] },
    { id: 'er-q-3', name: 'Diversion Alerts', description: 'Alert when ER approaches capacity and initiate diversion protocol', level: 'Advanced', tags: ['Alert API', 'Capacity Monitor'] },
    { id: 'er-q-4', name: 'Bottleneck Detection', description: 'Identify and surface flow bottlenecks in real time', level: 'Advanced', tags: ['Flow Analytics'] },
  ],
  'er-resus': [
    { id: 'er-r-1', name: 'Resus Readiness Check', description: 'Confirm equipment and staff readiness before critical patient arrival', level: 'Advanced', tags: ['Checklist Engine'] },
    { id: 'er-r-2', name: 'Team Paging', description: 'Auto-page resus team based on incoming case severity', level: 'Basic', tags: ['Paging System'] },
  ],

  'icu-icubed': [
    { id: 'icu-b-1', name: 'Step-down Eligibility', description: 'Score each ICU patient for HDU/step-down transfer readiness', level: 'Expert', tags: ['APACHE Score', 'Clinical Rules'] },
    { id: 'icu-b-2', name: 'HDU Compatibility Match', description: 'Match ICU patients to HDU beds by acuity and isolation requirements', level: 'Expert', tags: ['Bed Matching Engine'] },
    { id: 'icu-b-3', name: 'Discharge Timeline Forecast', description: 'Predict ICU length of stay and discharge window', level: 'Advanced', tags: ['LOS Model'] },
  ],
  'icu-icuacuity': [
    { id: 'icu-a-1', name: 'APACHE II Scoring', description: 'Compute APACHE II score from lab and clinical data', level: 'Expert', tags: ['Lab API', 'Scoring Engine'] },
    { id: 'icu-a-2', name: 'SOFA Score Tracking', description: 'Continuous SOFA scoring with trend alerting', level: 'Advanced', tags: ['SOFA Model', 'Trend Alert'] },
    { id: 'icu-a-3', name: 'Deterioration Alert', description: 'Early warning alert when acuity score worsens beyond threshold', level: 'Advanced', tags: ['Alert API'] },
    { id: 'icu-a-4', name: 'Sedation Level Monitor', description: 'Track RASS sedation score and flag anomalies', level: 'Basic', tags: ['RASS Protocol'] },
  ],
  'icu-icutransfer': [
    { id: 'icu-t-1', name: 'Transfer Risk Assessment', description: 'Quantify risk of adverse event during ICU-to-ward transfer', level: 'Expert', tags: ['Risk Model'] },
    { id: 'icu-t-2', name: 'Escort Coordination', description: 'Assign and brief transfer escort team', level: 'Basic', tags: ['Staff Roster'] },
    { id: 'icu-t-3', name: 'Receiving Ward Briefing', description: 'Generate handover summary for receiving ward nurse', level: 'Advanced', tags: ['Handover Template'] },
  ],

  'bed-occupancy': [
    { id: 'bed-o-1', name: 'Real-time Bed Mapping', description: 'Live ward-level bed status across all wards', level: 'Expert', tags: ['HMS Integration', 'IoT Sensors'] },
    { id: 'bed-o-2', name: 'Ward-level Capacity View', description: 'Aggregated occupancy rate by ward and speciality', level: 'Advanced', tags: ['Dashboard API'] },
    { id: 'bed-o-3', name: 'Cross-ward Availability', description: 'Identify available beds across ward boundaries for overflow', level: 'Advanced', tags: ['Cross-ward Routing'] },
  ],
  'bed-discharge-pred': [
    { id: 'bed-d-1', name: 'Discharge Window Prediction', description: 'Predict discharge time within ±2 hour window using LOS model', level: 'Expert', tags: ['LOS Model', 'Doctor Orders'] },
    { id: 'bed-d-2', name: 'Barrier Identification', description: 'Surface barriers to discharge: lab pending, transport, consent', level: 'Advanced', tags: ['Barrier Tracker'] },
    { id: 'bed-d-3', name: 'Discharge Readiness Score', description: 'Composite score for readiness across clinical, admin, and pharmacy', level: 'Advanced', tags: ['Composite Score'] },
  ],
  'bed-assignment': [
    { id: 'bed-a-1', name: 'Optimal Bed Matching', description: 'Match patient to best available bed by acuity, gender, and infection status', level: 'Expert', tags: ['Matching Engine'] },
    { id: 'bed-a-2', name: 'Isolation Compliance', description: 'Enforce infection control rules during bed assignment', level: 'Advanced', tags: ['Infection Control'] },
    { id: 'bed-a-3', name: 'Priority Queue', description: 'Manage admission queue by urgency and bed type', level: 'Basic', tags: ['Queue Manager'] },
    { id: 'bed-a-4', name: 'Preference Handling', description: 'Honour patient and clinical preferences in bed allocation', level: 'Basic', tags: ['Preference Rules'] },
  ],

  'staffing-beds': [
    { id: 'sta-b-1', name: 'Live Bed Census', description: 'Real-time nurse-accessible bed availability dashboard', level: 'Basic', tags: ['HMS Feed'] },
    { id: 'sta-b-2', name: 'Occupancy Rate Alert', description: 'Alert charge nurse when ward occupancy exceeds 90%', level: 'Advanced', tags: ['Alert API'] },
  ],
  'staffing-allocation': [
    { id: 'sta-a-1', name: 'Nurse-to-Patient Matching', description: 'Assign nurses based on patient acuity and nurse competency', level: 'Expert', tags: ['Competency Matrix'] },
    { id: 'sta-a-2', name: 'Ratio Compliance Check', description: 'Ensure nurse-patient ratios comply with regulatory standards', level: 'Advanced', tags: ['Regulatory Rules'] },
    { id: 'sta-a-3', name: 'Float Pool Dispatch', description: 'Request and dispatch float pool nurses when understaffed', level: 'Basic', tags: ['Float Pool Roster'] },
  ],
  'staffing-shift-opt': [
    { id: 'sta-s-1', name: 'Shift Overlap Optimisation', description: 'Minimise handover gaps with optimised shift timing', level: 'Advanced', tags: ['Schedule Optimizer'] },
    { id: 'sta-s-2', name: 'Fatigue Score Monitoring', description: 'Track cumulative hours and flag fatigue risk', level: 'Expert', tags: ['Fatigue Model'] },
  ],

  'discharge-med-clear': [
    { id: 'dis-m-1', name: 'Medical Sign-off Tracker', description: 'Track and chase pending doctor sign-offs for discharge', level: 'Basic', tags: ['Doctor Orders'] },
    { id: 'dis-m-2', name: 'Clinical Criteria Check', description: 'Verify patient meets clinical discharge criteria', level: 'Advanced', tags: ['Clinical Rules'] },
  ],
  'discharge-pharm-clear': [
    { id: 'dis-p-1', name: 'Take-home Medication Prep', description: 'Prepare and dispense take-home medications before discharge', level: 'Advanced', tags: ['Pharmacy System'] },
    { id: 'dis-p-2', name: 'Medication Reconciliation', description: 'Reconcile admission vs discharge medication lists', level: 'Expert', tags: ['Medication List', 'EMR'] },
  ],
  'discharge-bill-clear': [
    { id: 'dis-b-1', name: 'Final Bill Generation', description: 'Generate itemised final bill from episode charges', level: 'Basic', tags: ['Billing Engine'] },
    { id: 'dis-b-2', name: 'Payment Clearance Check', description: 'Confirm outstanding balance settled before discharge', level: 'Advanced', tags: ['Payment Gateway'] },
  ],

  'ot-schedule': [
    { id: 'ot-s-1', name: 'OT Block Time Management', description: 'Allocate and optimise OT block time across surgical teams', level: 'Expert', tags: ['Block Scheduler'] },
    { id: 'ot-s-2', name: 'Delay Detection & Replan', description: 'Detect OT running behind and replan remaining cases', level: 'Advanced', tags: ['Schedule Engine'] },
    { id: 'ot-s-3', name: 'Case Prioritisation', description: 'Rank pending cases by urgency, surgeon preference, and resources', level: 'Advanced', tags: ['Priority Rules'] },
  ],
  'ot-preop': [
    { id: 'ot-p-1', name: 'Pre-op Checklist Automation', description: 'Auto-complete pre-op checklist from EMR and lab data', level: 'Advanced', tags: ['Checklist Engine', 'Lab API'] },
    { id: 'ot-p-2', name: 'Lab TAT Monitoring', description: 'Track pending lab TAT and escalate if surgery at risk', level: 'Expert', tags: ['Lab API', 'Escalation'] },
    { id: 'ot-p-3', name: 'Consent Verification', description: 'Verify signed consent is in place before case proceeds', level: 'Basic', tags: ['Document Store'] },
  ],
  'ot-otstaff': [
    { id: 'ot-o-1', name: 'Scrub Nurse Assignment', description: 'Match scrub nurse to case by speciality and availability', level: 'Advanced', tags: ['Staff Roster'] },
    { id: 'ot-o-2', name: 'Anaesthesia Team Briefing', description: 'Brief anaesthesia team on case specifics and patient history', level: 'Advanced', tags: ['EMR Integration'] },
    { id: 'ot-o-3', name: 'Equipment Readiness', description: 'Verify all required instruments and equipment are sterilised', level: 'Basic', tags: ['CSSD Integration'] },
  ],

  'lab-latest': [
    { id: 'lab-l-1', name: 'Result Retrieval & Parse', description: 'Pull latest lab results from LIS and parse structured values', level: 'Basic', tags: ['LIS API'] },
    { id: 'lab-l-2', name: 'Reference Range Flagging', description: 'Flag values outside reference range with clinical context', level: 'Advanced', tags: ['Reference DB'] },
    { id: 'lab-l-3', name: 'Trend Analysis', description: 'Track lab value trends over time and detect deterioration', level: 'Expert', tags: ['Trend Engine'] },
  ],
  'lab-pending': [
    { id: 'lab-p-1', name: 'TAT Monitoring', description: 'Track turnaround time for each pending test in real time', level: 'Basic', tags: ['LIS Feed'] },
    { id: 'lab-p-2', name: 'Escalation Trigger', description: 'Escalate to lab supervisor when TAT exceeds clinical threshold', level: 'Advanced', tags: ['Escalation API'] },
  ],
  'lab-critical': [
    { id: 'lab-c-1', name: 'Panic Value Detection', description: 'Detect critical/panic lab values the moment they are resulted', level: 'Expert', tags: ['Panic Rules', 'LIS API'] },
    { id: 'lab-c-2', name: 'Clinician Notification', description: 'Immediately notify responsible clinician via SMS and in-app alert', level: 'Advanced', tags: ['Alert API', 'SMS Gateway'] },
  ],

  'billing-preauth': [
    { id: 'bil-a-1', name: 'Pre-auth Submission', description: 'Submit pre-authorisation request to TPA with clinical documents', level: 'Basic', tags: ['TPA Portal'] },
    { id: 'bil-a-2', name: 'Verbal Auth Tracking', description: 'Record verbal pre-auth and chase for written confirmation', level: 'Advanced', tags: ['Tracker'] },
  ],
  'billing-tpaliaison': [
    { id: 'bil-t-1', name: 'TPA Follow-up Automation', description: 'Auto-follow-up with TPA on pending approvals', level: 'Advanced', tags: ['TPA API', 'Email'] },
    { id: 'bil-t-2', name: 'Rejection Management', description: 'Handle TPA rejections with re-submission workflow', level: 'Expert', tags: ['Appeals Engine'] },
  ],
  'billing-deposit': [
    { id: 'bil-d-1', name: 'Estimate Generation', description: 'Generate patient liability estimate from package rates', level: 'Basic', tags: ['Rate Card'] },
    { id: 'bil-d-2', name: 'Deposit Collection Tracker', description: 'Track deposit collection and flag outstanding cases', level: 'Advanced', tags: ['Payment API'] },
  ],

  'revenue-unbilled': [
    { id: 'rev-u-1', name: 'Procedure Audit', description: 'Cross-check performed procedures against billed items daily', level: 'Expert', tags: ['Procedure Log', 'Billing System'] },
    { id: 'rev-u-2', name: 'Charge Capture', description: 'Automatically capture charges from clinical documentation', level: 'Advanced', tags: ['CDI Rules'] },
  ],
  'revenue-claims': [
    { id: 'rev-c-1', name: 'Aging Report', description: 'Generate claims aging report by payer and outstanding days', level: 'Basic', tags: ['Payer Data'] },
    { id: 'rev-c-2', name: 'Denial Pattern Analysis', description: 'Identify common claim denial patterns and root causes', level: 'Expert', tags: ['ML Model', 'Denial DB'] },
  ],
  'revenue-collection': [
    { id: 'rev-col-1', name: 'Dues Tracker', description: 'Track outstanding patient dues with ageing classification', level: 'Basic', tags: ['AR System'] },
    { id: 'rev-col-2', name: 'Payment Plan Generator', description: 'Generate customised payment plans for high-outstanding cases', level: 'Advanced', tags: ['Plan Templates'] },
  ],
}

export interface AgentMeta {
  category: string
  successRate: number
  avgResponse: string
  expertCoverage: number
}

export const AGENT_META: Record<string, AgentMeta> = {
  er:          { category: 'Emergency Care',   successRate: 96, avgResponse: '1.2s', expertCoverage: 27 },
  icu:         { category: 'Critical Care',    successRate: 98, avgResponse: '0.8s', expertCoverage: 50 },
  bed:         { category: 'Resource Mgmt',    successRate: 94, avgResponse: '1.5s', expertCoverage: 45 },
  staffing:    { category: 'Workforce Mgmt',   successRate: 92, avgResponse: '2.1s', expertCoverage: 33 },
  discharge:   { category: 'Patient Exit',     successRate: 90, avgResponse: '1.8s', expertCoverage: 25 },
  ot:          { category: 'Surgical Ops',     successRate: 97, avgResponse: '1.0s', expertCoverage: 55 },
  lab:         { category: 'Diagnostics',      successRate: 99, avgResponse: '0.5s', expertCoverage: 40 },
  pharmacy:    { category: 'Medication Safety',successRate: 99, avgResponse: '0.7s', expertCoverage: 30 },
  clinical:    { category: 'Clinical Ops',     successRate: 95, avgResponse: '1.3s', expertCoverage: 35 },
  housekeeping:{ category: 'Facility Ops',     successRate: 88, avgResponse: '3.0s', expertCoverage: 10 },
  patientflow: { category: 'Patient Flow',     successRate: 91, avgResponse: '1.6s', expertCoverage: 20 },
  billing:     { category: 'Revenue Cycle',    successRate: 89, avgResponse: '2.5s', expertCoverage: 30 },
  revenue:     { category: 'Revenue Cycle',    successRate: 87, avgResponse: '2.8s', expertCoverage: 38 },
}

export const COVERAGE_DOMAINS = [
  { domain: 'Emergency Care',      coverage: 78, color: '#ef4444' },
  { domain: 'Resource Management', coverage: 91, color: '#14b8a6' },
  { domain: 'Patient Flow',        coverage: 72, color: '#f97316' },
  { domain: 'Critical Care',       coverage: 88, color: '#8b5cf6' },
  { domain: 'Surgical Ops',        coverage: 85, color: '#7c3aed' },
  { domain: 'Revenue Cycle',       coverage: 67, color: '#eab308' },
]

export const ORCHESTRATION_INSIGHTS: Record<string, string[]> = {
  er: [
    'Maps incoming emergencies to triage and ambulance sub-agents first',
    'Escalates to ICU agent when admission probability exceeds 80%',
    'Uses wait time forecasting to auto-trigger diversion alerts',
    'Selects resus bay sub-agent only for red/orange triage categories',
  ],
  icu: [
    'Runs acuity scoring before any bed reallocation decision',
    'Blocks transfer if APACHE II worsened in last 6 hours',
    'Pairs with Bed Intelligence agent for HDU availability',
    'Requires human approval before step-down for SOFA > 8',
  ],
  bed: [
    'Queries occupancy sub-agent first on every admission request',
    'Runs discharge prediction to pre-empty beds proactively',
    'Enforces isolation rules via assignment sub-agent at all times',
    'Integrates with staffing agent to confirm nurse availability',
  ],
  staffing: [
    'Checks nurse-to-patient ratios before confirming any transfer',
    'Dispatches float pool when ward falls below safe ratio',
    'Cross-references with shift schedule to avoid fatigue assignments',
    'Notifies charge nurse when allocation confidence is low',
  ],
  default: [
    'Maps incoming tasks to agent capabilities automatically',
    'Selects best agent based on capability level and load',
    'Considers performance metrics for routing decisions',
    'Balances load across agents with similar capabilities',
  ],
}
