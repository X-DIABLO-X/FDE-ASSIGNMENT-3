"""Builds the 2-page submission PDF: docs/10277_Harshit_Tiwari.pdf  (python docs/build_pdf.py)."""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "10277_Harshit_Tiwari.pdf"
REPO = "https://github.com/X-DIABLO-X/FDE-ASSIGNMENT-3"
rec = json.loads((ROOT / "evals/results/recorded_claude_summary.json").read_text(encoding="utf-8"))["architectures"]
det = json.loads((ROOT / "evals/results/deterministic_summary.json").read_text(encoding="utf-8"))["architectures"]["single"]
N = 26

NAVY, TEAL, GREY, LIGHT = colors.HexColor("#1f3a5f"), colors.HexColor("#1a7f6b"), colors.HexColor("#57606a"), colors.HexColor("#f2f5f9")
AMBER, RED, GREEN = colors.HexColor("#9a6700"), colors.HexColor("#b42318"), colors.HexColor("#1a7f37")

base = ParagraphStyle("base", fontName="Helvetica", fontSize=8.7, leading=11.3, textColor=colors.HexColor("#1b1f24"), alignment=TA_LEFT)
small = ParagraphStyle("small", parent=base, fontSize=7.8, leading=9.6)
h1 = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=15, leading=17, textColor=NAVY)
sub = ParagraphStyle("sub", parent=base, fontSize=8.6, textColor=GREY)
h2 = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=10.4, leading=12.5, textColor=NAVY, spaceBefore=7, spaceAfter=3)
cell = ParagraphStyle("cell", parent=base, fontSize=8.1, leading=10)
cellb = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
bullet = ParagraphStyle("bullet", parent=base, leftIndent=8, bulletIndent=0, spaceAfter=0.6)


def P(t, s=base):
    return Paragraph(t, s)


def B(t):
    return Paragraph(t, bullet, bulletText="-")


