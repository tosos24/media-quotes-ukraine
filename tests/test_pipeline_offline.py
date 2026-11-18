"""End-to-end pipeline test with a fake LLM and a synthetic article page (no network, no API key)."""
from __future__ import annotations

import json
from datetime import date

import pytest

from mqcs import db, pipeline
from mqcs.llm import ArticleExtraction, ProposedQuote, ReportPlan, SectionAssignment
from mqcs.logs import RunLogger
from mqcs.settings import settings

HTML = """<html><head><title>t</title>
<script type="application/ld+json">{"@context":"http://schema.org","@type":"NewsArticle",
 "headline":"Russia and Ukraine trade drone strikes as talks stall","datePublished":"2026-11-16T08:00:00Z",
 "publisher":{"@type":"NewsMediaOrganization","name":"BBC News"}}</script></head>
<body><article>
<div class="RichTextContainer"><p>Russia launched 450 drones at Ukraine overnight, Ukraine's air force said.</p></div>
<figure><figcaption>Image caption, Firefighters in Kyiv</figcaption></figure>
<div class="RichTextContainer"><p>This video can not be played</p></div>
<div class="RichTextContainer"><p>President Volodymyr Zelensky said Kyiv needed "more air defence, now". He spoke in Kyiv.</p></div>
<div class="RichTextContainer"><p>Kremlin spokesman Dmitry Peskov said talks with Ukraine were paused.</p></div>
<div class="RichTextContainer"><p>Sign up for our newsletter.</p></div>
</article></body></html>"""


class FakeLLM:
    model = "fake-model"

    def parse(self, system, user, schema, effort="medium", max_tokens=0):
        meta = {"model": self.model, "request_id": "req_x", "stop_reason": "end_turn",
                "input_tokens": 1, "output_tokens": 1}
        if schema is ArticleExtraction:
            return ArticleExtraction(article_relevant=True, relevance_note="", quotes=[
                ProposedQuote(start="p0.s0", end="p0.s0", quote_text="Russia launched 450 drones at Ukraine "
                              "overnight, Ukraine's air force said.", speaker="Ukraine's air force",
                              kind="reported_statement", topics=["war_intensity"], materiality=4, rationale="r"),
                ProposedQuote(start="p1.s0", end="p1.s0", quote_text='President Volodymyr Zelensky said Kyiv '
                              'needed "more air defence, now".', speaker="Volodymyr Zelensky",
                              kind="direct_quote", topics=["international_support"], materiality=4, rationale="r"),
                # Hallucinated wording -> must be rejected by the verifier.
                ProposedQuote(start="p2.s0", end="p2.s0", quote_text="Peskov said talks were over for good.",
                              speaker="Dmitry Peskov", kind="reported_statement", topics=["peace_initiatives"],
                              materiality=3, rationale="r"),
            ]), meta
        assert schema is ReportPlan
        ids = [json.loads(line)["id"] for line in user.splitlines()[1:]]
        return ReportPlan(sections=[SectionAssignment(section_key="war_intensity",
                                                      summary="Commentators report heavy strikes.", quote_ids=ids)],
                          obsolete=[], not_material=[]), meta


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name, sub in (("data_dir", "data"), ("reports_dir", "reports"), ("logs_dir", "logs")):
        object.__setattr__(settings, name, tmp_path / sub)
    conn = db.connect(tmp_path / "data" / "quotes.db")
    monkeypatch.setattr(db, "connect", lambda path=None: conn)
    pipeline.setup(conn)
    return conn


def test_end_to_end(env):
    conn = env
    log = RunLogger(conn, "test", console_level="ERROR")
    src = pipeline.get_sources(conn, ["bbc"])[0]
    url = "https://www.bbc.co.uk/news/articles/ctest0001"
    conn.execute("INSERT INTO articles(source_id,url,discovered_at,discovered_via,status) VALUES "
                 "('bbc',?,'now','test','discovered')", (url,))
    conn.commit()
    row = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
    status = pipeline._store_fetched(conn, log, row, src, HTML.encode(), url, 200,
                                     date(2026, 10, 1), date(2026, 11, 17))
    assert status == "fetched"
    body = json.loads(conn.execute("SELECT body_json FROM articles").fetchone()[0])
    assert [p["text"][:12] for p in body] == ["Russia launc", "President Vo", "Kremlin spok"]  # furniture removed

    pipeline.select_quotes(conn, log, FakeLLM(), [src], date(2026, 10, 1), date(2026, 11, 17))
    quotes = conn.execute("SELECT * FROM quotes").fetchall()
    assert len(quotes) == 2
    assert conn.execute("SELECT COUNT(*) FROM rejected_quotes").fetchone()[0] == 1
    assert all(q["publication_name"] == "BBC News" and q["published_at"].startswith("2026-11-16") for q in quotes)

    from mqcs.report import generate_report
    path = generate_report(conn, date(2026, 11, 17), llm=FakeLLM())
    md = open(path, encoding="utf-8").read()
    assert "4–17 November 2026" in md and 'needed "more air defence, now".' in md
    assert "talks were over for good" not in md

    assert pipeline.reverify(conn) == {"verified": 2, "problems": 0}

    from mqcs.dashboard.app import create_app
    client = create_app().test_client()
    qid = quotes[0]["quote_id"]
    report_id = conn.execute("SELECT report_id FROM reports").fetchone()[0]
    for page in ["/", "/reports", f"/reports/{report_id}", "/quotes", f"/quotes/{qid}", "/articles",
                 "/articles/1", "/runs", "/logs", "/logs?level=ERROR", "/api/quotes", "/api/status",
                 "/quotes?q=drones&topic=war_intensity"]:
        assert client.get(page).status_code == 200, page
    assert len(client.get("/api/quotes").get_json()) == 2
