"""
Competitor Blocklist v2 (home_state aware)
Shared filter used by score_competitors.py and export_cohorts.py so the
"benchmark against a competitor" mechanic never names a directory, an auto
repair shop, an out-of-state stray, or a junk lead-gen domain.

One blocklist, two consumers, no drift.

Usage:
    from competitor_blocklist import is_blocked
    blocked, reason = is_blocked(domain, display_name)

Built against the 2026-07-08 harvest of 160 unique competitor domains.
Layers:
  1. DIRECTORY_DOMAINS   - platforms and directories, hard block, subdomain aware
  2. EXPLICIT_BLOCKS     - specific domains from the harvest (auto shops,
                           out-of-state, junk) blocked by name
  3. AUTO_KEYWORDS       - word-boundary keyword match on domain + display name
                           (word boundaries so "Carrier" is never hit by "car")
  4. JUNK_TLDS           - doorway-page TLDs
"""
import re

# Layer 1: directories, marketplaces, social, reference. Subdomain aware:
# blocks exact domain and anything ending in "." + domain.
DIRECTORY_DOMAINS = {
    "yelp.com", "google.com", "facebook.com", "bbb.org", "angi.com",
    "homeadvisor.com", "homedepot.com", "lowes.com", "reddit.com",
    "mapquest.com", "wikipedia.org", "yellowpages.com", "thumbtack.com",
    "houzz.com", "nextdoor.com", "porch.com", "instagram.com",
    "linkedin.com", "tripadvisor.com", "manta.com", "buildzoom.com",
    "chamberofcommerce.com", "indeed.com", "glassdoor.com",
    "craigslist.org", "amazon.com", "walmart.com", "costco.com",
    "bing.com", "yahoo.com", "expertise.com", "birdeye.com",
}

# Layer 2: explicit blocks from the 2026-07-08 harvest.
# reason recorded so --list style previews can show why.
EXPLICIT_BLOCKS = {
    # auto / car AC shops (also caught by keyword layer, kept explicit as belt and braces)
    "acautorepairmiami.com": "auto shop (Artemisa Auto Air)",
    "autoacworld.com": "auto shop (Auto AC World)",
    "cbac.com": "auto shop (car AC repair, Bradenton)",
    "firestonecompleteautocare.com": "auto chain (Firestone)",
    "motorcityoffortmyers.com": "auto shop (Motor City Automotive & Tires)",
    "wellingtonautoservice.com": "auto shop (Wellington Auto Service)",
}

# Known businesses mapped to their actual home state. Blocked only when the
# LEAD's home state differs: Team Enoch is a stray in a Florida SERP and a
# legitimate competitor in a Texas one. v2 change for the multi-state rollout.
STRAY_DOMAINS = {
    "jmartiniaq.com": "CA",
    "nexgenairandplumbing.com": "CA",
    "petermanhvac.com": "IN",
    "reliablehvacrepairbrea.com": "CA",
    "servicechampions.com": "CA",
    "shalom2u.com": "CA",
    "teamenoch.com": "TX",
}


def state_from_location(location: str) -> str:
    """'Tampa, FL' or 'Tampa, FL 33604' -> 'FL'. Empty string if unknown."""
    if not location or "," not in location:
        return ""
    token = location.split(",")[1].strip().split()
    return token[0].upper() if token else ""

# Layer 3: word-boundary keywords, checked against domain and display name.
AUTO_KEYWORDS = re.compile(r"\b(auto|automotive|car|tires?)\b", re.IGNORECASE)

# Layer 4: TLDs that in practice are doorway / lead-gen pages, not businesses.
JUNK_TLDS = (".top", ".site", ".pro")


def normalise(domain: str) -> str:
    d = (domain or "").strip().lower().strip("/")
    if d.startswith("http://"):
        d = d[7:]
    if d.startswith("https://"):
        d = d[8:]
    if d.startswith("www."):
        d = d[4:]
    return d.split("/")[0]


def is_blocked(domain: str, name: str = "", home_state: str = "FL"):
    """Returns (blocked: bool, reason: str). home_state is the LEAD's state,
    two-letter code; strays are blocked relative to it. Defaults to FL so all
    pre-v2 callers keep their exact behaviour."""
    d = normalise(domain)
    if not d:
        return True, "empty domain"

    for blocked in DIRECTORY_DOMAINS:
        if d == blocked or d.endswith("." + blocked):
            return True, "directory/platform (" + blocked + ")"

    if d in EXPLICIT_BLOCKS:
        return True, EXPLICIT_BLOCKS[d]

    if d in STRAY_DOMAINS and home_state and STRAY_DOMAINS[d] != home_state.upper():
        return True, "out of state (" + STRAY_DOMAINS[d] + " business, lead is " + home_state.upper() + ")"

    haystack = d.replace("-", " ").replace(".", " ") + " " + (name or "")
    if AUTO_KEYWORDS.search(haystack):
        return True, "auto keyword match"

    for tld in JUNK_TLDS:
        if d.endswith(tld):
            return True, "junk TLD (" + tld + ")"

    return False, ""


if __name__ == "__main__":
    # quick self-test against known cases from the 2026-07-08 harvest
    cases = [
        ("yelp.com", "TOP 10 BEST Air Conditioning Repair", True),
        ("en.wikipedia.org", "Air conditioning", True),
        ("acrepairhialeah.acbrothers.com", "AC REPAIR HIALEAH", False),
        ("cbac.com", "Reliable Car AC Repair Services in Bradenton, FL", True),
        ("aanesacrepairalva.top", "Aanes Ac Repair Alva", True),
        ("teamenoch.com", "Team Enoch: AC Repair", True),
        ("caldeco.com", "Caldeco Air Conditioning & Heating", False),
        ("coolair-inc.com", "Coolair Conditioning Inc", False),
        ("wellington.airease.net", "AirEase AC Repair Wellington", False),
    ]
    # home_state-aware cases: (domain, name, home_state, expected)
    state_cases = [
        ("teamenoch.com", "Team Enoch: AC Repair", "TX", False),
        ("teamenoch.com", "Team Enoch: AC Repair", "FL", True),
        ("petermanhvac.com", "Expert AC Repair Indianapolis", "TX", True),
        ("caldeco.com", "Caldeco Air Conditioning & Heating", "TX", False),
    ]
    failures = 0
    for dom, nm, expect in cases:
        got, reason = is_blocked(dom, nm)
        status = "OK " if got == expect else "FAIL"
        if got != expect:
            failures += 1
        print(f"{status} {dom:35s} blocked={got} {reason}")
    for dom, nm, hs, expect in state_cases:
        got, reason = is_blocked(dom, nm, home_state=hs)
        status = "OK " if got == expect else "FAIL"
        if got != expect:
            failures += 1
        print(f"{status} {dom:35s} home={hs} blocked={got} {reason}")
    print(f"\nself-test failures: {failures}")
