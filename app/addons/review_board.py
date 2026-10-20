# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Part 4 Add-On: Multi-Persona Virtual Review Board with ADK 2.0 Graph Workflows.

This module is a self-contained, plug-and-play add-on that leaves `app/agent.py`,
`app/tools.py`, and `app/addons/ai_comments.py` 100% untouched.

Architecture (ADK 2.0 Graph `Workflow` — Diamond Fan-Out / Fan-In Topology):
  1. Stage 1 (`ingest_google_doc` — Python `FunctionNode`):
     Reads the Google Doc ONCE into `session.state["doc_text"]` with zero LLM
     token overhead. Emits `route="loaded"` on success or `route="error"` if the
     document cannot be loaded (short-circuiting to `handle_doc_load_error`).
  2. Stage 2 (Concurrent Fan-Out to 3 Specialist `LlmAgent` Nodes):
     - `security_reviewer`: Audits secrets, IAM roles, network exposure, and
       architectural risks -> writes `session.state["security_findings"]`.
     - `fact_checker_reviewer`: Verifies technical claims, quotas, CLI flags,
       and dates via Developer Knowledge MCP + Google Search Grounding ->
       writes `session.state["fact_check_findings"]`.
     - `copy_editor_reviewer`: Tightens prose, fixes passive voice, and catches
       typos/grammar -> writes `session.state["editorial_findings"]`.
  3. Synchronization Barrier (`join_specialist_reviews = JoinNode(...)`):
     Waits until all 3 parallel specialist nodes finish before triggering Stage 3.
  4. Stage 3 (`chief_editor_synthesizer` — Fan-In & Conflict Resolver `LlmAgent`):
     Reads all three specialist reports, deduplicates overlapping `target_text`
     quotes (`Security > Fact-Check > Copy Edit`), prefixes every comment with
     its persona badge (`[🔒 Security Review]`, `[🔍 Fact-Check]`,
     `[✍️ Copy Editor]`), and calls `insert_comments` FIRST followed by
     `propose_suggested_edits` SECOND.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from app.agent import developer_knowledge_mcp, web_search_verifier
from app.tools import (
    insert_comments,
    propose_suggested_edits,
    read_google_doc,
)

MODEL_NAME = "gemini-3.8-flash"

_DOC_URL_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
_DOC_ID_TOKEN_RE = re.compile(r"\b([a-zA-Z0-9_-]{20,})\b")


def _extract_doc_id(raw_value: str) -> str:
    """Extracts a clean Google Document ID from a URL, prompt string, or raw ID."""
    text = (raw_value or "").strip()
    url_match = _DOC_URL_RE.search(text)
    if url_match:
        return url_match.group(1)
    token_match = _DOC_ID_TOKEN_RE.search(text)
    if token_match:
        return token_match.group(1)
    return text


def _resolve_document_id(ctx: Context, node_input: Any) -> str:
    """Resolves the target Google Document ID from session.state or node_input."""
    state_doc_id = str(ctx.state.get("document_id", "")).strip()
    if state_doc_id:
        return _extract_doc_id(state_doc_id)
    return _extract_doc_id(str(node_input or ""))


# =====================================================================
# Stage 1: Pure Python FunctionNode (Single-Read Doc Ingestion + Routing)
# =====================================================================
def ingest_google_doc(ctx: Context, node_input: str) -> Event:
    """Reads a Google Doc once into session.state and routes 'loaded' or 'error'.

    Because this is a pure Python FunctionNode in an ADK 2.0 Graph Workflow,
    it executes deterministically with zero LLM call and zero token overhead.
    """
    clean_id = _resolve_document_id(ctx, node_input)
    if not clean_id:
        error_msg = "No valid Google Document ID was provided."
        return Event(
            output=error_msg,
            route="error",
            state={"doc_error": error_msg},
        )

    doc_data = read_google_doc(clean_id, ctx)
    if doc_data.get("status") == "error":
        error_msg = doc_data.get(
            "error_message", f"Failed to read Google Document '{clean_id}'."
        )
        return Event(
            output=error_msg,
            route="error",
            state={"doc_error": error_msg},
        )

    doc_title = doc_data.get("title", "Untitled")
    doc_text = doc_data.get("full_text") or doc_data.get("text", "")
    if not doc_text.strip():
        error_msg = f"Google Document '{doc_title}' ({clean_id}) is empty."
        ctx.state["doc_error"] = error_msg
        return Event(
            output=error_msg,
            route="error",
            state={"doc_error": error_msg},
        )

    state_updates = {
        "document_id": clean_id,
        "doc_title": doc_title,
        "doc_text": doc_text,
        "security_findings": "No security findings.",
        "fact_check_findings": "No fact-check findings.",
        "editorial_findings": "No editorial findings.",
    }
    for k, v in state_updates.items():
        ctx.state[k] = v

    return Event(
        output=(
            f"Review Google Document '{doc_title}' (ID: {clean_id}) "
            f"using your specialist persona rubric."
        ),
        route="loaded",
        state=state_updates,
    )


