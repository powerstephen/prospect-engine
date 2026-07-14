"""
console_engine/clean.py - Layer 1, step 3: CLEAN + VALIDATE

Takes net-new normalized rows and removes/flags junk so only genuine, reachable
contacts move forward. Encodes the cleaning rules we applied by hand all session.

Rules:
  - drop image-file "emails" (.webp/.png/.jpg/.avif etc masquerading as address)
  - drop obvious placeholder emails (example.com, domain.com, your@email, etc)
  - drop tracking/hash junk (sentry, wixmp, noreply hashes)
  - flag dead placeholder hosts (comcastbiz, wixsite, business.site...) so they
    are not sent to scoring (they hang the scorer / have no real site)
  - basic email shape validation

Output: (clean_rows, junk_rows, stats)
Standalone test:
  python -m console_engine.clean --in console_netnew.csv
"""
import csv, re, sys

# hosts that are dead stubs / builder placeholders - real site rarely scoreable
DEAD_HOSTS = ["comcastbiz.net","wixsite.com","business.site","godaddysites.com",
              "weebly.com","blogspot.com","wordpress.com"]

# junk email local-parts / domains
JUNK_EMAIL_SUBSTR = ["example.com","domain.com","email.com","yourdomain","your@",
                     "test@","noreply@","no-reply@","sentry","wixmp","@2x","@3x"]
IMG_EXT = re.compile(r'\.(webp|avif|png|jpg|jpeg|gif|svg)(\?|$)', re.I)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def is_junk_email(e):
    if not e or '@' not in e: return True
    if not EMAIL_RE.match(e): return True
    if IMG_EXT.search(e): return True
    low = e.lower()
    return any(s in low for s in JUNK_EMAIL_SUBSTR)

def is_dead_host(website):
    w = (website or '').lower()
    return any(h in w for h in DEAD_HOSTS)

def clean_rows(rows):
    clean, junk = [], []
    dead_host_flagged = 0
    for r in rows:
        e = (r.get("email") or "").strip().lower()
        if is_junk_email(e):
            r["_reason"] = "junk/invalid email"
            junk.append(r); continue
        # flag dead host: keep the row (email may be fine) but mark unscoreable
        if is_dead_host(r.get("website")):
            r["_dead_host"] = True
            dead_host_flagged += 1
        else:
            r["_dead_host"] = False
        clean.append(r)
    stats = {
        "input": len(rows),
        "clean": len(clean),
        "junk_dropped": len(junk),
        "dead_host_flagged": dead_host_flagged,
    }
    return clean, junk, stats

def main():
    args = sys.argv
    inp = args[args.index("--in")+1] if "--in" in args else None
    if not inp:
        print("usage: python -m console_engine.clean --in console_netnew.csv")
        return
    rows = list(csv.DictReader(open(inp, newline='', encoding='utf-8-sig')))
    clean, junk, stats = clean_rows(rows)
    print("=== Clean result ===")
    for k,v in stats.items(): print(f"  {k}: {v}")
    fields = ["company","first_name","last_name","email","phone","website","city","state","source_tag","_dead_host"]
    with open("console_clean.csv","w",newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader()
        for r in clean: w.writerow(r)
    print("\nclean written: console_clean.csv")
    if junk:
        print(f"\nSample junk dropped:")
        for r in junk[:6]:
            print(f"  {r.get('email','')[:40]:40} - {r.get('_reason','')}")

if __name__ == "__main__":
    main()
