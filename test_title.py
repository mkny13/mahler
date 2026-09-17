def capture_title(text, limit=80):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "Empty capture"
    line = lines[0]
    if len(line) <= limit:
        return line
    cut = line[:limit]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 else cut

print(repr(capture_title("\n\nBug: help button is broken")))
print(repr(capture_title("This is a very long line without spaces AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")))
