import pytest

from sww.resume import MAX_RESUME_BYTES, extract_resume, extract_skills


@pytest.mark.parametrize("filename", ["resume.md", "resume.MD", "resume.markdown"])
def test_markdown_preserves_unicode_structure_and_evidence(filename):
    source = "# 简历\n\n## Experience\n- Built **Python** services with SQL.\n[Portfolio](https://example.com)"
    result = extract_resume(source.encode("utf-8-sig"), filename)
    assert result.text == source
    assert extract_skills(result.text) == ["Python", "SQL"]


@pytest.mark.parametrize("data,message", [
    (b"", "non-empty"),
    (b"\n # --- * \t", "No readable text"),
    (b"\xff invalid", "UTF-8"),
    (b"Python\x00SQL", "binary"),
    (b"x" * 200_001, "too much text"),
    (b"x" * (MAX_RESUME_BYTES + 1), "10 MiB"),
])
def test_markdown_rejects_unreadable_or_oversized_files(data, message):
    with pytest.raises(ValueError, match=message):
        extract_resume(data, "resume.md")


def test_plain_text_requires_markdown_extension():
    with pytest.raises(ValueError):
        extract_resume(b"Python software developer", "resume.exe")
