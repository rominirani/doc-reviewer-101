# app/agent.py
import os
from typing import Optional

import google.auth
import google.auth.transport.requests
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.tools import AgentTool, google_search
from google.adk.tools.mcp_tool.mcp_toolset import (
    McpToolset,
    StreamableHTTPConnectionParams,
)

from app.tools import insert_comments, propose_suggested_edits, read_google_doc

load_dotenv()

DEFAULT_SUGGESTED_EDITS_CRITERIA = (
    "- Outdated or inaccurate technical numbers, quotas, CLI flags, or dates verified via Developer Knowledge or Google Search.\n"
    "- Spelling mistakes and typos.\n"
    "- Grammatical errors (such as subject-verb disagreement, tense errors, or punctuation).\n"
    "- Awkward or redundant word choices."
)

DEFAULT_COMMENTS_CRITERIA = (
    "- Factual claims, quotas, CLI commands, or statistics that were verified or corrected (attach the authoritative source URL and brief explanation in the comment).\n"
    "- Exaggerated or unverified claims that lack citations or benchmark data.\n"
    "- Unrealistic timelines or vague action items that need clear owners or dates."
)


def ensure_review_criteria_defaults(callback_context: CallbackContext):
    """Populates default review templates in session state if the user didn't override them."""
    if not callback_context.state.get("suggested_edits_criteria"):
        callback_context.state["suggested_edits_criteria"] = (
            DEFAULT_SUGGESTED_EDITS_CRITERIA
        )
    if not callback_context.state.get("comments_criteria"):
        callback_context.state["comments_criteria"] = DEFAULT_COMMENTS_CRITERIA
    return None


# ---------------------------------------------------------------------------
# 1. Google Developer Knowledge Remote MCP Server (Official Google Docs)
# ---------------------------------------------------------------------------
def _developer_knowledge_headers(
    readonly_context: Optional[ReadonlyContext] = None,
) -> dict[str, str]:
    """Builds auth headers for https://developerknowledge.googleapis.com/mcp."""
    api_key = os.getenv("DEVELOPERKNOWLEDGE_API_KEY", "").strip()
    if api_key:
        return {"X-Goog-Api-Key": api_key}

    creds, project_id = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())

    quota_project = os.getenv("GOOGLE_CLOUD_PROJECT") or project_id or ""
    headers = {"Authorization": f"Bearer {creds.token}"}
    if quota_project:
        headers["X-Goog-User-Project"] = quota_project
    return headers


developer_knowledge_mcp = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://developerknowledge.googleapis.com/mcp",
        headers=_developer_knowledge_headers(),
        timeout=30.0,
        sse_read_timeout=30.0,
    ),
    header_provider=_developer_knowledge_headers,
)


# ---------------------------------------------------------------------------
# 2. Google Search Grounding Sub-Agent (Wrapped as an AgentTool)
# ---------------------------------------------------------------------------
web_search_verifier = Agent(
    name="web_search_verifier",
    model="gemini-3.8-flash",
    description=(
        "Verifies general public facts, industry statistics, release dates, "
        "and non-Google technical claims using Google Search Grounding."
    ),
    instruction=(
        "You are a precise fact-checking researcher. Given a factual claim from a document:\n"
        "1. Use `google_search` to verify whether the claim is accurate and up to date.\n"
        "2. Return the verdict (Accurate, Outdated/Incorrect, or Unverifiable), "
        "the exact corrected value/fact if inaccurate, and the authoritative source URL."
    ),
    tools=[google_search],
)


# ---------------------------------------------------------------------------
# 3. Root Document Reviewer & Fact-Checker Agent
# ---------------------------------------------------------------------------
INSTRUCTION = """
You are an AI Document Reviewer and Fact-Checker for Google Docs.
When given a Google Document ID, execute these steps in exact order:

1. READ:
   Call `read_google_doc` to retrieve the document's text.

2. FACT-CHECK & VERIFY CLAIMS:
   Identify verifiable technical claims, quotas, CLI commands, release dates, or statistics in the document:
   - For Google Cloud (Cloud Run, GKE, BigQuery, Vertex AI, IAM), Gemini, ADK, Firebase, Android, Chrome, Flutter, or Go claims:
     Use the Developer Knowledge MCP tools (`answer_query` or `search_documents`) to verify against official Google documentation.
   - For general world facts, industry statistics, release dates, or non-Google claims:
     Call `web_search_verifier` to verify using Google Search.

3. COMMENT FIRST (Based on User's Comment Criteria & Fact-Check Citations):
   Review the document against these Comment Criteria:
   {comments_criteria}

   Call `insert_comments` FIRST with the exact `target_text` from the document and helpful `comment_text`.
   Whenever you verified or corrected a factual claim in Step 2, include the authoritative source URL and explanation in `comment_text`.

4. SUGGEST EDITS SECOND (Based on User's Suggested Edits Criteria & Fact Corrections):
   Review the document against these Suggested Edits Criteria:
   {suggested_edits_criteria}

   Call `propose_suggested_edits` SECOND with each exact `target_text` and its `replacement_text` (including factual corrections found in Step 2 as well as grammar/typo fixes).
   Include 1-2 surrounding words in `target_text` if a typo or number is short to ensure unique matching.

5. SUMMARIZE:
   Return a clear summary of all fact-checks, citations, comments, and suggested edits applied, and provide the link:
   https://docs.google.com/document/d/<DOCUMENT_ID>/edit
"""

root_agent = Agent(
    name="doc_reviewer_101",
    model="gemini-3.8-flash",
    description="Reviews and fact-checks a Google Doc using Developer Knowledge MCP and Google Search Grounding, then adds comments and suggested edits.",
    instruction=INSTRUCTION,
    tools=[
        read_google_doc,
        developer_knowledge_mcp,
        AgentTool(web_search_verifier),
        insert_comments,
        propose_suggested_edits,
    ],
    before_agent_callback=ensure_review_criteria_defaults,
)

app = App(root_agent=root_agent, name="app")