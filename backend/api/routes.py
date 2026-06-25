import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from backend.api.auth import get_current_user
from backend.models.schemas import AnalysisResponse, ComponentScores, JDComparison, SkillValidationDetails
from backend.utils.file_utils import (
    get_default_grammar_results,
    get_default_location_results,
    get_default_skill_validation_results,
)

logger = logging.getLogger('ats_resume_scorer')

router = APIRouter(prefix='/api/v1', tags=['Analysis'])

def _clean(text: str) -> str:
    for prefix in ('✅', '🌟', '❌', '⚠️', '📝', '🔴', '🟡', '🟢', '🟠', '👍'):
        text = text.lstrip(prefix)
    return text.strip()

@router.post('/analyze-resume', response_model=AnalysisResponse)
async def analyze_resume(
    request: Request,
    resume: UploadFile = File(..., description='Resume file — PDF or DOCX, max 5 MB'),
    job_description: str = Form('', description='Job description text (optional)'),
    user_id: str = Depends(get_current_user),
):
    print("\n===== ANALYZE API HIT =====")
    print("USER =", user_id)

    warnings: List[str] = []

    nlp = request.app.state.nlp
    embedder = request.app.state.embedder

    try:
        print("STEP 1 - Reading uploaded file")

        file_bytes = await resume.read()

        filename = resume.filename or "resume"

        print("STEP 2 - File read complete")
        print("Filename:", filename)
        print("Size:", len(file_bytes))

        from backend.services.resume_parser import (
            FileParsingError,
            FileValidationError,
            parse_resume_file,
        )

        print("STEP 3 - Parsing resume")

        resume_text, _metadata = parse_resume_file(
            file_bytes,
            filename,
        )

        print("STEP 4 - Resume parsed")
        print("Characters extracted:", len(resume_text))

    except Exception:
        import traceback
        print("\nERROR DURING RESUME PARSING")
        traceback.print_exc()
        raise

    try:
        print("STEP 5 - Calling analyze_full_resume")

        from backend.services.resume_analyzer import analyze_full_resume

        result = analyze_full_resume(
            resume_text=resume_text,
            nlp=nlp,
            embedder=embedder,
            job_description=job_description,
        )

        print("STEP 6 - analyze_full_resume finished")

    except Exception:
        import traceback
        print("\nERROR INSIDE analyze_full_resume")
        traceback.print_exc()
        raise

    try:
        print("STEP 7 - Building response")

        jd_comparison_result = None

        if result.get("jd_comparison"):
            jd_comparison_result = JDComparison(
                match_percentage=round(
                    float(result["jd_comparison"].get("match_percentage", 0.0)), 1
                ),
                semantic_similarity=round(
                    float(result["jd_comparison"].get("semantic_similarity", 0.0)), 3
                ),
                matched_keywords=result["jd_comparison"].get("matched_keywords", [])[:20],
                missing_keywords=result["jd_comparison"].get("missing_keywords", [])[:15],
                skills_gap=result["jd_comparison"].get("skills_gap", [])[:10],
            )

        detailed_fb = result.get("detailed_feedback", [])

        svd_raw = result.get("skill_validation_details") or {}

        skill_val_details = SkillValidationDetails(
            validated=svd_raw.get("validated", []),
            unvalidated=svd_raw.get("unvalidated", []),
            total=svd_raw.get("total", 0),
            validated_count=svd_raw.get("validated_count", 0),
            validation_pct=svd_raw.get("validation_pct", 0.0),
        )

        response = AnalysisResponse(
            ATS_score=result["ats_score"],
            component_scores=ComponentScores(**result["component_scores"]),
            issues_summary=result["issues_summary"],
            detailed_feedback=detailed_fb,
            jd_match_analysis=jd_comparison_result,
            skill_validation_details=skill_val_details,

            ats_score=result["ats_score"],
            keyword_match=jd_comparison_result.match_percentage if jd_comparison_result else 0.0,
            missing_keywords=result.get("missing_keywords", []),
            matched_keywords=result.get("matched_keywords", []),
            skills=list(result.get("skills", [])[:20]),
            jd_comparison=jd_comparison_result,
            interpretation=result.get("interpretation", ""),
        )

        print("STEP 8 - Response built")

    except Exception:
        import traceback
        print("\nERROR BUILDING RESPONSE")
        traceback.print_exc()
        raise

    try:
        print("STEP 9 - Saving history")

        from backend.database.supabase_db import save_analysis

        await save_analysis(user_id, filename, result)

        print("STEP 10 - History saved")

    except Exception:
        import traceback
        print("\nERROR SAVING HISTORY")
        traceback.print_exc()

    print("STEP 11 - Returning response")

    return response

@router.get('/health')
async def health_check(request: Request):
    """Health check — confirms models are loaded and the API is ready."""
    return {
        'status':          'healthy',
        'nlp_loaded':      request.app.state.nlp is not None,
        'embedder_loaded': request.app.state.embedder is not None,
    }

@router.get('/history')
async def get_history(user_id: str = Depends(get_current_user)):
    """Return the signed-in user's past analyses (identity comes from the JWT)."""
    from backend.database.supabase_db import get_user_history
    try:
        return await get_user_history(user_id)
    except Exception as exc:
        logger.error(f'History fetch failed: {exc}')
        raise HTTPException(status_code=500, detail=f'Could not load history: {exc}')


@router.delete('/history/{analysis_id}')
async def delete_history_entry(
    analysis_id: str,
    user_id: str = Depends(get_current_user),
):
    """Delete one analysis from the signed-in user's history."""
    from backend.database.supabase_db import delete_analysis
    try:
        success = await delete_analysis(analysis_id, user_id)
        if not success:
            raise HTTPException(status_code=404, detail='Analysis not found or not owned by this user.')
        return {'status': 'deleted', 'id': analysis_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f'History delete failed: {exc}')
        raise HTTPException(status_code=500, detail=f'Could not delete: {exc}')
    

@router.post('/generate-pdf')
async def generate_pdf(
    data: AnalysisResponse,
    user_id: str = Depends(get_current_user),
):
    from backend.services.report_generator import generate_html_reports
    from backend.services.pdf_export import generate_combined_pdf
    from fastapi.responses import Response

    try:
        html_docs = generate_html_reports(data.model_dump())
        pdf_bytes = generate_combined_pdf(html_docs)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": "attachment; filename=ats_report.pdf"
            }
        )
    except Exception as e:
        logger.error(f'Failed to generate PDF: {e}')
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {e}")
    

@router.get('/history/{analysis_id}/pdf')
async def generate_history_pdf(
    analysis_id: str,
    user_id: str = Depends(get_current_user),
):
    from backend.database.supabase_db import get_user_history
    from backend.services.report_generator import generate_html_reports
    from backend.services.pdf_export import generate_combined_pdf
    from fastapi.responses import Response

    history = await get_user_history(user_id)
    analysis_data = next((item["analysis_result"] for item in history if item["id"] == analysis_id), None)

    if not analysis_data:
        raise HTTPException(status_code=404, detail="Analysis not found")

    try:
        html_docs = generate_html_reports(analysis_data)
        pdf_bytes = generate_combined_pdf(html_docs)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=ats_report_{analysis_id}.pdf"
            }
        )
    except Exception as e:
        logger.error(f'Failed to generate PDF for history: {e}')
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {e}")