"""FX-FIX-42: card's real-host RepoTags sample; isolated FX-M7-06A executor."""

import json

import pytest
from test_M7_06A_uninstall import fixture_M7_06A as fixture_M7_06A
from test_M7_06A_uninstall import uninstall

DIGEST = "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
HOST_TAGS = [
    "docker.1ms.run/library/postgres:16-alpine",
    "docker.1ms.run/library/postgres:16.15-alpine",
    f"docker.1ms.run/library/postgres@{DIGEST}",
    "holm/postgres:16.15-alpine",
]


@pytest.mark.parametrize(
    "foreign,last_tag_removes_image", [(True, False), (False, False), (False, True)]
)
def test_FIX_42_digest_references_follow_existing_ownership(
    fixture_M7_06A, capsys, foreign, last_tag_removes_image
):
    f = fixture_M7_06A
    image = f.runner.images[0]
    f.runner.images[:] = [image]
    tags = list(HOST_TAGS) if foreign else ["holm/postgres:16.15-alpine", f"holm/postgres@{DIGEST}"]
    expected = sorted(tag for tag in tags if tag.startswith("holm/"))
    original = f.runner.run

    def run(argv, *, timeout=120):
        if argv[:3] == ["docker", "image", "inspect"]:
            f.runner.calls.append(argv)
            assert argv[3:] == [image, "--format", "{{json .RepoTags}}"]
            return json.dumps(tags).encode()
        if argv[:3] == ["docker", "image", "rm"] and argv[-1] in tags:
            f.runner.calls.append(argv)
            tags.remove(argv[-1])
            if not tags and last_tag_removes_image:
                f.runner.images.remove(image)
            return b""
        return original(argv, timeout=timeout)

    f.runner.run = run
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 0
    if not foreign and not last_tag_removes_image:
        expected.append(image)
    assert [argv for argv in f.runner.calls if argv[:3] == ["docker", "image", "rm"]] == [
        ["docker", "image", "rm", reference] for reference in expected
    ]
    assert f.runner.images == ([image] if foreign else [])
    assert tags == (HOST_TAGS[:3] if foreign else [])
    assert (f"preserve image={image} reason=foreign_tag" in capsys.readouterr().out) == foreign


@pytest.mark.parametrize(
    "invalid",
    [
        "x@sha256:abc",
        "x@latest",
        f"a@b@{DIGEST}",
        "--x",
        "x@sha256:" + "A" * 64,
        f"x@{DIGEST}/suffix",
        "--FX-SECRET-SENTINEL",
        42,
        None,
    ],
)
def test_FIX_42_invalid_references_fail_before_image_removal(fixture_M7_06A, capsys, invalid):
    f = fixture_M7_06A
    original = f.runner.run

    def run(argv, *, timeout=120):
        if argv[:3] == ["docker", "image", "inspect"]:
            f.runner.calls.append(argv)
            return json.dumps(["holm/postgres:16.15-alpine", invalid]).encode()
        return original(argv, timeout=timeout)

    f.runner.run = run
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 32
    output = capsys.readouterr()
    assert "code=invalid_image_tags" in output.out
    assert str(invalid) not in output.out + output.err
    assert "FX-SECRET-SENTINEL" not in output.out + output.err
    assert not any(argv[:3] == ["docker", "image", "rm"] for argv in f.runner.calls)
