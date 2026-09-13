"""
Runs the AI-dependent stages (2-5) for one or more SELECTED courses --
this is what powers "run AI for just this one course, hold the rest" from
the GUI. Each course processed here must already have completed extraction
(stage 1) -- this orchestrator does not run extraction itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processors.tagging_processor import tag_evidence_for_course
from processors.linking_processor import cross_link_course
from processors.reconstruction_processor import reconstruct_strategies_for_course
from processors.quality_check import run_quality_check
from processors.course_summary import generate_course_summary


def run_ai_pipeline_for_course(conn, course_id: str, output_dir: str, api_key: str,
                                tagging_model: str = None, reconstruction_model: str = None) -> dict:
    """
    Runs tagging -> cross-linking -> reconstruction -> quality check, in
    that order, for ONE course. Returns a combined summary. Safe to re-run
    -- each stage only processes what the previous stage left unfinished.
    """
    print(f"\n##### AI PIPELINE: {course_id} #####")

    kwargs = {}
    if tagging_model:
        kwargs["model"] = tagging_model
    print("\n=== STAGE 2: TAGGING ===")
    tagging_result = tag_evidence_for_course(conn, course_id, api_key=api_key, **kwargs)
    print(tagging_result)

    print("\n=== STAGE 3: CROSS-LINKING ===")
    linking_result = cross_link_course(conn, course_id)
    print(linking_result)

    print("\n=== STAGE 4: RECONSTRUCTION ===")
    recon_dir = str(Path(output_dir) / "04_STRATEGY_RECONSTRUCTION")
    kwargs2 = {}
    if reconstruction_model:
        kwargs2["model"] = reconstruction_model
    recon_result = reconstruct_strategies_for_course(conn, course_id, recon_dir, api_key=api_key, **kwargs2)
    print(recon_result)

    print("\n=== STAGE 5: QUALITY CHECK ===")
    review_dir = str(Path(output_dir) / "05_REVIEW")
    quality_result = run_quality_check(conn, course_id, review_dir, api_key=api_key)

    print("\n=== STAGE 6: COURSE SUMMARY ===")
    summary_result = generate_course_summary(conn, course_id, output_dir)
    print(f"  {summary_result['strategies']} strategies, {summary_result['concepts']} concepts -> {summary_result['path']}")

    total_cost = tagging_result.get("cost_estimate_usd", 0) + recon_result.get("cost_estimate_usd", 0)
    print(f"\n##### DONE: {course_id} -- actual cost this run: ~${total_cost:.4f} #####")

    return {
        "course_id": course_id,
        "tagging": tagging_result,
        "linking": linking_result,
        "reconstruction": recon_result,
        "quality_check": {"flagged": quality_result["flagged_count"], "clean": quality_result["clean_count"]},
        "course_summary": summary_result,
        "actual_cost_usd": round(total_cost, 4),
    }


def run_ai_pipeline_for_courses(conn, course_ids: list, output_courses_dir: str, api_key: str,
                                 tagging_model: str = None, reconstruction_model: str = None,
                                 output_youtube_dir: str = None) -> list:
    """Runs the AI pipeline for a LIST of selected courses, one at a time.
    Correctly resolves output location for both local courses and
    YouTube-derived ones (course_id starting with youtube_)."""
    from pipeline.course_manager import output_root_for_course
    results = []
    for course_id in course_ids:
        if output_youtube_dir:
            output_dir = output_root_for_course(course_id, output_courses_dir, output_youtube_dir, conn=conn)
        else:
            output_dir = str(Path(output_courses_dir) / course_id)
        result = run_ai_pipeline_for_course(
            conn, course_id, output_dir, api_key,
            tagging_model=tagging_model, reconstruction_model=reconstruction_model,
        )
        results.append(result)
    return results
