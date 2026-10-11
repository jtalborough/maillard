"""Public routes for the Maillard web app.

Serves the public cupping intake form at /cup/{roast_id} and the
supporting API endpoints. No authentication required.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from maillard.db.aggregation import aggregate_cuppings
from maillard.db.converters import cupping_to_response
from maillard.db.repository import CoffeeRepository, CuppingRepository, ProfileRepository, RoastRepository
from maillard.db.schemas import CuppingCreate, CuppingScores
from maillard.mcp.state import ApplicationState

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_MAX_SUBMISSIONS_PER_HOUR = 10
_MAX_KEYS = 1000
_WINDOW_SECONDS = 3600

# (ip, roast_id) -> list of timestamps
_rate_limit_store: dict[tuple[str, str], list[float]] = {}


def _check_rate_limit(ip: str, roast_id: str) -> bool:
    """Check if the request is within rate limits.

    Returns True if allowed, False if rate-limited.
    """
    now = time.monotonic()
    key = (ip, roast_id)

    # Prune expired entries from this key
    if key in _rate_limit_store:
        _rate_limit_store[key] = [
            t for t in _rate_limit_store[key] if now - t < _WINDOW_SECONDS
        ]
        if not _rate_limit_store[key]:
            del _rate_limit_store[key]

    # Check limit
    timestamps = _rate_limit_store.get(key, [])
    if len(timestamps) >= _MAX_SUBMISSIONS_PER_HOUR:
        return False

    # Evict oldest keys if at capacity
    if key not in _rate_limit_store and len(_rate_limit_store) >= _MAX_KEYS:
        oldest_key = min(_rate_limit_store, key=lambda k: _rate_limit_store[k][0])
        del _rate_limit_store[oldest_key]

    # Record this request
    _rate_limit_store.setdefault(key, []).append(now)
    return True


def _get_client_ip(request: Request) -> str:
    """Get client IP from request, checking X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_state(request: Request) -> ApplicationState:
    return request.app.state.maillard  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Public cupping submission model
# ---------------------------------------------------------------------------


