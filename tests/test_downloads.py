"""Songs that cannot be downloaded stay in the genre, marked, and can be retried or linked."""

from conftest import URL_B, song, traktor_playlists, wait

SID = lambda name: name.ljust(22, "x")[:22]  # noqa: E731


def setup(env, songs):
    svc, fake, music, nml = env
    fake.playlists[URL_B] = songs
    return svc, fake, music, nml


def by_title(svc, title):
    return next(t for t in svc.library.tracks.values() if t.title == title)


def test_technical_errors_are_retried_within_the_update(env):
    svc, fake, music, nml = setup(env, [song("flaky", "A", "Flaky"), song("ok", "B", "Fine")])
    fake.flaky[SID("flaky")] = 1  # fails once, works on the second attempt
    wait(svc.submit_edit_link("techno_acid", URL_B))
    flaky = by_title(svc, "Flaky")
    assert svc.library.has_file(flaky) and flaky.download_status == ""
    assert fake.attempts[SID("flaky")] == 2 and fake.attempts[SID("ok")] == 1


def test_failed_and_unavailable_songs_stay_in_the_genre(env):
    svc, fake, music, nml = setup(env, [song("gone", "A", "Gone"), song("broken", "B", "Broken"),
                                        song("ok", "C", "Fine")])
    fake.unavailable.add(SID("gone"))
    fake.fail.add(SID("broken"))
    wait(svc.submit_edit_link("techno_acid", URL_B))
    lib, pl = svc.library, svc.library.playlists["techno_acid"]
    gone, broken = by_title(svc, "Gone"), by_title(svc, "Broken")
    assert gone.id in pl.members and broken.id in pl.members  # shown in the playlist
    assert gone.download_status == "unavailable" and broken.download_status == "failed"
    assert not gone.path and not broken.path
    assert fake.attempts[SID("gone")] == 1        # "not on YouTube" is not retried ...
    assert fake.attempts[SID("broken")] == 3      # ... technical errors are (1 + 2 retries)
    assert "1 download errors" in pl.last_error and "1 not on YouTube" in pl.last_error
    # not in Traktor and not analysed while there is no file
    keys = [k for v in traktor_playlists(nml).values() for k in v]
    assert not any("Gone" in k or "Broken" in k for k in keys)
    assert gone not in svc._analysable(lib)

    wait(svc.submit_sync("techno_acid"))           # next update: only the broken one again
    assert fake.attempts[SID("gone")] == 1 and fake.attempts[SID("broken")] == 6

    fake.unavailable.clear()                       # now it is on YouTube
    wait(svc.submit_retry_downloads("techno_acid"))  # Retry also searches "not on YouTube" songs
    assert lib.has_file(gone) and gone.download_status == ""
    assert len([t for t in lib.tracks.values() if t.title == "Gone"]) == 1  # same entry, no duplicate


def test_link_a_manually_downloaded_file(env, tmp_path):
    svc, fake, music, nml = setup(env, [song("gone", "A", "Gone")])
    fake.unavailable.add(SID("gone"))
    wait(svc.submit_edit_link("techno_acid", URL_B))
    gone = by_title(svc, "Gone")
    manual = tmp_path / "downloads" / "my rip.mp3"
    manual.parent.mkdir()
    manual.write_bytes(b"x")
    wait(svc.submit_link_file(gone.id, str(manual), "techno_acid"))
    assert gone.path == "techno/acid/A - Gone.mp3" and (music / gone.path).exists()
    assert not manual.exists()  # moved into the genre's folder
    assert gone.download_status == ""
    assert any(k.endswith("/:techno/:acid/:A - Gone.mp3") for k in traktor_playlists(nml)["techno_acid"])


def test_rescan_links_a_file_dropped_into_the_folder(env):
    svc, fake, music, nml = setup(env, [song("gone", "Some Artist", "Gone Track", duration=0)])
    fake.unavailable.add(SID("gone"))
    wait(svc.submit_edit_link("techno_acid", URL_B))
    gone = by_title(svc, "Gone Track")
    (music / "techno" / "acid" / "Some Artist - Gone Track.mp3").write_bytes(b"x")
    count = len(svc.library.tracks)
    wait(svc.submit_rescan())
    assert gone.path == "techno/acid/Some Artist - Gone Track.mp3" and gone.download_status == ""
    assert len(svc.library.tracks) == count  # linked to the waiting entry, not a new track
