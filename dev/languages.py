"""Regenerate plexcache/languages.py from the Library of Congress's ISO 639-2 table."""
import re
import sys
import urllib.request

URL = "https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt"
OUT = "plexcache/languages.py"

def main():
    text = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8-sig")
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        b, t, one, en, _ = parts[:5]
        name = en.split(";")[0].strip().replace('"', "'")
        rows += [(c, name) for c in (b, t, one) if c]
    rows.sort()
    src = open(OUT, encoding="utf-8").read()
    body = "\n".join(f'    "{c}": "{n}",' for c, n in rows)
    src = re.sub(r"NAMES = \{\n.*?\n\}\n", "NAMES = {\n" + body + "\n}\n", src, count=1, flags=re.S)
    open(OUT, "w", encoding="utf-8").write(src)
    print(f"{len(rows)} codes")

if __name__ == "__main__":
    sys.exit(main())