class PublicCuppingSubmission(BaseModel):
    """Pydantic model for public cupping form submission."""

    aroma: float | None = Field(default=None, ge=1, le=10)
    acidity: float | None = Field(default=None, ge=1, le=10)
    sweetness: float | None = Field(default=None, ge=1, le=10)
    body: float | None = Field(default=None, ge=1, le=10)
    aftertaste: float | None = Field(default=None, ge=1, le=10)
    overall: float | None = Field(default=None, ge=1, le=10)
    flavor_notes: str | None = Field(default=None, max_length=500)
    brew_method: str | None = Field(default=None, max_length=50)
    cupper_name: str | None = Field(default=None, max_length=100)
    rest_days: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_public_router() -> APIRouter:
    """Create the public-facing router for cupping intake."""
    router = APIRouter()

    @router.get("/cup/{roast_id}", response_class=HTMLResponse)
    async def cupping_form(request: Request, roast_id: str) -> HTMLResponse:
        """Serve the public cupping intake form.

        Returns a static HTML shell that fetches roast data client-side.
        """
        _get_state(request)

        # Verify roast exists
        roast_repo = RoastRepository()
        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return HTMLResponse(
                content=_render_404_page(),
                status_code=404,
            )

        return HTMLResponse(content=_render_form_page(roast_id))

    @router.get("/api/public/roast/{roast_id}")
    async def public_roast_info(request: Request, roast_id: str) -> JSONResponse:
        """Return lightweight public roast info for the cupping form."""
        _get_state(request)

        roast_repo = RoastRepository()
        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return JSONResponse(
                content={"error": "Roast not found"},
                status_code=404,
            )

        # Load related entities separately (MongoDB has no ORM relationships)
        coffee = None
        if roast.coffee_id:
            coffee_repo = CoffeeRepository()
            coffee = await coffee_repo.get_by_id(roast.coffee_id)

        profile = None
        if roast.profile_id:
            profile_repo = ProfileRepository()
            profile = await profile_repo.get_by_id(roast.profile_id)

        return JSONResponse(content={
            "coffee_name": coffee.name if coffee else None,
            "origin": coffee.origin if coffee else None,
            "processing": coffee.processing if coffee else None,
            "roast_level": profile.roast_level if profile else None,
            "roast_date": roast.start_time[:10] if roast.start_time else None,
        })

    @router.post("/api/public/cuppings/{roast_id}")
    async def submit_public_cupping(
        request: Request, roast_id: str, submission: PublicCuppingSubmission
    ) -> JSONResponse:
        """Accept a public cupping submission."""
        # Rate limiting
        client_ip = _get_client_ip(request)
        if not _check_rate_limit(client_ip, roast_id):
            return JSONResponse(
                content={"error": "Rate limit exceeded. Maximum 10 submissions per hour."},
                status_code=429,
            )

        _get_state(request)

        # Verify roast exists
        roast_repo = RoastRepository()
        roast = await roast_repo.get_by_id(roast_id)
        if roast is None:
            return JSONResponse(
                content={"error": "Roast not found"},
                status_code=404,
            )

        # Build scores
        scores = None
        if any(
            s is not None
            for s in [
                submission.aroma, submission.acidity, submission.sweetness,
                submission.body, submission.aftertaste, submission.overall,
            ]
        ):
            scores = CuppingScores(
                aroma=submission.aroma,
                acidity=submission.acidity,
                sweetness=submission.sweetness,
                body=submission.body,
                aftertaste=submission.aftertaste,
                overall=submission.overall,
            )

        # Parse flavor notes from comma-separated string
        flavor_notes_list: list[str] | None = None
        if submission.flavor_notes:
            flavor_notes_list = [
                n.strip() for n in submission.flavor_notes.split(",") if n.strip()
            ]

        cupping_data = CuppingCreate(
            roast_id=roast_id,
            scores=scores,
            flavor_notes=flavor_notes_list,
            brew_method=submission.brew_method,
            cupper_name=submission.cupper_name,
            rest_days=submission.rest_days,
            notes=submission.notes,
        )

        cupping_repo = CuppingRepository()
        cupping = await cupping_repo.create(cupping_data)

        # Refresh cuppings list for aggregation
        all_cuppings = await cupping_repo.get_by_roast_id(roast_id)
        agg = aggregate_cuppings(all_cuppings)

        response = cupping_to_response(cupping).model_dump()
        response["aggregation"] = {
            "count": agg.count,
            "avg_total": agg.avg_total,
            "dimension_averages": [
                {"dimension": d.dimension, "average": d.average, "count": d.count}
                for d in agg.dimension_averages
            ],
            "ranked_flavor_notes": agg.ranked_flavor_notes,
        }

        _logger.info(
            "Public cupping submitted for roast %s from %s",
            roast_id, client_ip,
        )
        return JSONResponse(content=response, status_code=201)

    return router


# ---------------------------------------------------------------------------
# HTML templates (self-contained, no server-side injection of user data)
# ---------------------------------------------------------------------------


