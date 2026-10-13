---
name: global-env-checking
description: "Unified environment setup for all custom skills: Python venvs, Node modules, and system tool checks (non-blocking)"
metadata:
  openclaw:
    emoji: "⚙️"
    events: ["command:new", "agent:bootstrap"]
    requires:
      bins: ["node", "npm", "curl"]
      anyBins: ["uv", "python3"]
---

# Unified Environment Hook

This hook ensures all skill dependencies are ready before any command is processed.  
It runs asynchronously and does not block the main flow.  

**What it does:**
- Checks and creates missing Python virtual environments (`venv_*`) with required packages.
- Installs missing Node.js modules (`docx`, `pdf-lib`, `pptxgenjs`, etc.).
- Detects system tools (`pandoc`, `soffice`, `tesseract`) but **never attempts apt-get install** (due to permission restrictions).
- Sends progress messages to the user (only for `/new` commands).

**Fallback strategy:**
- If a system tool is missing, the hook logs a warning but does not block – skills are expected to use Node/Python alternatives.
- All installations run in the background to keep the chat responsive.