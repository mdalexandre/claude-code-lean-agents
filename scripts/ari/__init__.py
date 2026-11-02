"""Agent Runtime Intelligence (ARI): read only analysis of local Claude Code transcripts.

Modules:
    evidence   canonical event model and transcript ingestion (shared by every tool)
    abduction  competing explanations with discriminating tests and bounded recursion
    forecast   weekly measured token burn and a simple, backtested forecast
    audit      skills and MCP inventory, exposure and invocation audit
    report     self contained HTML report and the opt in privacy screened share file
    learn      nightly, opt in, idempotent lesson store with a gated lifecycle
    interpret  findings with competing readings that gate capability advice (2.0)
    discovery  bounded abductive discovery ledger (2.0)
    holons     responsibility contracts of the six ARI holons (2.0)

Standard library only. Nothing here opens a network connection.
"""

ARI_VERSION = "2.0.0"
