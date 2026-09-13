"""
The actual final-output package -- one workbook with four purpose-built
views, designed for what this data actually gets used for afterward:
understanding what exists (Catalog), coding a backtest (Ruleset), trading
live (Playbook), and knowing what still needs a human's own verification
before either of those (Open Questions).

CONCEPT_GROUPS below is maintained by hand, not inferred -- grouping by
shared core mechanic (not just similar names) is a judgment call, and it's
cheap to keep updating as new strategies get reconstructed. A strategy not
yet listed here falls into "Ungrouped (new)" so nothing silently goes
missing from the catalog while this mapping catches up.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

CONCEPT_GROUPS = {
    "Reference-Candle + Retracement Breakout Family": [
        "God Strategy (First-Candle Retracement, Time-Boxed, Fixed Size)",
        "NOLA Strategy (Timing Candle -- 5-Minute Filtered Window)",
        "Guru Candle (144th-Candle / Jupiter Number System)",
        "Time Cycle Bias Strategy (Open=High / Open=Low Filter)",
        "Gap Up/Gap Down Strategy (First 30-Minute Range)",
        "WDC Date Strategy (Calendar Number-Match Time Cycle)",
        "Birthday / Anniversary Date Strategy (Long-Term Positional)",
        "Trend Change Date (Trendline Projection)",
        "Intraday Timing Candle (Digit-Sum + Combo Cycle)",
        "TTS -- Time-Price Squaring (Dynamic Chained Timing Candles)",
        "Pitchfork (Median-Line Support/Resistance Tool)",
        "Ichimoku (Trend-Following Wealth Creation System)",
    ],
    "Candle Confirmation / Signal Validity Patterns": [
        "Fake vs. Genuine Sell/Buy Signal (Candle-Close Confirmation)",
        "Magical Candle Pattern (a.k.a. Jackpot Candle)",
        "Fractal Candle (Aggressive Early Entry)",
    ],
    "Options Premium Numerology Systems": [
        "Square Number Strategy (Option Premium Perfect-Square Levels)",
        "POB Strategy (Purity Of Breakout -- O&M / Decider Number Levels)",
        "Hero Zero Trade (Option Writing on Genuine-Sell Confirmation)",
        "Monthly Close Option Alert System (Fake Sell/Buy on Strike Options)",
        "Momentum-Filtered Straddle (vs. Plain Long Straddle)",
        "VIX-Based Range Calculator (Weekly/Monthly Volatility Range Projection)",
    ],
    "Sector Selection / Astro Filters": [
        "Planetary Aspect Sector Rotation Filter",
        "Mega Combination (Multi-Date Confluence Filter)",
        "Cyclic Line (21/42/63-Day Confluence + Digital-Root Validation)",
        "Gann Degree-Date & Yuga Cycle Time Projection (Swing Point + Fixed Day-Count)",
    ],
}

CONCEPT_BLURBS = {
    "Reference-Candle + Retracement Breakout Family": (
        "All mark a reference candle or level, require price to CLOSE beyond it (never just touch), then "
        "wait for a retracement back to that level before entering -- the retracement requirement and the "
        "close-not-touch confirmation are the shared, load-bearing rule across this entire family. They "
        "differ mainly in HOW the reference candle/level is chosen (first candle of the day, a selected "
        "5-min window, the 144th candle, a calendar-formula date, an opening-range comparison, etc.)."
    ),
    "Candle Confirmation / Signal Validity Patterns": (
        "Define what counts as a genuinely confirmed signal vs. a fake one -- foundational logic that the "
        "breakout family above depends on, but taught and evidenced as their own distinct named patterns."
    ),
    "Options Premium Numerology Systems": (
        "Trade the OPTION'S PREMIUM directly using calculated numeric levels (perfect squares, or "
        "custom O&M/Decider offsets), rather than the underlying instrument's price action. Higher "
        "complexity, Excel/indicator-dependent, and Hero Zero specifically is an option-WRITING (selling) "
        "approach rather than buying."
    ),
    "Sector Selection / Astro Filters": (
        "Not a complete trade rule on its own -- tells you WHICH stock/sector to watch during a given "
        "astro-event window. Meant to be combined with an entry-mechanic strategy from the breakout family "
        "above once the relevant window arrives."
    ),
}


def _load_specs(conn, course_id):
    rows = conn.execute(
        "SELECT strategy_id, name, spec_json, maturity, approved, confidence_category, version, updated_at "
        "FROM strategies WHERE course_id = ? ORDER BY name",
        (course_id,),
    ).fetchall()
    out = []
    for strategy_id, name, spec_json, maturity, approved, confidence, version, updated_at in rows:
        spec = json.loads(spec_json) if spec_json else {}
        citation_count = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE related_strategy_id = ?", (strategy_id,)
        ).fetchone()[0]
        out.append({
            "strategy_id": strategy_id, "name": name, "spec": spec, "maturity": maturity,
            "approved": bool(approved), "confidence": confidence or "(not yet checked)",
            "version": version, "updated_at": updated_at, "citation_count": citation_count,
        })
    return out


def _group_for(name, concept_groups=None):
    concept_groups = concept_groups if concept_groups is not None else CONCEPT_GROUPS
    for group, members in concept_groups.items():
        if name in members:
            return group
    return "Ungrouped (new -- add to CONCEPT_GROUPS)"


HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
CONCEPT_FILL = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CONCEPT_FONT = Font(bold=True, size=12)
WRAP = Alignment(wrap_text=True, vertical="top")


def _header_row(ws, row, headers, widths):
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    ws.freeze_panes = f"A{row + 1}"


def _build_catalog_sheet(wb, strategies, concept_groups=None, concept_blurbs=None):
    concept_groups = concept_groups if concept_groups is not None else CONCEPT_GROUPS
    concept_blurbs = concept_blurbs if concept_blurbs is not None else CONCEPT_BLURBS
    ws = wb.create_sheet("1. Strategy Catalog")
    headers = ["Concept / Strategy", "Approved", "Confidence", "Instrument", "One-Line Summary"]
    widths = [45, 12, 14, 26, 70]
    _header_row(ws, 1, headers, widths)

    by_group = {}
    for s in strategies:
        by_group.setdefault(_group_for(s["name"], concept_groups), []).append(s)

    row = 2
    for group in list(concept_groups.keys()) + [g for g in by_group if g not in concept_groups]:
        members = by_group.get(group)
        if not members:
            continue
        ws.cell(row=row, column=1, value=group).font = CONCEPT_FONT
        for c in range(1, 6):
            ws.cell(row=row, column=c).fill = CONCEPT_FILL
        row += 1
        blurb = concept_blurbs.get(group, "")
        if blurb:
            ws.cell(row=row, column=1, value=blurb).alignment = WRAP
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
            ws.row_dimensions[row].height = 45
            row += 1
        for s in members:
            spec = s["spec"]
            ws.cell(row=row, column=1, value="   " + s["name"])
            ws.cell(row=row, column=2, value="Yes" if s["approved"] else "No")
            ws.cell(row=row, column=3, value=s["confidence"])
            ws.cell(row=row, column=4, value=spec.get("instrument", ""))
            ws.cell(row=row, column=5, value=spec.get("purpose", "")).alignment = WRAP
            for c in range(1, 6):
                ws.cell(row=row, column=c).alignment = WRAP
            row += 1
        row += 1  # blank spacer row between concept groups
    return ws


def _build_ruleset_sheet(wb, strategies, concept_groups=None):
    ws = wb.create_sheet("2. Backtesting Ruleset")
    headers = ["Concept", "Strategy", "Instrument", "Timeframe", "Entry Rule", "Stop Loss",
               "Exit / Target", "No-Trade Filters", "Excel/Indicator Needed?"]
    widths = [30, 40, 22, 22, 55, 40, 35, 40, 30]
    _header_row(ws, 1, headers, widths)
    row = 2
    for s in strategies:
        spec = s["spec"]
        excel_req = spec.get("excel_dependency", {}).get("required")
        ind_req = spec.get("indicator_dependency", {}).get("required")
        needs = []
        if excel_req:
            needs.append(f"Excel: {spec['excel_dependency'].get('workbook') or 'yes'}")
        if ind_req:
            needs.append(f"Indicator: {spec['indicator_dependency'].get('name') or 'yes'}")
        vals = [
            _group_for(s["name"], concept_groups), s["name"], spec.get("instrument", ""), spec.get("timeframe", ""),
            spec.get("entry_conditions", ""), spec.get("stop_loss", ""), spec.get("exit_conditions", "") or
            spec.get("target", ""), spec.get("no_trade_conditions", ""),
            "; ".join(needs) if needs else "No",
        ]
        for col_idx, v in enumerate(vals, start=1):
            ws.cell(row=row, column=col_idx, value=v).alignment = WRAP
        row += 1
    return ws


def _build_playbook_sheet(wb, strategies):
    ws = wb.create_sheet("3. Live Trading Playbook")
    headers = ["Strategy", "Before You Trade (checklist)", "Entry Trigger", "Stop Loss Rule",
               "Daily Risk Cap / Circuit Breaker", "Confidence"]
    widths = [40, 55, 45, 35, 45, 14]
    _header_row(ws, 1, headers, widths)
    row = 2
    for s in strategies:
        spec = s["spec"]
        checklist_bits = []
        if spec.get("no_trade_conditions"):
            checklist_bits.append("No-trade check: " + spec["no_trade_conditions"][:200])
        if spec.get("excel_dependency", {}).get("required"):
            checklist_bits.append("Have the Excel sheet open and the high/low marked first.")
        if spec.get("indicator_dependency", {}).get("required"):
            checklist_bits.append(f"Confirm indicator signal: {spec['indicator_dependency'].get('name', '')}")
        checklist = " | ".join(checklist_bits) if checklist_bits else "(no specific pre-trade filter recorded)"

        # pull any daily-loss/circuit-breaker language out of no_trade_conditions if present
        risk_cap = spec.get("no_trade_conditions", "")
        risk_cap = risk_cap if any(k in risk_cap.lower() for k in ["stop loss trigger", "close the system", "circuit", "threshold"]) else "(none recorded -- set your own)"

        vals = [
            s["name"], checklist, spec.get("entry_conditions", "")[:400],
            spec.get("stop_loss", ""), risk_cap, s["confidence"],
        ]
        for col_idx, v in enumerate(vals, start=1):
            ws.cell(row=row, column=col_idx, value=v).alignment = WRAP
        row += 1
    return ws


def _build_open_questions_sheet(wb, strategies):
    ws = wb.create_sheet("4. Open Questions To Verify")
    headers = ["Strategy", "Open Question", "Confidence"]
    widths = [40, 90, 14]
    _header_row(ws, 1, headers, widths)
    row = 2
    for s in strategies:
        for q in s["spec"].get("open_questions", []):
            ws.cell(row=row, column=1, value=s["name"]).alignment = WRAP
            ws.cell(row=row, column=2, value=q).alignment = WRAP
            ws.cell(row=row, column=3, value=s["confidence"])
            row += 1
    return ws


def export_final_package(conn, course_id, output_path: str, concept_groups=None, concept_blurbs=None) -> dict:
    """concept_groups/concept_blurbs let a different course use its own grouping instead of the
    main course's -- defaults to the module-level dicts for backward compatibility."""
    concept_groups = concept_groups if concept_groups is not None else CONCEPT_GROUPS
    concept_blurbs = concept_blurbs if concept_blurbs is not None else CONCEPT_BLURBS
    strategies = _load_specs(conn, course_id)
    wb = openpyxl.Workbook()
    _build_catalog_sheet(wb, strategies, concept_groups, concept_blurbs)
    _build_ruleset_sheet(wb, strategies, concept_groups)
    _build_playbook_sheet(wb, strategies)
    _build_open_questions_sheet(wb, strategies)
    del wb["Sheet"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)

    ungrouped = [s["name"] for s in strategies if _group_for(s["name"], concept_groups).startswith("Ungrouped")]
    return {
        "output_path": output_path,
        "total_strategies": len(strategies),
        "approved_count": sum(1 for s in strategies if s["approved"]),
        "concept_groups": list(concept_groups.keys()),
        "ungrouped_strategies": ungrouped,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
