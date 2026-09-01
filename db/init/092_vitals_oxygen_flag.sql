-- 092 · Supplemental oxygen and ventilation status on hospilot.vitals
--
-- ⚠ THIS FILE ALTERS A HOSPILOT TABLE. It is the only one that does, it is deliberately
--   separate from 091 for that reason, and it must not be run without sign-off from whoever
--   owns the hospilot sync.
--
-- B.1 — supplemental-O2 boolean. "The single highest-leverage item in this document."
--   One boolean feeding three factors across two components:
--     · NEWS2 completeness   the oxygen parameter is worth 2 of 20 points. hospilot.vitals
--                            has 6 of NEWS2's 7 parameters; this is the missing one
--     · Oxygen Severity      .20 of Clinical Benefit
--     · Oxygen Trend         .30 of Urgency
--
-- B.2 — O2 delivery mode / patient ventilation status. hospilot.ventilator is
--   (id, bed_id, status, branch_id): BED-LEVEL EQUIPMENT, not patient state. ER's headline
--   clinical condition — "high-flow oxygen, deteriorating" — is currently unrepresentable.
--
-- Until this runs, the code treats oxygen as ABSENT, not as room air, and NEWS2 reports
-- reduced coverage. The interim workaround (inferring an O2 flag from an active oxygen order
-- in pharmacy_orders, as END_TO_END Appendix C.2 does) is unreliable: oxygen is frequently
-- not ordered through pharmacy.

BEGIN;

ALTER TABLE hospilot.vitals
    ADD COLUMN IF NOT EXISTS on_oxygen boolean,
    ADD COLUMN IF NOT EXISTS oxygen_delivery_mode text,
    ADD COLUMN IF NOT EXISTS oxygen_flow_rate numeric(5,2);

COMMENT ON COLUMN hospilot.vitals.on_oxygen IS
    'NEWS2 supplemental-oxygen parameter (B.1). NULL = not recorded, NOT room air.';
COMMENT ON COLUMN hospilot.vitals.oxygen_delivery_mode IS
    'B.2 patient-level ventilation status: room_air | nasal_cannula | face_mask | '
    'high_flow | niv | invasive. hospilot.ventilator is bed-level equipment, not this.';

COMMIT;


-- ---------------------------------------------------------------------------------------
-- FALLBACK, if 092 cannot be run
-- ---------------------------------------------------------------------------------------
-- A view in the allocation schema that reproduces the interim behaviour without touching
-- hospilot. Inferior — see the caveat above — but it keeps the allocation package running
-- against an unmodified database.
--
-- CREATE OR REPLACE VIEW allocation.vitals_with_oxygen AS
-- SELECT v.*,
--        EXISTS (
--            SELECT 1 FROM hospilot.pharmacy_orders o
--            WHERE o.patient_token = v.patient_token
--              AND lower(coalesce(o.generic_name, o.medication_name)) LIKE '%oxygen%'
--              AND lower(coalesce(o.status, '')) IN ('active', 'dispensed', 'administered')
--              AND o.prescribed_at <= v.recorded_at
--        ) AS on_oxygen_inferred
-- FROM hospilot.vitals v;
