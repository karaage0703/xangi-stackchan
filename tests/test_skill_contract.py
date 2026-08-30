from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "xs-xangi-stackchan" / "SKILL.md"


def test_skill_uses_xangi_search_style_location():
    assert SKILL.is_file()
    assert not (ROOT / "SKILL.md").exists()


def test_skill_uses_managed_extension_operations():
    text = SKILL.read_text(encoding="utf-8")

    assert "name: xs-xangi-stackchan" in text
    assert "xangi tool extension_request" in text
    assert "/api/health" in text
    assert "/api/diagnostics" in text
    assert "固定port" in text
    assert "直接curlしない" in text
    assert "pkill -f" not in text
    assert "setsid -f" not in text
    assert "127.0.0.1:7897" not in text


def test_setup_points_to_bundled_skill_and_managed_diagnostics():
    text = (ROOT / "XANGI_SETUP.md").read_text(encoding="utf-8")

    assert "skills/xs-xangi-stackchan/SKILL.md" in text
    assert "xangi tool extension_request" in text
    assert "/api/health" in text
    assert "/api/diagnostics" in text
    assert "認証tokenは取得・記録しない" in text
