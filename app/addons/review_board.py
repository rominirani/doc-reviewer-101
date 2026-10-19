# app/addons/review_board.py
"""Part 4 Add-On: Multi-Persona Virtual Review Board (ParallelAgent + SequentialAgent).

Demonstrates ADK's deterministic Workflow orchestration (`SequentialAgent` + `ParallelAgent`)
with shared session state (`output_key` + `{state_key}` templating):

  Stage 1 (`doc_ingestion_agent`):
      Reads the Google Doc once and stores `doc_text`, `doc_title`, and `document_id`
      in `session.state` so parallel reviewers don't make redundant Docs API calls.

  Stage 2 (`specialist_review_board` — `ParallelAgent`):
      Runs three specialist personas concurrently in parallel, each writing to its own
      isolated `output_key` in `session.state` without mutating the Google Doc directly:
        1. `security_reviewer`     -> `output_key="security_findings"`
        2. `fact_checker_reviewer` -> `output_key="fact_check_findings"` (MCP + Google Search)
        3. `copy_editor_reviewer`  -> `output_key="editorial_findings"`

  Stage 3 (`chief_editor_synthesizer`):
      Reads all three findings from `session.state`, resolves overlapping edit ranges,
      prefixes every margin comment with its persona badge (`[🔒 Security Review]`,
      `[🔍 Fact-Check]`, `[✍️ Copy Editor]`), and executes `insert_comments` FIRST
      followed by `propose_suggested_edits` SECOND in one conflict-free pass.
"""
from typing import Any, Dict

from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.tools import AgentTool, ToolContext

from app.agent import developer_knowledge_mcp, web_search_verifier
from app.tools import insert_comments, propose_suggested_edits, read_google_doc


def ensure_review_board_state(callback_context: CallbackContext):
    """Initializes default session state keys before the SequentialAgent pipeline starts."""
    defaults = {
        "document_id": "",
        "doc_title": "Untitled",
        "doc_text": "",
        "security_findings": "No security findings.",
        "fact_check_findings": "No fact-check findings.",
        "editorial_findings": "No editorial findings.",
    }
    for key, val in defaults.items():
        if key not in callback_context.state:
            callback_context.state[key] = val
    return None


def load_doc_into_state(
    document_id: str, tool_context: ToolContext
) -> Dict[str, Any]:
    """Reads a Google Document once and stores its content in `session.state` for parallel agents.

    Args:
        document_id: The Google Document ID to load.

    Returns:
        dict with 'status', 'document_id', 'title', and 'character_count'.
    """
    result = read_google_doc(document_id=document_id, tool_context=tool_context)
    if result.get("status") != "success":
        return result

    full_text = result.get("full_text", "")
    title = result.get("title", "Untitled")

    tool_context.state["document_id"] = document_id
    tool_context.state["doc_title"] = title
    tool_context.state["doc_text"] = full_text

    return {
        "status": "success",
        "document_id": document_id,
        "title": title,
        "character_count": len(full_text),
    }


# ---------------------------------------------------------------------------
# Stage 1: Document Ingestion Agent (Reads Doc Once into session.state)
# ---------------------------------------------------------------------------
doc_ingestion_agent = Agent(
    name="doc_ingestion_agent",
    model="gemini-3.8-flash",
    description="Loads the target Google Doc into shared session state once before parallel review.",
    instruction=(
        "Extract the Google Document ID from the user's request and call `load_doc_into_state` "
        "immediately. Once `load_doc_into_state` succeeds, reply with a one-line confirmation "
        "stating the document title and character count."
    ),
    tools=[load_doc_into_state],
)


# ---------------------------------------------------------------------------
# Stage 2: Parallel Specialist Review Board (3 Concurrent Persona Agents)
# ---------------------------------------------------------------------------
SECURITY_REVIEWER_INSTRUCTION = """
You are the 🔒 Security & Architecture Reviewer on a Virtual Review Board.
Review the following Google Document ("{doc_title?}") strictly through a security, IAM, authentication, data privacy, and operational resilience lens:

<DOCUMENT_TEXT>
{doc_text?}
</DOCUMENT_TEXT>

Identify:
1. SECURITY COMMENTS (Margin Notes):
   - Unauthenticated public endpoints (e.g., `--public-access` or `--allow-unauthenticated` without IAM/IAP justification).
   - Overly broad IAM roles, hardcoded tokens/secrets, missing encryption/VPC guardrails, or high-blast-radius migration assumptions (e.g., skipping load testing or security reviews).
   - For each issue, provide the exact verbatim `target_text` from `<DOCUMENT_TEXT>` and your `comment_text`.

2. SECURITY SUGGESTED EDITS (Inline Fixes, if applicable):
   - Only when a specific insecure flag or role in `<DOCUMENT_TEXT>` has a direct drop-in secure replacement, provide the exact `target_text` and `replacement_text`.

Do NOT flag general spelling typos or non-security prose style—other specialists handle those.
Output your findings clearly with exact `target_text` quotes from the document.
"""

