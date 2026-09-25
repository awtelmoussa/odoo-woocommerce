def sanitize_text(value):
    if value is None:
        return ""

    value = str(value)

    # remove emojis / unsupported 4-byte characters
    value = value.encode("utf-8", "ignore").decode("utf-8", "ignore")

    # keep text clean
    return value.strip()