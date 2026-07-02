from __future__ import annotations

from duet.prompting import build_prompt
from duet.transcript import Message, Transcript


def test_transcript_json_roundtrip_and_markdown(tmp_path):
    transcript = Transcript("do work", workspace="/tmp/w")
    transcript.add(Message(1, "claude", "hello", 0, 1.2, "2026-01-01T00:00:00+00:00"))
    path = tmp_path / "t.json"
    transcript.save_json(path)
    loaded = Transcript.load_json(path)
    assert loaded.to_dict() == transcript.to_dict()
    rendered = loaded.render_markdown()
    assert "Duet Session" in rendered
    assert "hello" in rendered


def test_build_prompt_includes_partner_state_and_bounds():
    transcript = Transcript("task")
    transcript.add(Message(1, "claude", "first turn", 0, 0.1))
    prompt = build_prompt("task", transcript, "partner said this", "Files:\nroman.py", "Verifier")
    assert "partner said this" in prompt
    assert "Files:\nroman.py" in prompt
    assert "[[DONE]]" in prompt
    assert len(prompt) <= 24000
