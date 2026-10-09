# app/tools.py
from typing import Any, Dict, List, Tuple
import google.auth
from google.adk.tools import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_docs_service(tool_context: ToolContext):
    """Builds the Google Docs API client using the logged-in user's OAuth token (or ADC fallback)."""
    user_token = tool_context.state.get("user_access_token")
    if user_token:
        creds = Credentials(token=user_token)
        return build("docs", "v1", credentials=creds)
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("docs", "v1", credentials=creds)


def _extract_text_and_index_map(doc: Dict[str, Any]) -> Tuple[str, List[int]]:
    """Extracts plain text and maps each character to its exact 1-based Docs API index."""
    tabs = doc.get("tabs", [])
    if tabs:
        body = tabs[0].get("documentTab", {}).get("body", {})
    else:
        body = doc.get("body", {})

    chars: List[str] = []
    doc_indices: List[int] = []

    def _walk_elements(content_list: List[Dict[str, Any]]):
        for element in content_list:
            if "paragraph" in element:
                for run in element["paragraph"].get("elements", []):
                    text_run = run.get("textRun")
                    if text_run and "content" in text_run:
                        text = text_run["content"]
                        start_idx = run.get("startIndex", 1)
                        for offset, ch in enumerate(text):
                            chars.append(ch)
                            doc_indices.append(start_idx + offset)
            elif "table" in element:
                for row in element["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        _walk_elements(cell.get("content", []))

    _walk_elements(body.get("content", []))
    return "".join(chars), doc_indices


def _find_doc_range(
    full_text: str, doc_indices: List[int], target_text: str
) -> Tuple[int, int] | None:
    """Finds `target_text` and returns its exact (startIndex, endIndex) in the Doc."""
    pos = full_text.find(target_text)
    if pos == -1 or not target_text:
        return None
    start_index = doc_indices[pos]
    end_index = doc_indices[pos + len(target_text) - 1] + 1
    return start_index, end_index


def read_google_doc(document_id: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Reads a Google Document and returns its title and text content.

    Args:
        document_id: The Google Document ID.

    Returns:
        dict with 'status', 'title', and 'full_text'.
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
        full_text, _ = _extract_text_and_index_map(doc)
        return {
            "status": "success",
            "title": doc.get("title", "Untitled"),
            "full_text": full_text,
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}


def insert_comments(
    document_id: str, comments: List[Dict[str, str]], tool_context: ToolContext
) -> Dict[str, Any]:
    """Attaches editorial comments to specific phrases in a Google Doc.

    Args:
        document_id: The Google Document ID.
        comments: A list of dicts, each with:
            - 'target_text': Exact phrase or sentence in the document to highlight.
            - 'comment_text': The feedback or question to attach as a comment.

    Returns:
        dict with status and number of comments inserted.
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

        requests = []
        skipped = []
        for item in comments:
            target = item.get("target_text", "")
            comment_body = item.get("comment_text", "")
            rng = _find_doc_range(full_text, doc_indices, target)
            if rng:
                requests.append(
                    {
                        "insertComment": {
                            "content": comment_body,
                            "range": {"startIndex": rng[0], "endIndex": rng[1]},
                        }
                    }
                )
            else:
                skipped.append(target)

        if not requests:
            return {
                "status": "warning",
                "message": "No matching text ranges found for comments.",
                "skipped_targets": skipped,
            }

        response = (
            service.documents()
            .batchUpdate(documentId=document_id, body={"requests": requests})
            .execute()
        )

        return {
            "status": "success",
            "inserted_comments_count": len(requests),
            "skipped_targets": skipped,
            "comment_update_state": response.get(
                "commentUpdateState", "ALL_UPDATES_APPLIED"
            ),
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}


def propose_suggested_edits(
    document_id: str, edits: List[Dict[str, str]], tool_context: ToolContext
) -> Dict[str, Any]:
    """Submits spelling, grammar, or style fixes as Suggested Edits in Google Docs.

    Args:
        document_id: The Google Document ID.
        edits: A list of dicts, each with:
            - 'target_text': Exact word or short phrase in the document to replace.
            - 'replacement_text': The corrected word or phrase.

    Returns:
        dict with status and number of suggestions applied.
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
        skipped = []
        for edit in edits:
            target = edit.get("target_text", "")
            replacement = edit.get("replacement_text", "")
            if not target or target == replacement:
                continue
            rng = _find_doc_range(full_text, doc_indices, target)
            if rng:
                resolved_edits.append(
                    {
                        "startIndex": rng[0],
                        "endIndex": rng[1],
                        "replacement": replacement,
                        "target": target,
                    }
                )
            else:
                skipped.append(target)

        if not resolved_edits:
            return {
                "status": "warning",
                "message": "No matching target text found for edits.",
                "skipped_targets": skipped,
            }

        # Sort descending by startIndex and filter out overlapping ranges
        resolved_edits.sort(key=lambda x: x["startIndex"], reverse=True)
        non_overlapping = []
        last_start = float("inf")
        for item in resolved_edits:
            if item["endIndex"] <= last_start:
                non_overlapping.append(item)
                last_start = item["startIndex"]
            else:
                skipped.append(item["target"])

        requests = []
        for item in non_overlapping:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": item["startIndex"],
                            "endIndex": item["endIndex"],
                        }
                    }
                }
            )
            requests.append(
                {
                    "insertText": {
                        "location": {"index": item["startIndex"]},
                        "text": item["replacement"],
                    }
                }
            )

        response = (
            service.documents()
            .batchUpdate(
                documentId=document_id,
                body={
                    "requests": requests,
                    "writeControl": {"writeMode": "SUGGEST"},
                },
            )
            .execute()
        )

        return {
            "status": "success",
            "applied_suggestions_count": len(non_overlapping),
            "skipped_targets": skipped,
            "comment_update_state": response.get(
                "commentUpdateState", "ALL_UPDATES_APPLIED"
            ),
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}