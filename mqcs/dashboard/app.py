"""Web dashboard: latest digests, quote database, articles, runs/logs and pipeline actions."""
from __future__ import annotations

import json
import threading
import traceback
from datetime import date, datetime, timezone

import markdown
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from .. import db, pipeline
from ..settings import settings

_job_lock = threading.Lock()
_job: dict = {"name": None, "started": None, "finished": None, "error": None}


def stats(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "quotes": q("SELECT COUNT(*) FROM quotes"),
        "quotes_by_source": {r[0]: r[1] for r in conn.execute(
            "SELECT source_name, COUNT(*) FROM quotes GROUP BY source_name")},
        "rejected_quotes": q("SELECT COUNT(*) FROM rejected_quotes"),
        "articles_by_status": {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM articles GROUP BY status")},
        "first_quote": q("SELECT MIN(published_at) FROM quotes"),
        "last_quote": q("SELECT MAX(published_at) FROM quotes"),
        "reports": q("SELECT COUNT(*) FROM reports"),
        "last_run": db.row_to_dict(conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()),
    }


def _run_job(name: str, fn) -> bool:
    if not _job_lock.acquire(blocking=False):
        return False

    def target():
        conn = db.connect()
        try:
            _job.update(name=name, started=db.utcnow(), finished=None, error=None)
            fn(conn)
        except Exception as e:  # surfaced on the dashboard; full trace in the server log
            _job["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            _job["finished"] = db.utcnow()
            conn.close()
            _job_lock.release()

    threading.Thread(target=target, daemon=True).start()
    return True


def create_app() -> Flask:
    app = Flask(__name__)
    conn = db.connect()
    pipeline.setup(conn)

    @app.template_filter("fromjson")
    def fromjson(s):
        return json.loads(s) if s else {}

    @app.template_filter("day")
    def day(s):
        return (s or "")[:10]

    @app.context_processor
    def inject():
        return {"job": _job, "job_running": _job_lock.locked(), "model": settings.model}

    @app.route("/")
    def home():
        sources = conn.execute("""SELECT s.*, (SELECT COUNT(*) FROM quotes q WHERE q.source_id=s.source_id) AS n_quotes,
                  (SELECT COUNT(*) FROM articles a WHERE a.source_id=s.source_id AND a.status='blocked') AS n_blocked,
                  (SELECT COUNT(*) FROM articles a WHERE a.source_id=s.source_id) AS n_articles
                  FROM sources s ORDER BY created_at""").fetchall()
        reports = conn.execute("SELECT * FROM reports ORDER BY period_end DESC, generated_at DESC LIMIT 5").fetchall()
        runs = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 8").fetchall()
        return render_template("home.html", s=stats(conn), sources=sources, reports=reports, runs=runs,
                               today=datetime.now(timezone.utc).date().isoformat(),
                               start=settings.collection_start.isoformat())

    @app.route("/reports")
    def reports():
        rows = conn.execute("SELECT * FROM reports ORDER BY period_end DESC, generated_at DESC").fetchall()
        return render_template("reports.html", reports=rows)

    @app.route("/reports/<report_id>")
    def report(report_id):
        row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone() or abort(404)
        md = (settings.reports_dir / row["md_path"]).read_text(encoding="utf-8")
        html = markdown.markdown(md, extensions=["toc", "tables"])
        decisions = json.loads((settings.reports_dir / row["decisions_path"]).read_text(encoding="utf-8"))
        return render_template("report.html", r=row, html=html, decisions=decisions)

    @app.route("/files/reports/<path:name>")
    def report_file(name):
        return send_from_directory(settings.reports_dir, name, as_attachment=request.args.get("dl") == "1")

    @app.route("/files/export/<path:name>")
    def export_file(name):
        return send_from_directory(settings.export_dir, name, as_attachment=True)

    @app.route("/quotes")
    def quotes():
        where, args = ["1=1"], []
        if request.args.get("q"):
            where.append("(quote_text LIKE ? OR article_title LIKE ? OR speaker LIKE ?)")
            args += [f"%{request.args['q']}%"] * 3
        for f in ("source_id",):
            if request.args.get(f):
                where.append(f"{f}=?")
                args.append(request.args[f])
        if request.args.get("topic"):
            where.append("topics_json LIKE ?")
            args.append(f'%"{request.args["topic"]}"%')
        if request.args.get("from"):
            where.append("substr(published_at,1,10) >= ?")
            args.append(request.args["from"])
        if request.args.get("to"):
            where.append("substr(published_at,1,10) <= ?")
            args.append(request.args["to"])
        rows = conn.execute(f"SELECT * FROM quotes WHERE {' AND '.join(where)} ORDER BY published_at DESC LIMIT 500",
                            args).fetchall()
        srcs = conn.execute("SELECT source_id, name FROM sources").fetchall()
        return render_template("quotes.html", quotes=rows, sources=srcs, sections=settings.load_json("sections.json"),
                               a=request.args)

    @app.route("/quotes/<qid>")
    def quote(qid):
        row = conn.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone() or abort(404)
        art = conn.execute("SELECT * FROM articles WHERE article_id=?", (row["article_id"],)).fetchone()
        return render_template("quote.html", q=row, art=art)

    @app.route("/articles")
    def articles():
        status = request.args.get("status")
        sql = "SELECT * FROM articles" + (" WHERE status=?" if status else "") + \
              " ORDER BY COALESCE(published_at, discovered_at) DESC LIMIT 500"
        rows = conn.execute(sql, (status,) if status else ()).fetchall()
        counts = conn.execute("SELECT status, COUNT(*) n FROM articles GROUP BY status").fetchall()
        return render_template("articles.html", articles=rows, counts=counts, status=status)

    @app.route("/articles/<int:aid>")
    def article(aid):
        row = conn.execute("SELECT * FROM articles WHERE article_id=?", (aid,)).fetchone() or abort(404)
        quotes_ = conn.execute("SELECT * FROM quotes WHERE article_id=?", (aid,)).fetchall()
        rejected = conn.execute("SELECT * FROM rejected_quotes WHERE article_id=?", (aid,)).fetchall()
        return render_template("article.html", a=row, quotes=quotes_, rejected=rejected,
                               body=json.loads(row["body_json"] or "[]"))

    @app.route("/runs")
    def runs():
        rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 200").fetchall()
        return render_template("runs.html", runs=rows)

    @app.route("/logs")
    def logs():
        level = request.args.get("level")
        run_id = request.args.get("run")
        where, args = ["1=1"], []
        if level:
            where.append("level=?")
            args.append(level)
        if run_id:
            where.append("run_id=?")
            args.append(run_id)
        rows = conn.execute(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1000",
                            args).fetchall()
        return render_template("logs.html", events=rows, level=level, run_id=run_id)

    @app.route("/api/quotes")
    def api_quotes():
        from ..export import quote_records
        return jsonify(quote_records(conn))

    @app.route("/api/status")
    def api_status():
        return jsonify({"stats": stats(conn), "job": _job, "job_running": _job_lock.locked()})

    @app.post("/actions/<action>")
    def actions(action):
        f = request.form
        jobs = {
            "daily": lambda c: pipeline.daily(c),
            "backfill": lambda c: pipeline.backfill(
                c, date.fromisoformat(f["start"]), date.fromisoformat(f["end"]),
                [f["source"]] if f.get("source") else None, exhaustive=bool(f.get("exhaustive"))),
            "report": lambda c: __import__("mqcs.report", fromlist=["x"]).generate_report(
                c, date.fromisoformat(f.get("date") or datetime.now(timezone.utc).date().isoformat())),
            "add_source": lambda c: pipeline.add_source(
                c, f["name"], f["url"], date.fromisoformat(f["start"]) if f.get("start") else None),
            "audit": lambda c: pipeline.reverify(c),
        }
        if action == "import_html":
            upload = request.files.get("file")
            if not upload:
                abort(400)
            data = upload.read()
            jobs["import_html"] = lambda c: pipeline.import_html(c, f["source"], f["url"], data)
        if action not in jobs:
            abort(404)
        started = _run_job(action, jobs[action])
        return redirect(url_for("home", started=action if started else "", busy="" if started else "1"))

    return app
