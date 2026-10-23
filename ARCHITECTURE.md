# Architecture

- **A short `ARCHITECTURE.md`** explaining your choices, what you left out, and how memory works: what is retained, how changing facts are represented, and what is forgotten or invalidated. Include roughly half a page on likely production failures and how you would test whether the system is useful and trustworthy.


Integration Layer
- skip unifying schemas and use the provided csv as hardcoded variables for now.

-->

Database / Memory
- sqlite (keep it simple.)
- context.json (per site / user / notes, flexible schemas)
- Pydantic layer + migration files for initializing data/*.csv. context.json will be filled in by an Agentic Node.
- Maintain everything (we have enough storage), but also track version change logs (high priority stays most recent) so we can undo decisions. Will most likely trigger new validation checks / conflicting data.

-->

CLI / SDK / MCP / Tools
- edit (add / modify)
- search (query / filter / extract)
- Python tool functions registration, skip tool_search for now, allow for flexible queries.

-->

Agentic Nodes

- Reasoning Node (verification, parsing, reasoning)
- Function Nodes (forecast / rules / alerts / actions API)
- Intervention Node (confirm, send message)
- Trigger Node (If XYZ happens, every 1 day ....)
- log each steps / processes. LiteLLM + python barebone is good enough.

-->

UI / UX
- just terminal output for now.


Likely Production failures
- Integration Scalability: Integration layer can get complex
- Capability Scalability: new customer features may require new agentic nodes
- Evaluation Scalability: Need verification / validation nodes to check tone / alignment / accuracy.