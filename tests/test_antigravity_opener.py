"""SidePulse Chat Opener: selects an Antigravity App conversation from the session menu."""
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS application bundle")
def test_chat_opener_has_a_named_stable_bundle_and_private_launch(tmp_path, monkeypatch):
    import plistlib
    from sidepulse import antigravity_opener as opener

    app = opener.install_opener(tmp_path)
    signature = app / "Contents/_CodeSignature/CodeResources"
    stamp = signature.stat().st_mtime_ns
    assert opener.install_opener(tmp_path) == app
    assert signature.stat().st_mtime_ns == stamp
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert (app / "Contents/Resources" / info["CFBundleIconFile"]).read_bytes() == opener.ICON.read_bytes()
    assert "CFBundleIconName" not in info
    assert info["CFBundleDisplayName"] == "SidePulse Chat Opener"
    assert info["CFBundleIdentifier"] == "io.sidepulse.chat-opener"
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)
    monkeypatch.setattr(opener, "install_opener", lambda: app)
    assert opener.run_antigravity_helper("check") in {"missing", "granted"}


def test_keyboard_search_uses_current_title_and_detects_duplicates(tmp_path):
    import sqlite3
    from sidepulse.antigravity_opener import conversation_search_title

    path = tmp_path / "summaries.db"
    assert conversation_search_title("one", path) == ("", "")
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversation_summaries (conversation_id, title)")
        db.execute("INSERT INTO conversation_summaries VALUES ('one', 'Café 🪴')")
        db.commit()
        assert conversation_search_title("one", path) == ("Café 🪴", "")
        db.execute("INSERT INTO conversation_summaries VALUES ('two', 'Café 🪴')")
        db.commit()
        assert conversation_search_title("one", path) == ("Café 🪴", "duplicate")
        db.execute("UPDATE conversation_summaries SET title = 'Renamed' WHERE conversation_id = 'one'")
        db.commit()
        assert conversation_search_title("one", path) == ("Renamed", "")
        assert conversation_search_title("absent", path) == ("", "")