def handle_doc_load_error(ctx: Context, node_input: str):
    """Terminal error-handling FunctionNode when a Google Doc cannot be loaded."""
    error_detail = ctx.state.get("doc_error") or node_input
    msg = (
        f"⚠️ **Multi-Persona Review Board Aborted**: {error_detail}\n\n"
        "Because `ingest_google_doc` emitted `route='error'`, the ADK 2.0 "
        "Graph Workflow short-circuited before invoking any specialist LLM agents."
    )
    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=msg)],
        )
    )
    yield Event(output=msg)


# =====================================================================
# Stage 2: Parallel Specialist Nodes (Fan-Out via RoutingMap)
# =====================================================================

# --- Specialist 1: Security & Architecture Reviewer ---
SECURITY_REVIEWER_INSTRUCTION = """You are the **🔒 Security & Cloud Architecture Reviewer** on a Multi-Persona Virtual Review Board.

### Document Under Review
- **Document Title**: {doc_title?}
- **Document Text**:
\"\"\"
{doc_text?}
\"\"\"

### Your Exclusive Focus
Audit the document **strictly** for security, privacy, IAM, and cloud architecture risks:
1. **Hardcoded Secrets / Credentials / PII**: API keys, tokens, passwords, private URLs, or unmasked secrets in code snippets or configuration blocks.
2. **Overly Permissive IAM & Access Control**: Using primitive roles (`roles/owner`, `roles/editor`), `allUsers` / `--allow-unauthenticated` on internal APIs, or missing service account least-privilege boundaries.
3. **Architecture & Reliability Single Points of Failure**: Missing authentication, unencrypted transport, or unsafe deployment patterns.

Do **NOT** review grammar, spelling, or general prose style (another specialist handles that).

### Required Output Format (JSON Only)
Output a clean JSON object with two arrays (`comments` and `suggested_edits`):
```json
{
  "persona": "Security & Architecture",
  "badge": "[🔒 Security Review]",
  "comments": [
    {
      "target_text": "exact verbatim substring from the document (15 to 120 chars, single paragraph, no newlines)",
      "comment_text": "[🔒 Security Review] Clear explanation of the security/IAM/architecture risk and how to remediate it."
    }
  ],
  "suggested_edits": [
    {
      "target_text": "exact verbatim substring to replace (single paragraph, no newlines)",
      "replacement_text": "secure replacement snippet or flag (e.g., replacing a hardcoded key with Secret Manager or fixing an insecure CLI flag)"
    }
  ]
}
```
CRITICAL RULES:
- Every `target_text` MUST be an **exact, character-for-character substring** copied from the Document Text above and MUST NOT span across paragraph breaks (`\\n`).
- Keep `comments` to the top 1–3 highest-impact security/architecture findings and `suggested_edits` to 1–3 concrete fixes.
"""

security_reviewer = LlmAgent(
    name="security_reviewer",
    model=MODEL_NAME,
    description="Audits the Google Doc for security, IAM, credentials, and architecture risks.",
    include_contents="none",
    instruction=SECURITY_REVIEWER_INSTRUCTION,
    output_key="security_findings",
)


# --- Specialist 2: Technical Fact-Checker & Citation Verifier ---
FACT_CHECKER_REVIEWER_INSTRUCTION = """You are the **🔍 Technical Fact-Checker & Citation Specialist** on a Multi-Persona Virtual Review Board.

### Document Under Review
- **Document Title**: {doc_title?}
- **Document Text**:
\"\"\"
{doc_text?}
\"\"\"

### Your Exclusive Focus
Verify the factual accuracy of technical claims, product limits, quotas, CLI commands, APIs, version numbers, and release dates in the document:
1. **Google Cloud, Gemini, ADK, Firebase, Android, Chrome, Flutter, or Go claims**:
   - Use the **Developer Knowledge MCP tools** (`answer_query`, `search_documents`, `get_documents`) to verify against official Google documentation.
   - Convert any `documents/...` path into a full URL (`https://...`).
2. **General public facts, open-source release dates (e.g. Python versions), or industry stats**:
   - Call `web_search_verifier` to verify via Google Search Grounding.

Do **NOT** flag general grammar/typos or security posture unless a technical fact/flag is objectively wrong.

### Required Output Format (JSON Only)
Output a clean JSON object with two arrays (`comments` and `suggested_edits`):
```json
{
  "persona": "Technical Fact-Checker",
  "badge": "[🔍 Fact-Check]",
  "comments": [
    {
      "target_text": "exact verbatim substring of the inaccurate or unverified claim (single paragraph, no newlines)",
      "comment_text": "[🔍 Fact-Check] Factual correction explaining the accurate specification/date + Verified Source: https://..."
    }
  ],
  "suggested_edits": [
    {
      "target_text": "exact verbatim substring containing the wrong quota/flag/date (single paragraph, no newlines)",
      "replacement_text": "corrected sentence or phrase with the verified number, CLI flag, or release date"
    }
  ]
}
```
CRITICAL RULES:
- Every `target_text` MUST be an **exact, character-for-character substring** copied from the Document Text above and MUST NOT span across paragraph breaks (`\\n`).
- Always include the verified `https://...` citation URL inside each `comment_text`.
"""

