"""Choose company review behavior from the chapter role, without env mutation."""
import os
from company_chapters import chapter


def research_chapter(number):
    try:return chapter(number)['mode']=='research'
    except ValueError:return False


def acceptance_profile(number):
    if not research_chapter(number):return 'strict_core'
    return os.getenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','').strip() or 'practical_core'


def review_chapter(records):
    numbers={r['decision']['chapter'] for r in records if r.get('decision',{}).get('status')=='use'}
    return next(iter(numbers)) if len(numbers)==1 else None


def semantic_review_enabled(records):
    mode=os.getenv('COMPANY_EDITORIAL_REVIEW_MODE','').strip()
    if mode:return mode=='semantic_core'
    return research_chapter(review_chapter(records))
