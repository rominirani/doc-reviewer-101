# app/addons/ai_comments.py
"""Part 3 Add-On: Comments as Prompts -> Suggested Edits as Output.

This module can be used standalone via `ai_comment_agent` (POST /api/ai-comments)
or bolted onto any existing ADK Agent using `attach_ai_comment_tools(agent)`.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.tools import AgentTool, ToolContext

from app.agent import developer_knowledge_mcp, web_search_verifier
from app.tools import (
    _extract_text_and_index_map,
    _find_doc_range,
    _get_docs_service,
)

AI_TRIGGER_PATTERN = re.compile(r"(?:^|\s)[@/][aA][iI]\b")
AI_REPLY_PREFIX = "🤖"


def _extract_text_for_range(
    full_text: str, doc_indices: List[int], start_index: int, end_index: int
) -> str:
    """Returns the exact current document text spanning [start_index, end_index)."""
    chars = [
        ch
        for ch, idx in zip(full_text, doc_indices)
        if start_index <= idx < end_index
    ]
    return "".join(chars)


def _trim_trailing_newlines(
    target_text: str, start_index: int, end_index: int
) -> Tuple[str, int, int]:
    """Trims trailing newline characters so deleteContentRange never deletes a paragraph break."""
    while target_text.endswith("\n") and end_index > start_index:
        target_text = target_text[:-1]
        end_index -= 1
    return target_text, start_index, end_index


def list_open_ai_comments(
    document_id: str, tool_context: ToolContext
) -> Dict[str, Any]:
    """Scans a Google Doc for open comment threads containing an unanswered '@ai' or '/ai' prompt.

    Resolves each comment's `anchorId` against `documentTab.commentAnchors` (with automatic
    fallback to `plainTextQuote`) to return the exact `[startIndex, endIndex)` character
    coordinates and highlighted text in the document.

    Args:
        document_id: The Google Document ID.

    Returns:
        dict with 'status', 'title', 'open_ai_comments_count', 'ai_comments', and 'document_context'.
    """
    try:
        service = _get_docs_service(tool_context)
        doc = (
            service.documents()
            .get(
                documentId=document_id,
                suggestionsViewMode="SUGGESTIONS_INLINE",
                commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
                includeTabsContent=True,
            )
            .execute()
        )

        full_text, doc_indices = _extract_text_and_index_map(doc)
        tabs = doc.get("tabs", [])
        comment_anchors: Dict[str, Any] = (
            tabs[0].get("documentTab", {}).get("commentAnchors", {}) if tabs else {}
        )

        comments_raw = doc.get("comments", [])
        threads = (
            list(comments_raw.values())
            if isinstance(comments_raw, dict)
            else list(comments_raw)
        )

        ai_comments: List[Dict[str, Any]] = []
        for thread in threads:
            if thread.get("status") == "RESOLVED":
                continue

            comment_id = thread.get("commentId", "")
            anchor_id = thread.get("anchorId", "")
            plain_quote = thread.get("plainTextQuote", "")
            head_post = thread.get("headPost", {})
            replies = thread.get("replies", [])

            active_posts = []
            if head_post and not head_post.get("deleted"):
                active_posts.append(head_post)
            for reply in replies:
                if reply and not reply.get("deleted"):
                    active_posts.append(reply)

            if not active_posts:
                continue

            # Idempotency check: if the latest post in the thread is already from our AI agent, skip it
            last_post_content = (active_posts[-1].get("content") or "").strip()
            if last_post_content.startswith(AI_REPLY_PREFIX):
                continue

            # Find the latest post in the thread that triggers @ai or /ai
            latest_ai_prompt: Optional[str] = None
            prompt_author: str = "Reviewer"
            for post in reversed(active_posts):
                content = (post.get("content") or "").strip()
                if AI_TRIGGER_PATTERN.search(content):
                    latest_ai_prompt = content
                    prompt_author = (
                        post.get("author", {}).get("displayName") or "User"
                    )
                    break

            if not latest_ai_prompt:
                continue

            # Resolve exact [startIndex, endIndex) from commentAnchors first, then plainTextQuote fallback
            start_idx: Optional[int] = None
            end_idx: Optional[int] = None
            target_text = ""

            anchor_obj = comment_anchors.get(anchor_id, {})
            anchor_ranges = anchor_obj.get("ranges", [])
            if anchor_ranges:
                r0 = anchor_ranges[0]
                start_idx = r0.get("startIndex")
                end_idx = r0.get("endIndex")
                if start_idx is not None and end_idx is not None:
                    target_text = _extract_text_for_range(
                        full_text, doc_indices, start_idx, end_idx
                    )

            if (not target_text or start_idx is None or end_idx is None) and plain_quote:
                rng = _find_doc_range(full_text, doc_indices, plain_quote)
                if rng:
                    start_idx, end_idx = rng
                    target_text = plain_quote

            if start_idx is not None and end_idx is not None and target_text:
                target_text, start_idx, end_idx = _trim_trailing_newlines(
                    target_text, start_idx, end_idx
                )

            thread_history = [
                f"{p.get('author', {}).get('displayName', 'User')}: {(p.get('content') or '').strip()}"
                for p in active_posts
            ]

            ai_comments.append(
                {
                    "comment_id": comment_id,
                    "anchor_id": anchor_id,
                    "author": prompt_author,
                    "ai_instruction": latest_ai_prompt,
                    "target_text": target_text or plain_quote,
                    "start_index": start_idx,
                    "end_index": end_idx,
                    "thread_history": thread_history,
                }
            )

        return {
            "status": "success",
            "title": doc.get("title", "Untitled"),
            "open_ai_comments_count": len(ai_comments),
            "ai_comments": ai_comments,
            "document_context": full_text[:6000],
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}


def respond_to_ai_comments(
    document_id: str,
    resolutions: List[Dict[str, Any]],
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Applies inline Suggested Edits for '@ai' comments and replies directly in each comment thread.

    Args:
        document_id: The Google Document ID.
        resolutions: A list of dicts, each containing:
            - 'comment_id': The unique ID of the CommentThread to reply to.
            - 'target_text': The exact anchored text in the document to replace (leave empty if only answering a question).
            - 'replacement_text': The proposed replacement text to submit as a Suggested Edit (leave empty if no inline edit is needed).
            - 'reply_text': The explanation, answer, or citation to post as a reply inside the comment thread.
            - 'start_index': Optional exact integer startIndex from `list_open_ai_comments`.
            - 'end_index': Optional exact integer endIndex from `list_open_ai_comments`.

    Returns:
        dict with counts of applied suggested edits and comment thread replies.
    """
    try:
        service = _get_docs_service(tool_context)
        doc = (
            service.documents()
            .get(
                documentId=document_id,
                suggestionsViewMode="SUGGESTIONS_INLINE",
                includeTabsContent=True,
            )
            .execute()
        )
        full_text, doc_indices = _extract_text_and_index_map(doc)

        resolved_edits = []
        reply_requests = []
        skipped_edits = []

        for item in resolutions:
            comment_id = (item.get("comment_id") or "").strip()
            target = item.get("target_text") or ""
            replacement = item.get("replacement_text") or ""
            reply_body = (item.get("reply_text") or "").strip()
            start_idx = item.get("start_index")
            end_idx = item.get("end_index")

            # 1. Queue the comment thread reply (prefixed with 🤖 for idempotency)
            if comment_id and reply_body:
                formatted_reply = (
                    reply_body
                    if reply_body.startswith(AI_REPLY_PREFIX)
                    else f"{AI_REPLY_PREFIX} {reply_body}"
                )
                reply_requests.append(
                    {
                        "addCommentReply": {
                            "commentId": comment_id,
                            "post": {"content": formatted_reply[:2000]},
                        }
                    }
                )

            # 2. Queue the inline Suggested Edit if replacement_text is provided
            if replacement and replacement != target:
                rng: Optional[Tuple[int, int]] = None
                if isinstance(start_idx, int) and isinstance(end_idx, int) and end_idx > start_idx:
                    live_slice = _extract_text_for_range(
                        full_text, doc_indices, start_idx, end_idx
                    )
                    live_slice, s_trim, e_trim = _trim_trailing_newlines(
                        live_slice, start_idx, end_idx
                    )
                    if live_slice and e_trim > s_trim:
                        rng = (s_trim, e_trim)

                if not rng and target:
                    clean_target = target.rstrip("\n")
                    rng = _find_doc_range(full_text, doc_indices, clean_target)

                if rng:
                    resolved_edits.append(
                        {
                            "startIndex": rng[0],
                            "endIndex": rng[1],
                            "replacement": replacement.rstrip("\n"),
                            "target": target,
                            "comment_id": comment_id,
                        }
                    )
                else:
                    skipped_edits.append(target)

        applied_suggestions = 0
        applied_replies = 0

        # Execute Suggested Edits first (sorted descending by startIndex so character offsets never shift)
        if resolved_edits:
            resolved_edits.sort(key=lambda x: x["startIndex"], reverse=True)
            non_overlapping = []
            last_start = float("inf")
            for edit_item in resolved_edits:
                if edit_item["endIndex"] <= last_start:
                    non_overlapping.append(edit_item)
                    last_start = edit_item["startIndex"]
                else:
                    skipped_edits.append(edit_item["target"])

            edit_requests = []
            for edit_item in non_overlapping:
                edit_requests.append(
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": edit_item["startIndex"],
                                "endIndex": edit_item["endIndex"],
                            }
                        }
                    }
                )
                edit_requests.append(
                    {
                        "insertText": {
                            "location": {"index": edit_item["startIndex"]},
                            "text": edit_item["replacement"],
                        }
                    }
                )

            if edit_requests:
                service.documents().batchUpdate(
                    documentId=document_id,
                    body={
                        "requests": edit_requests,
                        "writeControl": {"writeMode": "SUGGEST"},
                    },
                ).execute()
                applied_suggestions = len(non_overlapping)

        # Execute Comment Thread Replies via native Docs API v1 AddCommentReplyRequest
        if reply_requests:
            service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": reply_requests},
            ).execute()
            applied_replies = len(reply_requests)

        return {
            "status": "success",
            "applied_suggestions_count": applied_suggestions,
            "applied_comment_replies_count": applied_replies,
            "skipped_edits": skipped_edits,
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}


