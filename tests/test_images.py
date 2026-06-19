import base64
import subprocess
import sys

from marim_harness import images


def test_media_ext_maps_common_types():
    assert images.media_ext("image/png") == "png"
    assert images.media_ext("image/jpeg") == "jpg"
    assert images.media_ext("image/webp") == "webp"
    assert images.media_ext("application/octet-stream") == "bin"


def test_read_clipboard_image_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(images.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"text/plain\nimage/png\n")
        if cmd == ["wl-paste", "--type", "image/png"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGdata")
        raise AssertionError(cmd)

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGdata", "image/png")


def test_read_clipboard_image_wayland_falls_back_to_any_image(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(images.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"text/plain\nimage/jpeg\n")
        if cmd == ["wl-paste", "--type", "image/jpeg"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"\xff\xd8JPGdata")
        raise AssertionError(cmd)

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\xff\xd8JPGdata", "image/jpeg")


def test_read_clipboard_image_none_when_no_image(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(images.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"text/plain\n"),
    )
    assert images.read_clipboard_image() is None


def test_store_image_is_content_addressed(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    a = images.store_image("sess1", b"\x89PNGbytes", "image/png")
    assert a.path.exists()
    assert a.path.read_bytes() == b"\x89PNGbytes"
    assert a.path.name == f"{a.sha}.png"
    assert a.path.parent.name == "sess1"
    # identical bytes reuse the same file
    b = images.store_image("sess1", b"\x89PNGbytes", "image/png")
    assert b.sha == a.sha and b.path == a.path
    # different bytes -> different file
    c = images.store_image("sess1", b"other", "image/png")
    assert c.sha != a.sha


def test_detect_image_path_only_for_bare_existing_image(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG")
    assert images.detect_image_path(f"  {img}  ") == img
    # quoted (drag-and-drop often quotes) still works
    assert images.detect_image_path(f'"{img}"') == img
    # path inside prose -> not an attachment
    assert images.detect_image_path(f"see {img} please") is None
    # non-image extension -> None
    other = tmp_path / "notes.txt"
    other.write_text("hi")
    assert images.detect_image_path(str(other)) is None
    # nonexistent -> None
    assert images.detect_image_path(str(tmp_path / "nope.png")) is None


def test_media_type_for_path():
    assert images.media_type_for_path(images.Path("a.PNG")) == "image/png"
    assert images.media_type_for_path(images.Path("a.jpg")) == "image/jpeg"
    assert images.media_type_for_path(images.Path("a.txt")) is None


def _binary_message(data_b64, media_type="image/png"):
    return [{"parts": [{"part_kind": "user-prompt", "content": [
        "hi", {"kind": "binary", "data": data_b64, "media_type": media_type,
               "identifier": "x", "vendor_metadata": None}]}]}]


def test_externalize_then_rehydrate_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    raw_bytes = b"\x89PNGpayload"
    b64 = base64.b64encode(raw_bytes).decode()
    msgs = _binary_message(b64)
    out = images.externalize_images(msgs, "sess")
    item = out[0]["parts"][0]["content"][1]
    assert item["data"].startswith("marim-image-cache://")
    assert b64 not in str(out)  # base64 no longer inline
    back = images.rehydrate_images(out, "sess")
    assert back[0]["parts"][0]["content"][1]["data"] == b64


def test_rehydrate_degrades_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    msgs = [{"parts": [{"part_kind": "user-prompt", "content": [
        "hi", {"kind": "binary", "data": "marim-image-cache://deadbeef",
               "media_type": "image/png", "identifier": "x",
               "vendor_metadata": None}]}]}]
    back = images.rehydrate_images(msgs, "sess")
    assert back[0]["parts"][0]["content"][1] == "[image unavailable]"


def test_read_clipboard_image_x11(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(images.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        if "TARGETS" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"image/png\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGx11")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGx11", "image/png")


def test_read_clipboard_image_macos(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(images.shutil, "which", lambda n: "/usr/bin/pngpaste")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGmac")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGmac", "image/png")
