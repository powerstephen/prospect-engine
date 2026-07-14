"""
console_engine/server.py - Local web console backend (Flask)

Wraps the Layer 1 engine (ingest, dedup, clean, load, segment) and serves the
console page. Runs entirely on your machine - no Railway, no deployment.

Run:
  pip install flask --break-system-packages
  python -m console_engine.server
Then open http://localhost:5000 in your browser.

Needs SUPABASE_URL + SUPABASE_SERVICE_KEY in env for dedup/load/segment steps.
"""
import os, sys, csv, io, tempfile, traceback
from flask import Flask, request, jsonify, send_from_directory, send_file

from console_engine.ingest import ingest_file
from console_engine.dedup import dedup, load_db_keys_live
from console_engine.clean import clean_rows

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_console_work")
os.makedirs(WORK, exist_ok=True)

@app.route("/")
def index():
    return send_from_directory(HERE, "console.html")

@app.route("/api/run", methods=["POST"])
def run():
    """
    Accepts uploaded CSV files + a vertical. Runs ingest -> dedup -> clean.
    Returns counts + writes a clean net-new CSV the user can download.
    (Load and score are separate deliberate steps, not auto-run here.)
    """
    try:
        vertical = request.form.get("vertical", "hvac")
        state = (request.form.get("state") or "").upper().strip()
        metro = (request.form.get("metro") or "").strip().lower()
        files = request.files.getlist("sources")
        if not files:
            return jsonify({"error": "No source files uploaded"}), 400

        # save uploads to temp, ingest each
        all_rows = []
        per_source = []
        for f in files:
            tmp = os.path.join(WORK, "src_" + f.filename)
            f.save(tmp)
            rows, fmap, headers = ingest_file(tmp, source_tag=f.filename)
            all_rows.extend(rows)
            per_source.append({"file": f.filename, "rows": len(rows),
                               "mapped_columns": list(fmap.keys())})

        # dedup against live DB (if key present)
        db_emails, db_domains = set(), set()
        db_note = "no DB key set - deduped within uploaded files only"
        if os.environ.get("SUPABASE_SERVICE_KEY"):
            try:
                db_emails, db_domains = load_db_keys_live()
                db_note = f"deduped against {len(db_emails)} DB emails / {len(db_domains)} domains"
            except Exception as e:
                db_note = f"DB dedup skipped (error: {str(e)[:80]})"
        net_new, dstats = dedup(all_rows, db_emails, db_domains)

        # clean
        clean, junk, cstats = clean_rows(net_new)

        # write outputs
        out_clean = os.path.join(WORK, f"clean_{vertical}.csv")
        fields = ["company","first_name","last_name","email","phone","website","city","state","source_tag","_dead_host"]
        with open(out_clean, "w", newline='') as fo:
            w = csv.DictWriter(fo, fieldnames=fields, extrasaction='ignore'); w.writeheader()
            for r in clean: w.writerow(r)

        # optional state filter (parse state from the row's state/city fields)
        import re as _re
        _STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"}
        def _row_state(r):
            st = (r.get("state") or "").upper().strip()
            if st in _STATES: return st
            # try parsing from city field if state blank
            for t in _re.split(r"[,\s]+", (r.get("city") or "").upper()):
                if t in _STATES: return t
            return ""
        clean_before_state = len(clean)
        def _row_city(r):
            return (r.get("city") or "").split(",")[0].strip().lower()
        if state:
            clean = [r for r in clean if _row_state(r) == state]
        if metro:
            clean = [r for r in clean if metro in _row_city(r)]
        clean_after_state = len(clean)

        # segment the clean set by named / generic (pre-score; slow needs scoring later)
        named = [r for r in clean if (r.get("first_name") or "").strip() and (r.get("first_name") or "").lower()!="null"]
        generic = [r for r in clean if r not in named]
        def wr(name, rows):
            p = os.path.join(WORK, name)
            with open(p,"w",newline='') as fo:
                w=csv.DictWriter(fo,fieldnames=fields,extrasaction='ignore'); w.writeheader()
                for r in rows: w.writerow(r)
        wr(f"named_{vertical}.csv", named)
        wr(f"generic_{vertical}.csv", generic)

        return jsonify({
            "ok": True,
            "vertical": vertical,
            "state": state or "ALL",
            "per_source": per_source,
            "db_note": db_note,
            "ingested_total": len(all_rows),
            "dedup": dstats,
            "clean": {**cstats, "clean": clean_after_state, "clean_before_state": clean_before_state},
            "segments": {
                "clean_all": {"n": len(clean), "file": f"clean_{vertical}.csv"},
                "named":     {"n": len(named), "file": f"named_{vertical}.csv"},
                "generic":   {"n": len(generic), "file": f"generic_{vertical}.csv"},
            },
            "note": "Load these to DB + score, then use segment.py for slow_loaders (needs scoring)."
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]}), 500


@app.route("/api/metros")
def metros():
    """Return distinct metros (cities) present in the DB for a given state."""
    state = (request.args.get("state") or "").upper().strip()
    if not state or not os.environ.get("SUPABASE_SERVICE_KEY"):
        return jsonify({"metros": []})
    try:
        import httpx, re as _re
        _STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"}
        headers = {"apikey": os.environ["SUPABASE_SERVICE_KEY"],
                   "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_KEY"]}
        seen = {}
        offset, page = 0, 1000
        with httpx.Client(timeout=30) as c:
            while True:
                r = c.get(SUPABASE_URL + "/rest/v1/contacts", headers=headers,
                          params={"select": "location", "limit": str(page), "offset": str(offset)})
                batch = r.json()
                if isinstance(batch, dict) or not batch: break
                for row in batch:
                    loc = (row.get("location") or "").strip()
                    if not loc: continue
                    toks = _re.split(r"[,\s]+", loc.upper())
                    st = next((t for t in reversed(toks) if t in _STATES), "")
                    if st == state:
                        city = loc.split(",")[0].strip()
                        if city:
                            seen[city] = seen.get(city, 0) + 1
                if len(batch) < page: break
                offset += page
        metros = sorted(seen.keys(), key=lambda k: -seen[k])
        return jsonify({"metros": metros})
    except Exception as e:
        return jsonify({"metros": [], "error": str(e)[:120]})

@app.route("/download/<name>")
def download(name):
    p = os.path.join(WORK, name)
    if not os.path.exists(p):
        return "not found", 404
    return send_file(p, as_attachment=True)

if __name__ == "__main__":
    print("Console at http://localhost:5000")
    app.run(port=5000, debug=False)
