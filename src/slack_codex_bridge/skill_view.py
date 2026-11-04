"""Bounded skill display metadata; identity always remains the exact native path."""

import re


def description(skill):
    interface = skill.get("interface") or {}
    for raw in (skill.get("shortDescription"), interface.get("shortDescription"), skill.get("description")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = " ".join(raw.split())
        if len(value) <= 240:
            return value
        sentences = re.findall(r".*?[.!?。！？](?=\s|$)|.*?[。！？]", value)
        result = ""
        for sentence in sentences[:2]:
            if len(result + sentence) > 240:
                break
            result += sentence
        if result.strip():
            return result.strip()
    return "No concise description available. Open Details to inspect this skill before using it."
