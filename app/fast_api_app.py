# app/fast_api_app.py
import contextlib
import os
import re
import secrets
import urllib.parse
import uuid
from collections.abc import AsyncIterator

import aiohttp
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.genai import types as genai_types
from pydantic import BaseModel

from app.agent import (
    DEFAULT_COMMENTS_CRITERIA,
    DEFAULT_SUGGESTED_EDITS_CRITERIA,
)
from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes

load_dotenv()
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = True

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Server-side session store mapping random session IDs to user OAuth access tokens
SESSION_TOKENS: dict[str, str] = {}
OAUTH_STATES: set[str] = set()

DEFAULT_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]

DOC_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{10,100}$")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=False,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "doc-reviewer-101"
app.description = "API for interacting with the Agent doc-reviewer-101"


def _get_user_token(request: Request) -> str | None:
    session_id = request.cookies.get("reviewer_session_id")
    if not session_id:
        return None
    return SESSION_TOKENS.get(session_id)


def _get_redirect_uri(request: Request) -> str:
    """Returns OAUTH_REDIRECT_URI if explicitly configured, or auto-detects from request headers."""
    env_uri = os.getenv("OAUTH_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}/auth/callback"


def _get_oauth_scopes() -> list[str]:
    custom_scopes = os.getenv("OAUTH_SCOPES", "").strip()
    if custom_scopes:
        return [s.strip() for s in custom_scopes.split(",") if s.strip()]
    return DEFAULT_OAUTH_SCOPES


@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/reviewer")


@app.get("/auth/login")
async def auth_login(request: Request):
    client_id = os.getenv("OAUTH_CLIENT_ID")
    redirect_uri = _get_redirect_uri(request)
    if not client_id:
        raise HTTPException(
            status_code=500, detail="OAUTH_CLIENT_ID is not configured."
        )

    state = secrets.token_urlsafe(32)
    OAUTH_STATES.add(state)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_get_oauth_scopes()),
        "access_type": "online",
        "prompt": "consent",
        "state": state,
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        params
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not state or state not in OAUTH_STATES:
        raise HTTPException(status_code=400, detail="Invalid OAuth state parameter.")
    OAUTH_STATES.discard(state)

    client_id = os.getenv("OAUTH_CLIENT_ID")
    client_secret = os.getenv("OAUTH_CLIENT_SECRET")
    redirect_uri = _get_redirect_uri(request)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        ) as resp:
            token_data = await resp.json()

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Failed to obtain access token.")

    session_id = secrets.token_urlsafe(32)
    SESSION_TOKENS[session_id] = access_token

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    is_https = proto == "https"

    response = RedirectResponse(url="/reviewer")
    response.set_cookie(
        key="reviewer_session_id",
        value=session_id,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=3600,
    )
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get("reviewer_session_id")
    if session_id:
        SESSION_TOKENS.pop(session_id, None)
    response = RedirectResponse(url="/reviewer")
    response.delete_cookie("reviewer_session_id")
    return response


@app.get("/api/defaults")
async def get_default_templates():
    return {
        "suggested_edits_criteria": DEFAULT_SUGGESTED_EDITS_CRITERIA,
        "comments_criteria": DEFAULT_COMMENTS_CRITERIA,
    }


@app.get("/api/picker-config")
async def get_picker_config(request: Request):
    """Returns authentication state and Google Picker configuration for the signed-in user."""
    token = _get_user_token(request)
    if not token:
        return JSONResponse({"authenticated": False}, status_code=401)

    return {
        "authenticated": True,
        "oauth_token": token,
        "developer_key": os.getenv("GOOGLE_PICKER_API_KEY", ""),
        "app_id": os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER", ""),
    }


class ReviewRequest(BaseModel):
    document_id: str
    suggested_edits_criteria: str = DEFAULT_SUGGESTED_EDITS_CRITERIA
    comments_criteria: str = DEFAULT_COMMENTS_CRITERIA


@app.post("/api/review")
async def review_document(body: ReviewRequest, request: Request):
    token = _get_user_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in with Google first.")

    doc_id = body.document_id.strip()
    if not DOC_ID_PATTERN.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid Google Document ID.")

    edits_criteria = (
        body.suggested_edits_criteria.strip() or DEFAULT_SUGGESTED_EDITS_CRITERIA
    )
    comments_criteria = body.comments_criteria.strip() or DEFAULT_COMMENTS_CRITERIA

    runner: Runner = request.app.state.runner
    app_name: str = request.app.state.agent_app_name
    user_id = "web-user"
    adk_session_id = str(uuid.uuid4())

    # Seed the ADK session state with the user's OAuth token AND their custom review criteria
    await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=adk_session_id,
        state={
            "user_access_token": token,
            "suggested_edits_criteria": edits_criteria[:2000],
            "comments_criteria": comments_criteria[:2000],
        },
    )

    prompt = f"Review Google Doc ID {doc_id}"
    final_text = ""

    async for event in runner.run_async(
        user_id=user_id,
        session_id=adk_session_id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part.from_text(text=prompt)]
        ),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)

    return {
        "status": "success",
        "document_id": doc_id,
        "document_url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "summary": final_text,
    }


REVIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Google Docs AI Reviewer</title>
  <script src="https://apis.google.com/js/api.js"></script>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; max-width: 820px; margin: 40px auto; padding: 0 20px; color: #1f2937; background: #f9fafb; }
    .card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }
    h1 { margin-top: 0; font-size: 1.5rem; }
    h2 { font-size: 1.15rem; margin-top: 0; }
    button, .btn { background: #2563eb; color: #fff; border: none; padding: 10px 18px; border-radius: 8px; font-size: 0.95rem; cursor: pointer; text-decoration: none; display: inline-block; }
    button:disabled { background: #93c5fd; cursor: not-allowed; }
    .btn-secondary { background: #e5e7eb; color: #374151; font-size: 0.85rem; padding: 8px 14px; }
    .btn-picker { background: #0f9d58; color: #fff; font-weight: 500; margin-top: 8px; margin-bottom: 12px; }
    input, textarea { width: 100%; padding: 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 0.95rem; margin-top: 6px; margin-bottom: 16px; box-sizing: border-box; font-family: inherit; }
    textarea { resize: vertical; min-height: 90px; line-height: 1.4; }
    pre { white-space: pre-wrap; background: #f3f4f6; padding: 16px; border-radius: 8px; line-height: 1.5; }
    .hidden { display: none; }
    .header-row { display: flex; justify-content: space-between; align-items: center; }
    .hint { color: #6b7280; font-size: 0.85rem; margin-top: -4px; margin-bottom: 8px; }
    .selected-doc-badge { background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; font-size: 0.95rem; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header-row">
      <h1>Google Docs AI Reviewer &amp; Fact-Checker</h1>
      <a id="logout-btn" href="/auth/logout" class="btn btn-secondary hidden">Sign Out</a>
    </div>
    <p>Automatically fact-check and review Google Docs using <strong>Google Developer Knowledge MCP</strong>, <strong>Google Search Grounding</strong>, and customizable criteria for Suggested Edits and Comments.</p>

    <div id="login-section">
      <a href="/auth/login" class="btn">Sign in with Google</a>
    </div>

    <div id="app-section" class="hidden">
      <div>
        <strong>1. Choose a Google Document:</strong><br/>
        <button id="picker-btn" type="button" class="btn-picker">Browse Google Drive</button>
      </div>

      <div id="selected-doc-banner" class="selected-doc-badge hidden"></div>

      <label for="doc-input"><strong>Selected Document ID (or paste any Doc ID directly):</strong></label>
      <input id="doc-input" type="text" placeholder="e.g. 1WvV8RJGqJ-h2VbmB41xvJuRM1ibU-MBkMsBulkSaAN" />

      <div class="header-row" style="margin-top: 8px;">
        <strong>2. Customize Review Templates</strong>
        <button id="reset-template-btn" type="button" class="btn-secondary">Reset to Default Templates</button>
      </div>

      <label for="edits-criteria" style="display:block; margin-top: 12px;">
        <strong>Suggested Edits Criteria (Direct Inline Replacements):</strong>
      </label>
      <p class="hint">What errors or style rules should the agent fix directly as Suggested Edits?</p>
      <textarea id="edits-criteria"></textarea>

      <label for="comments-criteria" style="display:block;">
        <strong>Editorial Comments Criteria (Margin Notes &amp; Questions):</strong>
      </label>
      <p class="hint">What topics, claims, or sections should the agent flag with a Comment?</p>
      <textarea id="comments-criteria"></textarea>

      <button id="review-btn" type="button">Run AI Review</button>
    </div>
  </div>

  <div id="result-card" class="card hidden">
    <h2>Review Complete</h2>
    <p><a id="doc-link" href="#" target="_blank" rel="noopener noreferrer" class="btn">Open Reviewed Google Doc</a></p>
    <pre id="result-summary"></pre>
  </div>

  <script>
    const loginSection = document.getElementById('login-section');
    const appSection = document.getElementById('app-section');
    const logoutBtn = document.getElementById('logout-btn');
    const pickerBtn = document.getElementById('picker-btn');
    const selectedDocBanner = document.getElementById('selected-doc-banner');
    const docInput = document.getElementById('doc-input');
    const editsCriteria = document.getElementById('edits-criteria');
    const commentsCriteria = document.getElementById('comments-criteria');
    const resetTemplateBtn = document.getElementById('reset-template-btn');
    const reviewBtn = document.getElementById('review-btn');
    const resultCard = document.getElementById('result-card');
    const resultSummary = document.getElementById('result-summary');
    const docLink = document.getElementById('doc-link');

    let defaultTemplates = { suggested_edits_criteria: '', comments_criteria: '' };
    let pickerConfig = null;
    let pickerApiLoaded = false;

    if (window.gapi) {
      gapi.load('picker', () => {
        pickerApiLoaded = true;
      });
    }

    async function loadDefaults() {
      const resp = await fetch('/api/defaults');
      if (resp.ok) {
        defaultTemplates = await resp.json();
        editsCriteria.value = defaultTemplates.suggested_edits_criteria;
        commentsCriteria.value = defaultTemplates.comments_criteria;
      }
    }

    resetTemplateBtn.addEventListener('click', () => {
      editsCriteria.value = defaultTemplates.suggested_edits_criteria;
      commentsCriteria.value = defaultTemplates.comments_criteria;
    });

    async function checkAuthAndConfig() {
      const resp = await fetch('/api/picker-config');
      if (!resp.ok) {
        loginSection.classList.remove('hidden');
        appSection.classList.add('hidden');
        logoutBtn.classList.add('hidden');
        return;
      }
      pickerConfig = await resp.json();
      loginSection.classList.add('hidden');
      appSection.classList.remove('hidden');
      logoutBtn.classList.remove('hidden');
    }

    pickerBtn.addEventListener('click', () => {
      if (!pickerApiLoaded || !window.google || !google.picker) {
        alert('Google Picker API is still loading. Please try again in a moment.');
        return;
      }
      if (!pickerConfig || !pickerConfig.developer_key) {
        alert('Please set GOOGLE_PICKER_API_KEY in your .env file and restart the server.');
        return;
      }

      // View 1: My Drive & Recent Google Docs (with folders)
      const myDriveView = new google.picker.DocsView(google.picker.ViewId.DOCUMENTS)
        .setIncludeFolders(true)
        .setMimeTypes('application/vnd.google-apps.document');

      // View 2: Shared Drives Google Docs
      const sharedDrivesView = new google.picker.DocsView(google.picker.ViewId.DOCUMENTS)
        .setEnableDrives(true)
        .setIncludeFolders(true)
        .setMimeTypes('application/vnd.google-apps.document');

      const builder = new google.picker.PickerBuilder()
        .addView(myDriveView)
        .addView(sharedDrivesView)
        .setOAuthToken(pickerConfig.oauth_token)
        .setDeveloperKey(pickerConfig.developer_key)
        .setTitle('Select a Google Doc to Review')
        .setCallback((data) => {
          if (data[google.picker.Response.ACTION] === google.picker.Action.PICKED) {
            const doc = data[google.picker.Response.DOCUMENTS][0];
            const docId = doc[google.picker.Document.ID];
            const docName = doc[google.picker.Document.NAME];
            docInput.value = docId;
            selectedDocBanner.textContent = 'Selected Document: ' + docName + ' (' + docId + ')';
            selectedDocBanner.classList.remove('hidden');
          }
        });

      if (pickerConfig.app_id) {
        builder.setAppId(pickerConfig.app_id);
      }

      const picker = builder.build();
      picker.setVisible(true);
    });

    reviewBtn.addEventListener('click', async () => {
      const docId = docInput.value.trim();
      if (!docId) {
        alert('Please choose a document with Browse Google Drive or enter a Document ID.');
        return;
      }
      reviewBtn.disabled = true;
      reviewBtn.textContent = 'Reviewing Document (takes ~15-30s)...';
      resultCard.classList.add('hidden');

      try {
        const resp = await fetch('/api/review', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            document_id: docId,
            suggested_edits_criteria: editsCriteria.value,
            comments_criteria: commentsCriteria.value
          })
        });
        const data = await resp.json();
        if (!resp.ok) {
          alert(data.detail || 'Error reviewing document.');
          return;
        }
        docLink.href = data.document_url;
        resultSummary.textContent = data.summary;
        resultCard.classList.remove('hidden');
      } catch (err) {
        alert('Request failed: ' + err.message);
      } finally {
        reviewBtn.disabled = false;
        reviewBtn.textContent = 'Run AI Review';
      }
    });

    loadDefaults();
    checkAuthAndConfig();
  </script>
</body>
</html>
"""


@app.get("/reviewer", response_class=HTMLResponse)
async def reviewer_ui():
    return HTMLResponse(content=REVIEWER_HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)