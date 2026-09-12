# Hospilot Roadmap

Building an open-source Agentic AI operating layer on top of HIS/HRMS.

Hospilot is evolving from operational AI agents into a platform where agents can observe, predict, simulate, recommend, act, and continuously evaluate outcomes across healthcare workflows.

Our direction:

```
Observe → Predict → Simulate → Recommend → Approve → Act → Evaluate → Learn
```

This roadmap represents our current direction and will evolve based on community contributions, healthcare requirements, and real-world deployments.

## 🚀 Where We Are Today

Hospilot already provides the foundation for building event-driven healthcare operational agents.

### Available Foundation

- ✅ Agentic framework
- ✅ Healthcare operational agents
- ✅ Fabric for integration with HIS/HRMS
- ✅ Kafka integration
- ✅ Workflow orchestration
- ✅ LangGraph-based agent orchestration
- ✅ Temporal workflows
- ✅ REST/API integration
- ✅ Assisted Mode
- ✅ Docker-based deployment
- ✅ Hospilot web interface

Initial operational agents cover areas such as:

- ICU occupancy
- Bed occupancy
- OT utilization
- ER wait time
- Delayed discharge
- Nurse demand
- ER surge
- Hospital gridlock risk
- Decongestion recommendations

# 🗺️ What's Next

Hospilot development will progress across several parallel tracks rather than as a strictly sequential feature roadmap.

## 1. 🧠 Advisory Mode

### From Insights to Recommendations

Move agents beyond identifying operational conditions toward recommending actions while keeping humans in control.

### Planned Capabilities

- Agent-generated recommendations
- Recommended actions
- Confidence scoring
- Explainability
- Supporting evidence
- Expected impact
- Alternative recommendations
- Recommendation prioritization
- Approve / Reject / Modify
- Human feedback
- Configurable approval workflows
- Escalation policies
- Recommendation history
- Decision audit trail

### Target Flow

```
Operational Event
  ↓
Agent
  ↓
Analysis / Prediction
  ↓
Recommendation
  ↓
Confidence + Evidence
  ↓
Approve / Modify / Reject
```

## 2. 🔓 Open-Source Model Runtime

### Make Hospilot Model-Agnostic

Hospilot agents should be able to run using cloud models, open-source models, locally deployed models, or specialized SLMs.

### Planned Capabilities

- Pluggable model providers
- Open-source LLM support
- Local model inference
- Cloud model support
- Specialized SLM support
- Per-agent model selection
- Model fallback
- Dynamic model routing
- Cost-aware routing
- Latency-aware routing
- Quality-aware routing
- Quantized model support
- On-premise inference
- Model benchmarking

Potential runtimes include:

- vLLM
- Ollama
- Hugging Face TGI
- Local GPU Runtime
- Cloud Model APIs

### Target Architecture

```
Agent
  ↓
Model Router
  ↓
┌─────────────┼─────────────┐
↓             ↓             ↓
Cloud Model   Open Model    Specialized SLM
↓             ↓             ↓
└─────────────┼─────────────┘
  ↓
Agent Output
```

## 3. 🔭 Forecasting & Predictive Agents

### Predict Before It Happens

Build reusable forecasting capabilities that can be consumed by multiple Hospilot agents.

### Initial Forecasting Areas

- ER arrival forecasting
- ER surge prediction
- Bed occupancy forecasting
- ICU occupancy forecasting
- OT utilization forecasting
- Discharge volume forecasting
- Nurse demand forecasting
- Staffing demand forecasting
- Pharmacy workload prediction
- Laboratory workload prediction
- Hospital gridlock prediction
- Operational bottleneck prediction

### Forecasting Framework

- Time-series forecasting
- Configurable forecast horizons
- 3-hour / 6-hour / 12-hour / 24-hour forecasting
- Multi-day forecasting
- Confidence intervals
- Trend detection
- Seasonality
- Anomaly detection
- Forecast-vs-actual tracking
- Forecast accuracy metrics
- Model comparison
- Retraining hooks

### Target Flow

```
Healthcare Events
  ↓
Feature Pipeline
  ↓
Forecasting Agent
  ↓
Future State / Risk
  ↓
Operational Agents
  ↓
Recommendation
```

## 4. 🔍 Agent Observability with Langfuse

### Understand Every Agent Decision

Agentic healthcare workflows need end-to-end traceability.

Hospilot plans to integrate Langfuse for agent and model observability.

### Planned Capabilities

- Agent execution tracing
- Prompt tracing
- Prompt versioning
- Model input/output tracing
- Tool-call tracing
- Multi-agent traces
- Workflow traces
- Session traces
- Agent execution lineage
- Model/version tracking
- Latency monitoring
- Token usage
- Model cost
- Error tracking
- Evaluation hooks
- Human feedback association

The goal is to answer:

- Why did the agent trigger?
- What data did it receive?
- Which model did it use?
- Which tools did it call?
- Which agents participated?
- What did it recommend?
- How confident was it?
- What action followed?
- What was the outcome?

## 5. 🧪 Agent Simulation Framework

