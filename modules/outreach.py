"""Outreach draft generator. Drafts are stored locally; nothing is ever sent.

Only facts that were actually collected are used (page title, dead links found
by the quality check). No claims such as "I loved your article" are invented.
"""
from . import db, safety

TEMPLATES = {
    "guest_post": {
        "subject": "Guest contribution idea for {their_site}",
        "relevance": "{site_name} covers {topic}, which overlaps with the readers of {their_site}.",
        "proposal": "I would like to offer an original article on {topic}. I can send two or three headline ideas "
                    "first so you can pick what fits your editorial plans.",
    },
    "industry_blog": {
        "subject": "Contribution to {their_site}: {topic}",
        "relevance": "{site_name} works in {topic}, a subject your blog already touches on.",
        "proposal": "I could write a practical piece on {topic} for your readers. Happy to follow any guidelines you have.",
    },
    "broken_link": {
        "subject": "A broken link on {their_site}",
        "relevance": "While reading {page_ref} I noticed a link that no longer works.",
        "proposal": "{dead_text}If it is useful, a page on {site_name} about {topic} could stand in as a replacement: {site_url}. "
                    "Please only use it if you think it genuinely helps your readers.",
    },
    "resource_page": {
        "subject": "Suggestion for your resource list at {their_site}",
        "relevance": "{page_ref} collects resources for people interested in {topic}, and {site_name} is on the same subject.",
        "proposal": "If you think it would help your readers, {site_name} could be considered for the list: {site_url}. "
                    "No problem at all if it is not a fit.",
    },
    "community_resource": {
        "subject": "Resource suggestion for the {their_site} community",
        "relevance": "{page_ref} points your community to helpful resources about {topic}.",
        "proposal": "{site_name} ({site_url}) may be a useful addition if it fits your guidelines.",
    },
}
DEFAULT = TEMPLATES["resource_page"]


def build_draft(target, opp, assessment=None):
    tpl = TEMPLATES.get(opp["category"], DEFAULT)
    their_site = opp["domain"]
    topic = (target.get("keywords", "").split(",")[0].strip() or target.get("category") or "your field")
    page_title = (opp.get("title") or "").strip()
    page_ref = f"your page \"{page_title}\"" if page_title else f"your page at {their_site}"
    dead_text = ""
    dead = ((assessment or {}).get("verified") or {}).get("dead_links") or []
    if opp["category"] == "broken_link" and dead:
        dead_text = f"The link to {dead[0]['url']} returns an error (HTTP {dead[0]['status']}) when I check it. "
    elif opp["category"] == "broken_link":
        dead_text = "I am happy to point out which link when you reply. "
    ctx = {"their_site": their_site, "site_name": target.get("name") or target["domain"], "topic": topic,
           "site_url": target["url"], "page_ref": page_ref, "dead_text": dead_text}
    desc = (target.get("description") or "").strip()
    body = [f"Hello {their_site} team,", "",
            tpl["relevance"].format(**ctx), "",
            tpl["proposal"].format(**ctx)]
    if desc:
        body += ["", f"A short description of {ctx['site_name']}: {desc}"]
    body += ["", f"Website: {target['url']}", "",
             "Thank you for your time, and no worries if this is not something you are looking for.", "",
             "Best regards,", ctx["site_name"] + (f"\n{target['contact_email']}" if target.get("contact_email") else "")]
    return {"subject": tpl["subject"].format(**ctx), "body": "\n".join(body)}


def create_draft(opp_id):
    opp = db.query("SELECT * FROM opportunities WHERE id=?", (opp_id,), one=True)
    if not opp:
        raise ValueError("Opportunity not found")
    target = db.query("SELECT * FROM targets WHERE id=?", (opp["target_id"],), one=True)
    a = db.query("SELECT verified FROM assessments WHERE opportunity_id=?", (opp_id,), one=True)
    assessment = {"verified": db.loads(a["verified"], {})} if a else None
    draft = build_draft(target, opp, assessment)
    res = db.execute("INSERT OR IGNORE INTO outreach_drafts (target_id, opportunity_id, subject, body, created_at) "
                     "VALUES (?,?,?,?,?)", (target["id"], opp_id, draft["subject"], draft["body"], db.now()))
    row = db.query("SELECT * FROM outreach_drafts WHERE opportunity_id=? AND body=?", (opp_id, draft["body"]), one=True)
    if res.rowcount:
        db.log("INFO", "outreach", f"Draft saved for {opp['domain']}", "", target["id"])
    return row
