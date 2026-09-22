"""Docker reclaim measurement is pure and mockable; prune passthroughs return bytes."""
from unittest import mock
from app.services import docker_runner as dr_mod
from app.agent.cleanup import reclaim_breakdown, per_container_estimate


DF = {
    "Containers": [
        {"ID": "c" * 64, "ImageID": "imgA", "SizeRw": 10, "Running": True},
        {"ID": "d" * 64, "ImageID": "imgA", "SizeRw": 30, "Running": False},
        {"ID": "e" * 64, "ImageID": "imgB", "SizeRw": 40, "Running": False},
    ],
    "Images": [
        {"ID": "imgA", "RepoTags": ["x:1"], "SizeRootFs": 100},
        {"ID": "imgB", "RepoTags": None, "SizeRootFs": 200},
    ],
    "BuildCache": [{"Size": 7}, {"Size": 13}],
}


def test_reclaim_breakdown_only_stopped_and_dangling():
    ctr, img, bc = reclaim_breakdown(DF)
    assert ctr == 30 + 40        # running container excluded
    assert img == 200            # dangling image only (imgB)
    assert bc == 20


def test_per_container_estimate_includes_image_when_last_user():
    assert per_container_estimate(DF, "d" * 64) == 30   # imgA still used by c (running)
    assert per_container_estimate(DF, "e" * 64) == 40 + 200  # imgB dangles after rm


def test_per_container_estimate_unknown_cid_zero():
    assert per_container_estimate(DF, "zzz") == 0


def test_docker_engine_field_names_and_states():
    cid = "f" * 64
    df = {
        "Containers": [
            {"Id": cid, "ImageID": "sha256:tagged", "SizeRw": 6 * 1024 ** 3,
             "State": "exited"},
            {"Id": "g" * 64, "ImageID": "sha256:other", "SizeRw": 9 * 1024 ** 3,
             "State": "running"},
            {"Id": "h" * 64, "ImageID": "sha256:other", "SizeRw": 2 * 1024 ** 3,
             "State": "paused"},
        ],
        "Images": [{"Id": "sha256:tagged", "RepoTags": ["example:latest"],
                    "Size": 20 * 1024 ** 3}],
    }
    assert reclaim_breakdown(df) == (6 * 1024 ** 3, 0, 0)
    # A tagged image is not removed by image_prune, so only its writable layer counts.
    assert per_container_estimate(df, cid) == 6 * 1024 ** 3


def test_image_prune_returns_space_reclaimed():
    dr = dr_mod.DockerRunner()
    dr.client = mock.Mock()
    dr.client.images.prune.return_value = {"SpaceReclaimed": 1234}
    assert dr.image_prune() == 1234


def test_build_cache_prune_returns_space_reclaimed():
    dr = dr_mod.DockerRunner()
    dr.client = mock.Mock()
    dr.client.api.prune_builds.return_value = {"SpaceReclaimed": 999}
    assert dr.build_cache_prune() == 999