security_reviewer = Agent(
    name="security_reviewer",
    model="gemini-3.8-flash",
    description="Audits the document for security, IAM, authentication, and operational resilience risks.",
    instruction=SECURITY_REVIEWER_INSTRUCTION,
    include_contents="none",
    output_key="security_findings",
)


FACT_CHECKER_INSTRUCTION = """
You are the 🔍 Technical Fact-Checker & Citation Researcher on a Virtual Review Board.
Review the following Google Document ("{doc_title?}") strictly for factual accuracy, cloud quotas, CLI flags, release dates, and industry statistics:

<DOCUMENT_TEXT>
{doc_text?}
</DOCUMENT_TEXT>

Execute these steps:
1. VERIFY CLAIMS WITH TOOLS:
   - For Google Cloud (Cloud Run, GKE, BigQuery, Vertex AI, IAM), Gemini, ADK, Firebase, Android, Chrome, Flutter, or Go claims (quotas, memory/timeout limits, CLI flags):
     Use the Developer Knowledge MCP tools (`answer_query` or `search_documents`) to verify against official Google documentation.
   - For general public facts, language release dates (e.g., Python versions), or industry statistics:
     Call `web_search_verifier` to verify using Google Search.

2. OUTPUT STRUCTURED FINDINGS:
   - FACT-CHECK COMMENTS: For every claim you verified or corrected, provide the exact verbatim `target_text` from `<DOCUMENT_TEXT>`, your explanation, and the authoritative source URL.
   - FACT-CHECK SUGGESTED EDITS: For every outdated number, wrong quota, wrong CLI flag, or wrong date, provide the exact verbatim `target_text` from `<DOCUMENT_TEXT>` (include 1-2 surrounding words for uniqueness) and the accurate `replacement_text`.

Do NOT flag spelling or style issues. Focus 100% on factual verification and citations.
"""

fact_checker_reviewer = Agent(
    name="fact_checker_reviewer",
    model="gemini-3.8-flash",
    description="Verifies technical claims, quotas, CLI commands, and dates using Developer Knowledge MCP and Google Search.",
    instruction=FACT_CHECKER_INSTRUCTION,
    include_contents="none",
    tools=[
        developer_knowledge_mcp,
        AgentTool(web_search_verifier),
    ],
    output_key="fact_check_findings",
)


COPY_EDITOR_INSTRUCTION = """
You are the ✍️ Executive Copy & Style Editor on a Virtual Review Board.
Review the following Google Document ("{doc_title?}") strictly for spelling, grammar, executive clarity, and action-item completeness:

<DOCUMENT_TEXT>
{doc_text?}
</DOCUMENT_TEXT>

Identify:
1. COPY-EDITING SUGGESTED EDITS (Direct Inline Replacements):
   - Spelling mistakes and typos.
   - Grammatical errors (subject-verb disagreement, punctuation, tense errors).
   - Wordy or redundant phrases that can be tightened without changing technical meaning.
   - Provide the exact verbatim `target_text` from `<DOCUMENT_TEXT>` (with 1-2 surrounding words if short) and the `replacement_text`.

2. EDITORIAL COMMENTS (Margin Notes):
   - Vague timelines ("next month", "within the first week") or unquantified/exaggerated business claims ("reduce bill by 95%") that lack owners, dates, or baseline metrics.
   - Provide the exact verbatim `target_text` from `<DOCUMENT_TEXT>` and constructive `comment_text`.

Do NOT invent or change technical quotas or CLI flags—the Technical Fact-Checker handles those.
"""

copy_editor_reviewer = Agent(
    name="copy_editor_reviewer",
    model="gemini-3.8-flash",
    description="Reviews the document for spelling, grammar, executive clarity, and vague timelines.",
    instruction=COPY_EDITOR_INSTRUCTION,
    include_contents="none",
    output_key="editorial_findings",
)