def _render_form_page(roast_id: str) -> str:
    """Render the cupping form HTML page.

    Only the roast_id (a UUID from the URL path) is embedded.
    All roast data is fetched client-side via the public JSON endpoint.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cup This Coffee</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
    <style>
        body {{ background-color: #1a1a2e; color: #e0e0e0; }}
        .card {{ background-color: #16213e; border: 1px solid #0f3460; }}
        .score-input {{ background-color: #0f3460; border: 1px solid #533483; color: #e0e0e0; }}
        .score-input:focus {{ border-color: #e94560; outline: none; box-shadow: 0 0 0 2px rgba(233,69,96,0.3); }}
        .btn-primary {{ background-color: #e94560; }}
        .btn-primary:hover {{ background-color: #c73e54; }}
        .btn-secondary {{ background-color: #533483; }}
        .btn-secondary:hover {{ background-color: #6b44a0; }}
        .info-label {{ color: #a0a0c0; }}
        .total-score {{ color: #e94560; }}
        select {{ background-color: #0f3460; border: 1px solid #533483; color: #e0e0e0; }}
        select:focus {{ border-color: #e94560; outline: none; }}
        textarea {{ background-color: #0f3460; border: 1px solid #533483; color: #e0e0e0; }}
        textarea:focus {{ border-color: #e94560; outline: none; }}
    </style>
</head>
<body class="min-h-screen p-4">
    <div class="max-w-lg mx-auto">
        <!-- Header -->
        <div class="text-center mb-6">
            <h1 class="text-2xl font-bold mb-1">Cup This Coffee</h1>
            <p class="text-sm info-label">Share your tasting notes</p>
        </div>

        <!-- Coffee info (populated via fetch) -->
        <div id="coffee-info" class="card rounded-lg p-4 mb-6">
            <p class="info-label text-sm">Loading coffee details...</p>
        </div>

        <!-- Form -->
        <form id="cupping-form" class="space-y-6">
            <!-- Scores grid -->
            <div class="card rounded-lg p-4">
                <h2 class="text-lg font-semibold mb-3">SCA Scores <span class="text-sm info-label">(1-10)</span></h2>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-sm info-label mb-1">Aroma</label>
                        <input type="number" name="aroma" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Acidity</label>
                        <input type="number" name="acidity" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Sweetness</label>
                        <input type="number" name="sweetness" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Body</label>
                        <input type="number" name="body" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Aftertaste</label>
                        <input type="number" name="aftertaste" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Overall</label>
                        <input type="number" name="overall" min="1" max="10" step="0.5"
                               class="score-input w-full rounded px-3 py-2" placeholder="1-10">
                    </div>
                </div>
                <div class="mt-3 text-right">
                    <span class="info-label text-sm">Total: </span>
                    <span id="total-score" class="total-score text-xl font-bold">0</span>
                    <span class="info-label text-sm"> / 60</span>
                </div>
            </div>

            <!-- Details -->
            <div class="card rounded-lg p-4 space-y-3">
                <div>
                    <label class="block text-sm info-label mb-1">Flavor Notes</label>
                    <input type="text" name="flavor_notes"
                           class="score-input w-full rounded px-3 py-2"
                           placeholder="e.g. citrus, floral, chocolate">
                </div>
                <div>
                    <label class="block text-sm info-label mb-1">Brew Method</label>
                    <select name="brew_method" class="w-full rounded px-3 py-2">
                        <option value="">Select...</option>
                        <option value="V60">V60</option>
                        <option value="Chemex">Chemex</option>
                        <option value="Aeropress">Aeropress</option>
                        <option value="French Press">French Press</option>
                        <option value="Espresso">Espresso</option>
                        <option value="Cupping Bowl">Cupping Bowl</option>
                        <option value="Kalita Wave">Kalita Wave</option>
                        <option value="Moka Pot">Moka Pot</option>
                        <option value="Other">Other</option>
                    </select>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-sm info-label mb-1">Your Name <span class="text-xs">(optional)</span></label>
                        <input type="text" name="cupper_name" maxlength="100"
                               class="score-input w-full rounded px-3 py-2" placeholder="Name">
                    </div>
                    <div>
                        <label class="block text-sm info-label mb-1">Rest Days</label>
                        <input type="number" name="rest_days" min="0"
                               class="score-input w-full rounded px-3 py-2" placeholder="Days">
                    </div>
                </div>
                <div>
                    <label class="block text-sm info-label mb-1">Notes <span class="text-xs">(optional)</span></label>
                    <textarea name="notes" rows="2" maxlength="1000"
                              class="w-full rounded px-3 py-2"
                              placeholder="Additional tasting notes..."></textarea>
                </div>
            </div>

            <!-- Submit -->
            <button type="submit" id="submit-btn"
                    class="btn-primary w-full py-3 rounded-lg font-semibold text-white text-lg">
                Submit Cupping
            </button>
        </form>

        <!-- Confirmation (hidden initially) -->
        <div id="confirmation" class="hidden space-y-4">
            <div class="card rounded-lg p-4 text-center">
                <h2 class="text-xl font-bold mb-2">Thank You!</h2>
                <p class="info-label">Your cupping has been recorded.</p>
            </div>
            <div id="submitted-scores" class="card rounded-lg p-4"></div>
            <div id="aggregation-results" class="card rounded-lg p-4"></div>
            <button onclick="resetForm()"
                    class="btn-secondary w-full py-3 rounded-lg font-semibold text-white">
                Submit Another Cupping
            </button>
        </div>
    </div>

    <script>
    const ROAST_ID = "{roast_id}";
    const SCORE_FIELDS = ["aroma", "acidity", "sweetness", "body", "aftertaste", "overall"];

    // Load coffee info
    async function loadCoffeeInfo() {{
        try {{
            const resp = await fetch("/api/public/roast/" + ROAST_ID);
            if (!resp.ok) throw new Error("Not found");
            const data = await resp.json();
            const el = document.getElementById("coffee-info");
            let html = "";
            if (data.coffee_name) html += '<h2 class="text-xl font-bold">' + escapeHtml(data.coffee_name) + "</h2>";
            const details = [];
            if (data.origin) details.push(escapeHtml(data.origin));
            if (data.processing) details.push(escapeHtml(data.processing));
            if (data.roast_level) details.push(escapeHtml(data.roast_level));
            if (details.length) html += '<p class="info-label text-sm mt-1">' + details.join(" · ") + "</p>";
            if (data.roast_date) html += '<p class="info-label text-xs mt-1">Roasted: ' + escapeHtml(data.roast_date) + "</p>";
            el.innerHTML = html || '<p class="info-label">Coffee details unavailable</p>';
        }} catch (e) {{
            document.getElementById("coffee-info").textContent = "Could not load coffee details.";
        }}
    }}

    function escapeHtml(str) {{
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }}

    // Live total
    function updateTotal() {{
        let total = 0;
        SCORE_FIELDS.forEach(function(f) {{
            const v = parseFloat(document.querySelector('[name="' + f + '"]').value);
            if (!isNaN(v)) total += v;
        }});
        document.getElementById("total-score").textContent = total % 1 === 0 ? total : total.toFixed(1);
    }}

    SCORE_FIELDS.forEach(function(f) {{
        document.querySelector('[name="' + f + '"]').addEventListener("input", updateTotal);
    }});

    // Submit
    document.getElementById("cupping-form").addEventListener("submit", async function(e) {{
        e.preventDefault();
        const btn = document.getElementById("submit-btn");
        btn.disabled = true;
        btn.textContent = "Submitting...";

        const payload = {{}};
        SCORE_FIELDS.forEach(function(f) {{
            const v = parseFloat(document.querySelector('[name="' + f + '"]').value);
            if (!isNaN(v)) payload[f] = v;
        }});

        const flavorNotes = document.querySelector('[name="flavor_notes"]').value.trim();
        if (flavorNotes) payload.flavor_notes = flavorNotes;

        const brewMethod = document.querySelector('[name="brew_method"]').value;
        if (brewMethod) payload.brew_method = brewMethod;

        const cupperName = document.querySelector('[name="cupper_name"]').value.trim();
        if (cupperName) payload.cupper_name = cupperName;

        const restDays = parseInt(document.querySelector('[name="rest_days"]').value);
        if (!isNaN(restDays)) payload.rest_days = restDays;

        const notes = document.querySelector('[name="notes"]').value.trim();
        if (notes) payload.notes = notes;

        try {{
            const resp = await fetch("/api/public/cuppings/" + ROAST_ID, {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify(payload),
            }});

            if (resp.status === 429) {{
                alert("Rate limit exceeded. Please try again later.");
                btn.disabled = false;
                btn.textContent = "Submit Cupping";
                return;
            }}

            if (!resp.ok) throw new Error("Submission failed");

            const result = await resp.json();
            showConfirmation(result);
        }} catch (err) {{
            alert("Error submitting cupping. Please try again.");
            btn.disabled = false;
            btn.textContent = "Submit Cupping";
        }}
    }});

    function showConfirmation(result) {{
        document.getElementById("cupping-form").classList.add("hidden");
        const conf = document.getElementById("confirmation");
        conf.classList.remove("hidden");

        // Submitted scores
        const scoresEl = document.getElementById("submitted-scores");
        let scoresHtml = '<h3 class="font-semibold mb-2">Your Scores</h3><div class="grid grid-cols-2 gap-2">';
        SCORE_FIELDS.forEach(function(f) {{
            const v = result[f];
            if (v != null) {{
                scoresHtml += '<div class="flex justify-between"><span class="info-label">' +
                    f.charAt(0).toUpperCase() + f.slice(1) +
                    '</span><span>' + v + '</span></div>';
            }}
        }});
        scoresHtml += "</div>";
        scoresEl.innerHTML = scoresHtml;

        // Aggregation
        const aggEl = document.getElementById("aggregation-results");
        const agg = result.aggregation;
        if (agg && agg.count > 0) {{
            let aggHtml = '<h3 class="font-semibold mb-2">Community Averages <span class="text-xs info-label">(' + agg.count + ' cupping' + (agg.count > 1 ? "s" : "") + ')</span></h3>';
            if (agg.avg_total != null) {{
                aggHtml += '<p class="mb-2">Average Total: <span class="total-score font-bold">' + agg.avg_total + '</span> / 60</p>';
            }}
            if (agg.dimension_averages && agg.dimension_averages.length) {{
                aggHtml += '<div class="grid grid-cols-2 gap-2">';
                agg.dimension_averages.forEach(function(d) {{
                    aggHtml += '<div class="flex justify-between"><span class="info-label">' +
                        d.dimension.charAt(0).toUpperCase() + d.dimension.slice(1) +
                        '</span><span>' + d.average + '</span></div>';
                }});
                aggHtml += "</div>";
            }}
            if (agg.ranked_flavor_notes && agg.ranked_flavor_notes.length) {{
                aggHtml += '<p class="mt-2 text-sm info-label">Top notes: ' + agg.ranked_flavor_notes.slice(0, 5).join(", ") + '</p>';
            }}
            aggEl.innerHTML = aggHtml;
        }} else {{
            aggEl.innerHTML = "";
        }}
    }}

    function resetForm() {{
        document.getElementById("confirmation").classList.add("hidden");
        const form = document.getElementById("cupping-form");
        form.classList.remove("hidden");
        form.reset();
        document.getElementById("total-score").textContent = "0";
        document.getElementById("submit-btn").disabled = false;
        document.getElementById("submit-btn").textContent = "Submit Cupping";
    }}

    loadCoffeeInfo();
    </script>
</body>
</html>"""


def _render_404_page() -> str:
    """Render a 404 page for when the roast is not found."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Roast Not Found</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
    <style>
        body { background-color: #1a1a2e; color: #e0e0e0; }
        .card { background-color: #16213e; border: 1px solid #0f3460; }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
    <div class="card rounded-lg p-8 text-center max-w-md">
        <h1 class="text-4xl font-bold mb-4">404</h1>
        <p class="text-lg mb-2">Roast Not Found</p>
        <p class="text-sm" style="color: #a0a0c0;">
            This coffee roast doesn't exist or the link may be incorrect.
        </p>
    </div>
</body>
</html>"""
