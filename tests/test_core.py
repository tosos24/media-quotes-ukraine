"""Offline tests for the deterministic core (no network, no API key needed)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from mqcs import db
from mqcs.llm import ObsoleteDecision, ProposedQuote, ReportPlan, SectionAssignment
from mqcs.quotes import IndexedArticle, verify_proposal
from mqcs.relevance import article_match, candidate_match
from mqcs.report import render_markdown, validate_plan, verify_markdown
from mqcs.textutil import split_sentences

SECTIONS = json.load(open("config/sections.json", encoding="utf-8"))
KEYS = {s["key"] for s in SECTIONS}

PARAS = [
    {"kind": "p", "text": "Russia launched 600 drones and 40 missiles overnight, Ukraine's air force said."},
    {"kind": "h", "text": "Talks in Abu Dhabi"},
    {"kind": "p", "text": "Kremlin spokesman Dmitry Peskov said Moscow was “ready to continue talks”. "
                          "He did not give a date. Mr. Peskov added that sanctions would not change Russia's course."},
    {"kind": "li", "text": "Ukraine will receive 500 Patriot missiles, the US said."},
]
META = {"source_name": "BBC", "publication_name": "BBC News", "article_title": "Russia strikes Ukraine",
        "article_url": "https://www.bbc.co.uk/news/articles/x", "published_at": "2026-11-10T10:00:00+00:00"}


def pq(start, end, text, speaker="", topics=("war_intensity",)):
    return ProposedQuote(start=start, end=end, quote_text=text, speaker=speaker, kind="reporting",
                         topics=list(topics), materiality=4, rationale="r")


def test_sentence_split_keeps_abbreviations_and_round_trips():
    p = PARAS[2]["text"]
    s = split_sentences(p)
    assert len(s) == 3 and s[2].startswith("Mr. Peskov")
    assert " ".join(s) == p


def test_valid_quote_is_rebuilt_from_source():
    art = IndexedArticle.from_body(PARAS)
    rep = verify_proposal(pq("p2.s0", "p2.s1", "Kremlin spokesman Dmitry Peskov said Moscow was \"ready to "
                             "continue talks\". He did not give a date.", "Dmitry Peskov"), art, META, KEYS)
    assert rep["ok"], rep
    assert rep["text"].startswith("Kremlin spokesman") and "“" in rep["text"]  # original curly quotes kept
    assert rep["checks"]["speaker_traceable"]


def test_frankenquote_is_rejected():
    art = IndexedArticle.from_body(PARAS)
    # Model claims p2.s0..p2.s2 but skipped the middle sentence -> copy mismatch.
    rep = verify_proposal(pq("p2.s0", "p2.s2", "Kremlin spokesman Dmitry Peskov said Moscow was \"ready to "
                             "continue talks\". Mr. Peskov added that sanctions would not change Russia's course."),
                          art, META, KEYS)
    assert not rep["ok"] and "model_copy_matches_source" in rep["reasons"]


def test_hallucinated_text_and_headings_rejected():
    art = IndexedArticle.from_body(PARAS)
    assert not verify_proposal(pq("p0.s0", "p0.s0", "Russia launched 900 drones overnight."), art, META, KEYS)["ok"]
    rep = verify_proposal(pq("p0.s0", "p2.s0", "x"), art, META, KEYS)  # crosses a subheading
    assert not rep["ok"] and rep["checks"]["no_headings_in_span"] is False
    assert not verify_proposal(pq("p9.s0", "p9.s0", "x"), art, META, KEYS)["ok"]


def test_missing_metadata_rejected_and_untraceable_speaker_flagged():
    art = IndexedArticle.from_body(PARAS)
    text = "Russia launched 600 drones and 40 missiles overnight, Ukraine's air force said."
    rep = verify_proposal(pq("p0.s0", "p0.s0", text), art, {**META, "published_at": ""}, KEYS)
    assert not rep["ok"] and "metadata_published_at" in rep["reasons"]
    rep = verify_proposal(pq("p0.s0", "p0.s0", text, speaker="Vladimir Zhirinovsky"), art, META, KEYS)
    assert rep["ok"] and rep["checks"]["speaker_traceable"] is False


def test_paragraph_ids_resolve_to_whole_paragraph():
    art = IndexedArticle.from_body(PARAS)
    whole = PARAS[2]["text"].replace("“", '"').replace("”", '"')
    assert verify_proposal(pq("p2", "p2", whole, "Dmitry Peskov"), art, META, KEYS)["ok"]
    assert not verify_proposal(pq("p2", "p2", "Kremlin spokesman Dmitry Peskov said Moscow was \"ready to "
                                  "continue talks\"."), art, META, KEYS)["ok"]  # copy must still match


def test_list_item_structure_preserved():
    art = IndexedArticle.from_body(PARAS)
    rep = verify_proposal(pq("p3.s0", "p3.s0", "Ukraine will receive 500 Patriot missiles, the US said."),
                          art, META, KEYS)
    assert rep["ok"] and rep["text"] == "- Ukraine will receive 500 Patriot missiles, the US said."


def test_relevance_rules():
    assert candidate_match("https://www.bbc.co.uk/news/articles/abc", "Zelensky meets Starmer")[0]
    assert candidate_match("https://x.com/a", "Putin says war aims unchanged")[0]
    assert not candidate_match("https://x.com/a", "Putin attends ballet")[0]
    assert not candidate_match("https://www.bbc.co.uk/sport/ukraine-football", "Ukraine win")[0]
    assert article_match("Budget news", "Ukraine " * 3)[0]
    assert not article_match("Budget news", "The Ukraine mention is passing.")[0]


def _q(i, day, topics, text):
    return {"quote_id": i, "published_at": f"2026-11-{day:02d}T09:00:00+00:00", "source_name": "BBC",
            "publication_name": "BBC News", "article_title": f"Title {i}", "article_url": f"https://b/{i}",
            "speaker": "", "topics_json": json.dumps(topics), "materiality": 3, "quote_text": text,
            "quote_sha256": __import__("hashlib").sha256(text.encode()).hexdigest()}


def test_report_plan_validation_and_verbatim_render():
    quotes = [_q("Q-1", 5, ["peace_initiatives"], "Talks are scheduled for Friday."),
              _q("Q-2", 8, ["peace_initiatives"], "Friday's talks were cancelled.\n\nNo new date was set."),
              _q("Q-3", 9, ["war_intensity"], "Fighting intensified near Pokrovsk."),
              _q("Q-4", 9, ["sanctions_economy"], "Forgotten by the model.")]
    plan = ReportPlan(
        sections=[SectionAssignment(section_key="peace_initiatives", summary="Talks have stalled.", quote_ids=["Q-2"]),
                  SectionAssignment(section_key="war_intensity", summary="Fighting is intensifying.",
                                    quote_ids=["Q-3", "Q-999"])],
        obsolete=[ObsoleteDecision(quote_id="Q-1", superseded_by=["Q-2"], reason="talks cancelled"),
                  ObsoleteDecision(quote_id="Q-3", superseded_by=["Q-1"], reason="bad: older quote")],
        not_material=[])
    clean, notes = validate_plan(plan, quotes, SECTIONS)
    assert [o["quote_id"] for o in clean["obsolete"]] == ["Q-1"]          # Q-3 reinstated (cited older quote)
    shown = {i for s in clean["sections"] for i in s["quote_ids"]}
    assert shown == {"Q-2", "Q-3", "Q-4"}                                  # Q-4 placed by fallback rule
    assert any("Q-999" in n for n in notes) and any("Q-4" in n for n in notes)
    by_id = {q["quote_id"]: q for q in quotes}
    md = render_markdown(clean, by_id, SECTIONS, date(2026, 11, 4), date(2026, 11, 17), "2026-11-17T08:00:00", "m")
    assert "4–17 November 2026" in md
    assert "Talks are scheduled" not in md                                  # obsolete quote not shown
    assert "> Friday's talks were cancelled.\n>\n> No new date was set." in md
    assert verify_markdown(md, clean, by_id) == []


def test_quotes_table_is_append_only(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    conn.execute("INSERT INTO sources VALUES ('s','S','https://s/','generic','{}',1,'now')")
    conn.execute("INSERT INTO articles(source_id,url,discovered_at,discovered_via) VALUES ('s','https://s/a','now','x')")
    conn.execute("""INSERT INTO quotes(quote_id,article_id,source_id,source_name,publication_name,article_title,
                    article_url,published_at,extracted_at,quote_text,quote_sha256,locator_json,extractor_model,
                    verification_json) VALUES ('Q',1,'s','S','S','T','https://s/a','2026','2026','t','h','{}','m','{}')""")
    conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE quotes SET quote_text='changed'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM quotes")
