from pathlib import Path

from scripts.sync_env_defaults import append_missing_keys, missing_keys, parse_env_file


def test_parse_env_file_reads_key_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "FOO=bar",
                "export BAR=baz",
                "INVALID-LINE",
                "BAZ=",
            ]
        ),
        encoding="utf-8",
    )

    order, values = parse_env_file(env)

    assert order == ["FOO", "BAR", "BAZ"]
    assert values == {"FOO": "bar", "BAR": "baz", "BAZ": ""}


def test_missing_keys_uses_example_defaults(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    env.write_text("FOO=1\n", encoding="utf-8")
    example.write_text("FOO=1\nBAR=2\nBAZ=\n", encoding="utf-8")

    missing = missing_keys(env, example)

    assert missing == [("BAR", "2"), ("BAZ", "")]


def test_append_missing_keys_appends_new_block(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=1\n", encoding="utf-8")

    append_missing_keys(env, [("BAR", "2"), ("BAZ", "")])
    text = env.read_text(encoding="utf-8")

    assert "FOO=1" in text
    assert "BAR=2" in text
    assert "BAZ=" in text
    assert "Auto-added from .env.example" in text