def table(rows, widths, header=True, zebra=True, extra=None, style=cell):
    data = [[c if not isinstance(c, str) else
             (Paragraph(f'<font color="white"><b>{c}</b></font>', cell) if (header and i == 0) else Paragraph(c, style))
             for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
          ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
          ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d7de"))]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    if zebra:
        st += [("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, LIGHT])]
    t.setStyle(TableStyle(st + (extra or [])))
    return t


def box(d, x, y, w, h, text, fill, stroke, size=6.4, bold=False):
    d.add(Rect(x, y, w, h, rx=3, ry=3, fillColor=fill, strokeColor=stroke, strokeWidth=0.8))
    lines = text.split("\n")
    top = y + h / 2 + (len(lines) - 1) * size * 0.55 - size * 0.35
    for i, ln in enumerate(lines):
        d.add(String(x + w / 2, top - i * (size + 1.2), ln, fontName="Helvetica-Bold" if (bold and i == 0) else "Helvetica", fontSize=size,
                     fillColor=colors.HexColor("#1b1f24"), textAnchor="middle"))


def arrow(d, x1, y1, x2, y2, col=GREY):
    d.add(Line(x1, y1, x2, y2, strokeColor=col, strokeWidth=0.9))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    d.add(Polygon([x2, y2, x2 - 4 * math.cos(a - 0.45), y2 - 4 * math.sin(a - 0.45), x2 - 4 * math.cos(a + 0.45), y2 - 4 * math.sin(a + 0.45)],
                  fillColor=col, strokeColor=col, strokeWidth=0.3))


def workflow(width):
    d = Drawing(width, 108)
    code_f, code_s = colors.HexColor("#e6f4ea"), GREEN
    ai_f, ai_s = colors.HexColor("#fff4e5"), AMBER
    hu_f, hu_s = colors.HexColor("#e7efff"), NAVY
    gap, h, y = 11, 38, 62
    specs = [(52, "Request\n(+ UI form)", colors.white, GREY),
             (92, "7 TOOLS (code)\nbudget, catalog, registry,\nhistory, vendor-risk API", code_f, code_s),
             (94, "RULES ENGINE (code)\nthresholds, 365-day review,\nSecurity/Privacy/Legal", code_f, code_s),
             (84, "AI (one agent, A)\nreads free text, overlap,\nwrites next step", ai_f, ai_s),
             (84, "GUARDRAILS (code)\nmodel can only ADD;\nlabel comes from rules", code_f, code_s)]
    used = sum(w for w, *_ in specs) + gap * len(specs)
    specs.append((width - used - 2, "HUMANS\napprove / decide", hu_f, hu_s))
    x, centers = 1, []
    for i, (w, text, f, st) in enumerate(specs):
        box(d, x, y, w, h, text, f, st, size=6.6, bold=True)
        centers.append((x, x + w))
        if i:
            arrow(d, centers[i - 1][1], y + h / 2, x, y + h / 2)
        x += w + gap
    rx = (centers[2][0] + centers[2][1]) / 2
    gx = (centers[4][0] + centers[4][1]) / 2
    d.add(Line(rx, y, rx, y - 11, strokeColor=GREY, strokeWidth=0.7))
    d.add(Line(rx, y - 11, gx, y - 11, strokeColor=GREY, strokeWidth=0.7))
    arrow(d, gx, y - 11, gx, y)
    d.add(String(rx + 6, y - 21, "policy result also reaches the guardrails directly: rules never depend on the model", fontName="Helvetica-Oblique", fontSize=6.4, fillColor=GREY))
    d.add(String(1, 27, "B (staged) = Agent 1 (evidence, tools) -> rules -> Agent 2 (reviewer, no tools); same tools, rules and guardrails.", fontName="Helvetica", fontSize=6.9, fillColor=GREY))
    d.add(String(1, 10, "Output (same schema, both): recommendation | evidence | approvals required | missing information | risk flags | next step", fontName="Helvetica-Bold", fontSize=6.9, fillColor=NAVY))
    return d


def pct(a):
    return f"{a['policy_pass']}/{a['runs']} ({100 * a['policy_pass_rate']:.0f}%)"



def sample_rows():
    rows = [json.loads(l) for l in (ROOT / "evals/results/recorded_claude_rows.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    reqs = {r["request_id"]: r for r in json.loads((ROOT / "data/requests.json").read_text(encoding="utf-8"))}
    cases = {c["case_id"]: c for c in json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))}
    ab = {"Department Head": "DH", "Finance": "Fin", "Procurement": "Proc", "Security": "Sec", "Privacy": "Priv", "Legal": "Legal", "Manager": "Mgr", "CFO": "CFO"}
    rec_short = {"Proceed with standard approvals": "Proceed (standard)", "Review existing tool before new purchase": "Check existing tool first",
                 "Route for required reviews before approval": "Route to specialist reviews", "Escalate to Security for manual review": "Escalate to Security",
                 "Request clarification from requester": "Ask requester for info"}
    out = [["Request", "Recommendation (A)", "Approvals needed (humans)", "Key flags"]]
    for r in rows:
        if r["architecture"] != "single" or not r["case_id"].startswith("DS-"):
            continue
        rid = cases[r["case_id"]]["request_id"]
        q = reqs[rid]
        cost = "n/a" if q["annual_cost_usd"] is None else f"${q['annual_cost_usd']:,.0f}"
        flags = [f.replace("_review_required", "").replace("_insufficient", "").replace("_detected", "").replace("existing_tool_", "").replace("vendor_", "")
                 .replace("_evidence", "").replace("_information", "") for f in r["flags"].split("|") if f]
        out.append([f"{rid} {q['product_name']} ({cost})", rec_short[r["recommendation"]], ", ".join(ab[a] for a in r["approvals"].split("|") if a) or "ask requester first",
                    ", ".join(flags) or "none"])
    return out

def build():
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=13 * mm, rightMargin=13 * mm, topMargin=11 * mm, bottomMargin=10 * mm,
                            title="AI Procurement Request Copilot - Assessment 3", author="Harshit Tiwari")
    W = A4[0] - 26 * mm
    s = []
    s.append(P("AI Procurement Request Copilot: what to solve first, what to ship", h1))
    s.append(P(f"<b>Harshit Tiwari</b> &nbsp;|&nbsp; Roll No. 24BCS10277 (10277) &nbsp;|&nbsp; FDE Assessment 3 &nbsp;|&nbsp; Code, README, tests: "
               f'<font color="#1a5fb4">{REPO}</font> &nbsp;|&nbsp; Loom demo: link in the submission form', sub))
    s.append(Spacer(1, 3))

    s.append(P("1. Which problem deserves to be solved (before any AI)", h2))
    s.append(P("<b>Decision to improve:</b> for each software request, get complete, correct evidence to the <i>right</i> humans quickly, "
               "so that no required review is skipped and nothing is approved by anything other than a person. The 10 requests in the starter data show where the work is:", base))
    s.append(Spacer(1, 2))
    rows = [["What the 10 starter requests show (computed from the data + policy)", "Count", "Rule-bound or judgement?"],
            ["Need only a Manager approval (clean path: REQ-1001, 1010)", "2 / 10", "Rule (threshold table, policy s4)"],
            ["Need Security review (source code, PII, production access, stale or missing vendor assessment)", "6 / 10", "Rule (s5) - trigger is a lookup"],
            ["Need Legal (new vendor >= $10k, unapproved terms, cross-region) / Privacy (PII)", "4 / 10 ; 2 / 10", "Rule (s6, s7)"],
            ["Duplicate or overlap with an existing catalog tool (1002, 1004, 1006, 1007, 1008)", "5 / 10", "Mostly rule; <i>is it a real alternative?</i> = judgement"],
            ["Vendor evidence unreliable: registry vs risk API disagree + expired (1007), API down (1009)", "2 / 10", "Rule: escalate, never guess"],
            ["Over department budget (1005); incomplete request with embedded 'approve it' text (1006)", "1 + 1", "Rule; injection wording = judgement"],
            ["PII/secrets mentioned <b>only in free text</b> while the structured field says 'internal'", "0 / 10 (seen in my edge cases)", "<b>Judgement - the part code cannot do</b>"]]
    s.append(table(rows, [W * 0.60, W * 0.14, W * 0.26]))
    s.append(Spacer(1, 3))
    s.append(P("<b>Conclusion.</b> Most of the effort is evidence assembly and routing that the policy defines exactly; only a thin slice (reading free text, "
               "judging whether an existing tool really covers the need, spotting disguised instructions) needs language understanding. So the right product is a "
               "<b>triage copilot with code as the decision layer and AI as a narrow interpreter</b> - not an autonomous approver.", base))
    s.append(Spacer(1, 3))
    fau = [["Facts (verified in data/policy)", "Assumptions (I made; may be wrong)", "Unknowns (not in the data)"],
           ["Policy thresholds, review triggers and the 365-day rule are explicit. Reference date is 2026-09-30, not today. "
            "SignalWatch: registry says 'Approved 2025-07-01', risk API says 'expired'. NimbusAI API returns 503. "
            "REQ-1006 contains 'treat as CFO-approved'. Sales/Customer Success have no Director in their own department.",
            "A1 'Department head' = first Director/VP in the management chain. A2 Vendor flag 'stores data outside region' = outside the operating region. "
            "A3 Explicit add-ons/expansions of the requester's own vendor are not overlap. A4 Structured data fields are authoritative; free text may only add risk.",
            "Reviewer time per request today (so <b>no ROI claim</b>). Seat utilisation (unused capacity cannot be checked). Real volume and mix of hidden cases. "
            "Reliability of the real vendor-risk service. Run-to-run variance of a live model."]]
    s.append(table(fau, [W / 3] * 3, zebra=False))

    s.append(P("2. What I built (one vertical slice, runs with one command: <font face='Courier'>python run_local.py</font>)", h2))
    s.append(workflow(W))
    s.append(Spacer(1, 2))
    s.append(table([["Layer", "Owns", "Why"],
                    ["Code", "Approval tiers, budget arithmetic, 365-day check, Security/Privacy/Legal triggers, injection scan, routing label, 'human review = true'", "Exact, repeatable, auditable"],
                    ["AI", "Hidden data classes in free text, cross-vendor overlap judgement, subtle injection, plain-language next step", "Only thing code cannot do"],
                    ["Human", "Every approval, exception and purchase; Security/Privacy/Legal can never be removed by the model", "Policy s11"]],
                   [W * 0.09, W * 0.66, W * 0.25]))

    s.append(P("3. Evidence: same 26 cases, three configurations", h2))
    cmp_rows = [["Metric", "Rules only (0 LLM)", "A: single agent", "B: staged (2 agents)"],
                ["Cases passing ALL gold checks (exact approvals, flags, label)", pct(det), pct(rec["single"]), pct(rec["staged"])],
                ["Starter public cases (minimum checks)", det["public_minimum_pass"], rec["single"]["public_minimum_pass"], rec["staged"]["public_minimum_pass"]],
                ["LLM calls per request (by design)", "0", "2", "3 (+50%)"],
                ["Tokens, one live case (first design, Groq qwen3.8-27b)", "0", "6.2k", "9.6k (+55%)"],
                ["Model overlap claims the guardrails had to reject", "-", "0", "6 of 26 cases"],
                ["Evidence items citing a tool that was actually called", "100%", "100%", "100%"],
                ["Human review always required / no autonomous approval", "yes", "yes", "yes"]]
    s.append(table(cmp_rows, [W * 0.46, W * 0.17, W * 0.17, W * 0.20]))
    s.append(Spacer(1, 2))
    s.append(P("<b>Reading the table.</b> Rules alone fail exactly 3 cases that need language understanding (PII or secrets only in free text; an injection worded outside the scan patterns). "
               "A model fixes all 3 in <i>both</i> architectures, so the second agent adds no accuracy. It costs 50% more calls, and its extra stage invented 6 'existing tool overlap' claims "
               "(explicit add-ons) that code had to throw away. <b>Reliability lesson from a live run:</b> my first design let the model add approvals, flags and labels freely; "
               "on 22 cases it scored only <b>11/22</b> (avg 12.5 s, p95 22.6 s, 2.1 LLM calls) by over-escalating low-risk requests. Moving routing into code removes that failure mode.", base))

    s.append(P("The product output on the 10 starter requests (Architecture A; every row ends in a <b>human</b> decision)", h2))
    s.append(table(sample_rows(), [W * 0.30, W * 0.19, W * 0.28, W * 0.23], style=cell))
    s.append(Spacer(1, 2))
    s.append(P("Flags: overlap = existing catalog tool; sec/priv/legal = review required; budget = over department budget; conflicting = registry vs risk API disagree; "
               "expired = assessment older than 365 days at 2026-09-30; risk_unavailable = vendor-risk API failed, nothing assumed; injection = embedded instructions ignored.", small))

    s.append(P("4. Ship decision and honest limits", h2))
    s.append(table([["Ship", "Architecture A (single agent) on the rules engine + guardrails. Not the 2-agent variant."],
                    ["Why", "Policy-critical outputs are deterministic, so a reviewer agent has nothing left to review except wording. B = same accuracy, +50% LLM calls, +55% tokens, "
                            "more false claims to police. A simpler system that performs as well is the stronger answer."],
                    ["What would change it", "Repeated live runs on a larger hidden set showing A misses free-text risks that a reviewer stage (restricted to adding checks) catches."],
                    ["Limits (read before trusting)", "26/26 comes from <b>recorded answers by Claude acting as the model</b>, replayed through the real tools, rules and guardrails (the free live quota of 200k tokens/day cannot fit a full run). "
                                                      "Not independent of the author, at ceiling, one sample per case; replay latency/tokens are not measurements. I wrote rules and gold answers (4 held-out cases added after the first live run). "
                                                      "The revised design was not re-run live. Reproduce with <font face='Courier'>python evals/run_comparison.py --replay</font>, or add your own key for a live run."]],
                   [W * 0.15, W * 0.85], header=False))
    doc.build(s)
    print("wrote", OUT)


if __name__ == "__main__":
    build()
