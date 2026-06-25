import io
import logging

logger = logging.getLogger('ats_resume_scorer')

# --- Backend priority: xhtml2pdf (Windows-friendly) → weasyprint → fpdf2 ---

def _try_xhtml2pdf(html_docs: dict) -> bytes:
    from xhtml2pdf import pisa
    combined_html = ""
    for name, html_str in html_docs.items():
        combined_html += html_str + '<div style="page-break-after:always;"></div>'
    
    buf = io.BytesIO()
    result = pisa.CreatePDF(io.StringIO(combined_html), dest=buf)
    if result.err:
        raise RuntimeError(f"xhtml2pdf error: {result.err}")
    return buf.getvalue()


def _try_weasyprint(html_docs: dict) -> bytes:
    from weasyprint import HTML
    documents = []
    for name, html_str in html_docs.items():
        doc = HTML(string=html_str).render()
        documents.append(doc)
    first_doc = documents[0]
    for other_doc in documents[1:]:
        for page in other_doc.pages:
            first_doc.pages.append(page)
    return first_doc.write_pdf()


def _try_fpdf2(html_docs: dict) -> bytes:
    """Minimal plain-text fallback using fpdf2 — no HTML rendering."""
    from fpdf import FPDF
    import re

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    for name, html_str in html_docs.items():
        # Strip HTML tags to plain text
        text = re.sub(r'<[^>]+>', ' ', html_str)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text).strip()
        pdf.multi_cell(0, 8, text)
        pdf.add_page()

    return pdf.output()


def generate_combined_pdf(html_docs: dict[str, str]) -> bytes:
    """
    Try PDF backends in order of preference:
    1. xhtml2pdf  — pure Python, works on Windows out of the box
    2. weasyprint  — best quality, but needs GTK (Linux/Mac friendly)
    3. fpdf2       — plain-text fallback, no HTML rendering
    Raises RuntimeError if all backends fail.
    """
    backends = [
        ("xhtml2pdf", _try_xhtml2pdf),
        ("weasyprint", _try_weasyprint),
        ("fpdf2",      _try_fpdf2),
    ]

    last_error = None
    for name, fn in backends:
        try:
            pdf_bytes = fn(html_docs)
            logger.info(f"PDF generated successfully using {name}")
            return pdf_bytes
        except ImportError:
            logger.debug(f"{name} not installed, trying next backend...")
        except Exception as e:
            logger.warning(f"{name} failed: {e}")
            last_error = e

    raise RuntimeError(
        f"All PDF backends failed. Install xhtml2pdf: `pip install xhtml2pdf`. "
        f"Last error: {last_error}"
    )
