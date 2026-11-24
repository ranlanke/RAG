"""Check delivery artifacts, not the truth of a source-bound claim."""
import hashlib
from .company_history_timeline import ROLE, render_svg


def finding_is_rendered(project,finding,section,rows):
    if finding.get('content_role')!=ROLE:
        return finding['text'] in section
    from research_store import research_store
    svg=render_svg(finding['timeline_data'],rows).encode()
    key=f'projects/{project.id}/visual_evidence/history_{hashlib.sha256(svg).hexdigest()[:20]}.svg'
    if f']({key})' not in section:return False
    try:return research_store.get_bytes(key)==svg
    except (OSError,KeyError,ValueError):return False
