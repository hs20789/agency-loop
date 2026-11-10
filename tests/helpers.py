"""Shared helpers for tests that use the synthetic session fixtures (see fixtures/README.md)."""

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"

# Every raw FAKE secret or personal value in demo_weather.jsonl and secrets.jsonl.
# None of these may appear in any API response, provider payload or log.
FAKE_SECRETS = [
    # demo_weather.jsonl
    "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0000",
    "student@example.com",
    "203.0.113.7",
    "/home/student/",
    # secrets.jsonl
    "sk-ant-api03-FAKE-secrets-fixture-0000000000",
    "hunter2hunter2",
    "alice.kim@example.com",
    "010-1234-5678",
    "/Users/alice/",
    "ghp_FAKE0123456789FAKE0123456789FAKE0123",
    "fake-api-key-1234567890",
    "admin:pa55word",
    "ops@example.org",
    "AKIAABCDEFGHIJKLMNOP",
    "fake-bearer-token-abcdef123456",
    "fake-mistral-key-0123456789abcdef",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlLXVzZXItMDAwMSIsIm5hbWUiOiJGQUtFIn0"
    ".ZmFrZS1zaWduYXR1cmUtbm90LXJlYWw",
    "FAKEPRIVATEKEYBODYFAKEPRIVATEKEYBODYFAKEPRIVATEKEYBODY0000000000",
    "NOTAREALKEYNOTAREALKEYNOTAREALKEYNOTAREALKEY00000000==",
]

# Each marker appears only in the target test output of pair_fail.jsonl / pair_pass.jsonl.
PAIR_MARKERS = ("MARKER-ONLY-IN-FAIL-OUTPUT-7Q2", "MARKER-ONLY-IN-PASS-OUTPUT-3K9")


def fixture_names() -> list[str]:
    return sorted(path.name for path in FIXTURES_DIR.glob("*.jsonl"))


def load_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()