### Test Before Deployment

Every Hospilot agent should be testable without requiring access to a live healthcare environment.

### Planned Capabilities

- Synthetic healthcare events
- Historical event replay
- Scenario-based testing
- What-if testing
- Agent behavior simulation
- Recommendation simulation
- Policy simulation
- Autonomous-action dry runs
- Edge-case simulation
- Failure injection
- Load scenarios
- Reproducible simulations
- Expected-vs-actual comparison
- Simulation scoring

### Target Agent Lifecycle

```
Develop
  ↓
Unit Test
  ↓
Historical Replay
  ↓
Synthetic Simulation
  ↓
Digital Twin Simulation
  ↓
Shadow Mode
  ↓
Advisory Mode
  ↓
Controlled Autonomous
  ↓
Autonomous
```

This will allow open-source contributors to build and evaluate agents without requiring real hospital data.

## 6. 🏥 Healthcare Digital Twin

### Create a Virtual Healthcare Operations Environment

Build a dynamic digital representation of healthcare operations that agents can use for simulation and decision support. This is the state representation the closed-loop system in [§12](#12--digital-twin-optimization) forecasts against and optimizes over.

### Initial Digital Twin Entities

```
Healthcare Facility
│
├── ER
├── ICU
├── Wards
├── Beds
├── OT
├── Patients
├── Doctors
├── Nurses
├── Staff
├── Discharge Pipeline
├── Pharmacy
├── Laboratory
├── Radiology
└── Operational Resources
```

### Planned Capabilities

- Real-time operational state
- Resource state representation
- Patient-flow representation
- Resource dependencies
- Capacity constraints
- Operational constraints
- State transitions
- Event propagation
- Historical state reconstruction
- Future-state simulation
- Agent interaction with the Digital Twin

## 7. 🔬 What-If & Scenario Engine

### Simulate Decisions Before Acting

Allow agents and healthcare teams to evaluate potential interventions before they are applied.

Examples:

- What if ER arrivals increase by 25%?
- What if 10 planned discharges are delayed?
- What if ICU occupancy reaches 90%?
- What if an OT case runs two hours late?
- What if nurse availability drops by 15%?
- What if we open 8 additional flex beds?

### Planned Capabilities

- Scenario builder
- Parameter modification
- Event injection
- Capacity modification
- Staffing modification
- Patient-flow simulation
- Bottleneck simulation
- Intervention simulation
- Multiple scenario comparison
- Outcome estimation
- Scenario ranking

## 8. 🤖 Autonomous Mode

### From Recommendations to Controlled Actions

Hospilot's autonomous capabilities will be introduced progressively rather than as an on/off feature.

### Autonomy Journey

```
Assisted
  ↓
Advisory
  ↓
Shadow
  ↓
Approval-Based Action
  ↓
Controlled Autonomous
  ↓
Autonomous
```

### Planned Capabilities

- Policy-based actions
- Configurable autonomy levels
- Action permissions
- Allow/deny action lists
- Approval gates
- Precondition validation
- Safety constraints
- Multi-step workflow execution
- Agent-to-agent delegation
- Healthcare-system write-back
- Retry handling
- Timeout handling
- Exception handling
- Rollback / compensation
- Human override
- Kill switch
- Dry-run execution
- Complete action audit trail

## 9. 📊 Agent Evaluation Framework

### Measure Agent Quality Continuously

Every Hospilot agent should have measurable quality and safety criteria.

### Planned Capabilities

- Agent-specific evaluation datasets
- Offline evaluation
- Online evaluation
- Task success rate
- Recommendation accuracy
- Forecast accuracy
- Tool-call accuracy
- Action success rate
- Confidence calibration
- Hallucination checks
- Safety evaluation
- Policy compliance
- Regression testing
- Model comparison
- Prompt comparison
- Agent-version comparison
- Human feedback scoring
- Production drift detection
- Failure analysis

The goal:

Move from "the agent seems to work" to "we can measure how well and how safely the agent works."

## 10. 🧩 Agent Hub

### Build, Configure, Discover and Operate Agents

Agent Hub will evolve into the control plane for the Hospilot agent ecosystem.

### Planned Capabilities

- Agent registry
- Agent discovery
- Agent manifests
- Capability schemas
- Input/output schemas
- Event subscriptions
- Tool registry
- Agent dependencies
- Model configuration
- Prompt configuration
- Policy configuration
- Agent permissions
- Agent versioning
- Enable / disable agents
- Agent lifecycle management
- Agent health
- Execution history
- Deployment configuration
- Tenant configuration
- Evaluation results
- Community agent packaging

### Target Agent Definition

```
Agent
│
├── Identity
├── Capabilities
├── Inputs / Outputs
├── Events
├── Tools
├── Model
├── Prompts
├── Policies
├── Permissions
├── Dependencies
├── Simulation
├── Evaluation
└── Version
```

## 11. 🧠 Model & Prompt Hub

### Manage the Intelligence Behind Agents

Separate agent functionality from the AI models and prompts powering each agent. Where [§2](#2--open-source-model-runtime) is *how* a model runs (routing, inference, fallback), this is the registry and governance layer on top of it — versioning, benchmarking, and per-agent assignment.

### Planned Capabilities

- Model registry
- Open-source model registry
- Cloud model registry
- Fine-tuned model registry
- Specialized SLM registry
- Embedding model registry
- Prompt registry
- Prompt versioning
- Model versioning
- Model cards
- Benchmark results
- Per-agent model assignment
- Routing policies
- Fallback policies
- Model comparison
- Prompt/model experimentation

## 12. 🔄 Digital Twin Optimization

### Forecast → Simulate → Optimize → Act

The integration layer: wires the Digital Twin ([§6](#6--healthcare-digital-twin)), Forecasting Agents ([§3](#3--forecasting--predictive-agents)), and the Scenario Engine ([§7](#7--what-if--scenario-engine)) into one closed loop, then adds optimization on top. No new twin is introduced here — this is what those three pieces produce together.

```
Real-Time Healthcare Data
  ↓
Digital Twin
  ↓
Forecasting Agents
  ↓
Future Risks / Bottlenecks
  ↓
Scenario Engine
  ↓
Simulate Multiple Actions
  ↓
Compare Expected Outcomes
  ↓
Optimization
  ↓
Recommendation
  ↓
Human / Policy Gate
  ↓
Agent Action
  ↓
Outcome
  ↓
Evaluation & Learning
```

### Target Optimization Areas

- Bed allocation
- Capacity management
- Patient flow
- ICU utilization
- Staffing
- Discharge
- OT utilization
- Resource allocation
- Bottleneck reduction
- Multi-objective operational optimization

## 13. 🛡️ Production Readiness & Governance

### Enterprise-Grade Agentic AI

Build the infrastructure required to safely operate Hospilot in production healthcare environments.

### Security & Platform

- Multi-tenancy
- Tenant isolation
- RBAC / ABAC
- SSO
- Secrets management
- Encryption
- API security
- Rate limiting
- Docker
- Kubernetes
- Horizontal scaling
- High availability
- Backup and recovery

### AI & Healthcare Governance

- PHI / PII controls
- Agent permissions
- Model governance
- Prompt governance
- Policy management
- Action governance
- Human approval policies
- Decision lineage
- Model/version lineage
- Complete audit trails

### Reliability

- Distributed tracing
- Metrics
- Logging
- Alerting
- Agent health monitoring
- Model health monitoring
- Kafka reliability
- Workflow resilience
- SLA / SLO dashboards
- Load testing
- Performance benchmarks
- Failure recovery

# Parallel Roadmap Tracks

Hospilot's development is intentionally parallel.

| Track | Direction |
| --- | --- |
| Agents | Detection → Prediction → Recommendation → Action |
| Models | Cloud → Open Source → Fine-Tuned → Specialized SLM |
| Forecasting | Prediction → Multi-Agent Forecasting → Optimization |
| Observability | Logging → Langfuse → Evaluation → Production Monitoring |
| Simulation | Synthetic → Historical Replay → Digital Twin |
| Digital Twin | Current State → Future State → What-If → Optimization |
| Autonomy | Assisted → Advisory → Shadow → Controlled → Autonomous |
| Evaluation | Tests → Simulation → Offline Eval → Online Eval → Continuous Eval |
| Integration | REST → Kafka → FHIR → Healthcare-System Write-Back |
| Governance | Audit → Policies → Permissions → Safety → Decision Lineage |

# 🎯 Long-Term Direction

Hospilot aims to create an open ecosystem where healthcare AI agents can be:

```
Built → Connected → Simulated → Evaluated → Governed → Deployed → Observed → Improved
```

The ultimate operational loop is:

```
OBSERVE
  ↓
PREDICT
  ↓
SIMULATE
  ↓
RECOMMEND
  ↓
APPROVE
  ↓
ACT
  ↓
EVALUATE
  ↓
LEARN
  ↺
```

Powered across the stack by:

Open Models · Forecasting · Langfuse · Simulation · Digital Twin · Evaluation · Governance

# 🤝 Contribute to the Roadmap

Hospilot is an open-source project, and this roadmap is intentionally open to community participation.

Areas where contributors can help include:

- Healthcare agents
- Forecasting models
- Open-source model integrations
- Langfuse observability
- Agent simulation
- Healthcare Digital Twin
- FHIR integrations
- OpenMRS integrations
- OpenEMR integrations
- Agent evaluation
- Agent orchestration
- Optimization
- Developer tooling
- Documentation

Look for issues labeled:

`good first issue` · `help wanted` · `agent` · `forecasting` · `simulation` · `digital-twin` · `open-model` · `integration` · `evaluation`

Have an idea that isn't listed here?

Open an issue or start a discussion. We would love to build the future of open-source Agentic AI for healthcare together.

---

**Note:** This roadmap describes the direction of the project, not guaranteed release commitments. Features, priorities, and sequencing may change based on community feedback, technical learnings, and healthcare requirements.
