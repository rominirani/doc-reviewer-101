# app/agent.py
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App

from app.tools import insert_comments, propose_suggested_edits, read_google_doc

DEFAULT_SUGGESTED_EDITS_CRITERIA = (
    "- Spelling mistakes and typos.\n"
    "- Grammatical errors (such as subject-verb disagreement, tense errors, or punctuation).\n"
    "- Awkward or redundant word choices."
)

DEFAULT_COMMENTS_CRITERIA = (
    "- Exaggerated or unverified claims that lack citations or benchmark data.\n"
    "- Unrealistic timelines or vague action items that need clear owners or dates.\n"
    "- Sections that are ambiguous and require clarification from the author."
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


INSTRUCTION = """
You are an AI Document Reviewer for Google Docs.
When given a Google Document ID, execute these steps in exact order:

1. READ:
   Call `read_google_doc` to retrieve the document's text.

2. COMMENT FIRST (Based on User's Comment Criteria):
   Review the document against these specific Comment Criteria:
   {comments_criteria}

   If any phrases or sentences match the Comment Criteria above, call `insert_comments` first with the exact `target_text` from the document and helpful `comment_text`.

3. SUGGEST EDITS SECOND (Based on User's Suggested Edits Criteria):
   Review the document against these specific Suggested Edits Criteria:
   {suggested_edits_criteria}

   If any words or phrases need correction based on the Suggested Edits Criteria above, call `propose_suggested_edits` with each exact `target_text` and its `replacement_text`.
   Include 1-2 surrounding words in `target_text` if a typo is a short word (like "teh") to ensure unique matching.

4. SUMMARIZE:
   Return a clear summary of all comments and suggested edits applied, and provide the link:
   https://docs.google.com/document/d/<DOCUMENT_ID>/edit
"""

root_agent = Agent(
    name="doc_reviewer_101",
    model="gemini-3.8-flash",
    description="Reviews a Google Doc based on customizable criteria and adds comments and suggested edits.",
    instruction=INSTRUCTION,
    tools=[read_google_doc, insert_comments, propose_suggested_edits],
    before_agent_callback=ensure_review_criteria_defaults,
)

app = App(root_agent=root_agent, name="app")