specialist_review_board = ParallelAgent(
    name="specialist_review_board",
    description="Runs the Security Reviewer, Technical Fact-Checker, and Executive Copy Editor concurrently in parallel.",
    sub_agents=[
        security_reviewer,
        fact_checker_reviewer,
        copy_editor_reviewer,
    ],
)


# ---------------------------------------------------------------------------
# Stage 3: Chief Editor / Synthesizer Agent (Fan-In & Atomic Docs API Update)
# ---------------------------------------------------------------------------
CHIEF_EDITOR_INSTRUCTION = """
You are the 🏛️ Chief Editor of the Multi-Persona Virtual Review Board.
Three specialist reviewers have just analyzed Google Document ID `{document_id?}` ("{doc_title?}") in parallel.

Here is the original document text for reference:
<DOCUMENT_TEXT>
{doc_text?}
</DOCUMENT_TEXT>

Here are the specialist findings from `session.state`:

=== 1. 🔒 SECURITY & ARCHITECTURE FINDINGS ===
{security_findings?}

=== 2. 🔍 TECHNICAL FACT-CHECKER FINDINGS ===
{fact_check_findings?}

=== 3. ✍️ EXECUTIVE COPY EDITOR FINDINGS ===
{editorial_findings?}

Execute these steps in exact order:

1. SYNTHESIZE & ATTRIBUTE MARGIN COMMENTS (`insert_comments` FIRST):
   - Combine the best, non-duplicate comments from all three specialists.
   - Prefix EVERY `comment_text` with the specialist persona badge that produced it:
     * `[🔒 Security Review] ...`
     * `[🔍 Fact-Check] ...` (always preserve the verified source URLs!)
     * `[✍️ Copy Editor] ...`
   - Ensure every `target_text` is an exact verbatim substring of `<DOCUMENT_TEXT>`.
   - Call `insert_comments` FIRST with `document_id="{document_id?}"` and the combined `comments` list.

2. DEDUPLICATE & APPLY SUGGESTED EDITS (`propose_suggested_edits` SECOND):
   - Combine all inline text replacements (`target_text` -> `replacement_text`) from the Fact-Checker, Security Reviewer, and Copy Editor.
   - Resolve any overlaps: if two specialists proposed edits to the same phrase, prioritize the Fact-Checker / Security fix and merge any typo fix cleanly so `target_text` appears only once.
   - Ensure every `target_text` is an exact verbatim substring of `<DOCUMENT_TEXT>` (including 1-2 surrounding words for short words/numbers).
   - Call `propose_suggested_edits` SECOND with `document_id="{document_id?}"` and the deduplicated `edits` list.

3. EXECUTIVE REVIEW BOARD REPORT:
   - Present a well-structured summary grouped by persona:
     * **🔒 Security & Architecture Reviewer**
     * **🔍 Technical Fact-Checker & Citations**
     * **✍️ Executive Copy & Style Editor**
   - Include the total counts of comments and suggested edits applied, and finish with the link:
     https://docs.google.com/document/d/{document_id?}/edit
"""

chief_editor_synthesizer = Agent(
    name="chief_editor_synthesizer",
    model="gemini-3.8-flash",
    description="Synthesizes parallel reviewer findings, deduplicates overlapping edits, and applies persona-badged comments and suggested edits to the Google Doc.",
    instruction=CHIEF_EDITOR_INSTRUCTION,
    include_contents="none",
    tools=[
        insert_comments,
        propose_suggested_edits,
    ],
)


# ---------------------------------------------------------------------------
# Root Workflow Pipeline: SequentialAgent(Stage 1 -> Stage 2 Parallel -> Stage 3)
# ---------------------------------------------------------------------------
review_board_agent = SequentialAgent(
    name="virtual_review_board",
    description=(
        "Multi-Persona Virtual Review Board workflow: loads the Google Doc once, "
        "runs Security, Fact-Checker, and Copy Editor specialists in parallel, "
        "and synthesizes persona-badged Comments and Suggested Edits."
    ),
    sub_agents=[
        doc_ingestion_agent,
        specialist_review_board,
        chief_editor_synthesizer,
    ],
    before_agent_callback=ensure_review_board_state,
)

review_board_app = App(root_agent=review_board_agent, name="review_board_app")