fact_checker_reviewer = LlmAgent(
    name="fact_checker_reviewer",
    model=MODEL_NAME,
    description="Verifies technical claims, quotas, CLI flags, and dates using Developer Knowledge MCP and Google Search.",
    include_contents="none",
    instruction=FACT_CHECKER_REVIEWER_INSTRUCTION,
    tools=[
        developer_knowledge_mcp,
        AgentTool(agent=web_search_verifier),
    ],
    output_key="fact_check_findings",
)


# --- Specialist 3: Executive Copy Editor & Clarity Specialist ---
COPY_EDITOR_INSTRUCTION = """You are the **✍️ Executive Copy Editor & Clarity Specialist** on a Multi-Persona Virtual Review Board.

### Document Under Review
- **Document Title**: {doc_title?}
- **Document Text**:
\"\"\"
{doc_text?}
\"\"\"

### Your Exclusive Focus
Review the document **strictly** for executive readability, clarity, active voice, and mechanics:
1. **Spelling, Typos, & Grammar**: Fix misspelled words, subject-verb disagreement, and awkward phrasing via `suggested_edits`.
2. **Wordiness & Passive Voice**: Rewrite bloated, passive, or jargon-heavy sentences into crisp, active prose via `suggested_edits`.
3. **Structural / Narrative Flow**: Add 1–2 anchored `comments` where a paragraph is vague, buried in the wrong section, or lacks a clear takeaway.

Do **NOT** change technical numbers, CLI flags, API names, or security configurations (other specialists handle those).

### Required Output Format (JSON Only)
Output a clean JSON object with two arrays (`comments` and `suggested_edits`):
```json
{
  "persona": "Executive Copy Editor",
  "badge": "[✍️ Copy Editor]",
  "comments": [
    {
      "target_text": "exact verbatim substring from the document (single paragraph, no newlines)",
      "comment_text": "[✍️ Copy Editor] Constructive editorial note on structure, tone, or executive clarity."
    }
  ],
  "suggested_edits": [
    {
      "target_text": "exact verbatim substring with typos or passive/wordy phrasing (single paragraph, no newlines)",
      "replacement_text": "crisp, typo-free, active-voice replacement text"
    }
  ]
}
```
CRITICAL RULES:
- Every `target_text` MUST be an **exact, character-for-character substring** copied from the Document Text above and MUST NOT span across paragraph breaks (`\\n`).
"""

copy_editor_reviewer = LlmAgent(
    name="copy_editor_reviewer",
    model=MODEL_NAME,
    description="Polishes prose clarity, active voice, spelling, and executive readability.",
    include_contents="none",
    instruction=COPY_EDITOR_INSTRUCTION,
    output_key="editorial_findings",
)


# =====================================================================
# Synchronization Barrier: JoinNode (Waits for all 3 Parallel Branches)
# =====================================================================
join_specialist_reviews = JoinNode(
    name="join_specialist_reviews",
    description=(
        "Synchronization barrier that waits for security_reviewer, "
        "fact_checker_reviewer, and copy_editor_reviewer to complete "
        "before triggering the Chief Editor Synthesizer."
    ),
)


