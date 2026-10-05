import pytest

from djmanager import updater
from djmanager.settings import Settings

RELEASE = {
    "tag_name": "v9.1.0",
    "html_url": "https://github.com/me/dj-manager/releases/tag/v9.1.0",
    "body": "notes",
    "assets": [
        {"name": "DJManager-9.1.0-setup.exe", "browser_download_url": "https://x/setup.exe"},
        {"name": "DJManager-9.1.0-portable.exe", "browser_download_url": "https://x/portable.exe"},
        {"name": "dj-manager-9.1.0-linux-x86_64", "browser_download_url": "https://x/linux"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"},
    ],
}


@pytest.fixture
def upd(monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url: RELEASE)
    return updater.Updater(Settings(update_repo="me/dj-manager"))


@pytest.mark.parametrize("mode,url", [
    ("installed", "https://x/setup.exe"),
    ("portable", "https://x/portable.exe"),
    ("binary", "https://x/linux"),
])
def test_picks_asset_for_install_mode(upd, monkeypatch, mode, url):
    monkeypatch.setattr(updater.paths, "install_mode", lambda: mode)
    info = upd.check()
    assert info.available and info.latest == "9.1.0"
    assert info.asset_url == url


def test_source_checkout_has_no_asset(upd, monkeypatch):
    monkeypatch.setattr(updater.paths, "install_mode", lambda: "source")
    info = upd.check()
    assert info.available and not info.asset_url and "git pull" in info.message


def test_up_to_date(upd, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "9.1.0")
    assert not upd.check().available


def test_no_repo_configured(monkeypatch):
    monkeypatch.setattr(updater, "BUILD_UPDATE_REPO", "")
    info = updater.Updater(Settings()).check()
    assert not info.available and "No update source" in info.message
