"""Financial connector -- Hospilot's revenue & billing agents read invoices,
claims, payments, etc. from CarerOS's external **plain-REST** financial API.

Unlike the clinical data plane (`fhirgw`), financial data is NOT FHIR -- provider
EHRs don't expose billing as FHIR resources, so this is ordinary JSON. The layout
mirrors `fhirgw` though:

  * `client`     -- HTTP binding to CarerOS `…/api/financial` (gated on config).
  * `repository` -- agent data-access; CarerOS-first when configured, else the
    existing Hasura/Redis projection (drop-in: identical field names).
"""
