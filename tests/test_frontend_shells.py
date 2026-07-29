from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATCHES_HTML = ROOT / "web" / "static" / "index.html"
PREMATCH_HTML = ROOT / "web" / "static" / "prematch.html"
APP_TSX = ROOT / "web" / "frontend" / "src" / "App.tsx"
TYPES_TS = ROOT / "web" / "frontend" / "src" / "types.ts"
PREMATCH_WORKSPACE_TSX = (
    ROOT / "web" / "frontend" / "src" / "components" / "PrematchWorkspace.tsx"
)
MATCH_WORKSPACE_TSX = (
    ROOT / "web" / "frontend" / "src" / "components" / "MatchWorkspace.tsx"
)


def test_legacy_pages_share_operator_navigation_and_paper_boundary() -> None:
    matches = MATCHES_HTML.read_text(encoding="utf-8")
    prematch = PREMATCH_HTML.read_text(encoding="utf-8")

    for document in (matches, prematch):
        assert '<html lang="zh-CN">' in document
        assert "PAPER / ANALYSIS ONLY" in document
        assert 'href="/monitor"' in document
        assert 'href="/matches"' in document
        assert 'href="/prematch"' in document
        assert 'href="/monitor?view=intelligence"' in document

    assert 'href="/">Matches</a>' not in prematch


def test_legacy_files_redirect_to_the_running_web_service() -> None:
    matches = MATCHES_HTML.read_text(encoding="utf-8")
    prematch = PREMATCH_HTML.read_text(encoding="utf-8")

    assert 'window.location.protocol === "file:"' in matches
    assert 'window.location.replace("http://127.0.0.1:8000/matches")' in matches
    assert 'window.location.protocol === "file:"' in prematch
    assert 'window.location.replace("http://127.0.0.1:8000/prematch")' in prematch


def test_matches_page_renders_remote_values_with_dom_apis() -> None:
    document = MATCHES_HTML.read_text(encoding="utf-8")

    assert "content.innerHTML" not in document
    assert "row.append" in document
    assert "cell.textContent" in document
    assert "content.replaceChildren" in document
    assert "data-label" in document
    assert "@media (max-width: 640px)" in document


def test_prematch_picker_has_keyboard_and_dialog_semantics() -> None:
    document = PREMATCH_HTML.read_text(encoding="utf-8")

    assert 'role="dialog"' in document
    assert 'aria-modal="true"' in document
    assert 'role="tablist"' in document
    assert "handleSlotKey" in document
    assert "handleClearKey" in document
    assert "event.key === 'Escape'" in document
    assert "safeImageUrl" in document
    assert "if (!raw) return '';" in document
    assert "renderFallback" in document
    assert document.index("image.addEventListener('error', renderFallback") < document.index(
        "image.src = imageUrl"
    )
    assert "img.onerror = () => {" in document
    assert "img.removeAttribute('src');" in document
    assert "select.appendChild(option)" in document
    assert "sel.replaceChildren(new Option" in document


def test_monitor_console_exposes_runtime_safety_boundary() -> None:
    app = APP_TSX.read_text(encoding="utf-8")
    types = TYPES_TS.read_text(encoding="utf-8")
    workspace = MATCH_WORKSPACE_TSX.read_text(encoding="utf-8")

    assert '<SafetyBoundaryBar snapshot={snapshot} />' in app
    assert "PAPER ONLY" in app
    assert "不包含真实下注入口" in app
    assert "market_source_policy?: string" in types
    assert "capabilities?: Record<string, MonitorCapability>" in types
    assert "milestone_governance?: MilestoneGovernanceProjection" in types
    assert '<main className="workspace" aria-live="polite">' not in workspace


def test_monitor_console_integrates_stratz_rosh_prematch_workspace() -> None:
    app = APP_TSX.read_text(encoding="utf-8")
    prematch = PREMATCH_WORKSPACE_TSX.read_text(encoding="utf-8")

    assert 'value="prematch">赛前预测</Tab>' in app
    assert 'window.location.pathname.replace(/\\/+$/, "") === "/prematch"' in app
    assert '<PrematchWorkspace />' in app
    assert "STRATZ Rosh" in prematch
    assert "createRoshAnalysis" in prematch
