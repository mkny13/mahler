import re

def needs_human_of(body):
    out, grab = [], False
    for line in (body or "").splitlines():
        h = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if h:
            grab = h.group(2).strip().lower() == "needs a human to check"
            continue
        if grab:
            out.append(line)
    text = "\n".join(out).strip()
    
    cmp_text = re.sub(r"^[-*]\s*", "", text).strip().lower()
    if cmp_text in ("nothing", "nothing.", "nothing to check", "none", "n/a"):
        return ""
    if cmp_text.startswith("nothing known yet"):
        return ""
        
    return text

print(repr(needs_human_of("## Needs a human to check\n- Nothing")))
print(repr(needs_human_of("## Needs a human to check\nNothing.")))
print(repr(needs_human_of("## Needs a human to check\n- Nothing to check")))
print(repr(needs_human_of("## Needs a human to check\nNone")))
print(repr(needs_human_of("## Needs a human to check\nN/A")))
print(repr(needs_human_of("## Needs a human to check\nNothing known yet — if X...")))
print(repr(needs_human_of("## Needs a human to check\n- Confirm login")))
print(repr(needs_human_of("## Needs a human to check\n- Nothing\n- Also check login")))