def attach_ai_comment_tools(agent: Agent) -> Agent:
    """One-line helper to bolt Part 3's @ai comment tools onto an existing ADK Agent."""
    existing_names = {getattr(t, "__name__", repr(t)) for t in agent.tools}
    if "list_open_ai_comments" not in existing_names:
        agent.tools.append(list_open_ai_comments)
    if "respond_to_ai_comments" not in existing_names:
        agent.tools.append(respond_to_ai_comments)
    return agent


AI_COMMENT_AGENT_INSTRUCTION = """
You are an Interactive In-Document AI Assistant for Google Docs.
Authors highlight specific sentences, paragraphs, or code blocks in their Google Doc and leave comments containing `@ai` or `/ai` (for example: `@ai make this 50% shorter`, `@ai convert this curl command to Python`, or `@ai verify if this Cloud Run quota is accurate`).

When given a Google Document ID, execute these steps in exact order:

1. DISCOVER OPEN `@ai` COMMENTS:
   Call `list_open_ai_comments` to retrieve all open, unanswered `@ai` / `/ai` comment threads along with their anchored `target_text`, `start_index`, `end_index`, and surrounding `document_context`.
   - If `open_ai_comments_count` is 0, inform the user that no unanswered `@ai` comments were found and explain how to add one in Google Docs.

2. GROUND & FACT-CHECK (When Needed):
   If any `@ai` comment asks to verify a fact, check a quota/CLI command, or add a citation:
   - Use the Developer Knowledge MCP tools (`answer_query` or `search_documents`) for Google Cloud, Gemini, ADK, Firebase, Android, Chrome, Flutter, or Go topics.
   - Call `web_search_verifier` for general public facts, industry stats, or non-Google claims.

3. APPLY SUGGESTED EDITS & REPLY IN THREADS:
   Call `respond_to_ai_comments` once with a `resolutions` list covering every open `@ai` comment:
   - Pass the exact `comment_id`, `target_text`, `start_index`, and `end_index` from Step 1.
   - If the `@ai` comment asks to rewrite, shorten, translate, format, or fix the highlighted text, provide the new text in `replacement_text` (so it appears as an inline Suggested Edit in Google Docs) and explain what you changed in `reply_text`.
   - If the `@ai` comment is purely a question and the highlighted text does not need to be modified, set `replacement_text` to `""` and provide the complete answer (with any source URLs) in `reply_text`.

4. SUMMARIZE:
   Return a clear, bulleted summary of each `@ai` comment you processed, the Suggested Edit proposed, and the reply posted, followed by the link:
   https://docs.google.com/document/d/<DOCUMENT_ID>/edit
"""

ai_comment_agent = Agent(
    name="doc_ai_comment_assistant",
    model="gemini-3.8-flash",
    description=(
        "Processes open @ai comments in a Google Doc, proposes inline Suggested Edits "
        "on the anchored text ranges, and replies directly in each comment thread."
    ),
    instruction=AI_COMMENT_AGENT_INSTRUCTION,
    tools=[
        list_open_ai_comments,
        developer_knowledge_mcp,
        AgentTool(web_search_verifier),
        respond_to_ai_comments,
    ],
)

ai_comment_app = App(root_agent=ai_comment_agent, name="ai_comment_app")