# =====================================================================
# Stage 3: Chief Editor Synthesizer (Fan-In & Google Docs API Writer)
# =====================================================================
CHIEF_EDITOR_INSTRUCTION = """You are the **🏛️ Chief Editor Synthesizer** leading the Multi-Persona Virtual Review Board.

Three specialist agents have just finished reviewing the Google Document in parallel and passed the `JoinNode` barrier:
- **Document ID**: `{document_id?}`
- **Document Title**: `{doc_title?}`

### Original Document Text (For Verbatim Substring Verification)
\"\"\"
{doc_text?}
\"\"\"

### Specialist Findings from Parallel Branches
1. **🔒 Security & Architecture Findings**:
{security_findings?}

2. **🔍 Technical Fact-Checker Findings**:
{fact_check_findings?}

3. **✍️ Executive Copy Editor Findings**:
{editorial_findings?}

### Your Synthesis & Conflict-Resolution Workflow
You MUST execute the following steps in exact order:

1. **Synthesize & Attribute Anchored Comments**:
   - Collect the `comments` from all three specialists.
   - Ensure every `comment_text` starts with its persona badge so the document author immediately knows which specialist wrote it:
     - `[🔒 Security Review] ...`
     - `[🔍 Fact-Check] ...` (keep all verified `https://...` source URLs intact!)
     - `[✍️ Copy Editor] ...`
   - Verify that every `target_text` is an exact, single-paragraph substring of the Original Document Text.

2. **Synthesize & Deduplicate Suggested Edits (Conflict Resolution)**:
   - Collect the `suggested_edits` from all three specialists.
   - **No Overlapping `target_text` Ranges**: If two specialists propose a `suggested_edit` on the exact same or overlapping `target_text` substring, merge them into a single clean replacement or prioritize in this order:
     1. **🔒 Security Fix** (highest priority)
     2. **🔍 Factual Correction** (second priority)
     3. **✍️ Copy-Editing Polish** (third priority)
   - Verify that every `target_text` is an exact, character-for-character substring of the Original Document Text and does NOT contain paragraph breaks (`\\n`) or identical `replacement_text`.

3. **Apply to Google Docs in Strict Two-Step Order**:
   - **FIRST**: Call `insert_comments(document_id="{document_id?}", comments=[...])` with the combined, persona-badged comments list.
   - **SECOND**: After `insert_comments` returns, call `propose_suggested_edits(document_id="{document_id?}", edits=[...])` with the deduplicated suggested edits list.

4. **Output an Executive Review Board Report**:
   - Present a clean Markdown summary broken down by specialist persona (**🔒 Security & Architecture**, **🔍 Technical Fact-Checking**, and **✍️ Copy Editing**), including a table of every comment and suggested edit applied to the document.
"""

chief_editor_synthesizer = LlmAgent(
    name="chief_editor_synthesizer",
    model=MODEL_NAME,
    description=(
        "Synthesizes findings from all parallel specialists, resolves "
        "overlapping edits, and applies persona-badged Comments and "
        "Suggested Edits to the Google Doc."
    ),
    include_contents="none",
    instruction=CHIEF_EDITOR_INSTRUCTION,
    tools=[
        insert_comments,
        propose_suggested_edits,
    ],
)


# =====================================================================
# Root Graph Workflow: Diamond Fan-Out / Fan-In Topology
# =====================================================================
review_board_agent = Workflow(
    name="multi_persona_review_board",
    description=(
        "An ADK 2.0 Graph Workflow that ingests a Google Doc once via a "
        "Python FunctionNode, fans out conditionally to Security, "
        "Fact-Checking, and Copy-Editing specialists in parallel, "
        "synchronizes at a JoinNode barrier, and synthesizes their findings "
        "into anchored Google Docs Comments and Suggested Edits."
    ),
    edges=[
        # Stage 1 -> Conditional Route ('loaded' fans out to 3 parallel agents; 'error' short-circuits)
        (
            "START",
            ingest_google_doc,
            {
                "loaded": (
                    security_reviewer,
                    fact_checker_reviewer,
                    copy_editor_reviewer,
                ),
                "error": handle_doc_load_error,
            },
        ),
        # Stage 2 -> JoinNode Barrier -> Stage 3 Chief Editor Synthesizer
        (
            (security_reviewer, fact_checker_reviewer, copy_editor_reviewer),
            join_specialist_reviews,
            chief_editor_synthesizer,
        ),
    ],
)

review_board_app = App(
    name="multi_persona_review_board",
    root_agent=review_board_agent,
)


async def run_multi_persona_review_board(
    document_id: str, tool_context: Context
) -> Dict[str, Any]:
    """Callable tool helper to bolt the Graph Workflow onto any existing LlmAgent.

    Usage in `app/agent.py`:
        from app.addons.review_board import run_multi_persona_review_board
        # Add `run_multi_persona_review_board` to `root_agent.tools`
    """
    clean_id = _extract_doc_id(document_id)
    tool_context.state["document_id"] = clean_id
    output = await tool_context.run_node(
        review_board_agent,
        node_input=clean_id,
    )
    return {
        "status": "success",
        "document_id": clean_id,
        "summary": output,
    }
