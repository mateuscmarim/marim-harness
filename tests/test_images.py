import subprocess

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
